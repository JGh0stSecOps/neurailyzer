# Changelog

All notable changes to NeurAIlyzer are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (0.x — the API may still move).

## [Unreleased]

### Fixed — remote wiper hardening
An adversarial review pass over the remote wipers found eleven defects; all
are fixed with a regression test each:
- **`verify()` could not tell "clean" from "we never managed to look"** — a
  failed wipe reported green. Enumeration now carries a `complete` flag, and
  `verify()` is true only if the listing succeeded *and* nothing is left.
- **MCP agents could irreversibly wipe every provider object.** The gate only
  blocked the literal string `all`, so `nl_wipe(scopes=["remote"],
  commit=true)` sailed through. Remote commits are now CLI-only (planning
  over MCP still works).
- **The snapshot manifest recorded no IDs** while three places said it did.
  `WipePlan` now carries `item_ids`, and they reach the manifest — for an
  irreversible delete, that record *is* the restore point.
- **Pagination truncated silently at 10,000 objects** and looped up to 100
  times against providers that ignore the cursor. It now detects a stalled
  cursor, refuses to wipe a partially-enumerated surface, and reports both.
- **A network error mid-delete aborted the run** after objects were already
  destroyed, with no report. Each delete is now guarded and counted.
- **A token with a trailing newline** (`export K=$(cat key.txt)`) raised
  inside http.client with the key in the traceback. Tokens are stripped, and
  a whitespace-only value counts as absent.
- **Provider-supplied IDs were interpolated into the DELETE URL unencoded**,
  letting a response reshape the path. They are percent-encoded now.
- **A non-JSON 200 response crashed even the dry-run.**
- **`list-state` reported the irreversible remote scope as "0 files"** — it
  makes no network call, so it now prints `?` for "not counted".

### Added — harness presets, remote wipers, liveness guard
- **Harness presets** (`neurailyzer detect [--enable]`): `claude-code`,
  `codex`, `hermes`, `scion`, `venice-web`. Each contributes session/sandbox
  targets *and* a keep-list covering the state that must never be wiped —
  credentials, durable memory, authorization allowlists, unmerged work.
  Presets carry a `verified` flag; `REGISTRY` refuses unverified entries at
  import, so an unconfirmed path can never reach a wipe plan.
- **Glob keep-list entries.** `~/.claude/projects/*/memory` protects matches
  created *after* the config was loaded, not just ones that existed then.
- **Remote provider wipers** (`--scope remote`) for OpenAI, Anthropic, and
  xAI, configured per surface via `[remote.<provider>]`. Tokens are read from
  the environment and never logged; a surface ships only where both list and
  delete endpoints exist. Remote deletes are marked irreversible and the
  pre-wipe snapshot records an ID manifest. Venice is registered with no
  surfaces: it stores no server-side conversation state by design.
- **Live-harness guard** (`liveness.py`): a commit-wipe refuses when a
  targeted harness looks like it is running (process match or SQLite WAL
  sidecar) — wiping a live WAL store corrupts rather than resets. `--force`
  overrides; dry-run is never blocked. Exit code 3.
- **`integrations/`**: how to register NeurAIlyzer as an MCP tool inside
  Claude Code, Codex, Hermes, and Scion, plus a ready-to-use Scion agent
  template (`scion-agent.yaml`).
- E2E smoke tests over a realistic multi-harness `$HOME`, driving the real
  CLI: detect → enable → dry-run → wipe → restore, plus the liveness block
  and a live MCP stdio handshake.

### Added — v0.1 core
- **Working core** for file-based state (`session` and `sandbox` scopes):
  - `PathWiper` with the `plan()` / `commit()` / `verify()` contract — keep-list
    skips reported, symlinks removed as links (never followed), Windows
    read-only files handled, target roots preserved.
  - Content-addressed snapshot store (`blobs/` + JSON manifests) with
    take / list / point-in-time resolve / true-rollback restore / retention
    pruning. Snapshot ids are filesystem-safe on every platform.
  - Core orchestrator enforcing **snapshot-before-commit** for every surface.
  - TOML config (`~/.neurailyzer/config.toml`, `--config`, or
    `NEURAILYZER_CONFIG`) with keep-list, per-scope targets, and sanity guards
    (refuses `/`, home, or targets that contain the snapshot store).
  - CLI: real `list-state`, `snapshot [--list]`, `wipe` (dry-run default,
    `--commit`, `--confirm all` gate), `restore --to <time|id>` (dry-run
    default; commit-restores snapshot first). `python -m neurailyzer` works.
  - MCP server on the `mcp` 2.x SDK (`neurailyzer mcp serve`, stdio or
    streamable-http): `nl_list_state`, `nl_snapshot`, `nl_wipe`, `nl_restore`
    with the same gates; `scope=all` refused over MCP.
- Cross-platform CI: ubuntu / macos / windows × Python 3.11–3.13, including an
  end-to-end smoke test that drives the real CLI as a subprocess.

### Changed
- `mcp` optional dependency now requires the 2.x SDK (`mcp>=2,<3`).
- Project URLs point at `JGh0stSecOps/neurailyzer`.
