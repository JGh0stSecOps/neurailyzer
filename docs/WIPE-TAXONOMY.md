# Wipe Taxonomy

The concrete map of agent/model state, where it lives, how an adapter resets it, and how to verify. Written in terms of **categories + example adapters** — no assumption about any one stack. To support a store we don't cover, implement the `Wiper` contract for it.

## Legend
- **Mechanism** — how an adapter resets it.
- **Snapshot** — what a restore point must capture first.
- **Verify** — how `verify()` confirms the wipe.

---

## Local

### Runtime — resident models & KV cache  (`--scope models`)
- **What:** models pinned in VRAM/RAM, warm inference process + its KV cache, warm page-cached weights.
- **Mechanism (adapters):** unload the model (e.g. Ollama `stop`, vLLM/llama-server unload or restart), flush KV, optionally drop page cache for a cold reload.
- **Snapshot:** none needed (runtime is derived) — record which model *was* resident so it can be re-warmed.
- **Verify:** accelerator memory near-idle for that model; runtime reports it unloaded.

### Conversation — chat / thread history  (`--scope session`)
- **What:** chat/thread rows in whatever store the frontend uses (SQLite, Postgres, JSON), or provider thread objects.
- **Mechanism (adapters):** scoped `DELETE` by id / user / time window against the store, or the frontend's API where a token exists.
- **Snapshot:** export the affected rows/objects before delete.
- **Verify:** rows gone; counts match plan.

### Memory / RAG — vector store  (`--scope rag`)
- **What:** embedded vectors + payloads (the retrieval memory that can bias answers).
- **Mechanism (adapters):** delete points by filter, or drop/recreate a collection (Qdrant, Chroma, pgvector, Weaviate, Pinecone…).
- **Snapshot:** the store's snapshot/export API, or a points export.
- **Verify:** collection point-count matches plan; filtered query returns empty.

### Scratch — tool sandbox & temp  (`--scope sandbox`)
- **What:** the workspace an agent runs code in, downloads, temp dirs, caches.
- **Mechanism (adapters):** reset the sandbox (recreate the container or clear the workspace); clear temp/cache dirs on the allow-list.
- **Snapshot:** optional tarball of the workspace (skippable for disposable scratch).
- **Verify:** workspace empty except seeded files.

### Learned corrections / notes (optional)
- **What:** agent scratch notes, per-tool caches, "learned" files.
- **Mechanism:** delete/reset per config; **the keep-list protects the ones you want.**

---

## Remote (where the provider API allows)

> Reality check: most inference APIs are **stateless per request** — there is nothing stored to wipe. Only *stateful* features accumulate.

| Provider surface | Stored state | Delete mechanism |
|---|---|---|
| OpenAI Assistants / Threads | threads, messages, runs | `DELETE /threads/{id}` |
| OpenAI Files | uploaded files | `DELETE /files/{id}` |
| Fine-tunes (any provider) | jobs + resulting models | provider delete / cancel |
| Provider "memory" features | stored user memory | provider-specific endpoint |
| Other hosted providers | varies | behind a per-provider capability flag until verified |

**Snapshot:** a manifest of the remote IDs. `restore` prints them and says plainly that they **cannot** be brought back — a provider delete is irreversible, so the record is all a restore point can offer. The wipe plan marks these `reversible: false` before you commit.
**Verify:** re-list; IDs absent.

---

## NOT wipeable (be honest about it)

- **Base-model weights / "training bias."** Frozen. Not state. The only lever is *swapping* the model, not wiping it. NeurAIlyzer will never claim otherwise.
- **Provider-side logs you don't own.** If a vendor retains request logs, that's their retention policy, not our state. Document it; don't pretend to delete it.

---

## Keep-list (never wiped)

A configurable allow-list of paths / collections / memory keys that are *never* wiped — e.g. your app's durable long-term memory, and NeurAIlyzer's own snapshot store. If a target intersects the keep-list, the adapter skips it and reports the skip — never silently honors *or* silently ignores it.
