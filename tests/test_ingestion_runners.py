"""Offline contract checks: fake credentials, temporary paths and mocked runners.

These tests do not authenticate to either provider or measure model quality.
"""
from __future__ import annotations

import base64
import copy
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kg_pilot import ingestion_runners as runners
from kg_pilot import qa
from kg_pilot.classification import FACET_DEFINITIONS

FAKE_KEY = "ingestion-unit-test-NOT-A-REAL-KEY"
QA_SENTINEL = "qa-credential-MUST-NOT-BE-USED"
MODEL = "DeepSeek-V3.2"
QUOTE = "Sample A was annealed at 200 C for 1 hour."


@pytest.fixture
def tmp_path():
    # pytest 9's mode=0o700 temp roots get inaccessible Windows ACLs in this
    # managed workspace.  Normal inherited permissions work; keep this local
    # override scoped to these tests, and delete only the directory we created.
    parent = (Path(__file__).resolve().parents[1] / ".cache").resolve()
    parent.mkdir(exist_ok=True)
    directory = parent / ("ingestion-runner-test-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        assert directory.resolve().parent == parent
        shutil.rmtree(directory)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """A test cannot read a real configuration, secret, network, or execute CLI."""
    monkeypatch.setattr(runners, "CONFIG_PATH", tmp_path / "ingestion_llm_config.json")
    monkeypatch.setattr(runners, "SECRET_PATH", tmp_path / "secrets/ingestion.key.dpapi")
    for name in ("KG_INGEST_PARATERA_API_KEY", "KG_INGEST_PARATERA_MODEL", "KG_INGEST_LLM_API_KEY",
                 "KG_INGEST_LLM_BASE_URL", "KG_INGEST_LLM_MODEL", "KG_LLM_BASE_URL", "KG_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PARATERA_API_KEY", QA_SENTINEL)
    monkeypatch.setenv("KG_LLM_API_KEY", QA_SENTINEL)
    monkeypatch.setenv("PARATERA_MODEL", "QA-model-MUST-NOT-BE-USED")

    def no_qa_access(*args, **kwargs):
        pytest.fail("Ingestion must never consult the QA credential/configuration helpers")

    monkeypatch.setattr(qa, "_api_key", no_qa_access)
    # Explicit QA reuse may read non-secret endpoint metadata. The private key
    # accessor remains forbidden until each reuse test deliberately overrides it.
    monkeypatch.setattr(qa, "CONFIG_PATH", tmp_path / "qa_llm_config.json")
    monkeypatch.setattr(qa, "SECRET_PATH", tmp_path / "secrets/qa.key.dpapi")
    monkeypatch.setattr(qa, "_config", lambda: {"provider": "paratera", "base_url": runners.BASE_URL,
                                             "model": "QA-model-MUST-NOT-BE-USED"})

    def fake_protect(data, *, decrypt=False):
        if decrypt:
            assert data.startswith(b"FAKE-DPAPI:")
            return data.removeprefix(b"FAKE-DPAPI:")
        return b"FAKE-DPAPI:" + data

    monkeypatch.setattr(runners, "protect_secret", fake_protect)

    def forbidden(*args, **kwargs):
        pytest.fail("Unit test must explicitly mock every HTTP or Codex execution")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(runners.subprocess, "run", forbidden)
    monkeypatch.setattr(runners.shutil, "which", lambda name: str(tmp_path / "codex.exe"))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("KG_INGEST_PARATERA_API_KEY", FAKE_KEY)
    monkeypatch.setenv("KG_INGEST_PARATERA_MODEL", MODEL)


@pytest.fixture
def packet():
    return {
        "doc_id": "synthetic-unit-fixture", "metadata": {"title": "Fixture paper"},
        "pages": [{"pdf_page": 3, "text": "Synthetic fixture only. " + QUOTE}],
        "taxonomy": {"categories": [{"id": "post_bond_annealing", "label": "键合后退火"}],
                     "facets": {field: list(options) for field, _, _, options in FACET_DEFINITIONS if options},
                     "topic_groups": ["热预算与退火"]},
        "limits": {"max_claims": 3},
    }


@pytest.fixture
def proposal():
    return {
        "metadata": {"title": "Fixture paper", "doi": None, "year": None, "authors": []},
        "classification": {
            "primary_category": "post_bond_annealing", "related_stages": [], "relevance": "core",
            "document_form": "journal_paper", "content_nature": "original_research", "publication_status": "unknown",
            "access_route": "other", "routes": [], "materials": ["Cu"], "topics": ["热预算与退火"],
            "methods": ["experimental"], "keywords": ["低温退火"],
            "classification_reason": "Synthetic fixture classification.", "evidence": [{"pdf_page": 3, "quote": QUOTE}],
        },
        "claims": [{
            "claim_id": "C1", "statement": "Synthetic claim only: Sample A annealing condition.", "material_system": "Cu",
            "sample_id": "Sample A", "stage": "post_bond_annealing", "quantity": "annealing temperature",
            "value_text": "200", "unit": "C", "conditions": {"duration": "1 hour"},
            "evidence": [{"pdf_page": 3, "quote": QUOTE}], "claim_type": "reported_experiment",
        }],
        "coverage_note": "Only one synthetic page is supplied; not exhaustive.",
    }


def review_result(status="accepted"):
    return {"classification_approved": True, "classification_notes": "Fixture source checked.",
            "claims": [{"claim_id": "C1", "status": status, "reason": "Fixture quote, sample and conditions checked."}],
            "summary": "Synthetic fixture review only."}


def envelope(content, *, finish_reason="stop", **message_fields):
    return {"choices": [{"finish_reason": finish_reason, "message": {
        "role": "assistant", "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
        **message_fields,
    }}], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}


def test_exact_input_provenance_echo_is_ignored_but_mutation_rejected(packet, proposal):
    packet["metadata"]["provenance"] = {"publisher": "Fixture", "source_url": "https://example.org/source"}
    proposal["metadata"]["provenance"] = copy.deepcopy(packet["metadata"]["provenance"])
    parsed = runners._parse_result(json.dumps(proposal), "extract", packet)
    assert "provenance" not in parsed["metadata"]
    proposal["metadata"]["provenance"]["publisher"] = "Invented publisher"
    with pytest.raises(runners.RunnerError):
        runners._parse_result(json.dumps(proposal), "extract", packet)


def test_missing_ingestion_configuration_does_not_fall_back_to_qa(packet, tmp_path):
    assert runners.provider_status()["configured"] is False
    with pytest.raises(runners.RunnerError, match="专用") as error:
        runners.run_task("api", "extract", packet, tmp_path)
    assert QA_SENTINEL not in str(error.value)


def test_own_file_paths_and_no_plaintext_key_in_config():
    assert runners.CONFIG_PATH.name == "ingestion_llm_config.json"
    status = runners.configure_provider(FAKE_KEY, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": MODEL}, {"id": "Qwen3-32B"}, {"id": "DeepSeek-OCR"}, {"id": "bge-m3-embedding"}]})))
    assert status["model"] == MODEL
    assert status["models"] == [MODEL, "Qwen3-32B"]
    config = runners.CONFIG_PATH.read_text(encoding="utf-8")
    assert FAKE_KEY not in config and QA_SENTINEL not in config
    assert json.loads(config)["base_url"] == "https://llmapi.paratera.com/v1"
    assert base64.b64decode(json.loads(config)["encrypted_credential"]).startswith(b"FAKE-DPAPI:")
    assert runners.provider_status()["configured"] is True


def test_explicit_actual_account_chat_model_is_supported():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url) == runners.BASE_URL + "/models"
        assert request.headers["Authorization"] == "Bearer " + FAKE_KEY
        return httpx.Response(200, json={"data": [{"id": MODEL}, {"id": "Qwen3-32B"}]})

    status = runners.configure_provider(FAKE_KEY, "Qwen3-32B", transport=httpx.MockTransport(handler))
    assert status["model"] == "Qwen3-32B" and len(calls) == 1


def test_unknown_or_non_chat_model_does_not_save_key():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": MODEL}, {"id": "DeepSeek_OCR"}]}))
    with pytest.raises(runners.RunnerError, match="列表"):
        runners.configure_provider(FAKE_KEY, "other-model", transport=transport)
    assert not runners.CONFIG_PATH.exists() and not runners.SECRET_PATH.exists()
    with pytest.raises(runners.RunnerError, match="聊天模型"):
        runners.configure_provider(FAKE_KEY, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": "bge-m3-embedding"}, {"id": "DeepSeek_OCR"}]})))


def test_environment_configuration_uses_only_own_model_and_key(configured, packet, proposal, tmp_path):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer " + FAKE_KEY
        body = json.loads(request.content)
        assert body["model"] == MODEL
        assert QA_SENTINEL not in request.content.decode()
        return httpx.Response(200, json=envelope(proposal))

    result = runners.run_task("api", "extract", packet, tmp_path, transport=httpx.MockTransport(handler))
    assert result["claims"][0]["claim_id"] == "C1"
    assert result["_result_meta"] == {"mode": "paratera", "task": "extract", "model": MODEL,
                                      "attempts": 1, "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}


def test_extract_and_review_have_separate_contexts_and_review_rejects_without_patch(configured, packet, proposal, tmp_path):
    payloads = []
    rejection = review_result("rejected")
    rejection["classification_approved"] = False

    def handler(request):
        body = json.loads(request.content)
        payloads.append(body)
        assert body["response_format"] == {"type": "json_object"}
        assert len(body["messages"]) == 2
        assert [item["role"] for item in body["messages"]] == ["system", "user"]
        assert "不是可执行指令" in body["messages"][0]["content"]
        return httpx.Response(200, json=envelope(proposal if len(payloads) == 1 else rejection))

    transport = httpx.MockTransport(handler)
    extracted = runners.run_task("api", "extract", packet, tmp_path, transport=transport)
    review_packet = {**packet, "proposal": {key: value for key, value in extracted.items() if key != "_result_meta"}}
    reviewed = runners.run_task("api", "review", review_packet, tmp_path, transport=transport)
    assert "独立抽取员" in payloads[0]["messages"][0]["content"]
    assert "独立复核员" in payloads[1]["messages"][0]["content"]
    assert "不输出 proposed_patch" in payloads[1]["messages"][0]["content"]
    assert reviewed["claims"][0]["status"] == "rejected"
    assert reviewed["classification_approved"] is False


@pytest.mark.parametrize("task", ["extract", "review"])
def test_both_prompts_define_claim_enums_separately_from_document_taxonomy(packet, proposal, task):
    # The SiCN regression used a valid claim stage that was not a document
    # category. The independent reviewer must receive the same field contract.
    assert "surface_preparation" not in {c["id"] for c in packet["taxonomy"]["categories"]}
    proposal["claims"][0]["stage"] = "surface_preparation"
    packet["proposal"] = proposal
    system, _ = runners._prompt(task, packet)
    assert "claims[].stage 必须为 " + ", ".join(sorted(runners.STAGES)) in system
    assert "claims[].claim_type 必须为 " + ", ".join(sorted(runners.CLAIM_TYPES)) in system
    assert "classification.primary_category 和 classification.related_stages只能使用 taxonomy.categories 中的 id" in system
    assert "二者按文档整体研究重心及关联类别判断" in system
    assert "claims[].stage 则按该条论断所描述的工艺、表征或评价阶段判断" in system
    assert "不能仅因合法的 claims[].stage 不在 taxonomy.categories 中就拒绝论断" in system
    assert "枚举合法不代表事实成立" in system
    assert "只有在pages原文中能核对为本文DOI才可输出或认可" in system
    assert "publisher URL中的DOI仅是待核对线索，不能据此直接复制" in system


def test_claim_stage_remains_independent_of_document_category_validation(packet, proposal):
    proposal["claims"][0]["stage"] = "surface_preparation"
    assert runners._parse_result(json.dumps(proposal), "extract", packet)["claims"][0]["stage"] == "surface_preparation"
    # Moving the valid claim stage into the document classification remains an
    # error; the prompt fix must not loosen either namespace's existing guards.
    proposal["classification"]["related_stages"] = ["surface_preparation"]
    with pytest.raises(runners.RunnerError, match="校验"):
        runners._parse_result(json.dumps(proposal), "extract", packet)


def test_invalid_json_retries_only_once_with_fresh_messages(configured, packet, proposal, tmp_path):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=envelope("not JSON" if len(bodies) == 1 else proposal))

    result = runners.run_task("api", "extract", packet, tmp_path, transport=httpx.MockTransport(handler))
    assert result["_result_meta"]["attempts"] == 2
    assert result["_result_meta"]["usage"]["total_tokens"] == 40
    assert len(bodies[1]["messages"]) == 2
    assert "最后一次格式重试" in bodies[1]["messages"][0]["content"]
    assert "not JSON" not in bodies[1]["messages"][1]["content"]


@pytest.mark.parametrize("invalid", ["```json\n{}\n```", "[]", '{"a":1,"a":2}', '{"value":NaN}', '{"metadata":'])
def test_invalid_json_never_becomes_success(configured, packet, tmp_path, invalid):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(invalid))

    with pytest.raises(runners.RunnerError, match="两次"):
        runners.run_task("api", "extract", packet, tmp_path, transport=httpx.MockTransport(handler))
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["401", "403", "429", "500", "redirect", "invalid_body", "timeout", "network", "length", "refusal", "content_filter", "missing_finish"])
def test_provider_failures_are_safe_and_do_not_retry(configured, packet, proposal, tmp_path, failure, capsys, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("upstream " + FAKE_KEY, request=request)
        if failure == "network":
            raise httpx.ConnectError("upstream " + FAKE_KEY, request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://invalid.example/" + FAKE_KEY})
        if failure == "invalid_body":
            return httpx.Response(200, text="upstream " + FAKE_KEY)
        if failure.isdigit():
            return httpx.Response(int(failure), json={"error": "upstream " + FAKE_KEY})
        if failure == "refusal":
            return httpx.Response(200, json=envelope(proposal, refusal="upstream " + FAKE_KEY))
        return httpx.Response(200, json=envelope(proposal, finish_reason=None if failure == "missing_finish" else failure))

    with pytest.raises(runners.RunnerError) as error:
        runners.run_task("api", "extract", packet, tmp_path, transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert FAKE_KEY not in str(error.value) + captured.out + captured.err + caplog.text


@pytest.mark.parametrize("mutation", ["quote", "page", "topic", "stage", "duplicate_id", "extra_field", "too_many", "category", "related_stage", "scalar_facet", "list_facet"])
def test_extract_structural_and_exact_quote_guards(packet, proposal, mutation):
    result = copy.deepcopy(proposal)
    if mutation == "quote":
        result["claims"][0]["evidence"][0]["quote"] = QUOTE.replace("200", "201")
    elif mutation == "page":
        result["claims"][0]["evidence"][0]["pdf_page"] = 1
    elif mutation == "topic":
        result["classification"]["topics"] = ["细分词不能作为中文大类"]
    elif mutation == "stage":
        result["claims"][0]["stage"] = "invented_stage"
    elif mutation == "duplicate_id":
        result["claims"].append(copy.deepcopy(result["claims"][0]))
    elif mutation == "extra_field":
        result["claims"][0]["review_status"] = "fake_success"
    elif mutation == "category":
        result["classification"]["primary_category"] = "measurement"
    elif mutation == "related_stage":
        result["classification"]["related_stages"] = ["measurement", "reliability_test"]
    elif mutation == "scalar_facet":
        result["classification"]["relevance"] = "high"
    elif mutation == "list_facet":
        result["classification"]["methods"] = ["measuring"]
    else:
        packet["limits"]["max_claims"] = 0
    with pytest.raises(runners.RunnerError):
        runners._parse_result(json.dumps(result, ensure_ascii=False), "extract", packet)


def test_quote_can_fold_whitespace_but_not_change_symbols(packet, proposal):
    packet["pages"][0]["text"] = QUOTE.replace(" at ", "\n at   ")
    assert runners._parse_result(json.dumps(proposal), "extract", packet)["claims"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "patch", "needs_fix", "string_boolean"])
def test_review_requires_every_id_exactly_once_and_no_patches(packet, proposal, mutation):
    packet["proposal"] = proposal
    result = review_result()
    if mutation == "missing":
        result["claims"] = []
    elif mutation == "duplicate":
        result["claims"] *= 2
    elif mutation == "extra":
        result["claims"][0]["claim_id"] = "C-unknown"
    elif mutation == "patch":
        result["claims"][0]["proposed_patch"] = {"value_text": "201"}
    elif mutation == "needs_fix":
        result["claims"][0]["status"] = "needs_fix"
    else:
        result["classification_approved"] = "true"
    with pytest.raises(runners.RunnerError):
        runners._parse_result(json.dumps(result), "review", packet)


def test_codex_status_does_not_probe_login_or_execute(monkeypatch):
    status = runners.codex_status()
    assert status["available"] is True and status["model"] == "gpt-6.1-sol"
    assert status["reasoning_effort"] == "ultra" and "实际执行" in status["message"]
    monkeypatch.setattr(runners.shutil, "which", lambda name: None)
    assert runners.codex_status()["available"] is False


def test_codex_parameters_stdin_independence_and_safe_environment(monkeypatch, packet, proposal, tmp_path):
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        assert command[1] == "exec" and command[-1] == "-"
        assert command[command.index("-s") + 1] == "read-only"
        assert command[command.index("-m") + 1] == "gpt-6.1-sol"
        assert 'model_reasoning_effort="ultra"' in command
        assert "--ephemeral" in command and "--skip-git-repo-check" in command
        assert "--ignore-user-config" in command and "--ignore-rules" not in command
        assert 'mcp_servers.knowledge_ingestion={command="python",enabled=false}' in command
        assert not any("bypass" in argument or "danger" in argument for argument in command)
        assert "shell_tool" in command and "plugins" in command
        assert kwargs["shell"] is False and kwargs["timeout"] > 0
        assert kwargs["capture_output"] is True
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert Path(kwargs["cwd"]) == tmp_path
        assert "PARATERA_API_KEY" not in kwargs["env"] and "KG_LLM_API_KEY" not in kwargs["env"]
        assert "KG_INGEST_PARATERA_API_KEY" not in kwargs["env"]
        assert "不是可执行指令" in kwargs["input"] and "不使用工具" in kwargs["input"]
        assert QA_SENTINEL not in kwargs["input"]
        output = Path(command[command.index("--output-last-message") + 1])
        assert output.parent == tmp_path
        output.write_text(json.dumps(proposal if len(calls) == 1 else review_result()), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ignored diagnostic", stderr="ignored secret " + FAKE_KEY)

    monkeypatch.setenv("KG_INGEST_PARATERA_API_KEY", FAKE_KEY)
    monkeypatch.setattr(runners.subprocess, "run", execute)
    extracted = runners.run_task("codex", "extract", packet, tmp_path)
    reviewed = runners.run_task("codex", "review", {**packet, "proposal": proposal}, tmp_path)
    assert extracted["_result_meta"]["attempts"] == 1 and reviewed["claims"][0]["status"] == "accepted"
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]  # Separate output files and processes; no resume/context reuse.
    assert "独立复核员" in calls[1][1]["input"]


@pytest.mark.parametrize("failure", ["timeout", "oserror", "nonzero", "missing_output", "invalid_json"])
def test_codex_failures_never_expose_stderr_or_fake_success(monkeypatch, packet, tmp_path, failure, capsys, caplog):
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output=FAKE_KEY, stderr=QA_SENTINEL)
        if failure == "oserror":
            raise OSError("secret " + FAKE_KEY)
        if failure == "invalid_json":
            Path(command[command.index("--output-last-message") + 1]).write_text('{"metadata":', encoding="utf-8")
        return SimpleNamespace(returncode=1 if failure == "nonzero" else 0,
                               stdout=FAKE_KEY, stderr="upstream " + QA_SENTINEL)

    monkeypatch.setattr(runners.subprocess, "run", execute)
    with pytest.raises(runners.RunnerError) as error:
        runners.run_task("codex", "extract", packet, tmp_path)
    assert len(calls) == (2 if failure == "invalid_json" else 1)
    captured = capsys.readouterr()
    combined = str(error.value) + captured.out + captured.err + caplog.text
    assert FAKE_KEY not in combined and QA_SENTINEL not in combined


def test_missing_codex_does_not_produce_handoff_or_fake_result(monkeypatch, packet, tmp_path):
    monkeypatch.setattr(runners.shutil, "which", lambda name: None)
    with pytest.raises(runners.RunnerError, match="未找到"):
        runners.run_task("codex", "extract", packet, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("task,change", [("unknown", None), ("extract", "pages"), ("extract", "duplicate_page"), ("extract", "limits"), ("review", "proposal")])
def test_invalid_packets_fail_before_any_execution(packet, tmp_path, task, change):
    if change == "pages":
        packet["pages"] = []
    elif change == "duplicate_page":
        packet["pages"] *= 2
    elif change == "limits":
        packet["limits"]["max_claims"] = True
    with pytest.raises(runners.RunnerError):
        runners.run_task("api", task, packet, tmp_path)


def test_usage_only_preserves_numeric_token_counts(configured, packet, proposal, tmp_path):
    response = envelope(proposal)
    response["usage"]["secret"] = FAKE_KEY
    response["usage"]["prompt_tokens_details"] = {"secret": FAKE_KEY}
    response["usage"]["total_tokens"] = FAKE_KEY
    result = runners.run_task("api", "extract", packet, tmp_path, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response)))
    assert result["_result_meta"]["usage"] == {"prompt_tokens": 12, "completion_tokens": 8}
    assert FAKE_KEY not in json.dumps(result)


