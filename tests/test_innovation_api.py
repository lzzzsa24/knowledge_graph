"""Local innovation endpoint contracts. Generation is mocked; no model API is used."""
import pytest
from fastapi.testclient import TestClient
from web import server


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake_generate(prompt="", history=None, count=3):
        calls.append({"prompt": prompt, "history": history, "count": count})
        return {"mode": "guided" if prompt or history else "random", "focus": prompt,
                "ideas": [], "sources": [], "warnings": [], "generated": True,
                "model": "mock-only", "provider": "mock-only"}

    monkeypatch.setattr(server, "generate_ideas", fake_generate)
    return TestClient(server.app), calls


def test_empty_request_starts_random_exploration(api):
    client, calls = api
    result = client.post("/api/ideas", json={})
    assert result.status_code == 200
    assert result.json()["mode"] == "random"
    assert calls == [{"prompt": "", "history": [], "count": 3}]


def test_direction_and_conversation_are_forwarded(api):
    client, calls = api
    history = [{"role": "user", "content": "只有等离子处理和粗糙度测量条件。"},
               {"role": "assistant", "content": "可以先讨论表面活化。"}]
    result = client.post("/api/ideas", json={"prompt": "低温混合键合", "history": history, "count": 2})
    assert result.status_code == 200
    assert calls == [{"prompt": "低温混合键合", "history": history, "count": 2}]


@pytest.mark.parametrize("body", [
    {"prompt": "x" * 6001}, {"count": 0}, {"count": 6}, {"count": True}, {"count": 1.5},
    {"history": [{"role": "system", "content": "untrusted"}]},
    {"history": [{"role": "user", "content": "x"}] * 13},
])
def test_invalid_requests_do_not_generate(api, body):
    client, calls = api
    result = client.post("/api/ideas", json=body)
    assert result.status_code == 422
    assert not calls
    assert "untrusted" not in result.text


def test_external_site_cannot_trigger_a_paid_generation(api):
    client, calls = api
    result = client.post("/api/ideas", json={}, headers={"Origin": "https://other.invalid"})
    assert result.status_code == 403
    assert not calls


def test_remote_client_cannot_trigger_generation(api):
    _, calls = api
    with TestClient(server.app, client=("203.0.113.1", 1234)) as client:
        assert client.post("/api/ideas", json={}).status_code == 403
    assert not calls


def test_safe_provider_failure_is_reported(api, monkeypatch):
    client, _ = api

    def unavailable(*args, **kwargs):
        raise server.QAError("并行平台响应超时，请稍后重试。")

    monkeypatch.setattr(server, "generate_ideas", unavailable)
    result = client.post("/api/ideas", json={})
    assert result.status_code == 503
    assert result.json()["detail"] == "并行平台响应超时，请稍后重试。"


def test_failure_exposes_only_safe_diagnostic_identifier(api, monkeypatch):
    client, _ = api
    run_id = "a" * 32

    def unavailable(*args, **kwargs):
        error = server.QAError("模型输出需要重试。")
        error.run_id = run_id
        raise error

    monkeypatch.setattr(server, "generate_ideas", unavailable)
    result = client.post("/api/ideas", json={})
    assert result.status_code == 503
    assert result.json() == {"detail": "模型输出需要重试。", "run_id": run_id}


def test_preview_uses_separate_no_generation_entrypoint(api, monkeypatch):
    client, calls = api
    received = []

    def preview(prompt, history, count):
        received.append((prompt, history, count))
        return {"mode": "guided", "focus": prompt, "sources": [], "contexts": [],
                "context_chars": 0, "count": count, "provider_called": False}

    monkeypatch.setattr(server, "preview_ideas", preview)
    response = client.post("/api/debug/ideas/preview", json={"prompt": "低温键合", "count": 2})
    assert response.status_code == 200 and response.json()["provider_called"] is False
    assert received == [("低温键合", [], 2)] and not calls


@pytest.mark.parametrize("path", ["/api/debug/health", "/api/debug/ideas/runs"])
def test_debug_status_is_local_only_and_stays_available_during_publication(api, monkeypatch, path):
    client, calls = api
    from kg_pilot import ingestion_publish
    monkeypatch.setattr(ingestion_publish, "maintenance_status", lambda *args: {"active": True})
    response = client.get(path)
    assert response.status_code == 200 and not calls
    assert client.get(path, headers={"Origin": "https://other.invalid"}).status_code == 403
    with TestClient(server.app, client=("203.0.113.1", 1234)) as remote:
        assert remote.get(path).status_code == 403


def test_debug_preview_rejects_cross_site_input(api, monkeypatch):
    client, calls = api
    monkeypatch.setattr(server, "preview_ideas", lambda *args: pytest.fail("Cross-site preview was executed"))
    assert client.post("/api/debug/ideas/preview", json={}, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert not calls
