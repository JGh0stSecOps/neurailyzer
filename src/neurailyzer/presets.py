"""Harness presets: known agent tools and where their state lives.

A preset packages, for one known harness (Claude Code, OpenCode, ...):

- ``detect``  -- paths whose existence means the harness is installed here
- ``session`` -- conversation/thread history trees (wipeable)
- ``sandbox`` -- scratch/cache/temp trees (wipeable)
- ``keep``    -- state that must NEVER be wiped (credentials, settings,
  plugins, durable memory), merged into the keep-list automatically

Users enable presets in config rather than hand-listing paths::

    [presets]
    enabled = ["claude-code", "opencode"]

or discover what's installed::

    neurailyzer detect            # what's here + what would be targeted
    neurailyzer detect --enable   # write/merge the [presets] block

Path templates may use ``~`` and environment variables (``$XDG_DATA_HOME``).
Every template is a *candidate*: entries that don't exist on this machine
simply contribute nothing. Definitions live in code so a release upgrade can
track harness layout changes; nothing is fetched remotely.
"""

from __future__ import annotations

import glob as globmod
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Preset:
    """One known harness and the state trees it accumulates."""

    id: str
    name: str
    vendor: str
    #: existence of ANY of these (expanded) means "installed here"
    detect: tuple[str, ...]
    #: REQUIRED, and deliberately has no default: every path below must be
    #: traced to upstream source or a live install before it can wipe. A
    #: default of True would make the REGISTRY gate vacuous -- a contributor
    #: who never thought about it would pass.
    verified: bool
    #: conversation/thread history -- `--scope session`
    session: tuple[str, ...] = ()
    #: scratch, caches, temp, downloads -- `--scope sandbox`
    sandbox: tuple[str, ...] = ()
    #: never wiped; merged into the keep-list whenever this preset is enabled
    keep: tuple[str, ...] = ()
    notes: str = ""
    #: Things this preset CANNOT clean, surfaced on every wipe -- not just in
    #: `detect`. A user who enabled a preset days ago must not read
    #: "verified" and conclude their chats are gone when they are not.
    caveats: tuple[str, ...] = ()


def _expand(template: str) -> Path:
    return Path(os.path.expandvars(template)).expanduser()


