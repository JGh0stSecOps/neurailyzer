# NeurAIlyzer — Design

> Status: design of record. Revise via PR as implementation lands.

## 1. Problem

Agent systems that run for a long time (or run many tasks back-to-back) accumulate **state** that quietly biases their behavior and grows storage without bound:

- **Runtime state** — KV cache, resident models, warm context.
- **Conversation state** — chat history, thread transcripts.
- **Memory state** — RAG vectors, embedded "facts," learned corrections.
- **Scratch state** — tool-sandbox files, temp artifacts, downloads.
- **Remote state** — provider-side threads, uploaded files, assistants, fine-tunes, "memory" features.

None of this is the model's *weights* (those are frozen). But all of it conditions the model's next output, so it behaves like drift/bias — and it never gets cleaned up.

## 2. Principle

**Wipe → restore, with a keep-list.**

1. **Wipe** the accumulated state, at a chosen scope, locally and remotely.
2. **Keep-list** protects what you explicitly mark (your durable long-term memory, pinned files) — those are never wiped.
3. **Restore** to a **point in time** when needed — because every wipe takes a snapshot first.

The mental model is the sci-fi neuralyzer, with one upgrade the name earns: **you set the target time.** It's not only "wipe it all," it's "be who you were at 04:00, before that bad tool loop poisoned your context."

NeurAIlyzer is a **wiper**, not a summarizer. It resets state; it doesn't compress or rewrite what it keeps (see [Non-goals](#11-non-goals)).

## 3. Provider-agnostic by design

NeurAIlyzer must not be tied to any one model vendor, runtime, or app. The core knows nothing about specific products — it speaks to state through **adapters**:

```
             ┌──────────────  NeurAIlyzer core  ──────────────┐
 CLI ─┐      │  scopes · snapshots · keep-list · safety gate  │
 MCP ─┼────▶ │                    orchestration               │
 lib ─┘      └──────────────────────┬─────────────────────────┘
                                     │  Wiper contract (plan/commit/verify)
        ┌────────────┬───────────────┼───────────────┬───────────────┐
     runtime      conversation      memory         sandbox         remote
     adapters      adapters        adapters        adapters        adapters
   (Ollama,      (any chat DB:    (Qdrant,       (any container/  (OpenAI,
    llama.cpp,    SQLite/PG,       Chroma,        workspace)       Anthropic,
    vLLM…)        Open WebUI…)     pgvector…)                      Gemini…)
```

Each adapter implements the same small `Wiper` contract, so **adding a store is a plugin, not a fork.** Vendor/runtime names above are *examples of adapters*, never assumptions — a user on a completely different stack writes one adapter and gets every interface for free.

## 4. Scopes

Wipes are addressed by scope, least-blast-radius first:

| `--scope` | Resets | Typical use |
|---|---|---|
| `session` | current conversation/thread + its runtime context | between tasks |
| `rag` | RAG vectors / embedded memory (a collection or a filter) | stale/biased retrieval |
| `sandbox` | tool sandbox, temp, downloads | after experimenting |
| `models` | unload resident models, flush KV, drop warm cache | force a cold, clean reload |
| `remote` | provider-side stored state (threads/files/etc.), where the API allows | cloud hygiene |
| `all` | everything above | factory reset |

Scopes compose: `--scope session,sandbox`.

## 5. Interfaces

Three surfaces over one core, so it serves a human, an agent, and an orchestrator:

- **CLI** (`neurailyzer …`) — scriptable, cron-able, the reference surface.
- **MCP server** (`neurailyzer mcp serve`) — the same verbs as MCP tools (`nl_list_state`, `nl_snapshot`, `nl_wipe`, `nl_restore`) so any MCP-capable agent can invoke hygiene mid-workflow.
- **Library** — import the core and call it from your own code.

## 6. Core modules

```
neurailyzer/
  config.py        # adapters config, credentials (via env/keyring), keep-list, scopes
  cli.py           # arg surface -> core
  mcp_server.py    # MCP tools -> core
  core.py          # orchestration: snapshot -> wipe -> verify -> report
  snapshots.py     # take / list / restore --to T  (content-addressed store)
  wipers/
    base.py        # Wiper ABC: plan() (dry-run) + commit() + verify()
    local.py       # local-runtime / chat-store / vector / sandbox adapters
    remote.py      # hosted-provider adapters (behind capability flags)
```

Every `Wiper` implements **`plan()`** (returns what *would* change — the dry-run) and **`commit()`** (does it, after a snapshot). Nothing deletes without a plan first.

## 7. Snapshots + point-in-time restore

- Before any `--commit` wipe, `snapshots.take()` writes a restore point: a content-addressed bundle of the affected state (DB rows, vector export, sandbox tarball, a manifest of remote IDs), tagged with a timestamp + label.
- `restore --to <T>` finds the nearest snapshot at/before `T` and rehydrates it.
- Snapshots live outside the wipe targets (their own store) and are themselves subject to a retention policy (so *they* don't become the storage creep).

## 8. Keep-list

A configurable allow-list of paths / collections / memory keys that are **never** wiped — your app's durable long-term memory, pinned notes, and NeurAIlyzer's own snapshot store. If a wipe target intersects the keep-list, the adapter skips it and **reports the skip** — it never silently honors *or* silently ignores a keep-list entry. This is how "keep what matters" works: you mark it, we leave it. No summarizing, no compression — just protection.

## 9. Safety rails

- **Dry-run default.** `wipe`/`restore` print a plan; `--commit` is required to act.
- **Snapshot-before-wipe.** Always. `--no-snapshot` exists but shouts.
- **Keep-list.** Honored on every wipe (§8).
- **Scoped credentials.** Remote adapters use least-privilege, per-provider tokens from env/keyring — never a broad admin token.
- **Confirmation token** for `--scope all --commit` (type the target name).

## 10. Threat model

- **Destructive misfire** → dry-run default + snapshot-before-wipe + keep-list.
- **Credential exposure** (remote adapters hold provider tokens) → least-privilege tokens, env/keyring only, never logged, never in the repo.
- **Snapshot as exfil surface** (snapshots contain the state) → encrypt-at-rest option, local-only by default, retention limits.
- **Supply chain** (agentic contributions) → the injection/slop PR guard screens PRs before review.

## 11. Non-goals

- **Not a compressor / summarizer.** If you want to shrink context *before* it reaches a model, that's a separate concern with its own dedicated tools — pair NeurAIlyzer with one if you like. NeurAIlyzer wipes state and protects a keep-list; it does not rewrite or condense what it keeps.
- **Not a memory store.** Long-term memory lives in your app and is protected by the keep-list; we wipe and snapshot around it.
- **Not a way to "de-bias" a base model's weights** — that's not state.

## 12. Open decisions

Tracked in issues; the live ones from design:
1. **Trigger model** — manual / end-of-task / scheduled — which ships first? (Leaning: CLI + MCP first, scheduler later.)
2. **Snapshot store** — filesystem CAS vs. a small sqlite index + blobs. (Leaning: sqlite index + content-addressed blobs.)
3. **Adapter priority** — which hosted + local adapters ship in v1 vs. community-contributed later.
