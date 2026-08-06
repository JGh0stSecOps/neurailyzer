# Changelog

All notable changes to NeurAIlyzer are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (0.x — the API may still move).

## [Unreleased]

## [0.1.0] - 2026-08-06

First tagged alpha. `main` now matches the hardened `dev` line — earlier `main`
was only the scaffold, so installing from `@main` fetched a build without the
working wipe/snapshot/restore. Everything below shipped in this release.

### Changed — MCP tool responses
- All four tools answer the same envelope. A missing or invalid config is now
  `{ok: false, reason, hint}` instead of an unhandled `ToolError` — an agent
  can act on the former and not the latter. `nl_list_state` returns
  `{ok, scopes}` and includes the **remote** scope (previously invisible,
  since it iterated only the file and pending scopes), flagged
  `irreversible` and `counted: false`.

### Fixed — keep-list correctness (the safety promise itself)
A second adversarial pass found the keep-list could silently fail — the one
bug class that loses data permanently. Each fix has a test that first proves
the protected file is genuinely inside a wipe target, so it cannot pass
vacuously:
- **A symlinked keep entry was never protected** and not even reported as a
  skip: entries were stored resolved while the walker sees the link itself,
  so the two never compared equal. Both spellings are now kept and matched.
- **Case-insensitive filesystems voided the keep-list.** `Path.resolve()`
  does not canonicalize case, and `os.path.normcase` is a *no-op on macOS* —
  so `~/projects` failed to protect on-disk `~/Projects`. Comparison now
  case-folds on macOS and Windows.
- **Keep patterns were stored unresolved while targets were resolved**, so a
  dotfiles-managed `~/.claude -> ~/dotfiles/claude` (stow, chezmoi) meant
  auto-memory patterns could never match. Pattern prefixes resolve now.
- **Unicode NFC/NFD divergence** on macOS defeated matching.
- **A symlinked target could redirect a wipe into an unrelated tree**
  (`~/.claude/downloads -> ~/Downloads`). Targets resolving inside Downloads,
  Documents, Desktop, Pictures, Music, Movies, or Public are refused, and
  every symlinked target is reported in the plan before anything is wiped.
- **`_force_unlink` chmodded *through* a symlink**, rewriting the mode of a
  file outside the target tree. It now refuses rather than following.

### Changed — a wipe now says what it could NOT clean
- Presets carry `caveats`, printed on **every** wipe and `list-state`, not
  just in `detect`. Enabling the `opencode` preset and wiping days later used
  to print a clean *"verified"* while chat history sat untouched in
  `opencode*.db` (the credential-bearing database the preset deliberately
  protects). The success line is also narrower now: *"everything the
  configured wipers cover is gone"* rather than *"state matches the plan"*.

### Fixed — a harness dir linked elsewhere is skipped, not emptied
- **`~/.claude/downloads -> ~/src/myrepo` emptied the repo.** The link lives
  inside the harness directory, so it looked like the harness's own scratch;
  a pre-release verifier destroyed a real git checkout this way. A preset
  target that resolves outside everywhere its own preset legitimately claims
  is now skipped and reported. A symlinked *harness root* (stow, chezmoi) is
  still followed — that redirection is the user's intent, not an accident.

### Fixed — release blockers
A pre-release readiness pass (three verifiers driving the real CLI in
disposable fake HOMEs) found two blockers and two majors:
- **Restore silently re-moded keep-listed directories.** The file and link
  loops consulted the keep-list; the directory loop did not — so a directory
  the user hardened to `0700` came back `0755`, unreported, while the CLI
  printed "restored." It hits the glob shape every shipped preset uses.
- **Keep-list globs were not escaped against the expansion.** A `[` anywhere
  in `$HOME` (e.g. `/Users/me[1]`) turned into a character class and silently
  voided the whole pattern. The split now comes from the raw template, so the
  user's wildcards survive and the filesystem's characters are escaped.
- **A keep entry whose filename contains `[` protected nothing** — it was
  read only as a glob. The literal spelling is kept as well.
- **Filesystem paths were interpolated into Rich markup**, so a filename
  could change what the user *read* before approving a wipe, and unbalanced
  markup crashed the commit path after the destruction. All paths are escaped.
- **The snapshot walker swallowed read errors** while the wipe walker
  reported them: a restore point could quietly omit an unreadable subtree it
  would then be unable to restore. Taking a snapshot now fails closed.

### Fixed — "never touched" now covers restore's write half
- **Restore overwrote keep-list files.** The keep-list stopped restore
  *removing* a protected file but not *rewriting* it, so rolling back
  reverted a rotated credential to its old value — silent damage of exactly
  the kind the keep-list exists to prevent. Protected destinations are now
  skipped and reported.
- **One un-removable file aborted the whole wipe.** A `PermissionError` that
  survived the read-only retry escaped `commit()`, stranding every remaining
  file. Failures are counted, reported, and mark the plan incomplete.

### Fixed — the snapshot store no longer leaks what it protects
- **Restore recreated a `0700` directory as `0755`**, so rolling back turned
  a private tree world-readable as a side effect. Directory modes are
  recorded in the manifest and restored (older manifests still load).
