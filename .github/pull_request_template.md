## What & why
<!-- What does this change, and why. Link the issue: Fixes #NN -->


## Type
<!-- The PR title must be a Conventional Commit: feat | fix | docs | refactor | test | chore | perf | ci | build | revert -->

## Checklist
- [ ] Branch is `<type>/<name>` (`feature/…`, `fix/…`, `docs/…`, `chore/…`) and the **PR title is a Conventional Commit**
- [ ] Tests added/updated; `pytest` green (3.11 + 3.12)
- [ ] `ruff check . && ruff format --check .` and `mypy` clean
- [ ] Docs updated in the same PR as the behavior
- [ ] If this touches capture/restore: **no lossy transform** of content — exact restore preserved, and a round-trip verify passes
- [ ] If this adds/changes a **source adapter**: it passes the conformance kit (`tests/conformance/`)
- [ ] No secrets/tokens, no research/handoff/prompt docs, nothing machine-specific committed
