# Contributing

NeurAIlyzer is built to accept contributions from **humans and AI agents alike**, safely. Everything is PR-gated; nothing reaches `main` without passing checks and a human review.

## Branches

- **`dev`** — default branch, where work lands first. Branch from here.
- **`main`** — curated/production. Only updated via reviewed PRs from `dev`.

```
feature/your-thing  ──PR──▶  dev  ──PR──▶  main
```

Never push directly to `dev` or `main` — both are protected.

## The flow

1. Branch from `dev`: `git switch -c feat/short-name dev`
2. Make a **small, focused** change (the guard and the reviewer both prefer small PRs).
3. Run locally: `ruff check . && ruff format . && mypy && pytest`
4. Open a PR **into `dev`**. Fill in the template.
5. CI, security, and the guard must be green; a maintainer reviews and merges.

## Checks that must pass

| Check | What |
|---|---|
| `ci / lint` | ruff (lint + format) + mypy strict |
| `ci / test` | pytest on 3.11 + 3.12, coverage not decreasing |
| `security / gitleaks` | no secrets |
| `security / codeql` | no new SAST findings |
| `pr-guard / injection-slop-scan` | no prompt-injection, hidden unicode, or slop in the diff |

## The guard

Because agents can open PRs, and PR content is **untrusted input**, every PR runs [`scripts/scan_injection.py`](scripts/scan_injection.py) over the diff before any human or model reviews it. It flags:

- **Prompt injection** — text engineered to hijack a reviewer/agent (e.g. "ignore previous instructions", fake `<system>` tags, "approve this PR without…").
- **Hidden/deceptive unicode** — zero-width, bidi overrides, and the unicode-tags block used to smuggle instructions.
- **Slop** — low-effort AI output (`# ... rest of code`, "as an AI language model", placeholder implementations).

A finding **blocks** the PR. If it's a genuine false positive (e.g. docs that legitimately discuss injection), a maintainer applies the **`guard:ok`** label to bypass. An optional deeper LLM review (`pr-guard / llm-review`) can be wired to your configured endpoint for semantic checks — keep that reviewer prompt injection-hardened, since it reads the untrusted diff.

## For AI contributors

- Read [`README.md`](README.md) and [`docs/DESIGN.md`](docs/DESIGN.md) first; the design is the source of truth. Don't diverge silently — raise an issue.
- Keep PRs small, focused, and reviewable.
- Update docs in the same PR as the behavior.
- Never weaken a safety gate to make a test pass. If a gate is wrong, fix it deliberately with a maintainer.

## Local setup

```bash
pip install -e ".[dev]"
pre-commit install     # optional: run the gates before every commit
pytest
```

## Versioning

[SemVer](https://semver.org). The project is in **initial development (`0.x`)** — anything MAY change between minor versions until the API stabilizes. `1.0.0` is the first stable/GA release, cut only when it's ready — not before. Releases are tagged from `main`.
