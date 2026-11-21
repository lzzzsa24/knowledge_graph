"""QA contracts and local API tests.

Provider-unit tests use httpx.MockTransport and a deliberately fake API key. They
verify request/validation behavior, NOT real-model answer quality or an end-to-end
generation result. Evidence fixtures are read from the actual pilot SQLite index.
No real credential is read, written, or sent by this module.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kg_pilot import qa
from web import server

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "pilot/indexes/pilot.sqlite"
FAKE_API_KEY = "unit-test-placeholder-NOT-A-REAL-PROVIDER-KEY"
FAKE_MODEL = "DeepSeek-V3.2"
LOCAL_ORIGIN = "http://127.0.0.1:8147"
REAL_FORMAT_EVENT = qa._format_event


@pytest.fixture(autouse=True)
def isolated_provider_configuration(monkeypatch, tmp_path):
    """Never consult the user's credential files or inherited provider variables."""
    for name in ("PARATERA_API_KEY", "KG_LLM_API_KEY", "PARATERA_MODEL", "OPENAI_API_KEY",
                 "KG_LLM_BASE_URL", "KG_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(qa, "CONFIG_PATH", tmp_path / "llm_config.json")
    monkeypatch.setattr(qa, "SECRET_PATH", tmp_path / "secrets" / "fake.key.dpapi")
    # Keep ordinary QA tests out of the real runs directory. One dedicated test
    # restores the function with PILOT redirected to its own tmp_path.
    monkeypatch.setattr(qa, "_format_event", lambda **kwargs: None)

    def forbid_outbound_http(*args, **kwargs):
        pytest.fail("QA unit tests must use MockTransport; outbound HTTP is forbidden")

    # TestClient has its own in-process ASGI transport; it remains usable.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_outbound_http)


@pytest.fixture
def configured_fake_environment(monkeypatch):
    monkeypatch.setenv("PARATERA_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("PARATERA_MODEL", FAKE_MODEL)


@pytest.fixture
def original_evidence():
    """A real source paragraph, with its recorded physical PDF page."""
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT evidence_id,doc_id,pdf_page,text FROM evidence "
            "WHERE doc_id=? AND pdf_page=3 AND text LIKE ? "
            "ORDER BY length(text) DESC LIMIT 1",
            ("reliability_gao_d2w_2021", "%200-250%"),
        ).fetchone()
    assert row is not None, "The collected Gao paper must have its annealing evidence indexed"
    return dict(zip(("evidence_id", "doc_id", "pdf_page", "text"), row))


def chat_completion_envelope(answer: str, answerability: str = "supported"):
    """OpenAI-compatible wire format; fake provider output is explicitly synthetic."""
    content = json.dumps({"answer": answer, "answerability": answerability}, ensure_ascii=False)
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def assert_original_sqlite_source(source):
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT doc_id,pdf_page,text FROM evidence WHERE evidence_id=?",
            (source["evidence_id"],),
        ).fetchone()
    assert row is not None
    assert (source["doc_id"], source["pdf_page"], source["text"]) == row
    assert isinstance(source["pdf_page"], int) and source["pdf_page"] >= 1


def test_source_fixture_is_real_sqlite_evidence(original_evidence):
    """Local evidence integration check, independent of any language model."""
    import pymupdf

    assert_original_sqlite_source(original_evidence)
    catalog = json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig"))
    paper = next(p for p in catalog if p["doc_id"] == original_evidence["doc_id"])
    with pymupdf.open(paper["local_path"]) as pdf:
        assert original_evidence["pdf_page"] <= len(pdf)


def test_missing_configuration_raises_before_retrieval_or_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No retrieval or provider request should run without API configuration")

    monkeypatch.setattr(qa, "retrieve_sources", forbidden)
    assert qa.provider_status()["configured"] is False
    with pytest.raises(qa.QAError, match="密钥"):
        qa.answer_question("五层芯片采用什么退火条件？", transport=httpx.MockTransport(forbidden))


