# Integrations

NeurAIlyzer plugs into agent harnesses two ways, and most harnesses support
both:

1. **As a wiper** — NeurAIlyzer targets the harness's own state trees. Enable
   a [preset](../src/neurailyzer/presets.py) and it knows the paths, including
   the ones that must never be touched:

   ```bash
   neurailyzer detect --enable
   ```

2. **As a tool inside the harness** — NeurAIlyzer's MCP server exposes
   `nl_list_state`, `nl_snapshot`, `nl_wipe`, `nl_restore`, so the agent can
   run hygiene mid-task instead of waiting for a human at a terminal.

Registration is one line in each harness's own config:

| Harness | Register NeurAIlyzer with |
|---|---|
| **Claude Code** | `claude mcp add neurailyzer -- neurailyzer mcp serve` |
| **OpenAI Codex CLI** | a `[mcp_servers.neurailyzer]` table in `~/.codex/config.toml` |
| **Hermes Agent** | an `mcp_servers:` entry in `~/.hermes/config.yaml` |
| **Scion** | an `mcp_servers:` entry in a template's `scion-agent.yaml` — see [scion/](scion/) |
| Anything MCP-capable | point it at `neurailyzer mcp serve` (stdio) or `--transport streamable-http` |

```toml
# ~/.codex/config.toml
[mcp_servers.neurailyzer]
command = "neurailyzer"
args = ["mcp", "serve"]
```

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  neurailyzer:
    command: neurailyzer
    args: ["mcp", "serve"]
```

## Safety notes that apply everywhere

- **`wipe`/`restore` are dry-run unless `commit=true`**, and a commit always
  snapshots first.
- **`scope=all` and `scope=remote` cannot be *committed* over MCP.** A factory
  reset is a human decision, and remote deletes are irreversible — no snapshot
  can undo them, so an agent may ask for the plan but not pull the trigger.
- **Stop the harness before wiping its live session store.** Several keep
  their history in WAL-mode SQLite (Hermes `state.db`, Codex's `*.sqlite`),
  and wiping a database out from under a running writer risks a corrupt
  remainder rather than a clean reset.
- **Put the snapshot store outside every wipe target.** NeurAIlyzer refuses a
  config that nests them, but in containerized harnesses (Scion) also make
  sure the store outlives the agent's scratch — otherwise restore points die
  with the state they were protecting.

## Per-harness notes

- **Claude Code** — durable auto-memory lives *inside* the session tree at
  `~/.claude/projects/<slug>/memory/`, so a naive `rm -rf ~/.claude/projects`
  destroys it. The preset protects that path by pattern, including project
  slugs created after you enabled it.
- **Hermes Agent** — `HERMES_HOME` mixes accumulated state with credentials
  *and the installation itself*, so the preset targets named subtrees only. It
  protects `.env` (every provider key), `auth.json`, and `pairing/` — that
  last one is an authorization allowlist, and wiping it silently
  de-authorizes every paired chat user.
- **OpenAI Codex CLI** — runtime databases carry schema-version suffixes
  (`state_5.sqlite`), so the preset globs rather than pinning names; version
  bumps don't silently stop matching. `auth.json` and `config.toml` are kept.
- **Scion** — per-agent session state under `agents/<name>/` is the hygiene
  target. Agent *worktrees* (`../.scion_worktrees/`) are deliberately never
  touched: they can hold unmerged work. `hub.db` (identity and signing keys)
  is kept. For full teardown of an agent, prefer `scion delete <name>`.
- **Venice** — stores no server-side conversation state by design, so there is
  nothing to wipe remotely; history lives in browser origin storage. The
  `venice-web` preset targets only per-origin IndexedDB directories, never
  Chromium's shared `Local Storage/leveldb` (that one is every site at once).
  Close the browser first — LevelDB holds a lock.

## Remote provider state

Provider-side objects are wiped through `--scope remote`, opt-in per surface,
with tokens read from the environment and never logged:

```toml
[remote.openai]      # OPENAI_API_KEY
surfaces = ["files", "vector_stores"]

[remote.anthropic]   # ANTHROPIC_API_KEY
surfaces = ["files", "batches"]

[remote.xai]         # XAI_API_KEY
surfaces = ["files"]
```

A surface ships only where both a *list* and a *delete* endpoint exist — you
can't honestly wipe what you can't enumerate. Remote deletions are **not
reversible**: the pre-wipe snapshot records the IDs that existed, and the plan
says so plainly.