def test_paratera_mode_is_canonical_and_api_alias_still_works(configured, packet, proposal, tmp_path):
    result = runners.run_task("paratera", "extract", packet, tmp_path, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=envelope(proposal))))
    assert result["_result_meta"]["mode"] == "paratera"


def test_unexpected_transport_exception_is_sanitized(configured, packet, tmp_path):
    def handler(request):
        raise RuntimeError("upstream " + FAKE_KEY)

    with pytest.raises(runners.RunnerError) as error:
        runners.run_task("paratera", "extract", packet, tmp_path, transport=httpx.MockTransport(handler))
    assert FAKE_KEY not in str(error.value)


def model_transport(*, expected_key=None):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/v1/models"
        if expected_key is not None:
            assert request.headers["Authorization"] == "Bearer " + expected_key
        return httpx.Response(200, json={"data": [{"id": MODEL}, {"id": "Qwen3-32B"}]})
    return httpx.MockTransport(handler)


def test_explicit_qa_reuse_saves_only_reference_and_independent_model(monkeypatch, packet, proposal, tmp_path):
    calls = []

    def qa_key(config=None):
        calls.append(True)
        return QA_SENTINEL

    monkeypatch.setattr(qa, "_api_key", qa_key)
    status = runners.configure_provider("", "Qwen3-32B", reuse_qa_key=True,
                                        transport=model_transport(expected_key=QA_SENTINEL))
    assert len(calls) == 1
    assert status["credential_source"] == "qa" and status["model"] == "Qwen3-32B"
    saved = runners.CONFIG_PATH.read_text(encoding="utf-8")
    assert json.loads(saved)["credential_source"] == "qa"
    assert QA_SENTINEL not in saved and not runners.SECRET_PATH.exists()
    # Status reads only the local reuse choice; it must not decrypt the QA key.
    assert runners.provider_status()["configured"] is True
    assert runners.provider_status()["credential_source"] == "qa"
    assert len(calls) == 1

    def handler(request):
        assert request.headers["Authorization"] == "Bearer " + QA_SENTINEL
        body = json.loads(request.content)
        assert body["model"] == "Qwen3-32B"  # Never the inherited QA model.
        return httpx.Response(200, json=envelope(proposal))

    result = runners.run_task("paratera", "extract", packet, tmp_path, transport=httpx.MockTransport(handler))
    assert len(calls) == 2 and result["_result_meta"]["model"] == "Qwen3-32B"
    assert QA_SENTINEL not in json.dumps(result)


