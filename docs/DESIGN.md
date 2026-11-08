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
2. **Keep-list** protects what you explicitly mark (credentials, settings, pinned files) — those are never wiped. A harness's own durable memory is the `memory` scope (§4): kept by every other scope, wiped only when you name it.
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
| `memory` | the harness's durable, agent-written memory (auto-memory dirs, memory DBs) | forget what it learned |
| `models` | unload resident models, flush KV, drop warm cache | force a cold, clean reload |
| `remote` | provider-side stored state (files, vector stores, batches), per-provider opt-in | cloud hygiene |
| `all` | everything above | factory reset |

Scopes compose: `neurailyzer wipe session sandbox`.

**Memory is kept by every scope but its own.** A preset's `memory` templates
are wipe targets for the `memory` scope and keep-list entries for every other
scope (`Config.keep_for`). `wipe session` therefore never takes memory by
surprise; `wipe memory` and `wipe all` do. A memory target that is, or sits
under, a symlink below the harness root is skipped and reported
(`Config.redirected_memory`): `projects/p/memory -> ~/.claude` must not turn
`wipe memory` into a wipe of the harness. Restore honours the keep-list
*without* the memory entries (`Config.hard_keep`) **only when the snapshot
was taken with the memory scope in play** (`wipe memory`, `wipe all`, a
manual `snapshot`); a `wipe session` restore point also holds memory bytes,
but restoring it never rolls memory back. Over MCP, memory moves in neither
direction: `nl_wipe` refuses to commit it and `nl_restore` always uses the
full keep-list. The agent asking is the one whose record would change.

**Explicit targets are taken as written.** A `[targets.<scope>]` path is the
user's instruction: if it is a symlink the wipe lands on its destination and
the plan says so (`symlinked_targets`). One shape is refused at config load
for every scope: a target that links to its own ancestor
(`.../projects/p/memory -> .../.claude`), which is never a dotfiles layout
and always means "wipe my parent tree". The preset redirect guard is
stricter because preset paths are implicit trust, not a user's own words.

**Time windows.** `--after T` and `--before T` narrow any file scope to the
entries whose own mtime (`lstat`, never the link's destination) falls in the
window. `--after` is the neuralizer flash (erase all state since T);
`--before` is the retention cut. The window is per file: a database file goes
whole or stays whole, and the plan names each such file. An entry that cannot
be dated is an error (incomplete plan, failed verify), never a quiet skip.
Directories are pruned only when the wipe emptied them; an untouched empty
directory is state outside the window and stays. The window never applies to
`remote` (both `wipe` and `list-state` refuse), and the pre-wipe snapshot
covers the whole root regardless.

**Presets** (`docs/../src/neurailyzer/presets.py`) map a known harness to the
scopes above plus its keep-list, so `neurailyzer detect --enable` configures a
machine without anyone hand-listing paths. A preset ships only when every path
is source-verified: `REGISTRY` refuses `verified=False` entries at import,
because a guessed path in a wiper is a destructive bug, not a stale doc.

**Liveness.** Wiping a live WAL-mode SQLite store corrupts rather than resets,
so a commit-wipe consults a best-effort running-harness check first
(`liveness.py`) and refuses unless `--force`. It is explicitly advisory: a
positive is a strong signal, a negative is not proof.

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
- Before changing any target, restore checks the SHA-256 and size of every required file blob. Missing, unreadable, or mismatched blobs abort the whole operation; dry-runs report the same errors. Files protected by the keep-list or already matching their recorded hash do not require a blob.
- A target root replaced by a symlink is refused. Files reached through a directory symlink inside a target require snapshot bytes even if their content matches: cleanup removes that link before rebuilding the original directory.
- Commit restores stream those bytes into a private temporary directory in the snapshot store, then restore from the verified copies. This needs temporary disk space proportional to the files being restored and prevents a changed store blob from being read after target cleanup. Staging is removed on return, including refusal. Target filesystem operations are not transactional; later write failures may still require the pre-restore snapshot.
- Snapshots live outside the wipe targets (their own store) and are themselves subject to a retention policy (so *they* don't become the storage creep).

## 8. Keep-list

A configurable allow-list of paths / collections / memory keys that are **never** wiped — credentials, settings, the harness's own install, pinned notes, and NeurAIlyzer's own snapshot store. (A harness's durable memory is the `memory` scope, §4: kept by every other scope, wiped only when asked by name.) If a wipe target intersects the keep-list, the adapter skips it and **reports the skip** — it never silently honors *or* silently ignores a keep-list entry. This is how "keep what matters" works: you mark it, we leave it. No summarizing, no compression — just protection.

## 9. Safety rails

- **Dry-run default.** `wipe`/`restore` print a plan; `--commit` is required to act.
- **Snapshot-before-wipe.** Always. `--no-snapshot` exists but shouts.
- **Keep-list.** Honored on every wipe (§8).
- **Scoped credentials.** Remote adapters read least-privilege, per-provider tokens from the **environment** (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`) — never a broad admin token, never logged, never stored in config. OS-keyring sourcing is a possible future addition; today it is env vars only.
- **Confirmation token** for `--scope all --commit` (type the target name).
- **Human-only levers over MCP.** `all` is refused outright over MCP (not even a plan); `remote` and `memory` can be planned by an agent but committed only at the CLI.
- **Time windows refuse the remote scope.** A provider API has no per-item clock; applying a window there would silently delete more than asked on the one scope that cannot be restored.

## 10. Threat model

- **Destructive misfire** → dry-run default + snapshot-before-wipe + keep-list.
- **Credential exposure** (remote adapters hold provider tokens) → least-privilege tokens, read from the environment only, stripped of stray whitespace, never logged, never in the repo. The snapshot store is created owner-only, since it holds copies of whatever was in the targets.
- **Snapshot as exfil surface** (snapshots contain the state) → encrypt-at-rest option, local-only by default, retention limits.
- **Supply chain** (agentic contributions) → the injection/slop PR guard screens PRs before review.

## 11. Non-goals

- **Not a compressor / summarizer.** If you want to shrink context *before* it reaches a model, that's a separate concern with its own dedicated tools — pair NeurAIlyzer with one if you like. NeurAIlyzer wipes state and protects a keep-list; it does not rewrite or condense what it keeps.
- **Not a memory store.** Long-term memory lives in your app. We never write it, summarize it or compress it; we snapshot it, keep it under every scope but `memory`, and remove it only when `wipe memory` or `wipe all` says so.
- **Not a way to "de-bias" a base model's weights** — that's not state.

## 12. Open decisions

Tracked in issues; the live ones from design:
1. **Trigger model** — manual / end-of-task / scheduled — which ships first? (Decided for v0.1: CLI + MCP shipped; scheduler later.)
2. **Snapshot store** — ~~filesystem CAS vs. sqlite index~~ **decided & shipped:** content-addressed blobs + one JSON manifest per snapshot (diffable, no extra dependency; a sqlite index can be added later if listing ever gets slow).
3. **Adapter priority** — file-tree adapters (`session`, `sandbox`) shipped first; which `rag` / `models` / `remote` adapters ship next vs. community-contributed is open.
