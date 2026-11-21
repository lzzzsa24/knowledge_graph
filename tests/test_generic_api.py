"""Offline API-connection regressions; all keys, providers and replies are fake.

These verify connection routing and credential boundaries, not model quality.
Both model workflows use temporary configuration and reversible fake encryption.
Any unexpected outbound HTTP or subprocess execution fails the test immediately.
"""
from __future__ import annotations

import base64
import json
import shutil
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kg_pilot import ingestion_runners as runners
from kg_pilot import qa
from web import server

FAKE_KEY = "generic-test-NOT-A-REAL-KEY"
OTHER_KEY = "second-generic-test-NOT-A-REAL-KEY"
MODEL = "vendor-arbitrary-chat-model"
OTHER_MODEL = "independent-ingestion-chat-model"
BASE_URL = "https://compatible.invalid/gateway/v1"
OTHER_URL = "https://second-compatible.invalid/v1"
LOCAL_ORIGIN = "http://127.0.0.1:8147"


@pytest.fixture
def isolated_paths():
    # Normal inherited ACLs avoid pytest 9's inaccessible 0o700 temp roots on
    # this managed Windows workspace. Remove only this fixture's UUID folder.
    parent = (Path(__file__).resolve().parents[1] / ".cache").resolve()
    parent.mkdir(exist_ok=True)
    directory = parent / ("generic-api-test-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        assert directory.resolve().parent == parent
        shutil.rmtree(directory)


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch, isolated_paths):
    for name in ("PARATERA_API_KEY", "PARATERA_MODEL", "KG_LLM_API_KEY", "KG_LLM_MODEL",
                 "KG_LLM_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                 "KG_INGEST_PARATERA_API_KEY", "KG_INGEST_PARATERA_MODEL",
                 "KG_INGEST_LLM_API_KEY", "KG_INGEST_LLM_BASE_URL", "KG_INGEST_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for module, folder in ((qa, "qa"), (runners, "ingestion")):
        root = isolated_paths / folder
        root.mkdir()
        monkeypatch.setattr(module, "CONFIG_PATH", root / "llm_config.json")
        monkeypatch.setattr(module, "SECRET_PATH", root / "secret.dpapi")

    def fake_protect(data, *, decrypt=False):
        if decrypt:
            assert data.startswith(b"FAKE-ENCRYPTED:")
            return base64.b64decode(data.removeprefix(b"FAKE-ENCRYPTED:"))
        return b"FAKE-ENCRYPTED:" + base64.b64encode(data)

    monkeypatch.setattr(qa, "protect_secret", fake_protect)
    monkeypatch.setattr(runners, "protect_secret", fake_protect)
    monkeypatch.setattr(qa, "_format_event", lambda **kwargs: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Every provider request or subprocess must be explicitly mocked")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(runners.subprocess, "run", forbidden)


@pytest.fixture(params=[qa, runners], ids=["qa", "ingestion"])
def workflow(request):
    return request.param


def completion(text="Synthetic connection probe passed."):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


def fake_endpoint(*, base_url=BASE_URL, key=FAKE_KEY, models=None, discovery_status=200, calls=None):
    models = [MODEL, OTHER_MODEL] if models is None else models
    calls = [] if calls is None else calls

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + key
        if request.method == "GET":
            assert str(request.url) == base_url + "/models"
            return httpx.Response(discovery_status, json={"data": [{"id": name} for name in models]})
        assert request.method == "POST" and str(request.url) == base_url + "/chat/completions"
        body = json.loads(request.content)
        assert body["model"] in (MODEL, OTHER_MODEL, "Qwen3-32B", "DeepSeek-V3.2")
        return httpx.Response(200, json=completion())

    return httpx.MockTransport(handler)


def configure_generic(workflow, *, key=FAKE_KEY, model=MODEL, base_url=BASE_URL, transport=None):
    return workflow.configure_provider(key, model, provider="openai_compatible", base_url=base_url,
                                       transport=transport or fake_endpoint(base_url=base_url, key=key))


def test_generic_model_and_prefixed_endpoint_are_saved_without_secret_leak(workflow, capsys, caplog):
    calls = []
    status = configure_generic(workflow, base_url=BASE_URL + "/",
                               transport=fake_endpoint(calls=calls))
    assert status["configured"] and status["model"] == MODEL
    assert status["provider_id"] == "openai_compatible" and status["base_url"] == BASE_URL
    assert any(request.method == "POST" for request in calls), "A generic connection must validate chat access"
    saved = workflow.CONFIG_PATH.read_text(encoding="utf-8")
    public = workflow.provider_status()
    assert public["configured"] and public["base_url"] == BASE_URL
    assert public["provider_id"] == "openai_compatible"
    captured = capsys.readouterr()
    assert FAKE_KEY not in saved + json.dumps(status) + json.dumps(public) + captured.out + captured.err + caplog.text
    encrypted = base64.b64decode(json.loads(saved)["encrypted_credential"])
    assert encrypted and FAKE_KEY.encode() not in encrypted
    assert workflow._api_key() == FAKE_KEY


def test_handwritten_model_does_not_require_a_models_discovery_endpoint(workflow):
    calls = []
    status = configure_generic(workflow, transport=fake_endpoint(discovery_status=404, calls=calls))
    assert status["configured"] and status["model"] == MODEL
    assert [request.method for request in calls] == ["POST"]


def test_blank_generic_model_can_be_selected_from_models_discovery(workflow):
    calls = []
    status = configure_generic(workflow, model="", transport=fake_endpoint(models=[MODEL], calls=calls))
    assert status["configured"] and status["model"] == MODEL
    assert [request.method for request in calls] == ["GET", "POST"]


def test_generic_probe_sends_only_basic_openai_chat_fields(workflow):
    calls = []
    status = configure_generic(workflow, transport=fake_endpoint(models=[OTHER_MODEL], calls=calls))
    assert status["model"] == MODEL
    payload = json.loads(calls[-1].content)
    assert payload["model"] == MODEL
    assert set(payload) <= {"model", "messages", "stream"}
    assert payload["messages"] and payload.get("stream") is not True


@pytest.mark.parametrize("failure", [401, 429, 500, "empty", "redirect"])
def test_failed_generic_chat_probe_never_saves_credentials(workflow, failure):
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url).startswith(BASE_URL + "/")
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        if failure == "empty":
            return httpx.Response(200, json=completion(""))
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": OTHER_URL + "/chat/completions"})
        return httpx.Response(failure, json={"error": FAKE_KEY})

    with pytest.raises((qa.QAError, runners.RunnerError)) as error:
        configure_generic(workflow, transport=httpx.MockTransport(handler))
    assert FAKE_KEY not in str(error.value)
    assert not workflow.CONFIG_PATH.exists() and not workflow.SECRET_PATH.exists()
    assert len(calls) == 1, "Probe failures and redirects must not retry or forward credentials"


@pytest.mark.parametrize("base_url", [
    "http://compatible.invalid/v1", "https://user:password@compatible.invalid/v1",
    "https://compatible.invalid/v1?api_key=hidden", "https://compatible.invalid/v1#fragment",
    "ftp://compatible.invalid/v1",
])
def test_invalid_or_nonlocal_plain_http_addresses_fail_before_encryption_or_http(workflow, base_url, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid endpoint must be rejected before reading or encrypting a key or making HTTP requests")

    monkeypatch.setattr(workflow, "protect_secret", forbidden)
    with pytest.raises((qa.QAError, runners.RunnerError)):
        configure_generic(workflow, base_url=base_url, transport=httpx.MockTransport(forbidden))
    assert not workflow.CONFIG_PATH.exists() and not workflow.SECRET_PATH.exists()


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:11434/v1", "http://localhost:1234/v1", "http://[::1]:1234/v1"])
def test_explicit_loopback_http_endpoint_supports_local_models(workflow, base_url):
    status = configure_generic(workflow, base_url=base_url)
    assert status["configured"] and status["base_url"] == base_url


@pytest.mark.parametrize("next_url", [OTHER_URL, BASE_URL + "/different-account"])
def test_address_changes_need_a_new_key_before_any_decryption_or_http(workflow, next_url, monkeypatch):
    configure_generic(workflow)
    before = workflow.CONFIG_PATH.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Changing endpoint without a new key must not decrypt or send the existing credential")

    monkeypatch.setattr(workflow, "protect_secret", forbidden)
    with pytest.raises((qa.QAError, runners.RunnerError)):
        configure_generic(workflow, key="", base_url=next_url, transport=httpx.MockTransport(forbidden))
    assert workflow.CONFIG_PATH.read_bytes() == before


def test_new_key_can_change_address_and_later_model_only_changes_keep_that_address(workflow):
    configure_generic(workflow)
    status = configure_generic(workflow, key=OTHER_KEY, base_url=OTHER_URL)
    assert status["base_url"] == OTHER_URL and workflow._api_key() == OTHER_KEY
    status = workflow.configure_provider("", OTHER_MODEL,
                                         transport=fake_endpoint(base_url=OTHER_URL, key=OTHER_KEY))
    assert status["model"] == OTHER_MODEL and status["base_url"] == OTHER_URL
    assert workflow._api_key() == OTHER_KEY


def test_legacy_paratera_plain_secret_and_config_still_work(workflow):
    workflow.CONFIG_PATH.write_text(json.dumps({"provider": "paratera", "base_url": qa.BASE_URL,
        "model": "DeepSeek-V3.2", "models": ["DeepSeek-V3.2"]}), encoding="utf-8")
    workflow.SECRET_PATH.write_bytes(workflow.protect_secret(FAKE_KEY.encode()))
    assert workflow.provider_status()["configured"] and workflow._api_key() == FAKE_KEY
    calls = []
    status = workflow.configure_provider("", "DeepSeek-V3.2", transport=fake_endpoint(
        base_url=qa.BASE_URL, models=["DeepSeek-V3.2"], calls=calls))
    assert status["configured"] and status["provider_id"] == "paratera" and status["base_url"] == qa.BASE_URL
    assert all(str(request.url).startswith(qa.BASE_URL + "/") for request in calls)


def test_paratera_preset_allows_other_chat_models_returned_by_the_account(workflow):
    status = workflow.configure_provider(FAKE_KEY, "Qwen3-32B", provider="paratera",
        transport=fake_endpoint(base_url=qa.BASE_URL, models=["DeepSeek-V3.2", "Qwen3-32B"]))
    assert status["configured"] and status["model"] == "Qwen3-32B"
    assert status["provider_id"] == "paratera" and status["base_url"] == qa.BASE_URL


def test_versioned_credential_never_falls_back_to_a_legacy_secret_when_record_is_damaged(workflow):
    configure_generic(workflow)
    workflow.SECRET_PATH.write_bytes(workflow.protect_secret(OTHER_KEY.encode()))
    config = json.loads(workflow.CONFIG_PATH.read_text(encoding="utf-8"))
    del config["encrypted_credential"]
    workflow.CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises((qa.QAError, runners.RunnerError)):
        workflow._api_key()
    assert not workflow.provider_status()["configured"]


def test_editing_saved_address_cannot_retarget_an_encrypted_credential(workflow):
    configure_generic(workflow)
    config = json.loads(workflow.CONFIG_PATH.read_text(encoding="utf-8"))
    config["base_url"] = config["credential_base_url"] = OTHER_URL
    workflow.CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises((qa.QAError, runners.RunnerError)):
        workflow._api_key()
    assert not workflow.provider_status()["configured"]


def test_failed_atomic_save_preserves_the_previous_connection_and_credential(workflow, monkeypatch):
    configure_generic(workflow)
    before = workflow.CONFIG_PATH.read_bytes()
    replace = Path.replace

    def fail_final_replace(path, target):
        if Path(target) == workflow.CONFIG_PATH:
            raise OSError("Synthetic atomic replace failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_final_replace)
    with pytest.raises((qa.QAError, runners.RunnerError)):
        configure_generic(workflow, key=OTHER_KEY, model=OTHER_MODEL)
    assert workflow.CONFIG_PATH.read_bytes() == before and workflow._api_key() == FAKE_KEY
    assert not list(workflow.CONFIG_PATH.parent.glob("*.tmp"))


def test_legacy_paratera_environment_key_is_never_reused_for_a_generic_address(workflow, monkeypatch):
    key_name = "PARATERA_API_KEY" if workflow is qa else "KG_INGEST_PARATERA_API_KEY"
    monkeypatch.setenv(key_name, FAKE_KEY)
    workflow.CONFIG_PATH.write_text(json.dumps({"provider": "openai_compatible", "base_url": BASE_URL,
        "model": MODEL, "models": [MODEL]}), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("A Paratera-only environment key must not be sent to a generic provider")

    assert not workflow.provider_status()["configured"]
    with pytest.raises((qa.QAError, runners.RunnerError)):
        configure_generic(workflow, key="", transport=httpx.MockTransport(forbidden))


def test_qa_answer_uses_saved_generic_endpoint_and_arbitrary_chat_model(monkeypatch):
    configure_generic(qa)
    source = {"id": "S1", "doc_id": "synthetic", "evidence_id": "synthetic:p1", "pdf_page": 1,
              "title": "Synthetic test fixture", "text": "Fixture process uses Cu.", "reviewed_claims": []}
    monkeypatch.setattr(qa, "retrieve_sources", lambda question, history: ([source], []))
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == BASE_URL + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + FAKE_KEY
        assert json.loads(request.content)["model"] == MODEL
        assert set(json.loads(request.content)) <= {"model", "messages", "stream"}
        return httpx.Response(200, json=completion(json.dumps({"answer": "Synthetic fixture uses Cu.[S1]",
                                                             "answerability": "supported"})))

    result = qa.answer_question("Synthetic fixture question", transport=httpx.MockTransport(handler))
    assert result["generated"] and result["model"] == MODEL and len(calls) == 1


def test_ingestion_explicit_qa_reuse_shares_endpoint_and_key_but_preserves_independent_model(monkeypatch):
    configure_generic(qa)
    status = runners.configure_provider("", OTHER_MODEL, reuse_qa_key=True,
                                        transport=fake_endpoint())
    assert status["credential_source"] == "qa" and status["model"] == OTHER_MODEL
    assert status["provider_id"] == "openai_compatible" and status["base_url"] == BASE_URL
    assert not runners.SECRET_PATH.exists()
    assert FAKE_KEY not in runners.CONFIG_PATH.read_text(encoding="utf-8")
    assert qa.provider_status()["model"] == MODEL
    calls = []
    monkeypatch.setattr(runners, "_prompt", lambda *args, **kwargs: ("Synthetic system", "Synthetic task"))
    monkeypatch.setattr(runners, "_parse_result", lambda *args: {"synthetic_validated": True})

    def handler(request):
        calls.append(request)
        assert str(request.url) == BASE_URL + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + FAKE_KEY
        assert json.loads(request.content)["model"] == OTHER_MODEL
        assert set(json.loads(request.content)) <= {"model", "messages", "stream"}
        return httpx.Response(200, json=completion("{}"))

    result = runners._api_task("extract", {}, transport=httpx.MockTransport(handler))
    assert result["synthetic_validated"] and result["_result_meta"]["model"] == OTHER_MODEL
    assert len(calls) == 1


def test_qa_address_change_invalidates_old_ingestion_qa_reference_without_sending_a_key():
    configure_generic(qa)
    runners.configure_provider("", OTHER_MODEL, reuse_qa_key=True, transport=fake_endpoint())
    before = runners.CONFIG_PATH.read_bytes()
    configure_generic(qa, key=OTHER_KEY, base_url=OTHER_URL)

    def forbidden(*args, **kwargs):
        pytest.fail("A saved QA key reference must not silently follow a later QA endpoint change")

    with pytest.raises(runners.RunnerError):
        runners._api_task("extract", {}, transport=httpx.MockTransport(forbidden))
    assert runners.CONFIG_PATH.read_bytes() == before


def test_independent_ingestion_configuration_never_consults_qa(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Independent ingestion must not read QA configuration or credentials")

    for name in ("_config", "_api_key", "_connection_snapshot"):
        monkeypatch.setattr(qa, name, forbidden, raising=False)
    assert configure_generic(runners)["credential_source"] == "independent"
    assert runners.provider_status()["configured"]


@pytest.mark.parametrize("endpoint,module", [("/api/chat/configure", qa), ("/api/ingestion/configure", runners)])
def test_web_configuration_routes_provider_and_address_to_correct_workflow(endpoint, module, monkeypatch):
    received = []

    def configure(api_key, model="", **kwargs):
        received.append({"api_key": api_key, "model": model, **kwargs})
        return {"configured": True, "provider_id": kwargs["provider"], "base_url": kwargs["base_url"], "model": model}

    if module is qa:
        monkeypatch.setattr(server, "configure_provider", configure)
    else:
        monkeypatch.setattr(runners, "configure_provider", configure)
    body = {"api_key": FAKE_KEY, "model": MODEL, "provider": "openai_compatible", "base_url": BASE_URL}
    with TestClient(server.app, base_url=LOCAL_ORIGIN) as client:
        response = client.post(endpoint, json=body, headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 200 and response.json()["base_url"] == BASE_URL
    assert len(received) == 1 and all(received[0][field] == value for field, value in body.items())
    assert FAKE_KEY not in response.text