- **A `0600` credential was copied into a `0644` blob under a `0755` tree.**
  The store holds verbatim copies of whatever was in the wipe targets, so on
  a shared machine the hygiene tool itself disclosed the secrets it was asked
  to clean up around. The store, its blobs, and its manifests (which list
  every path that existed) are created owner-only.

### Fixed — a wipe that could not read everything now says so
- **An unreadable subtree made a wipe silently partial while `verify()`
  returned True.** `os.walk` swallows permission errors by default, so a
  locked directory vanished from the plan *and* from the verification — the
  tool reported "state matches the plan" while the files the user asked to
  destroy were still on disk. For a privacy tool that false assurance of
  deletion is the worst possible failure. Walk errors are now collected,
  reported as `UNREADABLE` notes, mark the plan `complete: false`, and make
  `verify()` return False (exit code 1).

### Fixed — the restore path is the safety net, so it must not be brittle
- **One truncated manifest made every snapshot in the store unreachable.**
  `list()` raised, so `snapshot --list`, `restore` and pruning all failed —
  a single corrupt file took every other restore point down with it. Damaged
  manifests are now skipped *and reported* (silently losing a snapshot the
  user believes they have would be equally bad).
- **A remote-only wipe's snapshot shadowed the real restore point.** It
  records no file targets (it exists to keep the deleted-id manifest), yet
  it was the nearest snapshot to "now" — so `restore --to <now>` after one
  silently rolled a tree back to nothing while the real restore point sat
  one entry behind. Content-free snapshots are skipped when resolving by
  time, and remain reachable by exact id.
- **A dry-run restore never checked that the blobs still exist**, so it
  listed files it could not actually restore — a safety net promising a
  catch it would drop. Missing blobs are reported while planning.
- **`snapshots._force_unlink` chmodded through symlinks** despite a docstring
  claiming otherwise; it was missing the guard its twin in `wipers/local.py`
  had.
- **`restore --force` was a dead flag**: the liveness guard never ran on the
  restore path, though a restore rewrites and deletes files.
- **The "precious directory" and home-directory refusals compared paths
  case-sensitively**, so on macOS and Windows `~/downloads` sailed past a
  guard that stopped `~/Downloads` — the same directory.

### Fixed — the guard, on every surface
- **A WAL sidecar was blamed on every enabled preset**, so one file under
  Codex's tree reported Hermes as running — a harness the user may not even
  have installed. Detection is scoped per preset, and hand-configured
  `[targets]` paths stay guarded under a pseudo-preset so the scoping does
  not leave them unchecked.
- The sidecar walk **materialized an entire target tree per preset** — on a
  real `~/.claude` that is gigabytes stat'd on every wipe. It stops early now.
- **Exit codes are in `--help`**, not only the changelog: `0` ok, `1` ran but
  did not fully succeed, `2` refused before doing anything, `3` refused
  because a harness looks live (`--force` overrides).
- **MCP had no liveness guard at all** — and it is the surface where an agent
  wipes mid-session, i.e. the guaranteed-live case. `nl_wipe` now runs the
  same check and takes `force`.
- **`restore --commit` had no guard either**, though it rewrites and deletes
  files; it now takes `--force` too.
- **The fallback matcher matched a harness name anywhere in a command line**,
  so an editor with `hermes-agent/` open, a `git clone` of it, or a `grep`
  mentioning it looked like the harness itself — and a false positive here
  blocks a legitimate wipe or restore. It matches the executable now. (Found
  because it intermittently broke this project's own test suite.)
- **Self-exclusion matched the substring "neurailyzer" in any command line**,
  so a harness launched from a directory with that name was invisible to the
  guard. Exclusion is by pid now.

### Fixed — tests that passed for the wrong reason
*(Four instances, all found by review rather than by the suite going red —
which is the point: a vacuous test is invisible until someone checks what it
would catch.)*
- Nine of ten "credentials and memory survive" assertions in the flagship E2E
  sat **outside every wipe target**, so they would pass with no keep-list at
  all. The suite now proves containment first, and a new
  `test_keeplist_contract.py` covers symlinks, case, unicode, and
  created-after-load paths where they actually bite.
- Both WAL-guard tests were satisfied by the ~36 real processes matching
  "claude" on any developer machine rather than by the sidecar they seed. The
  process scan is now stubbed so the sidecar is the only possible trigger,
  and they assert the reason and preset, not just the exit code.
- The symlink-chmod guard test created its link in a *writable* directory, so
  `unlink()` succeeded and the read-only recovery path — the only place the
  guard lives — never ran. It would have passed with the guard deleted.
- The E2E found its snapshot id by matching a `"2026"` prefix while the
  fixture seeded `sessions/2026/07/30/…`, so line-wrapping at certain widths
  matched the wrong token. It matches the full id shape now.

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
- **Remote provider wipers** (`wipe remote`) for OpenAI, Anthropic, and
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
  template (`scion-agent.yaml`) whose setup creates the config it points at
  and puts snapshots somewhere that outlives the agent's scratch.
- `py.typed`, so downstream users get the inline type information (the
  package is `mypy --strict` clean).
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