@pytest.mark.parametrize("failure", ["401", "429", "500", "redirect", "invalid_json", "timeout", "network"])
def test_provider_errors_are_sanitized(failure, caplog, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + FAKE_API_KEY
        if failure == "timeout":
            raise httpx.ReadTimeout("upstream message " + FAKE_API_KEY, request=request)
        if failure == "network":
            raise httpx.ConnectError("network message " + FAKE_API_KEY, request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text=FAKE_API_KEY)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.invalid/" + FAKE_API_KEY})
        return httpx.Response(int(failure), json={"error": "provider body " + FAKE_API_KEY})

    with pytest.raises(qa.QAError) as error:
        qa._request("POST", "/chat/completions", FAKE_API_KEY, {"model": FAKE_MODEL},
                    transport=httpx.MockTransport(handler))
    assert len(calls) == 1, "Redirects or hidden retries must not forward the credential"
    captured = capsys.readouterr()
    assert FAKE_API_KEY not in str(error.value) + caplog.text + captured.out + captured.err
    assert "upstream message" not in str(error.value)


@pytest.mark.parametrize("answer,answerability,accepted", [
    ("退火温度为 200–250 °C，持续 1 h。[S1]", "supported", True),
    ("已提供的证据只支持退火条件。[S1]", "partial", True),
    ("本次证据不足以判断。", "insufficient", True),
    ("退火温度为 200–250 °C。", "supported", False),
    ("只能回答部分。", "partial", False),
    ("退火温度为 200–250 °C。[S99]", "supported", False),
    ("证据不足。[S99]", "insufficient", False),
])
def test_answers_only_accept_available_source_citations(original_evidence, answer, answerability, accepted):
    sources = [{"id": "S1", **original_evidence}]
    content = json.dumps({"answer": answer, "answerability": answerability}, ensure_ascii=False)
    if accepted:
        assert qa.validate_answer(content, sources) == {"answer": answer, "answerability": answerability}
    else:
        with pytest.raises(qa.QAError):
            qa.validate_answer(content, sources)


@pytest.mark.parametrize("content", ["a plain-text answer [S1]", "[]", '{"answer":"[S1]","answerability":"certain"}'])
def test_answer_must_have_supported_json_schema(content, original_evidence):
    with pytest.raises(qa.QAError):
        qa.validate_answer(content, [{"id": "S1", **original_evidence}])


def test_missing_assessment_keeps_complete_cited_answer_without_guessing_support(original_evidence):
    answer = "退火温度为 200–250 °C，持续 1 h。[S1]"
    result = qa.validate_answer(json.dumps({"answer": answer}, ensure_ascii=False),
                                [{"id": "S1", **original_evidence}])
    assert result == {"answer": answer, "answerability": "unassessed"}


@pytest.mark.parametrize("body", [
    {"answer": "退火温度为 200–250 °C。"},
    {"answer": "资料不足。"},
    {"answer": "退火温度为 200–250 °C。[S99]"},
    {"answer": ""},
    {"answer": "有引用。[S1]", "answerability": None},
    {"answer": "有引用。[S1]", "answerability": "unassessed"},
])
def test_missing_assessment_does_not_bypass_answer_or_citation_guards(original_evidence, body):
    with pytest.raises(qa.QAError):
        qa.validate_answer(json.dumps(body, ensure_ascii=False), [{"id": "S1", **original_evidence}])


def test_system_prompt_example_is_a_valid_contract_instance():
    # A model can copy an example verbatim. The example must be a valid value,
    # not a schema placeholder such as "supported|partial|insufficient".
    start = qa.SYSTEM_PROMPT.index('{"answer"')
    example, _ = json.JSONDecoder().raw_decode(qa.SYSTEM_PROMPT[start:])
    assert set(example) == {"answer", "answerability"}
    assert example["answerability"] in {"supported", "partial", "insufficient"}
    assert isinstance(example["answer"], str) and example["answer"].strip()
    if example["answerability"] != "insufficient":
        assert "[S1]" in example["answer"]


@pytest.mark.parametrize("prefix,suffix", [
    ("\ufeff", ""),
    ("```json\n", "\n```"),
    (" \n```JSON\n", "\n```\n"),
    ("\ufeff \n```\n", "\n```"),
])
def test_complete_bom_and_code_fence_wrappers_preserve_cited_answer(original_evidence, prefix, suffix):
    expected = {"answer": "退火条件为 200–250 °C、1 h。[S1]", "answerability": "supported"}
    content = prefix + json.dumps(expected, ensure_ascii=False) + suffix
    assert qa.validate_answer(content, [{"id": "S1", **original_evidence}]) == expected


@pytest.fixture
def isolated_generation_sources(monkeypatch, original_evidence):
    """Fixed real evidence isolates retry mechanics from the retrieval model."""
    source = {**original_evidence, "id": "S1", "title": "Low Temperature Hybrid Bonding for Die to Wafer Stacking Applications",
              "reviewed_claims": [], "claim_ids": []}
    calls = []

    def retrieve(question, history=None):
        calls.append((question, history))
        return [source.copy()], []

    monkeypatch.setattr(qa, "retrieve_sources", retrieve)
    return source, calls


@pytest.mark.parametrize("first_content", [
    "普通文本虽然带引用，但仍不是约定 JSON。[S1]",
    "",
    '{"answer":"截断的 JSON。[S1]",',
    '{"answer":"字段错误。[S1]","answerability":"supported|partial|insufficient"}',
])
def test_format_failure_retries_once_with_identical_evidence(monkeypatch, configured_fake_environment,
                                                           isolated_generation_sources, first_content):
    source, retrieval_calls = isolated_generation_sources
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": first_content}}]})
        assert len(payloads) == 2, "Only one format retry is allowed"
        return httpx.Response(200, json=chat_completion_envelope("退火条件为 200–250 °C、1 h。[S1]"))

    result = qa.answer_question("retry-test question", transport=httpx.MockTransport(handler))
    assert result["generated"] is True and result["generation_attempts"] == 2
    assert result["answerability"] == "supported" and result["answer"].endswith("[S1]")
    assert len(payloads) == 2 and len(retrieval_calls) == 1
    assert all(p["response_format"] == {"type": "json_object"} for p in payloads)
    assert payloads[1]["messages"][:-1] == payloads[0]["messages"]
    first_context = json.loads(payloads[0]["messages"][-1]["content"])
    second_context = json.loads(payloads[1]["messages"][-2]["content"])
    assert first_context == second_context
    assert first_context["evidence"][0]["text"] == source["text"][:3500]
    assert first_context["evidence"][0]["id"] == "S1"
    assert_original_sqlite_source(result["sources"][0])


def test_two_format_failures_stop_at_two_calls_and_release_lock(configured_fake_environment,
                                                               isolated_generation_sources):
    _, retrieval_calls = isolated_generation_sources
    calls = []

    def bad_handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "not JSON [S1]"}}]})

    with pytest.raises(qa.QAError, match="自动重试一次后"):
        qa.answer_question("two invalid responses", transport=httpx.MockTransport(bad_handler))
    assert len(calls) == 2 and len(retrieval_calls) == 1

    # A new user request must still work after the failed two-attempt request.
    fresh_calls = []

    def valid_handler(request):
        fresh_calls.append(request)
        return httpx.Response(200, json=chat_completion_envelope("退火条件为 200–250 °C。[S1]"))

    result = qa.answer_question("new request", transport=httpx.MockTransport(valid_handler))
    assert result["generation_attempts"] == 1 and len(fresh_calls) == 1
    assert len(retrieval_calls) == 2


