# Security Policy

NeurAIlyzer is a **destructive tool** (it deletes state) that also **holds credentials** (remote wipers need provider tokens). Both facts shape its security model.

## Reporting a vulnerability

Open a private security advisory on the repo (GitHub → Security → Advisories) or contact the maintainers directly. Please don't file public issues for vulnerabilities.

## Design guarantees

- **No destructive default.** `wipe`/`restore` are dry-run unless `--commit`. A commit-wipe always snapshots first. `--scope all --commit` requires a confirmation token.
- **Keep-list is honored.** Configured paths/collections/memory keys are never wiped; skips are reported, never silent.
- **Least-privilege credentials.** Remote wipers read per-provider, minimally-scoped tokens from the environment or OS keyring — never a broad admin token, never hardcoded, never logged.
- **Snapshots are sensitive.** They contain the state that was wiped. Local-only by default, retention-limited, and an encrypt-at-rest option is on the roadmap. Treat the snapshot store like a backup: it's a target.

## Contribution security (untrusted PRs)

Contributions may come from agents, and **PR content is untrusted input**. The [`pr-guard`](.github/workflows/pr-guard.yml) workflow screens every diff for prompt injection, hidden unicode, and slop before review (see [CONTRIBUTING.md](CONTRIBUTING.md#the-guard)). `gitleaks` runs on every PR and weekly (SAST is covered by ruff's S/bandit rules).

## What NeurAIlyzer will never claim

- It cannot wipe a hosted model's weights or training "bias" — that isn't state. It resets only the state *you* created around a model. Any messaging or output that implies otherwise is a bug.
- It cannot guarantee a remote provider truly deleted data (their retention policy governs that). Remote wipes report best-effort and say so.

## Supported versions

Pre-1.0: only the latest `main` is supported. Security fixes land on `dev` and are fast-tracked to `main`.
