#!/usr/bin/env python3
"""
Muxboard CodexBar caching proxy (stale-while-revalidate).

Problem: `codexbar serve` fetches usage live from web dashboards. Its /usage is
slow (~10-45s) when its cache is cold, and it BLOCKS the request during a
refetch (no stale-while-revalidate) with only a lazy TTL. Muxboard polls /usage
every 45s and the plugin aborts a fetch after a few seconds, so every cache
cycle produced a slow poll and the LCD flickered between data and "no providers".

Fix: this proxy sits in front of `codexbar serve`. It keeps a last-good snapshot
of /usage and per-provider /cost, refreshed on a BACKGROUND timer, and answers
the plugin INSTANTLY from that snapshot. CodexBar's slowness never reaches the
plugin; the plugin always gets a fast response (possibly a few seconds stale,
which is irrelevant for quota windows measured in hours/days).

  plugin  ->  proxy (127.0.0.1:LISTEN_PORT)  ->  codexbar serve (127.0.0.1:UPSTREAM_PORT)
              always instant                     slow, polled in background

Claude multi-account: `codexbar serve` (source=auto) shows a SINGLE Claude
account -- whichever the browser is logged into -- because auto prefers the web
cookie source. To show every Claude login independently, we drop the upstream's
web-sourced `claude` entry and instead source each account from the Claude CLI
(`--source cli`) with its own CLAUDE_CONFIG_DIR. That is deterministic (immune to
which account the browser happens to hold) and self-refreshing (the Claude CLI
manages each token). Each account is re-keyed to its own provider id so the
plugin renders one tile per account.

Endpoints mirrored (only what Muxboard uses): /health, /usage, /usage?provider=,
/cost, /cost?provider=. Localhost-only.
"""
import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("MUXBOARD_PROXY_PORT", "17777"))
UPSTREAM = f"http://127.0.0.1:{os.environ.get('MUXBOARD_UPSTREAM_PORT', '17778')}"
REFRESH_SECS = float(os.environ.get("MUXBOARD_PROXY_REFRESH", "45"))
UPSTREAM_TIMEOUT = float(os.environ.get("MUXBOARD_UPSTREAM_TIMEOUT", "150"))

# Full path so it resolves under launchd's minimal PATH.
CODEXBAR_BIN = os.environ.get("MUXBOARD_CODEXBAR_BIN", "/opt/homebrew/bin/codexbar")
CLI_TIMEOUT = float(os.environ.get("MUXBOARD_CLI_TIMEOUT", "60"))

# Optional LCD display order. Muxboard renders providers in the array order we
# return (its allow-list is empty by default), so this is the lever for segment
# order. Comma-separated provider ids via MUXBOARD_PROVIDER_ORDER: listed ids
# come first in this order; any others keep their existing (discovery) order.
# Empty (default) preserves discovery order so the repo stays portable — set the
# per-user order in the LaunchAgent's EnvironmentVariables, e.g.
#   MUXBOARD_PROVIDER_ORDER = codex,claude,claude-robocup,commandcode,perplexity
# That agent is the only place the order lives; see
# scripts/codexbar-proxy.launchagent.plist.sample to reproduce one.
_PROVIDER_ORDER = [p.strip() for p in os.environ.get("MUXBOARD_PROVIDER_ORDER", "").split(",") if p.strip()]

# Claude accounts to source from the Claude CLI, one tile each. Configure via the
# MUXBOARD_CLAUDE_ACCOUNTS env var (a JSON array of {"key", "config_dir"}); config_dir
# maps to CLAUDE_CONFIG_DIR (null => the default ~/.claude) and `key` is the provider
# id the tile is shown under. Defaults to just the default account so the repo stays
# portable — set per-user accounts in the LaunchAgent's EnvironmentVariables, e.g.
#   MUXBOARD_CLAUDE_ACCOUNTS = [{"key":"claude","config_dir":null},
#                               {"key":"claude-work","config_dir":"~/.claude-work"}]
_DEFAULT_CLAUDE_ACCOUNTS = [{"key": "claude", "config_dir": None}]


