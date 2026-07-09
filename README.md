<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark-light.svg" alt="NeurAIlyzer" width="340">
</picture>

**State hygiene for AI agents — for any model, any stack.** Wipe the drift, roll back to any point in time.


[Design](docs/DESIGN.md) · [Wipe taxonomy](docs/WIPE-TAXONOMY.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

</div>

---

> **Status: early development.** The design, CI, and guardrails are in place; implementation lands incrementally. This README describes the target, not yet shipped behavior.

## The idea

Long-running agent systems don't drift because their **weights** change — the weights are frozen. They drift because of the **state that accumulates around them**: KV cache, conversation history, injected RAG memory, system-prompt cruft, tool-sandbox leftovers, and stored threads/files on remote providers. That accumulated state is what biases outputs and creeps storage.

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

| Layer | Wipeable? |
|---|---|
| Local runtime state (KV cache, resident models) | ✅ unload + flush |
| Conversation / thread history (any chat store) | ✅ scoped delete |
| RAG / vector memory | ✅ scoped/collection delete |
| Tool-sandbox scratch, temp/cache | ✅ reset |
| Remote provider state (threads/files/assistants/fine-tunes) | ⚠️ where the API allows |
| Base-model "bias" (the frozen weights) | ❌ not state — swap the model, you can't wipe it |

The honest line: you can't wipe a hosted model's training. You *can* wipe every bit of **state you created** around it — and that's what actually drifts.

## Interfaces

- **CLI** — `neurailyzer wipe --scope … · list-state · snapshot · restore --to <T>`
- **MCP server** — the same verbs as MCP tools, so any agent (Claude, Codex, GPT-based, local…) can call it mid-workflow
- **Library** — import the core and drive it from your own orchestrator

## Safety

Wiping is destructive, so the defaults are conservative: **dry-run by default**, an explicit `--commit` to act, a **keep-list** that's never touched, and a **snapshot taken before every wipe** so any reset is reversible. See [SECURITY.md](SECURITY.md).

## Quick start

*Planned surface — implementation in progress.*

```bash
neurailyzer list-state                      # what exists + would be affected
neurailyzer snapshot --label pre-task       # take a restore point
neurailyzer wipe --scope session            # dry-run: what would be wiped
neurailyzer wipe --scope session --commit   # actually wipe (snapshot taken first)
neurailyzer restore --to 2026-07-09T04:00   # roll state back to a point in time
neurailyzer mcp serve                        # expose the verbs to agents
```

## Contributing

Built to accept contributions from **humans and AI agents alike**, safely. All changes are **PR-gated on `dev`**, with CI (lint + tests), security scans, and an **injection/slop guard** that screens every contribution for prompt-injection payloads and low-quality output before review. `main` is the curated/release branch. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE) — permissive, so anyone can adopt NeurAIlyzer or point their own agent at it.