def test_missing_auxiliary_assessment_does_not_trigger_another_model_call(configured_fake_environment,
                                                                        isolated_generation_sources):
    _, retrieval_calls = isolated_generation_sources
    calls = []

    def handler(request):
        calls.append(request)
        content = json.dumps({"answer": "退火温度为 200–250 °C，持续 1 h。[S1]"}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    result = qa.answer_question("退火条件是什么？", transport=httpx.MockTransport(handler))
    assert result["answerability"] == "unassessed"
    assert result["generation_attempts"] == 1 and len(calls) == 1 and len(retrieval_calls) == 1


@pytest.mark.parametrize("failure", ["unknown_citation", "missing_citation", "length", "http_error"])
def test_nonformat_failures_do_not_retry(configured_fake_environment, isolated_generation_sources, failure):
    _, retrieval_calls = isolated_generation_sources
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "http_error":
            return httpx.Response(500, json={"error": FAKE_API_KEY})
        answer = "退火条件为 200–250 °C。"
        if failure == "unknown_citation":
            answer += "[S99]"
        elif failure == "length":
            answer += "[S1]"
        envelope = chat_completion_envelope(answer)
        envelope["choices"][0]["finish_reason"] = "length" if failure == "length" else "stop"
        return httpx.Response(200, json=envelope)

    with pytest.raises(qa.QAError) as error:
        qa.answer_question("do not retry this failure", transport=httpx.MockTransport(handler))
    assert not isinstance(error.value, qa.QAFormatError)
    assert len(calls) == 1 and len(retrieval_calls) == 1
    assert FAKE_API_KEY not in str(error.value)


def test_format_event_logs_only_metadata_in_isolated_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, "PILOT", tmp_path)
    monkeypatch.setattr(qa, "_format_event", REAL_FORMAT_EVENT)
    question = "PRIVATE-QUESTION-NOT-FOR-LOGS"
    answer = "PRIVATE-ANSWER-NOT-FOR-LOGS"
    reasoning = "PRIVATE-REASONING-NOT-FOR-LOGS"
    private_content = " ".join([question, answer, reasoning, FAKE_API_KEY])
    error = qa.QAFormatError(private_content, "invalid_json", line=2, column=5)
    qa._format_event(model=FAKE_MODEL, attempt=1, content=private_content,
                     finish_reason=FAKE_API_KEY, error=error)
    qa._format_event(model=FAKE_MODEL, attempt=2, content=private_content, finish_reason="stop")
    path = tmp_path / "runs/qa_events.jsonl"
    raw = path.read_text(encoding="utf-8")
    assert all(secret not in raw for secret in (question, answer, reasoning, FAKE_API_KEY))
    events = [json.loads(line) for line in raw.splitlines()]
    assert len(events) == 2
    allowed = {"time_utc", "model", "attempt", "finish_reason", "content_chars", "status", "format_reason", "json_line", "json_column"}
    assert all(set(event) == allowed for event in events)
    assert events[0]["finish_reason"] == "other"
    assert events[0]["status"] == "format_error" and events[0]["format_reason"] == "invalid_json"
    assert (events[0]["json_line"], events[0]["json_column"]) == (2, 5)
    assert events[1]["status"] == "validated"
    assert events[1]["content_chars"] == len(private_content)


