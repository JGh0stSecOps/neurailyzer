# Changelog

All notable changes to NeurAIlyzer are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (0.x — the API may still move).

## [Unreleased]

### Added
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
