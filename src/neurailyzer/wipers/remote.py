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
from urllib.parse import quote

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
            if not body:
                return resp.status, {}
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError as exc:
                # a 200 that isn't JSON means we do not understand this API
                raise RuntimeError(f"{url}: HTTP {resp.status} body was not JSON ({exc})") from exc
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
    more_key: str = "has_more"  # truthy field meaning "another page exists"
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


@dataclass
class Enumeration:
    """What a listing pass found -- and whether it could look at all.

    The distinction matters: "there is nothing there" and "we never managed
    to ask" must never render the same, or a failed wipe reports success.
    """

    found: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    complete: bool = True  # every requested surface was listed end to end

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.found.values())

    @property
    def all_ids(self) -> list[str]:
        return [f"{surface}:{i}" for surface, ids in self.found.items() for i in ids]


class RemoteWiper(Wiper):
    """Wipes enumerable stored objects at one provider, surface by surface."""

    #: hard bound on pages per surface; hitting it is reported, never silent.
    MAX_PAGES = 100

    def __init__(self, provider: Provider, surfaces: Sequence[str]) -> None:
        self.scope = "remote"
        self.provider = provider
        self.surface_names = tuple(surfaces)

    # -- plumbing -------------------------------------------------------------

    def _token(self) -> str | None:
        raw = os.environ.get(self.provider.token_env)
        if raw is None:
            return None
        # Keys arrive with stray whitespace all the time -- `export K=$(cat
        # key.txt)`, a .env line, a paste. A newline in a header value raises
        # deep in http.client, and the traceback carries the key.
        token = raw.strip()
        return token or None

    def _headers(self) -> dict[str, str]:
        token = self._token() or ""
        return {
            self.provider.auth_header: f"{self.provider.auth_prefix}{token}",
            "Content-Type": "application/json",
            **self.provider.extra_headers,
        }

    def _list_ids(self, surface: Surface) -> tuple[list[str], list[str]]:
        """(ids, notes). Raises RuntimeError if the listing cannot be trusted."""
        ids: list[str] = []
        notes: list[str] = []
        seen: set[str] = set()
        url = f"{self.provider.base_url}{surface.list_path}"
        for page in range(self.MAX_PAGES):
            status, payload = http_json("GET", url, self._headers())
            if status != 200:
                raise RuntimeError(
                    f"{self.provider.id}/{surface.name}: list failed with HTTP {status}"
                )
            data = payload.get(surface.data_key, [])
            if not isinstance(data, list):
                raise RuntimeError(
                    f"{self.provider.id}/{surface.name}: {surface.data_key!r} was not a list"
                )
            page_ids = [str(item[surface.id_key]) for item in data]
            fresh = [i for i in page_ids if i not in seen]
            ids.extend(fresh)
            seen.update(fresh)
            if not data or not payload.get(surface.more_key):
                return ids, notes
            if not fresh:
                # the cursor did not advance: paginating again would loop
                notes.append(
                    f"{self.provider.id}/{surface.name}: pagination stopped -- "
                    f"page {page + 2} repeated ids already seen"
                )
                return ids, notes
            sep = "&" if "?" in surface.list_path else "?"
            cursor = quote(str(data[-1][surface.id_key]), safe="")
            url = f"{self.provider.base_url}{surface.list_path}{sep}{surface.cursor_param}={cursor}"
        raise RuntimeError(
            f"{self.provider.id}/{surface.name}: more than "
            f"{self.MAX_PAGES} pages of results -- refusing to wipe a "
            "partially-enumerated surface"
        )

    def _enumerate(self) -> Enumeration:
        """List every configured surface. Failures are reported, not hidden."""
        out = Enumeration()
        if not self._token():
            out.notes.append(f"{self.provider.id}: {self.provider.token_env} not set -- skipped")
            out.complete = False
            return out
        for name in self.surface_names:
            surface = self.provider.surfaces[name]
            try:
                ids, notes = self._list_ids(surface)
            except (RuntimeError, OSError, KeyError, TypeError, ValueError) as exc:
                out.notes.append(f"{self.provider.id}/{name}: {exc}")
                out.complete = False
                continue
            out.found[name] = ids
            out.notes.extend(notes)
            if notes:  # a truncated listing is not a complete one
                out.complete = False
        return out

    # -- Wiper contract -------------------------------------------------------

    def plan(self) -> WipePlan:
        enum = self._enumerate()
        detail = ", ".join(f"{k}: {len(v)}" for k, v in enum.found.items()) or "nothing listed"
        notes = [
            f"{self.provider.id}: remote deletions are NOT restorable "
            "(the snapshot records ids only)",
            *enum.notes,
        ]
        if not enum.complete:
            notes.append(
                f"{self.provider.id}: this listing is INCOMPLETE -- a wipe would "
                "not cover everything, and cannot be verified"
            )
        return WipePlan(
            scope="remote",
            description=f"[{self.provider.id}] delete {enum.total} object(s) ({detail})",
            item_count=enum.total,
            reversible=False,
            complete=enum.complete,
            item_ids=tuple(enum.all_ids),
            notes=tuple(notes),
        )

    def commit(self) -> WipePlan:
        enum = self._enumerate()
        notes = list(enum.notes)
        deleted: list[str] = []
        for name, ids in enum.found.items():
            surface = self.provider.surfaces[name]
            for obj_id in ids:
                # the id came from a remote response: encode it, never let it
                # reshape the URL path
                safe_id = quote(obj_id, safe="")
                url = f"{self.provider.base_url}{surface.delete_path.format(id=safe_id)}"
                try:
                    status, _payload = http_json("DELETE", url, self._headers())
                except (OSError, ValueError) as exc:  # network died mid-run
                    notes.append(f"{self.provider.id}/{name}: delete {obj_id} failed: {exc}")
                    continue
                if status in (200, 202, 204):
                    deleted.append(f"{name}:{obj_id}")
                else:
                    notes.append(f"{self.provider.id}/{name}: delete {obj_id} -> HTTP {status}")
        failed = enum.total - len(deleted)
        if failed:
            notes.append(f"{self.provider.id}: {failed} object(s) could NOT be deleted")
        return WipePlan(
            scope="remote",
            description=f"[{self.provider.id}] deleted {len(deleted)} of {enum.total} object(s)",
            item_count=len(deleted),
            reversible=False,
            complete=enum.complete and not failed,
            item_ids=tuple(deleted),
            notes=tuple(notes),
        )

    def verify(self) -> bool:
        """True only if we could look AND nothing is left.

        An enumeration that failed must never read as "clean" -- that would
        turn a broken wipe into a green report.
        """
        enum = self._enumerate()
        return enum.complete and enum.total == 0