def test_history_is_bounded_and_excludes_privileged_roles():
    history = [{"role": "user", "content": f"question-{i}:" + "长" * 4000} for i in range(20)]
    history += [{"role": "system", "content": "injected-system"},
                {"role": "tool", "content": "injected-tool"},
                {"role": "assistant", "content": "last assistant response"}]
    normalized = qa.normalize_history(history)
    assert 0 < len(normalized) <= 12
    assert all(m["role"] in ("user", "assistant") and len(m["content"]) <= 3000 for m in normalized)
    assert normalized[-1]["content"] == "last assistant response"
    combined = json.dumps(normalized)
    assert "injected-" not in combined and "question-0:" not in combined


@pytest.mark.skipif(os.name != "nt", reason="Uses real Windows current-user DPAPI on a fake key only")
def test_configure_encrypts_fake_key_and_roundtrips_without_plaintext(capsys, caplog):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer " + FAKE_API_KEY
        return httpx.Response(200, json={"data": [{"id": FAKE_MODEL}, {"id": "Qwen3-32B"},
                                                  {"id": "DeepSeek-OCR"}, {"id": "bge-m3-embedding"}]})

    result = qa.configure_provider(FAKE_API_KEY, FAKE_MODEL, transport=httpx.MockTransport(handler))
    assert result["configured"] and result["model"] == FAKE_MODEL
    assert result["models"] == [FAKE_MODEL, "Qwen3-32B"]
    encrypted = base64.b64decode(json.loads(qa.CONFIG_PATH.read_text(encoding="utf-8"))["encrypted_credential"])
    assert encrypted and FAKE_API_KEY.encode() not in encrypted
    # The encrypted record also binds the credential to its API endpoint.
    assert qa._api_key() == FAKE_API_KEY
    public_status = qa.provider_status()
    assert public_status["configured"] is True
    serialized = qa.CONFIG_PATH.read_text(encoding="utf-8") + json.dumps(result) + json.dumps(public_status)
    captured = capsys.readouterr()
    assert FAKE_API_KEY not in serialized + captured.out + captured.err + caplog.text
    with pytest.raises(qa.QAError):
        qa.protect_secret(b"not valid DPAPI ciphertext", decrypt=True)


