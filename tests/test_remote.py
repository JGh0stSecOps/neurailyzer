"""Remote provider wipers: enumerate, delete, verify -- against a fake API.

No network and no real keys: ``http_json`` is monkeypatched, which is also the
seam a contributor uses when adding a provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from neurailyzer import core
from neurailyzer.config import ConfigError, load
from neurailyzer.wipers import remote as remote_mod
from neurailyzer.wipers.remote import PROVIDERS, RemoteWiper


class FakeApi:
    """A tiny stand-in for a provider: holds objects, records deletes."""

    def __init__(self, objects: dict[str, list[str]], page_size: int = 100) -> None:
        self.objects = {k: list(v) for k, v in objects.items()}
        self.page_size = page_size
        self.deleted: list[str] = []
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.delete_status = 200

    def __call__(
        self, method: str, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, url, headers))
        kind = next((k for k in self.objects if k in url), None)
        if kind is None:
            return 404, {}
        if method == "GET":
            items = self.objects[kind]
            after = None
            if "after" in url:
                after = (
                    url.split("after_id=")[-1] if "after_id=" in url else url.split("after=")[-1]
                )
            start = items.index(after) + 1 if after in items else 0
            page = items[start : start + self.page_size]
            return 200, {
                "data": [{"id": i} for i in page],
                "has_more": start + self.page_size < len(items),
            }
        if method == "DELETE":
            obj_id = url.rstrip("/").split("/")[-1].split("=")[-1]
            if self.delete_status in (200, 202, 204):
                self.objects[kind] = [i for i in self.objects[kind] if i != obj_id]
                self.deleted.append(obj_id)
            return self.delete_status, {"deleted": True}
        return 405, {}


@pytest.fixture
def openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")


def test_plan_enumerates_without_deleting(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    api = FakeApi({"files": ["file-a", "file-b", "file-c"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    assert plan.item_count == 3
    assert not api.deleted
    assert plan.reversible is False  # remote deletes are one-way
    assert any("NOT restorable" in n for n in plan.notes)


def test_commit_deletes_every_listed_object(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    api = FakeApi({"files": ["file-a", "file-b"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    wiper = RemoteWiper(PROVIDERS["openai"], ["files"])
    result = wiper.commit()
    assert result.item_count == 2
    assert sorted(api.deleted) == ["file-a", "file-b"]
    assert wiper.verify()


def test_pagination_walks_every_page(monkeypatch: pytest.MonkeyPatch, openai_key: None) -> None:
    api = FakeApi({"files": [f"file-{i:03d}" for i in range(250)]}, page_size=100)
    monkeypatch.setattr(remote_mod, "http_json", api)
    assert RemoteWiper(PROVIDERS["openai"], ["files"]).plan().item_count == 250


def test_missing_token_is_a_reported_skip_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api = FakeApi({"files": ["file-a"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    assert plan.item_count == 0
    assert any("OPENAI_API_KEY not set" in n for n in plan.notes)
    assert not api.calls  # no request attempted without a token


def test_token_never_appears_in_plan_output(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    api = FakeApi({"files": ["file-a"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    blob = plan.description + " ".join(plan.notes)
    assert "sk-test-not-a-real-key" not in blob


def test_failed_delete_is_reported(monkeypatch: pytest.MonkeyPatch, openai_key: None) -> None:
    api = FakeApi({"files": ["file-a"]})
    api.delete_status = 409
    monkeypatch.setattr(remote_mod, "http_json", api)
    wiper = RemoteWiper(PROVIDERS["openai"], ["files"])
    result = wiper.commit()
    assert result.item_count == 0
    assert any("HTTP 409" in n for n in result.notes)
    assert not wiper.verify()  # the object is still there -- verify says so


def test_list_error_becomes_a_note(monkeypatch: pytest.MonkeyPatch, openai_key: None) -> None:
    monkeypatch.setattr(remote_mod, "http_json", lambda *a, **k: (500, {"error": "boom"}))
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    assert plan.item_count == 0
    assert any("HTTP 500" in n for n in plan.notes)


def test_anthropic_uses_x_api_key_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    api = FakeApi({"files": ["file-1"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    RemoteWiper(PROVIDERS["anthropic"], ["files"]).plan()
    _method, _url, headers = api.calls[0]
    assert headers["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in headers
    assert headers["anthropic-version"] == "2023-06-01"


def test_venice_has_no_wipe_surfaces() -> None:
    # Venice's privacy design means there is nothing server-side to wipe;
    # the registry says so instead of inventing an endpoint.
    assert PROVIDERS["venice"].surfaces == {}


# -- config wiring ----------------------------------------------------------


def _cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(body + f'\n[snapshots]\ndir = "{(tmp_path / "snaps").as_posix()}"\n')
    return p


def test_config_accepts_known_provider_and_surface(tmp_path: Path) -> None:
    cfg = load(_cfg(tmp_path, '[remote.openai]\nsurfaces = ["files", "vector_stores"]'))
    assert cfg.remote == {"openai": ("files", "vector_stores")}


def test_config_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a known provider"):
        load(_cfg(tmp_path, '[remote.acme]\nsurfaces = ["files"]'))


def test_config_rejects_unknown_surface(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown surface"):
        load(_cfg(tmp_path, '[remote.openai]\nsurfaces = ["threads"]'))


def test_config_rejects_empty_surfaces(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="explicit opt-in"):
        load(_cfg(tmp_path, "[remote.openai]\nsurfaces = []"))


def test_config_rejects_remote_as_path_target(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a path scope"):
        load(_cfg(tmp_path, '[targets.remote]\npaths = ["/tmp/x"]'))


def test_core_builds_remote_wiper(tmp_path: Path, openai_key: None) -> None:
    cfg = load(_cfg(tmp_path, '[remote.openai]\nsurfaces = ["files"]'))
    wipers = core.build_wipers(cfg, ["remote"])
    assert len(wipers) == 1
    assert isinstance(wipers[0], RemoteWiper)


def test_scope_all_includes_remote() -> None:
    assert "remote" in core.expand_scopes(["all"])


def test_remote_status_lists_providers(tmp_path: Path) -> None:
    cfg = load(_cfg(tmp_path, '[remote.openai]\nsurfaces = ["files"]'))
    st = core.scope_status(cfg, "remote")
    assert st.configured and st.available
    assert st.roots == ("openai: files",)
