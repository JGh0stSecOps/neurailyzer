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
    #: conversation/thread history -- `--scope session`
    session: tuple[str, ...] = ()
    #: scratch, caches, temp, downloads -- `--scope sandbox`
    sandbox: tuple[str, ...] = ()
    #: never wiped; merged into the keep-list whenever this preset is enabled
    keep: tuple[str, ...] = ()
    notes: str = ""
    #: paths verified against a live install / primary sources; presets with
    #: unverified layouts stay out of the registry until confirmed.
    verified: bool = True


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
    name="Claude Code",
    vendor="Anthropic",
    detect=("~/.claude",),
    session=(
        # per-project transcripts + per-session subagent dirs
        "~/.claude/projects",
        "~/.claude/history.jsonl",
        "~/.claude/sessions",
        "~/.claude/session-env",
    ),
    sandbox=(
        "~/.claude/shell-snapshots",
        "~/.claude/paste-cache",
        "~/.claude/file-history",
        "~/.claude/debug",
        "~/.claude/cache",
        "~/.claude/downloads",
        "~/.claude/telemetry",
    ),
    keep=(
        # auto-memory lives INSIDE the projects tree -- the one trap that
        # makes a naive `rm -rf ~/.claude/projects` destructive
        "~/.claude/projects/*/memory",
        "~/.claude/CLAUDE.md",
        "~/.claude/settings.json",
        "~/.claude/settings.local.json",
        "~/.claude/keybindings.json",
        "~/.claude/plugins",
        "~/.claude/hooks",
        "~/.claude/ide",
        "~/.claude/plans",
        "~/.claude/backups",
        "~/.claude/tasks",
        "~/.claude/scheduled_tasks.lock",
        "~/.claude/.credentials.json",
    ),
    notes="Layout verified against a live 2026-07 install (macOS). "
    "projects/<slug>/*.jsonl are transcripts; projects/<slug>/memory/ is "
    "durable auto-memory and must survive every wipe.",
)


#: HELD OUT of REGISTRY: the layout came from a research pass whose citations
#: could not be traced to primary sources. A guessed path in a wiper is a
#: destructive bug, so this ships only once each path is source-verified.
GROK_BUILD = Preset(
    verified=False,
    id="grok-build",
    name="Grok Build",
    vendor="xAI",
    detect=("$GROK_HOME", "~/.grok"),
    session=(
        # per-cwd session trees: updates.jsonl, chat_history.jsonl, plans,
        # rewind points, compaction checkpoints, subagent + MCP spill dirs
        "$GROK_HOME/sessions",
        "~/.grok/sessions",
    ),
    sandbox=(
        "$GROK_HOME/logs",
        "~/.grok/logs",
    ),
    keep=(
        "~/.grok/auth.json",
        "~/.grok/mcp_credentials.json",
        "~/.grok/config.toml",
        "~/.grok/memory",  # user-curated cross-session knowledge (MEMORY.md + index)
        "~/.grok/skills",
        "$GROK_HOME/auth.json",
        "$GROK_HOME/mcp_credentials.json",
        "$GROK_HOME/config.toml",
        "$GROK_HOME/memory",
        "$GROK_HOME/skills",
    ),
    notes="UNVERIFIED -- not in the registry. Paths await source verification "
    "against xai-org/grok-build. Once confirmed: auth.json and "
    "mcp_credentials.json are OAuth tokens (never wipe), and NeurAIlyzer "
    "registers with: grok mcp add neurailyzer -- neurailyzer mcp serve",
)

CODEX = Preset(
    id="codex",
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
REGISTRY: dict[str, Preset] = {
    p.id: p
    for p in (
        CLAUDE_CODE,
        CODEX,
        HERMES,
        SCION,
        VENICE_WEB,
        # GROK_BUILD / OPENCODE are held out of the registry until their
        # layouts are source-verified -- a guessed path in a wiper is a
        # destructive bug, so unverified beats plausible.
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
