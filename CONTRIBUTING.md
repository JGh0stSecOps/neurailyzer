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

1. Branch from `dev`: `git switch -c feature/short-name dev` (branch name must be `<type>/<name>` — see below)
2. Make a **small, focused** change (the guard and the reviewer both prefer small PRs).
3. Run locally: `ruff check . && ruff format . && mypy && pytest`
4. Open a PR **into `dev`**. Fill in the template.
5. CI, security, and the guard must be green; a maintainer reviews and merges.

## Commits & branch names

Both are **enforced by CI** (`pr-hygiene`), for humans and agents alike:

- **Branch names** must be `<type>/<name>`, where `<type>` is one of `feature`, `fix`, `docs`, `chore`, `ci`, `refactor`, `perf`, `test` — e.g. `feature/openwebui-adapter`, `fix/restore-conflict-mode`.
- **The PR title and every commit** must follow [Conventional Commits](https://www.conventionalcommits.org): `type(optional-scope): summary` — e.g. `feat(openwebui): introspect the live schema`. Allowed types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`, `build`, `revert`.

## Checks that must pass

| Check | What |
|---|---|
| `lint` | ruff (lint + format) + mypy strict |
| `test (<os>, <py>)` | pytest on ubuntu + macOS + Windows x Python 3.11-3.13, including the end-to-end smoke tests |
| `secret scan` | gitleaks — no secrets |
| `injection / slop / hidden-unicode scan` | no prompt-injection, hidden unicode, or slop in the diff |
| `branch-name` | branch is `<type>/<name>` |
| `conventional-commits` | PR title + commits follow Conventional Commits |

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
pip install -e ".[dev,mcp]"   # [mcp] so the MCP tests run rather than skip
pre-commit install     # optional: run the gates before every commit
pytest
```

## Versioning

[SemVer](https://semver.org). The project is in **initial development (`0.x`)** — anything MAY change between minor versions until the API stabilizes. `1.0.0` is the first stable/GA release, cut only when it's ready — not before. Releases are tagged from `main`.

## Adding a harness preset

A preset teaches NeurAIlyzer where one agent tool keeps its state — and, more
importantly, what it must never touch. Add one in
[`src/neurailyzer/presets.py`](src/neurailyzer/presets.py):

```python
MY_HARNESS = Preset(
    id="my-harness",
    verified=True,  # REQUIRED: see below
    name="My Harness",
    vendor="Someone",
    detect=("$MY_HOME", "~/.my-harness"),
    session=("~/.my-harness/sessions",),
    sandbox=("~/.my-harness/cache",),
    keep=("~/.my-harness/auth.json", "~/.my-harness/config.toml"),
    notes="Where these paths came from, and the trap they encode.",
)
```

Then add it to `REGISTRY` and write a test that proves the trap, in the style
of `tests/test_presets.py::test_grok_install_paths_are_protected`.

**`verified` has no default, on purpose.** Setting it to `True` is a claim
that *every path* was traced to upstream source or a live install — cite
where in `notes`. `REGISTRY` refuses an unverified preset at import time,
because a guessed path in a wiper is a destructive bug, not a stale doc.

Two rules learned the hard way:

- **Never target a bare harness root.** These roots routinely mix state with
  credentials, and sometimes with the installation itself.
- **A keep-list test only counts if the protected file is genuinely inside a
  wipe target.** Otherwise it passes with no keep-list at all — assert
  containment first.

## Adding a remote provider

Providers live in [`src/neurailyzer/wipers/remote.py`](src/neurailyzer/wipers/remote.py).
A surface ships only when **both** a list and a delete endpoint are verified —
you cannot honestly wipe what you cannot enumerate. Tests monkeypatch
`http_json`, so no network or real key is needed; see `tests/test_remote.py`.
