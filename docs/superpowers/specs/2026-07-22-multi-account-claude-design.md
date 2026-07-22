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

1. Drops the upstream web-sourced `claude` entry.
2. For each account in `CLAUDE_CLI_ACCOUNTS` (`{key, config_dir}`), runs the CLI
   with the matching `CLAUDE_CONFIG_DIR`, re-keys the result to `key` (e.g.
   `claude`, `claude-robocup`), and labels it with the email read from that
   dir's `.claude.json`.
3. Falls back to each account's last-good entry on a transient CLI failure so a
   hiccup doesn't drop a tile.

### Plugin

No logic change: the plugin auto-discovers providers and renders one LCD segment
per entry, showing the account email. `providerIconSvg` gains a one-line alias so
`claude-robocup` (and any `claude-*`) falls back to the base `claude` glyph.

## Trade-offs / follow-ups

- Both Claude tiles currently share the upstream `claude` /cost series; per-account
  cost is a later refinement.
- Codex is unchanged (still web-sourced); moving it off the browser is future work.
- `setup.sh` could be wired to install `scripts/codexbar-proxy.py` and its
  LaunchAgent (currently the proxy is installed out-of-band).

## Status

Implemented and verified live: `/usage` serves `codex`, `claude`
(personal), and `claude-robocup` with distinct per-account usage; the plugin logs
`codexbar poll ok: codex,claude,claude-robocup`.
