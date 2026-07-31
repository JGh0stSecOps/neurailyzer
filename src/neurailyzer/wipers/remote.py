"""Remote wipers: provider-side stored state, where the API allows.

Least-privilege, per-provider tokens read from the environment only -- never a
broad admin token, never logged. Dry-run enumerates remote IDs without
deleting. Remote deletions are **NOT restorable**: the plan says so loudly and
``reversible=False`` -- the pre-wipe snapshot records the IDs that existed,
nothing more.

Providers and surfaces ship only where BOTH a list endpoint and a delete
endpoint are verified (you can't wipe what you can't enumerate)::

    [remote.openai]                     # OPENAI_API_KEY
    surfaces = ["files", "vector_stores"]

    [remote.anthropic]                  # ANTHROPIC_API_KEY
    surfaces = ["files", "batches"]

    [remote.xai]                        # XAI_API_KEY
    surfaces = ["files"]

Venice is registered but has no wipe surfaces: it stores no server-side
conversation state by design (history is client-side), which for a hygiene
tool is a feature to document, not a gap to fill.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import WipePlan, Wiper

_TIMEOUT = 30.0
_USER_AGENT = "neurailyzer (state-hygiene; +https://github.com/JGh0stSecOps/neurailyzer)"


def http_json(method: str, url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """One JSON round-trip. Module-level so tests can monkeypatch it."""
    req = urllib.request.Request(  # noqa: S310 -- https URLs from our own registry
        url, method=method, headers={**headers, "User-Agent": _USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", "replace")[:200]}
        return exc.code, payload


@dataclass(frozen=True)
class Surface:
    """One enumerable+deletable kind of stored object at a provider."""

    name: str
    list_path: str  # GET, paginated
    delete_path: str  # DELETE, with {id}
    cursor_param: str = "after"  # provider's pagination param, fed the last id
    data_key: str = "data"
    id_key: str = "id"
    notes: str = ""


@dataclass(frozen=True)
class Provider:
    """A hosted API that stores deletable state."""

    id: str
    base_url: str
    #: NAME of the env var holding the token -- never the token itself.
    token_env: str
    surfaces: dict[str, Surface]
    #: how the token is sent
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    extra_headers: dict[str, str] = field(default_factory=dict)


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        id="openai",
        base_url="https://api.openai.com",
        token_env="OPENAI_API_KEY",  # noqa: S106 -- env var NAME, not a secret
        surfaces={
            "files": Surface(
                name="files",
                list_path="/v1/files?limit=100",
                delete_path="/v1/files/{id}",
            ),
            "vector_stores": Surface(
                name="vector_stores",
                list_path="/v1/vector_stores?limit=100",
                delete_path="/v1/vector_stores/{id}",
            ),
        },
    ),
    "anthropic": Provider(
        id="anthropic",
        base_url="https://api.anthropic.com",
        token_env="ANTHROPIC_API_KEY",  # noqa: S106 -- env var NAME, not a secret
        auth_header="x-api-key",
        auth_prefix="",
        extra_headers={
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "files-api-2025-04-14",
        },
        surfaces={
            "files": Surface(
                name="files",
                list_path="/v1/files?limit=100",
                delete_path="/v1/files/{id}",
                cursor_param="after_id",
            ),
            "batches": Surface(
                name="batches",
                list_path="/v1/messages/batches?limit=100",
                delete_path="/v1/messages/batches/{id}",
                cursor_param="after_id",
                notes="only ended batches can be deleted; in-flight ones are reported",
            ),
        },
    ),
    "xai": Provider(
        id="xai",
        base_url="https://api.x.ai",
        token_env="XAI_API_KEY",  # noqa: S106 -- env var NAME, not a secret
        surfaces={
            "files": Surface(
                name="files",
                list_path="/v1/files?limit=100",
                delete_path="/v1/files/{id}",
            ),
            # responses are deletable (DELETE /v1/responses/{id}) but NOT
            # enumerable -- no list endpoint, so no wipe surface. Enable
            # team-wide Zero Data Retention in console.x.ai for logs.
        },
    ),
    "venice": Provider(
        id="venice",
        base_url="https://api.venice.ai",
        token_env="VENICE_API_KEY",  # noqa: S106 -- env var NAME, not a secret
        # Venice's design point is that it stores no server-side conversation
        # state (history lives client-side). No surface ships until a stored,
        # enumerable+deletable object kind is verified against their docs --
        # and account API keys will never be one (deleting the key you're
        # authenticated with is lockout, not hygiene).
        surfaces={},
    ),
}


class RemoteWiper(Wiper):
    """Wipes enumerable stored objects at one provider, surface by surface."""

    def __init__(self, provider: Provider, surfaces: Sequence[str]) -> None:
        self.scope = "remote"
        self.provider = provider
        self.surface_names = tuple(surfaces)

    # -- plumbing -------------------------------------------------------------

    def _token(self) -> str | None:
        return os.environ.get(self.provider.token_env) or None

    def _headers(self) -> dict[str, str]:
        token = self._token() or ""
        return {
            self.provider.auth_header: f"{self.provider.auth_prefix}{token}",
            "Content-Type": "application/json",
            **self.provider.extra_headers,
        }

    def _list_ids(self, surface: Surface) -> list[str]:
        ids: list[str] = []
        url = f"{self.provider.base_url}{surface.list_path}"
        for _ in range(100):  # pagination bound, not a hot loop
            status, payload = http_json("GET", url, self._headers())
            if status != 200:
                raise RuntimeError(
                    f"{self.provider.id}/{surface.name}: list failed with HTTP {status}"
                )
            data = payload.get(surface.data_key, [])
            ids.extend(str(item[surface.id_key]) for item in data)
            if not payload.get("has_more") or not data:
                break
            sep = "&" if "?" in surface.list_path else "?"
            last = data[-1][surface.id_key]
            url = f"{self.provider.base_url}{surface.list_path}{sep}{surface.cursor_param}={last}"
        return ids

    def _enumerate(self) -> tuple[dict[str, list[str]], list[str]]:
        """(surface -> ids, notes). Missing token or list errors become notes."""
        notes: list[str] = []
        found: dict[str, list[str]] = {}
        if not self._token():
            notes.append(f"{self.provider.id}: {self.provider.token_env} not set -- skipped")
            return found, notes
        for name in self.surface_names:
            surface = self.provider.surfaces[name]
            try:
                found[name] = self._list_ids(surface)
            except (RuntimeError, OSError, KeyError, TypeError) as exc:
                notes.append(f"{self.provider.id}/{name}: {exc}")
        return found, notes

    # -- Wiper contract -------------------------------------------------------

    def plan(self) -> WipePlan:
        found, notes = self._enumerate()
        total = sum(len(v) for v in found.values())
        detail = ", ".join(f"{k}: {len(v)}" for k, v in found.items()) or "nothing listed"
        return WipePlan(
            scope="remote",
            description=f"[{self.provider.id}] delete {total} object(s) ({detail})",
            item_count=total,
            reversible=False,
            notes=(
                f"{self.provider.id}: remote deletions are NOT restorable "
                "(the snapshot records IDs only)",
                *notes,
            ),
        )

    def commit(self) -> WipePlan:
        found, notes = self._enumerate()
        deleted = 0
        for name, ids in found.items():
            surface = self.provider.surfaces[name]
            for obj_id in ids:
                url = f"{self.provider.base_url}{surface.delete_path.format(id=obj_id)}"
                status, payload = http_json("DELETE", url, self._headers())
                if status in (200, 202, 204):
                    deleted += 1
                else:
                    notes.append(f"{self.provider.id}/{name}: delete {obj_id} -> HTTP {status}")
        return WipePlan(
            scope="remote",
            description=f"[{self.provider.id}] deleted {deleted} object(s)",
            item_count=deleted,
            reversible=False,
            notes=tuple(notes),
        )

    def verify(self) -> bool:
        found, _notes = self._enumerate()
        return sum(len(v) for v in found.values()) == 0