def _parse_claude_accounts(raw):
    accounts = []
    for a in json.loads(raw):
        cd = a.get("config_dir")
        accounts.append({"key": a["key"],
                         "config_dir": os.path.expanduser(cd) if cd else None})
    return accounts


try:
    _raw_accounts = os.environ.get("MUXBOARD_CLAUDE_ACCOUNTS")
    CLAUDE_CLI_ACCOUNTS = _parse_claude_accounts(_raw_accounts) if _raw_accounts else _DEFAULT_CLAUDE_ACCOUNTS
    if not CLAUDE_CLI_ACCOUNTS:
        raise ValueError("empty account list")
except Exception:
    CLAUDE_CLI_ACCOUNTS = _DEFAULT_CLAUDE_ACCOUNTS
# CodexBar computes Claude /cost from LOCAL Claude Code logs for the DEFAULT
# (~/.claude) profile only, so cost is attached solely to that account's tile.
_DEFAULT_CLAUDE_KEY = next((a["key"] for a in CLAUDE_CLI_ACCOUNTS if not a["config_dir"]), None)

_lock = threading.Lock()
_state = {
    "usage": None,        # last-good bare /usage array
    "cost": {},           # provider -> last-good /cost?provider= payload
    "claude_cli": {},     # account key -> last-good CLI usage entry
    "last_ok": 0.0,       # epoch of last successful upstream /usage
    "last_err": None,     # last error string
    "refreshes": 0,
}


def _provider_name(entry):
    if not isinstance(entry, dict):
        return None
    p = entry.get("provider")
    if p:
        return p
    ident = (entry.get("usage") or {}).get("identity") or {}
    return ident.get("providerID")


def _apply_provider_order(entries):
    """Order entries for the LCD per MUXBOARD_PROVIDER_ORDER (stable; unlisted last)."""
    if not _PROVIDER_ORDER:
        return entries
    rank = {}
    for i, name in enumerate(_PROVIDER_ORDER):
        rank.setdefault(name, i)  # first occurrence wins if an id is listed twice
    # sorted() is stable, so providers not in the list keep their relative order.
    return sorted(entries, key=lambda e: rank.get(_provider_name(e) or "", len(_PROVIDER_ORDER)))


