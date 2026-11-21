"""The browser HTTP surface controls API intake only; agent intake stays local."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from kg_pilot import ingestion as ing, ingestion_publish, ingestion_runners
from web import server


@pytest.fixture
def isolated_web(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    monkeypatch.setattr(server, "provider_status", lambda: {"configured": False})
    monkeypatch.setattr(ingestion_runners, "provider_status", lambda: {"configured": False})
    monkeypatch.setattr(ingestion_runners, "codex_status", lambda: pytest.fail("Web must not probe agent availability"))
    with TestClient(server.app) as client:
        yield tmp_path, client


def checkpoint(pilot, mode, *, state="queued", suffix="a"):
    batch_id = "b_" + suffix * 20
    path = pilot / "ingestion/batches" / batch_id / "batch.json"
    path.parent.mkdir(parents=True)
    value = {"id": batch_id, "status": state, "stage": state, "documents": [], "events": []}
    if mode is not None:
        value["mode"] = mode
    path.write_text(json.dumps(value), encoding="utf-8")
    return batch_id


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_status_and_schema_advertise_only_api_modes(isolated_web):
    _, client = isolated_web
    response = client.get("/api/ingestion/status")
    assert response.status_code == 200
    assert "codex" not in response.json()
    schema = client.get("/openapi.json").json()["components"]["schemas"]["IntakeCreate"]["properties"]["mode"]
    assert schema["default"] == "api" and schema["enum"] == ["api", "paratera"]


@pytest.mark.parametrize("mode", [None, "api", "paratera"])
def test_create_uses_api_default_and_preserves_api_aliases(isolated_web, monkeypatch, mode):
    pilot, client = isolated_web
    received = []

    def create(files, execution_mode, max_claims, auto_publish, destination):
        received.append((files, execution_mode, max_claims, auto_publish, destination))
        return {"id": "b_" + "a" * 20, "mode": execution_mode, "status": "queued"}

    monkeypatch.setattr(ing, "create_batch", create)
    payload = {"files": [{"name": "fixture.pdf", "content_base64": base64.b64encode(b"%PDF-fixture").decode()}]}
    if mode is not None:
        payload["mode"] = mode
    response = client.post("/api/ingestion/batches", json=payload)
    assert response.status_code == 200
    assert received == [([("fixture.pdf", b"%PDF-fixture")], mode or "api", 8, True, pilot)]


def test_create_rejects_agent_mode_before_any_domain_call(isolated_web, monkeypatch):
    pilot, client = isolated_web
    monkeypatch.setattr(ing, "create_batch", lambda *args, **kwargs: pytest.fail("Agent creation reached domain"))
    before = snapshot(pilot)
    response = client.post("/api/ingestion/batches", json={"mode": "codex", "files": [
        {"name": "fixture.pdf", "content_base64": "JVBERi1maXh0dXJl"}]})
    assert response.status_code == 422
    assert snapshot(pilot) == before


@pytest.mark.parametrize("mode", ["codex", "unknown", None])
@pytest.mark.parametrize("state", ["queued", "running", "ready", "completed"])
def test_agent_or_ambiguous_batches_have_no_http_details_or_controls(isolated_web, monkeypatch, mode, state):
    pilot, client = isolated_web
    batch_id = checkpoint(pilot, mode, state=state)
    for name in ("get_batch", "start_batch", "cancel_batch", "skip_document"):
        monkeypatch.setattr(ing, name, lambda *args, **kwargs: pytest.fail("Non-API batch reached domain control"))
    before = snapshot(pilot)
    assert client.get("/api/ingestion/batches").json() == {"batches": []}
    assert client.get(f"/api/ingestion/batches/{batch_id}").status_code == 404
    for action in ("run", "publish", "cancel", "documents/item_000/skip"):
        assert client.post(f"/api/ingestion/batches/{batch_id}/{action}").status_code == 404
    assert snapshot(pilot) == before


@pytest.mark.parametrize("mode", ["api", "paratera"])
def test_api_history_details_and_actions_remain_available(isolated_web, monkeypatch, mode):
    pilot, client = isolated_web
    batch_id = checkpoint(pilot, mode, state="ready")
    checkpoint(pilot, "codex", suffix="b")
    calls = []

    def control(name):
        def invoke(*args, **kwargs):
            calls.append((name, args, kwargs))
            return {"id": batch_id, "mode": mode, "status": "ready"}
        return invoke

    for name in ("start_batch", "cancel_batch", "skip_document"):
        monkeypatch.setattr(ing, name, control(name))
    listing = client.get("/api/ingestion/batches")
    assert listing.status_code == 200 and [item["id"] for item in listing.json()["batches"]] == [batch_id]
    assert not (pilot / "ingestion/batches" / ("b_" + "b" * 20) / "worker.lock").exists()
    assert client.get(f"/api/ingestion/batches/{batch_id}").json()["mode"] == mode
    for action in ("run", "publish", "cancel", "documents/item_000/skip"):
        assert client.post(f"/api/ingestion/batches/{batch_id}/{action}").status_code == 200
    assert calls == [
        ("start_batch", (batch_id, pilot), {}),
        ("start_batch", (batch_id, pilot), {"publish_only": True}),
        ("cancel_batch", (batch_id, pilot), {}),
        ("skip_document", (batch_id, "item_000", pilot), {}),
    ]


def interrupted_commit(pilot, owner, *, state="publishing", marker_owner=None):
    ing.write_atomic(pilot / ingestion_publish.JOURNAL, {
        "batch_id": owner, "status": state,
        "backup_relative": "ingestion/backups/" + owner, "targets": [],
    })
    ing.write_atomic(pilot / ingestion_publish.MAINTENANCE,
                     {"active": True, "batch_id": marker_owner or owner})
    lock = pilot / ingestion_publish.LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"0")


@pytest.mark.parametrize("mode", ["api", "paratera"])
def test_api_dataset_recovery_remains_explicit_and_owner_scoped(isolated_web, monkeypatch, mode):
    pilot, client = isolated_web
    owner = checkpoint(pilot, mode, state="ready")
    interrupted_commit(pilot, owner)
    recover = ingestion_publish.recover_publication
    received = []

    def scoped_recover(target, **kwargs):
        received.append((target, kwargs))
        return recover(target, **kwargs)

    monkeypatch.setattr(ingestion_publish, "recover_publication", scoped_recover)
    status = client.get("/api/ingestion/status").json()
    assert status["maintenance"] and status["recovery_available"]
    assert not received
    response = client.post("/api/ingestion/recover")
    assert response.status_code == 200 and response.json()["status"] == "rolled_back"
    assert received == [(pilot, {"expected_batch_id": owner})]
    assert not (pilot / ingestion_publish.MAINTENANCE).exists()


@pytest.mark.parametrize("prefix", ["table-backfill-", "text-quality-"])
def test_shared_backfill_recovery_remains_available(isolated_web, prefix):
    pilot, client = isolated_web
    owner = prefix + "a" * 32
    interrupted_commit(pilot, owner)
    assert client.get("/api/ingestion/status").json()["recovery_available"]
    response = client.post("/api/ingestion/recover")
    assert response.status_code == 200 and response.json()["batch_id"] == owner
    assert response.json()["status"] == "rolled_back"


@pytest.mark.parametrize("mode", ["codex", "unknown", None, "missing"])
@pytest.mark.parametrize("state", ["publishing", "recovery_failed", "committed"])
def test_agent_or_ambiguous_maintenance_has_no_web_recovery(isolated_web, monkeypatch, mode, state):
    pilot, client = isolated_web
    owner = "b_" + "a" * 20 if mode == "missing" else checkpoint(pilot, mode)
    interrupted_commit(pilot, owner, state=state)
    monkeypatch.setattr(ingestion_publish, "recover_publication",
                        lambda *args, **kwargs: pytest.fail("Non-API recovery reached domain mutation"))
    before = snapshot(pilot)
    status = client.get("/api/ingestion/status").json()
    assert status["maintenance"] and not status["recovery_available"]
    assert owner not in json.dumps(status)
    assert client.post("/api/ingestion/recover").status_code == 409
    assert snapshot(pilot) == before


def test_api_journal_with_agent_maintenance_marker_is_not_recoverable(isolated_web, monkeypatch):
    pilot, client = isolated_web
    owner = checkpoint(pilot, "api")
    agent = checkpoint(pilot, "codex", suffix="b")
    interrupted_commit(pilot, owner, marker_owner=agent)
    monkeypatch.setattr(ingestion_publish, "recover_publication",
                        lambda *args, **kwargs: pytest.fail("Mismatched recovery reached mutation"))
    before = snapshot(pilot)
    assert not client.get("/api/ingestion/status").json()["recovery_available"]
    assert client.post("/api/ingestion/recover").status_code == 409
    assert snapshot(pilot) == before


@pytest.mark.parametrize("switch", ["journal_and_marker", "marker"])
def test_recovery_owner_is_rechecked_under_dataset_lock(isolated_web, monkeypatch, switch):
    pilot, client = isolated_web
    owner = checkpoint(pilot, "api")
    agent = checkpoint(pilot, "codex", suffix="b")
    interrupted_commit(pilot, owner)
    recover = ingestion_publish.recover_publication
    changed = []

    def switch_owner_before_lock(target, **kwargs):
        assert kwargs == {"expected_batch_id": owner}
        if switch == "journal_and_marker":
            interrupted_commit(pilot, agent)
        else:
            ing.write_atomic(pilot / ingestion_publish.MAINTENANCE, {"active": True, "batch_id": agent})
        changed.append(snapshot(pilot))
        return recover(target, **kwargs)

    monkeypatch.setattr(ingestion_publish, "recover_publication", switch_owner_before_lock)
    assert client.post("/api/ingestion/recover").status_code == 409
    assert changed and snapshot(pilot) == changed[0]


def test_idle_recovery_cannot_pick_up_a_new_unchecked_journal(isolated_web, monkeypatch):
    pilot, client = isolated_web
    monkeypatch.setattr(ingestion_publish, "recover_publication",
                        lambda *args, **kwargs: pytest.fail("Idle Web recovery must not enter a global recovery"))
    before = snapshot(pilot)
    assert client.post("/api/ingestion/recover").json() == {"status": "idle"}
    assert snapshot(pilot) == before
