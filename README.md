<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark-light.svg" alt="NeurAIlyzer" width="340">
</picture>

**State hygiene for AI agents — for any model, any stack.** Wipe the drift, roll back to any point in time.


[Design](docs/DESIGN.md) · [Wipe taxonomy](docs/WIPE-TAXONOMY.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

</div>

---

> **Status: alpha — the core works.** `wipe` / `snapshot` / `restore` are implemented and end-to-end tested for **file-based state** (`session` and `sandbox` scopes) on Linux, macOS, and Windows, from the CLI, the MCP server, and the library. See [What's built · what needs building](#whats-built--what-needs-building) — the CLI tells you honestly when a scope has no adapter yet.

## The idea

Long-running agent systems don't drift because their **weights** change — the weights are frozen. They drift because of the **state that accumulates around them**: KV cache, conversation history, injected RAG memory, instruction cruft, tool-sandbox leftovers, and stored threads/files on remote providers. That accumulated state is what biases outputs and creeps storage.

**NeurAIlyzer is the flash that resets it.** It:

1. **Wipes** the accumulated state — locally *and* on remote providers, wherever an API/CLI allows it.
2. **Protects** a keep-list — the paths, collections, and memories you mark are never touched.
3. **Snapshots + restores** — every wipe takes a restore point first, so you can roll state back to a **point in time you provide**.

## Works with your models — not one stack

NeurAIlyzer is **provider-agnostic by design.** It talks to your models and stores through a thin **adapter** layer, so the same `wipe`/`snapshot`/`restore` verbs work whether you're on hosted APIs, local inference, or a mix:

| Adapter kind | Examples |
|---|---|
| **Hosted model providers** | OpenAI, Anthropic, Google (Gemini), Mistral, Cohere, Azure/Bedrock endpoints |
| **Local / self-hosted runtimes** | Ollama, llama.cpp / llama-server, vLLM, LM Studio, text-generation-webui |
| **Agent frontends & memory** | Open WebUI, LibreChat, and any app storing chats in SQLite/Postgres |
| **Vector / RAG stores** | Qdrant, Chroma, pgvector, Weaviate, Pinecone |
| **Tool sandboxes** | any container/workspace an agent runs code in |

Adapters are pluggable — implement the small `Wiper` contract for a store we don't cover yet, and every interface (CLI, MCP, library) gets it for free. Nothing here is tied to any one vendor or setup.

## What it resets (and what it can't)

| Layer | Scope | Status |
|---|---|---|
| Conversation / thread history (file/JSONL/SQLite stores) | `session` | ✅ **shipped** |
| Tool-sandbox scratch, temp/cache | `sandbox` | ✅ **shipped** |
| RAG / vector memory | `rag` | 🔜 adapter planned |
| Local runtime state (KV cache, resident models) | `models` | 🔜 adapter planned |
| Remote provider state (threads/files/assistants/fine-tunes) | `remote` | 🔜 planned, where the API allows |
| Base-model "bias" (the frozen weights) | — | ❌ not state — swap the model, you can't wipe it |

The honest line: you can't wipe a hosted model's training. You *can* wipe every bit of **state you created** around it — and that's what actually drifts.

## Known harnesses, out of the box

NeurAIlyzer ships **presets** for harnesses it knows, so you don't hand-list paths — and, more importantly, so it knows what must *never* be touched:

```bash
neurailyzer detect --enable   # find what's installed, write the config
```

| Preset | Harness | The trap it knows about |
|---|---|---|
| `claude-code` | Claude Code | durable auto-memory lives **inside** the session tree (`projects/*/memory`) — a naive `rm -rf` destroys it |
| `codex` | OpenAI Codex CLI | runtime DBs carry schema-version suffixes (`state_5.sqlite`) — globbed, so version bumps don't rot the preset |
| `hermes` | Hermes Agent (Nous Research) | `HERMES_HOME` mixes state with credentials *and the install*; `.env` holds every provider key, `pairing/` is an authorization allowlist |
| `scion` | Scion (Google, experimental) | agent worktrees may hold **unmerged work**; `hub.db` holds identity/signing keys — neither is ever touched |
| `venice-web` | Venice | history is browser-side; targets per-origin IndexedDB only, never Chromium's *shared* localStorage |

Every preset's keep-list is applied automatically, and each skip is reported. See [integrations/](integrations/) to register NeurAIlyzer as a **tool inside** these harnesses over MCP.

## What's built · what needs building

**Built and tested today:**

- `wipe` / `snapshot` / `restore` for any **file-tree state** — session transcripts, chat DB files, JSONL history, sandbox scratch, temp dirs — with dry-run defaults, keep-list protection, and point-in-time rollback
- Content-addressed snapshot store with retention pruning; restores are bit-identical and themselves reversible
- **Harness presets** + `detect` for the five harnesses above, with glob keep-rules that protect state created *after* you enabled them
- **Remote provider wipers** (`--scope remote`) for OpenAI, Anthropic, and xAI — per-surface opt-in, tokens from the environment, never logged
- **Live-harness guard**: a commit-wipe refuses when a targeted harness looks like it's running (process match or SQLite WAL sidecar), because wiping a live WAL store corrupts rather than resets
- CLI, MCP server (mcp 2.x, stdio + streamable-http), and library — one core, three surfaces
- CI on Linux/macOS/Windows × Python 3.11–3.13, including E2E smoke tests that drive the real CLI over a realistic multi-harness home

**Needs building (contributions welcome — see [the taxonomy](docs/WIPE-TAXONOMY.md) and [CONTRIBUTING](CONTRIBUTING.md)):**

- `rag` — vector-store wipers (Qdrant, Chroma, pgvector, Weaviate, Pinecone…)
- `models` — runtime unload/KV-flush (Ollama, llama.cpp/llama-server, vLLM…)
- More harness presets — **OpenCode** and **Grok Build** are researched but held back until every path is source-verified (a guessed path in a wiper is a destructive bug, so `REGISTRY` refuses unverified presets at import)
- Chat-store adapters that speak SQL schemas directly (Open WebUI, LibreChat…) rather than treating the DB as an opaque file
- Scheduling/trigger hooks (end-of-task, cron) and snapshot encryption-at-rest

A new store is one `Wiper` implementation (`plan()` / `commit()` / `verify()`); a new harness is one `Preset` entry. The safety rails, snapshots, CLI, and MCP surface come for free.

## Interfaces

- **CLI** — `neurailyzer list-state · wipe <scope…> · snapshot · restore --to <T>`
- **MCP server** — the same verbs as MCP tools, so any agent (Claude, Codex, GPT-based, local…) can call it mid-workflow
- **Library** — import the core and drive it from your own orchestrator

## Safety

Wiping is destructive, so the defaults are conservative — and tested end-to-end on all three platforms:

- **Dry-run by default.** `wipe` and `restore` print a plan; `--commit` is required to act.
- **Snapshot before every commit** — wipes *and* restores — so any reset is reversible (`--no-snapshot` exists but shouts).
- **Keep-list** entries are never touched, and every skip is reported.
- **Symlinks are never followed.** A sandbox link aimed at your home directory removes the link, not your home.
- **Config sanity guards** refuse targets like `/`, your home directory, or anything containing the snapshot store.

See [SECURITY.md](SECURITY.md).

## Install

```bash
pip install "neurailyzer[mcp] @ git+https://github.com/JGh0stSecOps/neurailyzer@main"
```

(or clone and `pip install -e ".[mcp]"`; drop `[mcp]` if you don't need the MCP server. Python ≥ 3.11.)

## Quick start

Point NeurAIlyzer at your agent's state in `~/.neurailyzer/config.toml`:

```toml
[keep]
# NEVER wiped, no matter what. Skips are reported, never silent.
paths = ["~/agents/memory"]

[targets.session]           # conversation/thread history (files, JSONL, SQLite…)
paths = ["~/agents/sessions"]

[targets.sandbox]           # tool-sandbox scratch, downloads, temp
paths = ["~/agents/scratch"]

[snapshots]
dir = "~/.neurailyzer/snapshots"   # always outside the wipe targets; auto-kept
retention = 20                     # snapshots pruned beyond this count
```

Then:

```bash
neurailyzer list-state                     # what exists + what a wipe would affect
neurailyzer snapshot --label pre-task      # take a restore point
neurailyzer wipe session                   # dry-run: what would be wiped
neurailyzer wipe session --commit          # actually wipe (snapshot taken first)
neurailyzer snapshot --list                # your restore points
neurailyzer restore --to 2026-07-09T04:00  # dry-run a point-in-time rollback
neurailyzer restore --to 2026-07-09T04:00 --commit
neurailyzer mcp serve                      # expose the verbs to agents (stdio)
```

`restore --to` accepts an ISO-8601 time (nearest snapshot at or before it wins) or an exact snapshot id. A commit-restore snapshots the current state first, so even a rollback is reversible. A factory reset (`wipe all --commit`) additionally demands `--confirm all`, and is refused entirely over MCP — that lever is human-only.

## Contributing

Built to accept contributions from **humans and AI agents alike**, safely. All changes are **PR-gated on `dev`**, with CI (lint + tests), security scans, and an **injection/slop guard** that screens every contribution for prompt-injection payloads and low-quality output before review. `main` is the curated/release branch. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE) — permissive, so anyone can adopt NeurAIlyzer or point their own agent at it.