def _fetch_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _claude_account_email(config_dir):
    """Best-effort account email from a Claude config dir's .claude.json."""
    path = (os.path.join(config_dir, ".claude.json") if config_dir
            else os.path.expanduser("~/.claude.json"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        email = (data.get("oauthAccount") or {}).get("emailAddress")
        return email.strip() if isinstance(email, str) and email.strip() else None
    except Exception:
        return None


def _fetch_claude_account(key, config_dir):
    """Fetch one Claude account via the CLI source, re-keyed as its own provider."""
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    proc = subprocess.run(
        [CODEXBAR_BIN, "usage", "--provider", "claude", "--source", "cli", "--json"],
        capture_output=True, text=True, timeout=CLI_TIMEOUT, env=env,
    )
    data = json.loads(proc.stdout or "[]")
    entry = data[0] if isinstance(data, list) and data else data
    if not isinstance(entry, dict):
        raise ValueError("unexpected codexbar cli output")
    err = entry.get("error")
    if err:  # any truthy error payload (dict or bare string) => fall back to last-good
        raise ValueError(err.get("message") if isinstance(err, dict) else str(err))
    # Re-key so the plugin renders this account as its own tile.
    entry["provider"] = key
    email = _claude_account_email(config_dir)
    if email:
        entry["account"] = email
        usage = entry.get("usage")
        if isinstance(usage, dict):
            ident = usage.get("identity")
            if not isinstance(ident, dict):
                ident = {}
                usage["identity"] = ident
            # Authoritative: overwrite any identity email the CLI carried, since
            # `email` is read from this account's own config dir. The plugin's
            # accountOf() prefers identity.accountEmail, so setdefault would let a
            # stale CLI-supplied email win over the correct per-account label.
            ident["accountEmail"] = email
    return entry


def _refresh_once():
    errors = []

    # Upstream (codex, etc.) is best-effort. On failure keep the last-good non-Claude
    # entries so those tiles don't vanish. Claude is sourced from the CLI below and
    # must NOT be gated on the upstream/web path being reachable.
    try:
        usage = _fetch_json(f"{UPSTREAM}/usage", UPSTREAM_TIMEOUT)
        if not isinstance(usage, list):
            raise ValueError(f"/usage returned non-list: {str(usage)[:120]}")
    except Exception as e:
        errors.append(f"upstream /usage: {e}")
        with _lock:
            usage = list(_state["usage"]) if _state["usage"] else []

    # Drop any Claude entry (the upstream web-sourced one, or a stale CLI one carried
    # over from last-good) and (re)source Claude from the CLI, one tile per account.
    # A failing account falls back to its own last-good so a transient hiccup or a
    # down upstream doesn't drop its tile.
    usage = [e for e in usage if not (_provider_name(e) or "").startswith("claude")]
    for acct in CLAUDE_CLI_ACCOUNTS:
        key = acct["key"]
        try:
            entry = _fetch_claude_account(key, acct["config_dir"])
            with _lock:
                _state["claude_cli"][key] = entry
        except Exception as e:
            errors.append(f"claude[{key}]: {e}")
            with _lock:
                entry = _state["claude_cli"].get(key)
        if entry:
            usage.append(entry)

    usage = _apply_provider_order(usage)
    with _lock:
        _state["usage"] = usage
    # Per-provider cost (best-effort; individually cached, usually fast). CodexBar
    # computes Claude /cost from LOCAL Claude Code logs for the default (~/.claude)
    # profile only, so attach it to that tile alone and leave non-default Claude
    # accounts costless rather than duplicating a wrong figure. (Per-account cost for
    # non-default profiles is a follow-up.)
    for entry in usage:
        name = _provider_name(entry)
        if not name:
            continue
        if name.startswith("claude"):
            if name != _DEFAULT_CLAUDE_KEY:
                with _lock:
                    _state["cost"][name] = []
                continue
            cost_provider = "claude"
        else:
            cost_provider = name
        try:
            cost = _fetch_json(f"{UPSTREAM}/cost?provider={cost_provider}", UPSTREAM_TIMEOUT)
            with _lock:
                _state["cost"][name] = cost
        except Exception:
            pass  # keep prior cost for this provider
    with _lock:
        _state["last_ok"] = time.time()
        _state["last_err"] = "; ".join(errors) if errors else None
        _state["refreshes"] += 1


def _refresher():
    while True:
        try:
            _refresh_once()
        except Exception as e:  # never let the loop die
            with _lock:
                _state["last_err"] = str(e)
        time.sleep(REFRESH_SECS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # quiet; launchd captures nothing useful otherwise

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = parse_qs(parsed.query)
        provider = (q.get("provider") or [None])[0]

        if path == "/health":
            with _lock:
                ok = _state["usage"] is not None
                last_ok = _state["last_ok"]
                err = _state["last_err"]
                refreshes = _state["refreshes"]
            # Always report ok so the plugin treats the LCD source as up; the
            # snapshot fields are informational.
            self._send({
                "status": "ok",
                "version": "muxboard-proxy",
                "warm": ok,
                "lastOkAgeSec": round(time.time() - last_ok, 1) if last_ok else None,
                "lastErr": err,
                "refreshes": refreshes,
            })
            return

        if path == "/usage":
            with _lock:
                usage = list(_state["usage"]) if _state["usage"] else []
            if provider and provider not in ("", "all"):
                usage = [e for e in usage if _provider_name(e) == provider]
            self._send(usage)
            return

        if path == "/cost":
            if provider:
                with _lock:
                    self._send(_state["cost"].get(provider, []))
            else:
                # Muxboard only uses /cost?provider=; bare /cost -> merged list.
                with _lock:
                    merged = []
                    for v in _state["cost"].values():
                        if isinstance(v, list):
                            merged.extend(v)
                self._send(merged)
            return

        self._send({"error": "not found", "path": path}, code=404)


def main():
    t = threading.Thread(target=_refresher, daemon=True)
    t.start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"muxboard codexbar-proxy listening on http://{LISTEN_HOST}:{LISTEN_PORT} "
          f"-> upstream {UPSTREAM}, refresh {REFRESH_SECS}s", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