def test_empty_key_model_switch_preserves_explicit_qa_source(monkeypatch):
    monkeypatch.setattr(qa, "_api_key", lambda config=None: QA_SENTINEL)
    runners.configure_provider("", reuse_qa_key=True, transport=model_transport(expected_key=QA_SENTINEL))
    status = runners.configure_provider("", "Qwen3-32B", transport=model_transport(expected_key=QA_SENTINEL))
    assert status["credential_source"] == "qa" and status["model"] == "Qwen3-32B"
    assert not runners.SECRET_PATH.exists()


def test_nonempty_independent_key_switches_away_from_reuse_and_takes_priority(monkeypatch):
    monkeypatch.setattr(qa, "_api_key", lambda config=None: QA_SENTINEL)
    runners.configure_provider("", reuse_qa_key=True, transport=model_transport(expected_key=QA_SENTINEL))

    def must_not_read_qa(config=None):
        pytest.fail("A supplied independent key takes priority even if the checkbox is still selected")

    monkeypatch.setattr(qa, "_api_key", must_not_read_qa)
    status = runners.configure_provider(FAKE_KEY, "Qwen3-32B", reuse_qa_key=True,
                                        transport=model_transport(expected_key=FAKE_KEY))
    assert status["credential_source"] == "independent"
    assert json.loads(runners.CONFIG_PATH.read_text(encoding="utf-8"))["credential_source"] == "independent"
    assert runners._api_key() == FAKE_KEY
    # Model-only changes continue to use the explicitly selected independent key.
    status = runners.configure_provider("", MODEL, transport=model_transport(expected_key=FAKE_KEY))
    assert status["credential_source"] == "independent"