def expand_existing(templates: tuple[str, ...]) -> tuple[Path, ...]:
    """Expand templates (globs allowed) and keep only paths that exist now.

    Globs matter because harnesses version their filenames (Codex's
    ``state_5.sqlite``) or nest per-profile/per-project trees — hardcoding
    those would silently rot.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for t in templates:
        p = _expand(t)
        candidates = (
            [Path(m) for m in sorted(globmod.glob(str(p), recursive=True))]
            if any(c in t for c in "*?[")
            else [p]
        )
        for c in candidates:
            r = c.resolve()
            if c.exists() and r not in seen:
                seen.add(r)
                out.append(c)
    return tuple(out)


def expand_all(templates: tuple[str, ...]) -> tuple[Path, ...]:
    """Expand templates whether or not they exist (keep-list wants this:
    a keep entry must hold even for state created *after* enablement)."""
    return tuple(_expand(t) for t in templates)


# ---------------------------------------------------------------------------
# Registry. Layouts verified against live installs or upstream source -- keep
# the `notes` honest about which.
# ---------------------------------------------------------------------------

CLAUDE_CODE = Preset(
    id="claude-code",
    verified=True,
    name="Claude Code",
    vendor="Anthropic",
    # CLAUDE_CONFIG_DIR relocates the whole tree; a preset that only knows
    # ~/.claude would silently see nothing on a machine that sets it.
    detect=("$CLAUDE_CONFIG_DIR", "~/.claude"),
    session=(
        # per-project transcripts + per-session subagent dirs
        "$CLAUDE_CONFIG_DIR/projects",
        "~/.claude/projects",
        "$CLAUDE_CONFIG_DIR/history.jsonl",
        "~/.claude/history.jsonl",
        "$CLAUDE_CONFIG_DIR/sessions",
        "~/.claude/sessions",
        "$CLAUDE_CONFIG_DIR/session-env",
        "~/.claude/session-env",
    ),
    sandbox=(
        "$CLAUDE_CONFIG_DIR/shell-snapshots",
        "~/.claude/shell-snapshots",
        "$CLAUDE_CONFIG_DIR/paste-cache",
        "~/.claude/paste-cache",
        "$CLAUDE_CONFIG_DIR/file-history",
        "~/.claude/file-history",
        "$CLAUDE_CONFIG_DIR/debug",
        "~/.claude/debug",
        "$CLAUDE_CONFIG_DIR/cache",
        "~/.claude/cache",
        "$CLAUDE_CONFIG_DIR/telemetry",
        "~/.claude/telemetry",
        "$CLAUDE_CONFIG_DIR/downloads",
        "~/.claude/downloads",
    ),
    keep=(
        # auto-memory lives INSIDE the projects tree -- the one trap that
        # makes a naive `rm -rf ~/.claude/projects` destructive
        "~/.claude/projects/*/memory",
        "~/.claude/CLAUDE.md",
        "~/.claude/settings.json",
        "~/.claude/settings.local.json",
        "~/.claude/remote-settings.json",  # cached managed policy
        "~/.claude/policy-limits.json",
        "~/.claude/keybindings.json",
        "~/.claude/plugins",
        "~/.claude/hooks",
        "~/.claude/ide",
        "~/.claude/plans",
        "~/.claude/backups",
        "~/.claude/tasks",
        "~/.claude/scheduled_tasks.lock",
        "~/.claude/.credentials.json",
        "~/.claude.json",  # account/org identity + project trust (outside the tree)
        "$CLAUDE_CONFIG_DIR/projects/*/memory",
        "$CLAUDE_CONFIG_DIR/CLAUDE.md",
        "$CLAUDE_CONFIG_DIR/settings.json",
        "$CLAUDE_CONFIG_DIR/settings.local.json",
        "$CLAUDE_CONFIG_DIR/remote-settings.json",
        "$CLAUDE_CONFIG_DIR/policy-limits.json",
        "$CLAUDE_CONFIG_DIR/plugins",
        "$CLAUDE_CONFIG_DIR/hooks",
        "$CLAUDE_CONFIG_DIR/ide",
        "$CLAUDE_CONFIG_DIR/plans",
        "$CLAUDE_CONFIG_DIR/backups",
        "$CLAUDE_CONFIG_DIR/tasks",
        "$CLAUDE_CONFIG_DIR/.credentials.json",
    ),
    notes="Layout verified against a live 2026-07 install (macOS) and the "
    "upstream claude-directory doc. projects/<slug>/*.jsonl are transcripts; "
    "projects/<slug>/memory/ is durable auto-memory and must survive every "
    "wipe. Storage is append-only JSONL -- no SQLite, so no WAL hazard; "
    "liveness comes from sessions/<pid>.json. file-history/ backs "
    "checkpoint-rewind for PAST sessions, so it sits in `sandbox`: wiped "
    "only when you ask for that scope, and restorable from the snapshot.",
)


GROK_BUILD = Preset(
    id="grok-build",
    verified=True,
    name="Grok Build",
    vendor="xAI",
    detect=("$GROK_HOME", "~/.grok"),
    session=(
        # sessions/<encoded-cwd>/<session-id>/ holds updates.jsonl,
        # chat_history.jsonl, summary.json, plan.json, rewind_points.jsonl.
        # Target the tree wholesale: the <encoded-cwd> segment is url-encoded
        # ONLY while it fits 255 bytes, and falls back to {slug}-{blake3} --
        # so any wiper that pattern-matches that name misses long-path
        # sessions (CJK, OneDrive, iCloud paths hit this).
        "$GROK_HOME/sessions",
        "~/.grok/sessions",
    ),
    sandbox=(
        "$GROK_HOME/logs",
        "~/.grok/logs",
        "$GROK_HOME/memtrace",
        "~/.grok/memtrace",
        "$GROK_HOME/debug",
        "~/.grok/debug",
        "$GROK_HOME/marketplace-cache",
        "~/.grok/marketplace-cache",
    ),
    keep=(
        # AGENT WORKTREES: may hold uncommitted work. Never touched -- same
        # rule as Scion. Use `grok`'s own worktree commands to reclaim these.
        "~/.grok/worktrees",
        "~/.grok/worktrees.db*",
        "~/.grok/worktree_pool",
        "$GROK_HOME/worktrees",
        "$GROK_HOME/worktrees.db*",
        "$GROK_HOME/worktree_pool",
        # pending server uploads -- dropping them loses queued data silently
        "~/.grok/upload_queue",
        "$GROK_HOME/upload_queue",
        # user-authored extension points
        "~/.grok/personas",
        "~/.grok/rules",
        "~/.grok/workflows",
        "~/.grok/hooks",
        "~/.grok/installed-plugins",
        "~/.grok/vendor",
        "~/.grok/pager.toml",
        "~/.grok/sandbox.toml",
        "~/.grok/lsp.json",
        "$GROK_HOME/personas",
        "$GROK_HOME/rules",
        "$GROK_HOME/workflows",
        "$GROK_HOME/hooks",
        "$GROK_HOME/installed-plugins",
        "$GROK_HOME/vendor",
        # credentials + their advisory flocks
        "~/.grok/auth.json",
        "~/.grok/auth.json.lock",
        "~/.grok/mcp_credentials.json",
        "~/.grok/mcp_credentials.json.lock",
        # THE INSTALLATION ITSELF: bin/grok is a symlink into downloads/,
        # which holds the real binary payloads. Wiping either uninstalls Grok.
        "~/.grok/bin",
        "~/.grok/downloads",
        "~/.grok/version.json",  # auto-updater state
        # config + runtime coordination
        "~/.grok/config.toml",
        "~/.grok/leader.sock",
        "~/.grok/leader*.lock",
        # user-curated knowledge, same stance as every other preset
        "~/.grok/memory",
        "~/.grok/skills",
        "$GROK_HOME/auth.json",
        "$GROK_HOME/auth.json.lock",
        "$GROK_HOME/mcp_credentials.json",
        "$GROK_HOME/mcp_credentials.json.lock",
        "$GROK_HOME/bin",
        "$GROK_HOME/downloads",
        "$GROK_HOME/version.json",
        "$GROK_HOME/config.toml",
        "$GROK_HOME/leader.sock",
        "$GROK_HOME/leader*.lock",
        "$GROK_HOME/memory",
        "$GROK_HOME/skills",
    ),
    notes="Layout source-verified against xai-org/grok-build @ 2a28b4a "
    "(paths.rs grok_home/sessions_cwd_dir, auth/storage.rs, "
    "mcp/credentials.rs). GROK_HOME is the install root as well as the state "
    "root -- bin/ and downloads/ hold the binary itself and are never "
    "touched. Liveness is read from leader.lock. Register NeurAIlyzer with: "
    "grok mcp add neurailyzer -- neurailyzer mcp serve",
)

CODEX = Preset(
    id="codex",
    verified=True,
    name="OpenAI Codex CLI",
    vendor="OpenAI",
    detect=("$CODEX_HOME", "~/.codex"),
    session=(
        # rollout jsonl per session under YYYY/MM/DD, plus global history and
        # the versioned runtime DBs (schema-suffixed names -- glob, don't pin)
        "$CODEX_HOME/sessions",
        "~/.codex/sessions",
        "$CODEX_HOME/history.jsonl",
        "~/.codex/history.jsonl",
        "$CODEX_HOME/state_*.sqlite*",
        "~/.codex/state_*.sqlite*",
        "$CODEX_HOME/thread_history_*.sqlite*",
        "~/.codex/thread_history_*.sqlite*",
    ),
    sandbox=(
        "$CODEX_HOME/log",
        "~/.codex/log",
        "$CODEX_HOME/logs_*.sqlite*",
        "~/.codex/logs_*.sqlite*",
        "$CODEX_HOME/version.json",
        "~/.codex/version.json",
    ),
    keep=(
        "~/.codex/auth.json",
        "~/.codex/config.toml",
        "~/.codex/requirements.toml",
        "~/.codex/hooks.json",
        "~/.codex/*.config.toml",  # named profiles
        "~/.codex/memories_*.sqlite*",  # agent memory: durable, not scratch
        "$CODEX_HOME/auth.json",
        "$CODEX_HOME/config.toml",
        "$CODEX_HOME/requirements.toml",
        "$CODEX_HOME/hooks.json",
        "$CODEX_HOME/*.config.toml",
        "$CODEX_HOME/memories_*.sqlite*",
    ),
    notes="Layout source-verified against openai/codex (rust, 2026-07). SQLite "
    "filenames carry schema-version suffixes -- targeted via glob so version "
    "bumps don't rot the preset. Register NeurAIlyzer inside Codex via "
    "[mcp_servers.neurailyzer] in ~/.codex/config.toml.",
)

HERMES = Preset(
    id="hermes",
    verified=True,
    name="Hermes Agent",
    vendor="Nous Research",
    detect=("$HERMES_HOME", "~/.hermes", "$LOCALAPPDATA/hermes"),
    session=(
        # state.db is the primary session store (WAL mode: take the sidecars
        # atomically); sessions/ holds the gateway index + legacy jsonl
        "$HERMES_HOME/state.db*",
        "~/.hermes/state.db*",
        "$LOCALAPPDATA/hermes/state.db*",
        "$HERMES_HOME/sessions",
        "~/.hermes/sessions",
        "$LOCALAPPDATA/hermes/sessions",
        "$HERMES_HOME/session-exports",
        "~/.hermes/session-exports",
        "~/.hermes/profiles/*/state.db*",
        "~/.hermes/profiles/*/sessions",
    ),
    sandbox=(
        "$HERMES_HOME/logs",
        "~/.hermes/logs",
        "$LOCALAPPDATA/hermes/logs",
        "$HERMES_HOME/cache",
        "~/.hermes/cache",
        "$LOCALAPPDATA/hermes/cache",
        "$HERMES_HOME/checkpoints",
        "~/.hermes/checkpoints",
        "~/.hermes/cron/output",
        "~/.hermes/profiles/*/logs",
        "~/.hermes/profiles/*/cache",
    ),
    keep=(
        # --- credentials + AUTHORIZATION state. Wiping pairing/ silently
        # de-authorizes every paired chat user; .env holds every provider key.
        "~/.hermes/.env",
        "~/.hermes/auth.json",
        "~/.hermes/.anthropic_oauth.json",
        "~/.hermes/slack_tokens.json",
        "~/.hermes/honcho.json",
        "~/.hermes/hindsight",
        "~/.hermes/pairing",
        "~/.hermes/platforms/pairing",
        "~/.hermes/feishu_comment_pairing.json",
        "~/.hermes/gateway_state.json",
        "~/.hermes/gateway",
        "~/.hermes/channel_directory.json",
        "~/.hermes/channel_aliases.json",
        "~/.hermes/processes.json",
        "~/.hermes/desktop-ssh",
        # --- config + the installation itself (HERMES_HOME holds both)
        "~/.hermes/config.yaml",
        "~/.hermes/hermes-agent",  # the agent's own git checkout
        "~/.hermes/venvs",
        "~/.hermes/bin",
        # --- memory + learned skills: the product's headline features, and
        # user work product. Wipe only by explicit [targets] opt-in.
        "~/.hermes/memories",
        "~/.hermes/memory_store.db*",
        "~/.hermes/response_store.db*",
        "~/.hermes/skills",
        "~/.hermes/optional-skills",
        "~/.hermes/plugins",
        # --- user-authored setup, not scratch
        "~/.hermes/projects.db*",
        "~/.hermes/verification_evidence.db*",
        "~/.hermes/kanban*",
        "~/.hermes/cron/jobs.json",
        "~/.hermes/cron/executions.db*",
        # --- Hermes' OWN rollback safety net
        "~/.hermes/state-snapshots",
        # --- per-profile mirrors of the above
        "~/.hermes/profiles/*/.env",
        "~/.hermes/profiles/*/auth.json",
        "~/.hermes/profiles/*/config.yaml",
        "~/.hermes/profiles/*/memories",
        "~/.hermes/profiles/*/skills",
        "~/.hermes/profiles/*/pairing",
        "~/.hermes/profiles/*/state-snapshots",
        # --- env-override + Windows roots
        "$HERMES_HOME/.env",
        "$HERMES_HOME/auth.json",
        "$HERMES_HOME/pairing",
        "$HERMES_HOME/gateway_state.json",
        "$HERMES_HOME/config.yaml",
        "$HERMES_HOME/memories",
        "$HERMES_HOME/memory_store.db*",
        "$HERMES_HOME/skills",
        "$HERMES_HOME/state-snapshots",
        "$LOCALAPPDATA/hermes/.env",
        "$LOCALAPPDATA/hermes/auth.json",
        "$LOCALAPPDATA/hermes/pairing",
        "$LOCALAPPDATA/hermes/config.yaml",
        "$LOCALAPPDATA/hermes/memories",
        "$LOCALAPPDATA/hermes/skills",
        "$LOCALAPPDATA/hermes/state-snapshots",
    ),
    notes="Layout source-verified against NousResearch/hermes-agent v0.19.1 "
    "(2026-07-30). HERMES_HOME mixes state with credentials AND the install "
    "itself, so this preset targets named subtrees only -- never the root. "
    "Memory stores, learned skills, and chat-platform pairing (an "
    "authorization allowlist) are protected by default. state.db is WAL "
    "SQLite: stop Hermes before wiping. Register NeurAIlyzer inside Hermes "
    "under mcp_servers: in ~/.hermes/config.yaml.",
)

SCION = Preset(
    id="scion",
    verified=True,
    name="Scion",
    vendor="Google (GoogleCloudPlatform/scion, experimental)",
    detect=("~/.scion",),
    session=(
        # per-agent session state: global project + split-storage projects.
        # Worktrees (../.scion_worktrees) are deliberately NOT targeted: they
        # can hold unmerged agent work.
        "~/.scion/agents",
        "~/.scion/project-configs/*/.scion/agents",
    ),
    sandbox=(),
    keep=(
        "~/.scion/settings.yaml",
        "~/.scion/templates",
        "~/.scion/harness-configs",
        "~/.scion/hub.db*",  # Hub control plane: identity, keys, registrations
        "~/.scion/projects",
        "~/.scion/project-configs/*/.scion/settings.yaml",
        "~/.scion/project-configs/*/.scion/templates",
        "~/.scion/project-configs/*/.scion/harness-configs",
    ),
    notes="Layout source-verified against GoogleCloudPlatform/scion (2026-07). "
    "Prefer `scion delete <agent>` for full lifecycle teardown; this preset "
    "wipes accumulated per-agent session state. Agent worktrees are never "
    "touched (may hold unmerged work). To present NeurAIlyzer as a tool "
    "inside Scion, add it to a template's scion-agent.yaml mcp_servers: map "
    "-- see integrations/scion/.",
)


VENICE_WEB = Preset(
    id="venice-web",
    verified=True,
    name="Venice (web app)",
    vendor="Venice AI",
    # Venice has no desktop app: its "local state" IS browser origin storage.
    detect=(
        "~/Library/Application Support/Google/Chrome/*/IndexedDB/"
        "https_venice.ai_0.indexeddb.leveldb",
        "~/.config/google-chrome/*/IndexedDB/https_venice.ai_0.indexeddb.leveldb",
        "$LOCALAPPDATA/Google/Chrome/User Data/*/IndexedDB/https_venice.ai_0.indexeddb.leveldb",
        "~/Library/Application Support/Firefox/Profiles/*/storage/default/https+++venice.ai",
        "~/.mozilla/firefox/*/storage/default/https+++venice.ai",
        "$APPDATA/Mozilla/Firefox/Profiles/*/storage/default/https+++venice.ai",
    ),
    session=(
        # Chromium keeps IndexedDB per-origin, so these dirs are exactly
        # Venice and nothing else. Chrome/Edge/Brave/Arc share the layout.
        "~/Library/Application Support/Google/Chrome/*/IndexedDB/https_venice.ai_0.indexeddb.*",
        "~/Library/Application Support/Microsoft Edge/*/IndexedDB/https_venice.ai_0.indexeddb.*",
        "~/Library/Application Support/BraveSoftware/Brave-Browser/*/IndexedDB/"
        "https_venice.ai_0.indexeddb.*",
        "~/.config/google-chrome/*/IndexedDB/https_venice.ai_0.indexeddb.*",
        "~/.config/BraveSoftware/Brave-Browser/*/IndexedDB/https_venice.ai_0.indexeddb.*",
        "$LOCALAPPDATA/Google/Chrome/User Data/*/IndexedDB/https_venice.ai_0.indexeddb.*",
        "$LOCALAPPDATA/Microsoft/Edge/User Data/*/IndexedDB/https_venice.ai_0.indexeddb.*",
        # Firefox isolates per origin under one directory
        "~/Library/Application Support/Firefox/Profiles/*/storage/default/https+++venice.ai",
        "~/.mozilla/firefox/*/storage/default/https+++venice.ai",
        "$APPDATA/Mozilla/Firefox/Profiles/*/storage/default/https+++venice.ai",
    ),
    sandbox=(),
    keep=(),
    notes="Venice stores NO server-side conversation state by design -- chat "
    "history lives in the browser, so hygiene here is a local wipe and there "
    "is nothing to delete remotely. Targets ONLY per-origin IndexedDB dirs; "
    "Chromium's Local Storage leveldb is shared across every site in the "
    "profile and is never touched. Close the browser first (LevelDB holds a "
    "lock). Wiping this also drops the client-side key that decrypts Venice "
    "encrypted backups -- export first if you use them.",
)


#: Only source-verified presets are enabled. `verified=False` definitions stay
#: in the module (as documentation of what still needs confirming) but never
#: reach a user's wipe plan -- enforced by the assertion below.
OPENCODE = Preset(
    id="opencode",
    verified=True,
    name="opencode",
    vendor="anomalyco (open source)",
    # XDG layout on every OS, including Windows (~/.local/share/opencode).
    detect=(
        "$XDG_DATA_HOME/opencode",
        "~/.local/share/opencode",
    ),
    session=(
        # The legacy JSON trees are safe to remove wholesale.
        #
        # NOTE the deliberate omission: <data>/opencode*.db is BOTH the
        # session store AND a credential store (its `credential` table holds
        # connector credentials), so unlinking it to clear chats would also
        # destroy those credentials. Clearing sessions there needs row-level
        # deletes, which this release's file wipers cannot do -- so the DB is
        # protected instead. See notes.
        "$XDG_DATA_HOME/opencode/storage",
        "~/.local/share/opencode/storage",
        "$XDG_DATA_HOME/opencode/project",
        "~/.local/share/opencode/project",
    ),
    sandbox=(
        "$XDG_CACHE_HOME/opencode",
        "~/.cache/opencode",
        "$TMPDIR/opencode",
        "$XDG_STATE_HOME/opencode/locks",
        "~/.local/state/opencode/locks",
    ),
    keep=(
        # credentials, and the DB that doubles as one
        "$XDG_DATA_HOME/opencode/auth.json",
        "~/.local/share/opencode/auth.json",
        "$XDG_DATA_HOME/opencode/mcp-auth.json",
        "~/.local/share/opencode/mcp-auth.json",
        "$XDG_DATA_HOME/opencode/opencode*.db*",
        "~/.local/share/opencode/opencode*.db*",
        # daemon shared secret: wiping it orphans a running server
        "$XDG_STATE_HOME/opencode/password",
        "~/.local/state/opencode/password",
        "$XDG_STATE_HOME/opencode/server.json",
        "~/.local/state/opencode/server.json",
        # managed git worktrees: real checkouts, possibly uncommitted
        "$XDG_DATA_HOME/opencode/worktree",
        "~/.local/share/opencode/worktree",
        # user configuration (wiping it silently un-configures every provider)
        "$XDG_CONFIG_HOME/opencode",
        "~/.config/opencode",
        "~/.opencode",
    ),
    caveats=(
        "opencode: chat history in opencode*.db is NOT wiped -- that database "
        "is also a credential store, so deleting the file would take your "
        "provider logins with it. Only the legacy JSON session trees and "
        "caches were cleared. Clearing chats from the DB needs row-level "
        "deletes (a future sqlite wiper).",
    ),
    notes="Layout source-verified against anomalyco/opencode @ da59457 "
    "(core/src/global.ts XDG roots, database/database.ts, auth/index.ts, "
    "cli/services/daemon.ts). XDG paths apply on every OS including Windows. "
    "IMPORTANT: opencode*.db is both the session store and a credential "
    "store, so this preset PROTECTS it and wipes only the legacy JSON trees "
    "plus caches -- clearing chats from the DB needs row-level deletes (a "
    "future sqlite wiper), and file-level deletion would take credentials "
    "with it. Liveness is read from <state>/server.json. Shared sessions "
    "pushed to opncd.ai are server-side and unaffected by any local wipe.",
)


REGISTRY: dict[str, Preset] = {
    p.id: p
    for p in (
        CLAUDE_CODE,
        CODEX,
        GROK_BUILD,
        HERMES,
        SCION,
        OPENCODE,
        VENICE_WEB,
    )
}


@dataclass(frozen=True)
class Detection:
    """A preset found installed on this machine."""

    preset: Preset
    hits: tuple[Path, ...] = field(default_factory=tuple)


def detect_installed() -> list[Detection]:
    """Which known harnesses exist on this machine?"""
    found: list[Detection] = []
    for preset in REGISTRY.values():
        hits = expand_existing(preset.detect)
        if hits:
            found.append(Detection(preset=preset, hits=hits))
    return found


_unverified = sorted(p.id for p in REGISTRY.values() if not p.verified)
if _unverified:  # pragma: no cover - a packaging mistake, caught at import
    raise RuntimeError(
        f"unverified preset(s) in REGISTRY: {', '.join(_unverified)}. "
        "Every shipped path must be source-verified before it can wipe."
    )
