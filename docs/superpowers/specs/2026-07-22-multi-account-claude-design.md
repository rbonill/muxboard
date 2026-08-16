# Multi-account Claude tiles

## Problem

CodexBar's `serve` (source=auto) exposes a **single** Claude account — whichever
one the browser is logged into — because `auto` prefers the web-cookie source and
only falls back to the CLI when cookies are missing. A user with two Claude.ai
logins (e.g. a personal `~/.claude` profile and a `claude-robocup` profile backed
by `CLAUDE_CONFIG_DIR=~/.claude-robocup`) therefore sees only one Claude tile on
the Stream Deck, and which one is shown depends on fragile browser state.

## Goal

Show **every** Claude account as its own tile, deterministically and without
manual token upkeep.

## Approach

Source each Claude account from the **Claude CLI** instead of the browser:

- `codexbar usage --provider claude --source cli --json` reads the active
  Claude-CLI credentials. It honors `CLAUDE_CONFIG_DIR`, so each configured dir
  yields a distinct account (verified: different reset windows / usage per dir).
- This is immune to browser login state and self-refreshing (the Claude CLI owns
  each token), so it needs no pasted tokens.

### Where the change lives

The caching proxy (`scripts/codexbar-proxy.py`, installed to
`~/Library/Application Support/muxboard/codexbar-proxy.py`) already refreshes
`/usage` on a background timer. It now:

1. Drops any `claude`-keyed entry (the upstream web-sourced one, or a stale CLI
   entry carried over from last-good).
2. For each configured account (`{key, config_dir}`), runs the CLI with the
   matching `CLAUDE_CONFIG_DIR`, re-keys the result to `key` (e.g. `claude`,
   `claude-robocup`), and labels it with the email read from that dir's
   `.claude.json`. Accounts are configured via the `MUXBOARD_CLAUDE_ACCOUNTS`
   env var (a JSON array), defaulting to just the single default account so the
   repo stays portable; per-user accounts live in the LaunchAgent's
   `EnvironmentVariables`.
3. Falls back to each account's last-good entry on a transient CLI failure so a
   hiccup doesn't drop a tile.

Claude sourcing is **independent of the upstream (`codexbar serve`) fetch**: if
upstream is down, its last-good non-Claude entries (e.g. codex) are retained and
the Claude CLI accounts still refresh — the two paths don't share a failure.

### Plugin

No logic change: the plugin auto-discovers providers and renders one LCD segment
per entry, showing the account email. `providerIconSvg` gains a one-line alias so
a per-account key falls back to its base provider's glyph (it strips the suffix
after the first `-` and reuses that glyph if it maps to a known provider).

## Trade-offs / follow-ups

- Claude `/cost` is computed from local Claude Code logs for the default
  (`~/.claude`) profile only, so cost is attached to the default account's tile
  alone; non-default accounts show usage but no cost. Per-account cost is a
  later refinement.
- Codex is unchanged (still web-sourced); moving it off the browser is future work.
- `setup.sh` could be wired to install `scripts/codexbar-proxy.py` and its
  LaunchAgent (currently the proxy is installed out-of-band). Partially
  mitigated: `scripts/codexbar-proxy.launchagent.plist.sample` is a documented
  template of that agent, so the configuration is reproducible by hand. It
  matters because the agent's `EnvironmentVariables` are the *only* home for the
  account list and the LCD segment order (`MUXBOARD_PROVIDER_ORDER`) — nothing
  in the repo or the plugin settings records them, so a machine rebuild loses
  both silently.

## Status

Implemented and verified live: `/usage` serves `codex`, `claude`
(personal), and `claude-robocup` with distinct per-account usage; the plugin logs
`codexbar poll ok: codex,claude,claude-robocup`.
