"""The subscription API advertises capabilities without calling another CLI."""

import pytest
from fastapi.testclient import TestClient

from kg_pilot import codex_subscription, subscription
from web import native, server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(subscription.shutil, "which", lambda name: "codex.exe" if name == "codex" else None)
    monkeypatch.setattr(codex_subscription, "run_subscription", lambda *_: pytest.fail("Unexpected model call"))
    monkeypatch.setattr(codex_subscription, "subscription_windows", lambda *_: pytest.fail("Unexpected quota probe"))
    monkeypatch.setattr(native, "queue_command", lambda *_: pytest.fail("Unexpected queue command"))
    return TestClient(server.app)


def test_chat_status_distinguishes_installed_cli_from_authentication(client, monkeypatch):
    url = "/api/chat/status?channel=subscription"
    status = client.get(url).json()
    assert (status["provider_id"], status["model"], status["effort"]) == ("codex", codex_subscription.SOL_MODEL, "high")
    assert status["configured"] and status["available"] and status["auth_status"] == "unknown"
    assert status["capabilities"] == {"text": True, "images": True, "quota": True}
    options = {row["provider_id"]: row for row in status["providers"]}
    assert set(options) == {"codex", "claude_code", "opencode", "gemini"}
    for key in ("claude_code", "opencode", "gemini"):
        assert not options[key]["implemented"] and not options[key]["available"]
        assert not any(options[key]["capabilities"].values())

    monkeypatch.setattr(subscription.shutil, "which", lambda _: None)
    missing = client.get(url).json()
    assert not missing["configured"] and not missing["available"]
    assert missing["auth_status"] == "not_installed" and missing["quota"]["status"] == "unavailable"


@pytest.mark.parametrize("scope", ["chat", "native"])
def test_configuration_validates_the_fixed_codex_profile(client, monkeypatch, tmp_path, scope):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    url = f"/api/{scope}/subscription/configure"
    assert client.post(url, json={"provider_id": "codex"}, headers={"origin": "https://other.example"}).status_code == 403
    assert client.post(url, json={"provider_id": "codex", "command": "some-cli"}).status_code == 422
    assert client.post(url, json={"provider_id": "other"}).status_code == 422
    assert client.post(url, json={"provider_id": "opencode"}).status_code == 409
    assert client.post(url, json={"provider_id": "codex", "effort": "low"}).status_code == 422

    accepted = client.post(url, json={"provider_id": "codex", "model": codex_subscription.SOL_MODEL, "effort": "high"})
    assert accepted.status_code == 200 and accepted.json()["provider_id"] == "codex"
    assert not list(tmp_path.iterdir())

    monkeypatch.setattr(subscription.shutil, "which", lambda _: None)
    assert client.post(url, json={"provider_id": "codex"}).status_code == 503


def test_native_status_reads_an_existing_reset_without_probing_the_cli(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "PILOT", tmp_path)
    lib = native.native_library
    root = lib.directory(tmp_path)
    process = native.psutil.Process()
    lib.save(root / "queue.json", {"documents": []})
    lib.save(root / "manifest.json", {"generation": 3, "documents": {}})
    lib.save(root / "scheduler_process.json", {"pid": process.pid, "created": process.create_time()})
    lib.save(root / "scheduler.json", {"status": "waiting_for_subscription_reset", "reset_at_unix": 2000000000})

    result = client.get("/api/native/status").json()
    assert result["generation"] == 3
    assert result["phase"] == "waiting_for_subscription_reset"
    assert result["provider_id"] == result["next_subscription"]["provider_id"] == "codex"
    assert result["cli_available"] and result["available"]
    assert result["quota"] == {"status": "waiting_for_reset", "reset_at": 2000000000}