def test_qa_reuse_validation_failure_does_not_overwrite_existing_connection(monkeypatch):
    runners.configure_provider(FAKE_KEY, transport=model_transport(expected_key=FAKE_KEY))
    config_before = runners.CONFIG_PATH.read_bytes()
    key_before = runners.SECRET_PATH.read_bytes() if runners.SECRET_PATH.exists() else None
    monkeypatch.setattr(qa, "_api_key", lambda config=None: QA_SENTINEL)
    with pytest.raises(runners.RunnerError):
        runners.configure_provider("", reuse_qa_key=True, transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": QA_SENTINEL})))
    assert runners.CONFIG_PATH.read_bytes() == config_before
    assert (runners.SECRET_PATH.read_bytes() if runners.SECRET_PATH.exists() else None) == key_before
    assert runners.provider_status()["credential_source"] == "independent"


def test_failed_qa_reuse_model_change_preserves_model_and_source(monkeypatch):
    monkeypatch.setattr(qa, "_api_key", lambda config=None: QA_SENTINEL)
    runners.configure_provider("", reuse_qa_key=True, transport=model_transport())
    before = runners.CONFIG_PATH.read_bytes()
    with pytest.raises(runners.RunnerError, match="列表"):
        runners.configure_provider("", "unavailable-model", transport=model_transport())
    assert runners.CONFIG_PATH.read_bytes() == before
    assert not runners.SECRET_PATH.exists()


def test_explicit_reuse_missing_key_does_not_fall_back_to_old_independent_secret(monkeypatch, packet, tmp_path):
    runners.configure_provider(FAKE_KEY, transport=model_transport())
    # A legacy credential may still exist after migration. Explicit QA reuse
    # must not silently fall back to that independent credential.
    runners.SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    runners.SECRET_PATH.write_bytes(runners.protect_secret(FAKE_KEY.encode()))
    independent_before = runners.SECRET_PATH.read_bytes()
    monkeypatch.setattr(qa, "_api_key", lambda config=None: QA_SENTINEL)
    runners.configure_provider("", reuse_qa_key=True, transport=model_transport())
    assert runners.SECRET_PATH.read_bytes() == independent_before  # No copy, overwrite or deletion.
    monkeypatch.setattr(qa, "_api_key", lambda config=None: "")
    assert runners.provider_status()["configured"] is True  # Saved choice; checked live only on execution.
    with pytest.raises(runners.RunnerError, match="可复用密钥"):
        runners.run_task("paratera", "extract", packet, tmp_path)


def test_qa_read_failure_is_sanitized_and_cannot_implicitly_opt_in(monkeypatch):
    def failure(config=None):
        raise qa.QAError("upstream secret " + QA_SENTINEL)

    monkeypatch.setattr(qa, "_api_key", failure)
    assert runners.provider_status()["credential_source"] == "independent"
    with pytest.raises(runners.RunnerError) as error:
        runners.configure_provider("", reuse_qa_key=True)
    assert QA_SENTINEL not in str(error.value)
    assert not runners.CONFIG_PATH.exists() and not runners.SECRET_PATH.exists()


@pytest.mark.parametrize("source,modified", [
    ("wafer-to-\nwafer hybrid interface", "wafer-to-wafer hybrid interface"),
    ("The pad conﬁguration was measured.", "The pad configuration was measured."),
    ("The pitch was 1 µm.", "The pitch was 1 μm."),
    ("The temperature was 200 ◦C.", "The temperature was 200 °C."),
])
def test_original_pdf_hyphenation_ligatures_and_symbols_stay_strict(source, modified):
    with pytest.raises(runners._FormatError) as error:
        runners._evidence([{"pdf_page": 3, "quote": modified}], {3: source}, path="classification.evidence")
    assert "classification.evidence[0]" in error.value.reason and "第 3 页" in error.value.reason
    assert modified not in error.value.reason  # Feedback does not echo upstream/source text.
    runners._evidence([{"pdf_page": 3, "quote": source}], {3: source}, path="classification.evidence")


def test_quote_retry_receives_safe_field_feedback_without_relaxing_source(configured, packet, proposal, tmp_path):
    bad = copy.deepcopy(proposal)
    bad["classification"]["evidence"][0]["quote"] = "The wafer-to-wafer hybrid interface was measured."
    good = copy.deepcopy(proposal)
    good["classification"]["evidence"][0]["quote"] = "The wafer-to- wafer hybrid interface was measured."
    packet["pages"][0]["text"] += " The wafer-to-\nwafer hybrid interface was measured."
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        system = body["messages"][0]["content"]
        assert "不要修复 PDF 行末断词或连字" in system
        if len(bodies) == 2:
            assert "本地校验反馈：classification.evidence[0]" in system
            assert "第 3 页不能逐字匹配" in system
            assert FAKE_KEY not in system and QA_SENTINEL not in system
        return httpx.Response(200, json=envelope(bad if len(bodies) == 1 else good))

    result = runners.run_task("paratera", "extract", packet, tmp_path, transport=httpx.MockTransport(handler))
    assert len(bodies) == 2 and result["_result_meta"]["attempts"] == 2
    assert result["classification"]["evidence"][0]["quote"] == good["classification"]["evidence"][0]["quote"]
