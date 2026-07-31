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


# -- hardening: every case below is a defect an adversarial review found ----


def test_verify_is_false_when_enumeration_failed(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    """'We could not look' must never render as 'it is clean'."""
    monkeypatch.setattr(remote_mod, "http_json", lambda *a, **k: (500, {"error": "boom"}))
    wiper = RemoteWiper(PROVIDERS["openai"], ["files"])
    assert not wiper.verify(), "a failed listing reported the wipe as verified"
    plan = wiper.plan()
    assert plan.complete is False
    assert any("INCOMPLETE" in n for n in plan.notes)


def test_verify_false_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not RemoteWiper(PROVIDERS["openai"], ["files"]).verify()


def test_plan_records_the_actual_ids(monkeypatch: pytest.MonkeyPatch, openai_key: None) -> None:
    """The ids ARE the restore point: the delete cannot be undone."""
    api = FakeApi({"files": ["file-a", "file-b"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    assert set(plan.item_ids) == {"files:file-a", "files:file-b"}


def test_snapshot_manifest_carries_the_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    from neurailyzer.snapshots import SnapshotStore

    api = FakeApi({"files": ["file-keepsake"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    cfg = load(_cfg(tmp_path, '[remote.openai]\nsurfaces = ["files"]'))
    report = core.execute_wipe(cfg, ["remote"])
    assert report.snapshot is not None
    stored = SnapshotStore(cfg.snapshot_dir).list()[-1]
    assert stored.remote_manifest is not None
    assert "files:file-keepsake" in stored.remote_manifest["openai"]["item_ids"]


def test_pagination_that_never_advances_is_reported_not_looped(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    """A provider that ignores the cursor must not spin 100 times."""
    calls = 0

    def _stuck(_m: str, _u: str, _h: dict[str, str]) -> tuple[int, dict[str, Any]]:
        nonlocal calls
        calls += 1
        return 200, {"data": [{"id": "same-id"}], "has_more": True}

    monkeypatch.setattr(remote_mod, "http_json", _stuck)
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    assert calls == 2, f"cursor stall not detected (made {calls} requests)"
    assert plan.complete is False
    assert any("pagination stopped" in n for n in plan.notes)


def test_exceeding_the_page_cap_refuses_rather_than_truncates(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    """Silently wiping a partially-enumerated surface is the worst outcome."""
    counter = {"n": 0}

    def _endless(_m: str, _u: str, _h: dict[str, str]) -> tuple[int, dict[str, Any]]:
        counter["n"] += 1
        return 200, {"data": [{"id": f"id-{counter['n']}"}], "has_more": True}

    monkeypatch.setattr(remote_mod, "http_json", _endless)
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    assert plan.complete is False
    assert any("refusing to wipe a partially-enumerated" in n for n in plan.notes)


def test_token_with_trailing_newline_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`export K=$(cat key.txt)` is common; a raw newline in a header raises
    deep in http.client and the traceback carries the key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key\n")
    api = FakeApi({"files": []})
    monkeypatch.setattr(remote_mod, "http_json", api)
    RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    _m, _u, headers = api.calls[0]
    assert headers["Authorization"] == "Bearer sk-real-key"
    assert "\n" not in headers["Authorization"]


def test_whitespace_only_token_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()
    assert any("not set" in n for n in plan.notes)


def test_hostile_id_cannot_reshape_the_delete_url(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    """The id comes from the remote response -- it must not choose the path."""
    api = FakeApi({"files": ["../vector_stores/vs_production"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    RemoteWiper(PROVIDERS["openai"], ["files"]).commit()
    deletes = [u for m, u, _h in api.calls if m == "DELETE"]
    assert deletes, "no delete was attempted"
    assert all("/v1/vector_stores/" not in u for u in deletes), deletes
    assert all("%2F" in u or "/v1/files/" in u for u in deletes)


def test_network_error_mid_delete_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    state = {"n": 0}
    objects = ["file-a", "file-b", "file-c"]

    def _flaky(method: str, url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            return 200, {"data": [{"id": i} for i in objects], "has_more": False}
        state["n"] += 1
        if state["n"] == 2:
            raise OSError("connection reset")
        return 200, {"deleted": True}

    monkeypatch.setattr(remote_mod, "http_json", _flaky)
    result = RemoteWiper(PROVIDERS["openai"], ["files"]).commit()
    assert result.item_count == 2  # the other two still went through
    assert any("connection reset" in n for n in result.notes)
    assert any("could NOT be deleted" in n for n in result.notes)
    assert result.complete is False


def test_non_json_success_body_is_a_note_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    monkeypatch.setattr(
        remote_mod,
        "http_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("body was not JSON")),
    )
    plan = RemoteWiper(PROVIDERS["openai"], ["files"]).plan()  # must not raise
    assert plan.complete is False


def test_list_state_does_not_claim_zero_for_remote(tmp_path: Path) -> None:
    """Reporting 0 would read as 'nothing to lose' for an irreversible scope."""
    cfg = load(_cfg(tmp_path, '[remote.openai]\nsurfaces = ["files"]'))
    st = core.scope_status(cfg, "remote")
    assert not st.counted
    assert st.file_count == -1


def test_restore_reports_the_remote_ids_it_cannot_bring_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai_key: None
) -> None:
    """The taxonomy promises restore reports what was removed remotely.

    Those ids are all a snapshot can offer for an irreversible delete, so
    surfacing them is the entire reason it records them.
    """
    from typer.testing import CliRunner

    from neurailyzer.cli import app

    api = FakeApi({"files": ["file-gone-forever"]})
    monkeypatch.setattr(remote_mod, "http_json", api)
    cfg = _cfg(tmp_path, '[remote.openai]\nsurfaces = ["files"]')
    conf = load(cfg)
    report = core.execute_wipe(conf, ["remote"])
    assert report.snapshot is not None

    result = CliRunner().invoke(app, ["--config", str(cfg), "restore", "--to", report.snapshot.id])
    assert result.exit_code == 0, result.output
    assert "CANNOT be restored" in result.stdout
    assert "file-gone-forever" in result.stdout