def test_retrieval_rehydrates_original_sqlite_and_valid_physical_pages(monkeypatch, original_evidence):
    import pymupdf

    # This stub tests the trust boundary after retrieval, not embedding quality.
    reason = {"method": "bilingual_fts", "terms": ["diameter"]}
    contaminated = {**original_evidence, "doc_id": "fabricated-document", "pdf_page": 999,
                    "text": "fabricated text that must not reach model evidence",
                    "retrieval_reasons": [reason]}
    hits = [{"evidence_id": "missing-from-sqlite", "doc_id": "fabricated-document"}, contaminated]
    monkeypatch.setattr(qa, "retrieval_model", lambda: object())
    monkeypatch.setattr(qa, "retrieve", lambda *args, **kwargs: hits)
    sources, warnings = qa.retrieve_sources("five-die stack annealing")
    assert sources and not warnings
    matching = next(s for s in sources if s["evidence_id"] == original_evidence["evidence_id"])
    assert reason in matching["retrieval_reasons"]
    assert len({s["id"] for s in sources}) == len(sources)
    papers = {p["doc_id"]: p for p in json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig"))}
    for source in sources:
        assert_original_sqlite_source(source)
        with pymupdf.open(papers[source["doc_id"]]["local_path"]) as pdf:
            assert 1 <= source["pdf_page"] <= len(pdf)


def test_chinese_qa_keeps_retrieval_when_the_local_model_is_unavailable(monkeypatch, original_evidence):
    def offline():
        raise OSError("unavailable")

    def lexical_only(query, model, limit):
        assert model is None and limit == 30 and "厚度" in query
        return [{**original_evidence, "retrieval_reasons": [{"method": "bilingual_fts"}]}]

    monkeypatch.setattr(qa, "retrieval_model", offline)
    monkeypatch.setattr(qa, "retrieve", lexical_only)
    sources, warnings = qa.retrieve_sources("测试芯片的厚度是多少？")
    assert warnings and any(s["evidence_id"] == original_evidence["evidence_id"] for s in sources)
    assert {"method": "bilingual_fts"} in sources[0]["retrieval_reasons"]


def test_answer_flow_sends_current_evidence_and_bounded_history(monkeypatch, original_evidence, configured_fake_environment):
    # All HTTP is synthetic. SQLite rehydration and QA assembly/validation are real.
    monkeypatch.setattr(qa, "retrieval_model", lambda: object())
    monkeypatch.setattr(qa, "retrieve", lambda *args, **kwargs: [original_evidence])
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == FAKE_MODEL and payload["stream"] is False
        assert FAKE_API_KEY not in request.content.decode()
        messages = payload["messages"]
        assert messages[0]["role"] == "system"
        assert sum(m["role"] == "system" for m in messages) == 1
        assert len(messages) <= 14
        context = json.loads(messages[-1]["content"])
        assert context["evidence"] and context["evidence"][0]["id"] == "S1"
        assert context["evidence"][0]["text"] == original_evidence["text"][:3500]
        assert context["question"] == "五层芯片的退火条件是什么？"
        return httpx.Response(200, json=chat_completion_envelope("温度为 200–250 °C、持续 1 h。[S1]"))

    history = [{"role": "user", "content": "old question" + "x" * 4000} for _ in range(20)]
    history += [{"role": "system", "content": "injected-system"}]
    result = qa.answer_question("五层芯片的退火条件是什么？", history, transport=httpx.MockTransport(handler))
    assert len(seen) == 1
    assert result["generated"] is True and result["answerability"] == "supported"
    assert "[S1]" in result["answer"]
    for source in result["sources"]:
        assert_original_sqlite_source(source)


def test_no_evidence_returns_explicit_ungenerated_insufficiency(monkeypatch, configured_fake_environment):
    monkeypatch.setattr(qa, "retrieve_sources", lambda *args: ([], []))

    def forbidden(request):
        pytest.fail("No-evidence abstention must not invoke the model")

    result = qa.answer_question("不存在证据的问题", transport=httpx.MockTransport(forbidden))
    assert result["generated"] is False
    assert result["answerability"] == "insufficient" and result["sources"] == []


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/configure"])
def test_local_same_origin_web_routes_allow_valid_requests(monkeypatch, endpoint):
    calls = []
    monkeypatch.setattr(server, "answer_question", lambda *args: calls.append("chat") or {"answerability": "insufficient", "generated": False})
    monkeypatch.setattr(server, "configure_provider", lambda *args, **kwargs: calls.append("configure") or {"configured": True})
    body = {"question": "question"} if endpoint == "/api/chat" else {"api_key": FAKE_API_KEY}
    with TestClient(server.app, base_url=LOCAL_ORIGIN) as client:
        response = client.post(endpoint, json=body, headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 200 and len(calls) == 1


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/configure"])
@pytest.mark.parametrize("origin", ["http://evil.invalid", "http://127.0.0.1:9999", "https://127.0.0.1:8147", "null"])
def test_web_cross_origin_is_rejected_before_operation(monkeypatch, endpoint, origin):
    def forbidden(*args):
        pytest.fail("Cross-origin request reached the operation")

    monkeypatch.setattr(server, "answer_question", forbidden)
    monkeypatch.setattr(server, "configure_provider", forbidden)
    body = {"question": "question"} if endpoint == "/api/chat" else {"api_key": FAKE_API_KEY}
    with TestClient(server.app, base_url=LOCAL_ORIGIN) as client:
        response = client.post(endpoint, json=body, headers={"Origin": origin})
    assert response.status_code == 403
    assert FAKE_API_KEY not in response.text


def test_nonlocal_client_is_rejected_even_with_matching_origin(monkeypatch):
    def forbidden(*args):
        pytest.fail("Nonlocal request reached configuration")

    monkeypatch.setattr(server, "configure_provider", forbidden)
    with TestClient(server.app, base_url=LOCAL_ORIGIN, client=("203.0.113.8", 12345)) as client:
        response = client.post("/api/chat/configure", json={"api_key": FAKE_API_KEY}, headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 403 and FAKE_API_KEY not in response.text


@pytest.mark.parametrize("endpoint,body", [
    ("/api/chat/configure", {"api_key": {"untrusted": FAKE_API_KEY}}),
    ("/api/chat", {"question": "question", "history": [{"role": "system", "content": FAKE_API_KEY}]}),
])
def test_validation_errors_do_not_echo_submitted_body(endpoint, body):
    with TestClient(server.app, base_url=LOCAL_ORIGIN) as client:
        response = client.post(endpoint, json=body, headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 422
    assert FAKE_API_KEY not in response.text
    assert set(response.json()) == {"detail"} and isinstance(response.json()["detail"], str)

