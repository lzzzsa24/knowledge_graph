"""The diagnostic CLI uses MockTransport exclusively; no service or browser."""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import httpx
import pytest

from scripts import diagnose_ideas as diagnostic


@pytest.fixture
def report_dir():
    # Inherit workspace ACLs; Windows sandbox identities cannot use pytest's
    # restrictive default temporary directory reliably in this workspace.
    directory = Path(__file__).resolve().parents[1] / ".cache/idea_diagnostics/tests" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    return directory


def reply(operation):
    if operation == "status":
        return {"status": "ok", "service": "knowledge_graph", "uptime_seconds": 2,
                "corpus": {"papers": 100, "claims": 566, "pages": 300},
                "indexes": {"sqlite": True, "qdrant": True, "embeddings": True}, "active_generations": 0}
    if operation == "runs":
        return {"runs": [{"run_id": "test-run-1", "stage": "complete", "status": "completed", "idea_count": 1}],
                "storage": "process_memory", "captures_content": False}
    if operation == "preview":
        return {"mode": "random", "focus": "测试方向", "sources": [{"id": "S1"}], "contexts": [],
                "context_chars": 0, "count": 3, "provider_called": False}
    return {"mode": "random", "generated": True, "ideas": [{"title": "MOCK_ONLY_DETAIL"}],
            "sources": [], "warnings": [], "run_id": "test-generation-1"}


def invoke(argv, report_dir, handler):
    stdout, stderr = io.StringIO(), io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        code = diagnostic.main(argv, client=client, output_dir=report_dir, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("operation,method,endpoint,timeout", [
    ("status", "GET", "/api/debug/health", 10),
    ("runs", "GET", "/api/debug/ideas/runs", 10),
    ("preview", "POST", "/api/debug/ideas/preview", 420),
    ("generate", "POST", "/api/ideas", 420),
])
def test_modes_fixed_local_endpoint_timeouts_and_brief_output(report_dir, operation, method, endpoint, timeout):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == method
        assert str(request.url) == diagnostic.BASE_URL + endpoint
        assert set(request.extensions["timeout"].values()) == {timeout}
        if method == "POST":
            assert json.loads(request.content) == {"prompt": "", "history": [], "count": 3}
        return httpx.Response(200, json=reply(operation))

    code, output, errors = invoke([operation], report_dir, handler)
    assert code == 0 and not errors and len(calls) == 1
    brief = json.loads(output)
    assert set(brief) == {"status", "count", "run_id", "report"}
    report = json.loads(Path(brief["report"]).read_text(encoding="utf-8"))
    assert report["operation"] == operation and report["http_status"] == 200
    assert report["response"] == reply(operation)
    assert "MOCK_ONLY_DETAIL" not in output
    if operation == "generate":
        assert brief["count"] == 1 and brief["run_id"] == "test-generation-1"


def test_utf8_bodyfile_and_history_are_sent_without_merging_or_truncation(report_dir):
    input_file, history_file = report_dir / "讨论.txt", report_dir / "history.json"
    prompt = "第一行：退火条件\n第二行：保持原有样品范围\n"
    history = [{"role": "user", "content": "不同于输入框的历史"}, {"role": "assistant", "content": "待验证假设"}]
    input_file.write_text(prompt, encoding="utf-8-sig")
    history_file.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    destination = report_dir / "chosen.json"

    def handler(request):
        assert request.url.path == "/api/debug/ideas/preview"
        assert json.loads(request.content) == {"prompt": prompt, "history": history, "count": 5}
        return httpx.Response(200, json={**reply("preview"), "mode": "guided", "count": 5})

    code, output, errors = invoke(["preview", "--input-file", str(input_file), "--history-file", str(history_file),
                                   "--count", "5", "--output", str(destination)], report_dir, handler)
    assert code == 0 and not errors
    assert json.loads(output)["report"] == str(destination.resolve())
    assert json.loads(output)["count"] == 5


def test_prompt_and_output_before_command(report_dir):
    destination = report_dir / "explicit.json"

    def handler(request):
        assert json.loads(request.content) == {"prompt": "Cu/SiO₂，低温键合", "history": [], "count": 1}
        return httpx.Response(200, json={**reply("generate"), "mode": "guided"})

    code, output, errors = invoke(["--output", str(destination), "generate", "--prompt", "Cu/SiO₂，低温键合", "--count", "1"], report_dir, handler)
    assert code == 0 and not errors and json.loads(output)["report"] == str(destination.resolve())


def test_unique_default_report_names(report_dir):
    names = []
    for _ in range(2):
        code, output, _ = invoke(["status"], report_dir, lambda _: httpx.Response(200, json=reply("status")))
        assert code == 0
        names.append(json.loads(output)["report"])
    assert names[0] != names[1] and all(Path(name).exists() for name in names)


@pytest.mark.parametrize("arguments", [
    ["preview", "--count", "0"], ["preview", "--count", "6"], ["preview", "--count", "1.5"],
    ["generate", "--prompt", "A", "--input-file", "ignored.txt"],
    ["preview", "--prompt", "字" * 6001], ["preview", "--input-file", "missing-file.txt"],
])
def test_input_errors_do_not_send_requests(report_dir, arguments):
    def unexpected(_):
        raise AssertionError("An invalid input must never reach any API.")

    code, output, errors = invoke(arguments, report_dir, unexpected)
    assert code == 1 and not output and "错误：" in errors and "Traceback" not in errors


@pytest.mark.parametrize("value", ["not json", "{}", json.dumps([{"role": "system", "content": "bad"}]),
                                  json.dumps([{"role": [], "content": "bad"}]),
                                  json.dumps([{"role": "user", "content": "ok"}] * 13),
                                  json.dumps([{"role": "user", "content": "ok", "extra": True}])])
def test_invalid_history_is_not_silently_ignored(report_dir, value):
    history = report_dir / "invalid-history.json"
    history.write_text(value, encoding="utf-8")
    code, output, errors = invoke(["preview", "--history-file", str(history)], report_dir,
                                  lambda _: pytest.fail("Invalid history reached the service."))
    assert code == 1 and not output and "对话" in errors


def test_invalid_utf8_input_is_actionable(report_dir):
    source = report_dir / "invalid.txt"
    source.write_bytes(b"\xff\xfe\x00\x00")
    code, output, errors = invoke(["preview", "--input-file", str(source)], report_dir,
                                  lambda _: pytest.fail("Invalid UTF-8 reached the service."))
    assert code == 1 and not output and "UTF-8" in errors


@pytest.mark.parametrize("error,message", [(httpx.ConnectError, "先启动本地服务"),
                                           (httpx.ReadTimeout, "超时"),
                                           (httpx.ReadError, "通信失败")])
def test_request_failures_are_saved_without_exception_details(report_dir, error, message):
    def handler(request):
        raise error("SENSITIVE_EXCEPTION_MARKER", request=request)

    code, output, errors = invoke(["preview"], report_dir, handler)
    assert code == 1 and message in errors
    assert "Traceback" not in errors and "SENSITIVE_EXCEPTION_MARKER" not in output + errors
    brief = json.loads(output)
    report = json.loads(Path(brief["report"]).read_text(encoding="utf-8"))
    assert brief["status"] == "error" and report["http_status"] is None and report["error"]
    assert "SENSITIVE_EXCEPTION_MARKER" not in json.dumps(report)


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(403, json={"detail": "PRIVATE_RESPONSE_MARKER"}), "HTTP 403"),
    (httpx.Response(503, text="PRIVATE_RESPONSE_MARKER"), "HTTP 503"),
    (httpx.Response(200, text="PRIVATE_RESPONSE_MARKER"), "非 JSON"),
    (httpx.Response(200, json={"unexpected": "PRIVATE_RESPONSE_MARKER"}), "结构不完整"),
    (httpx.Response(200, json={**reply("preview"), "mode": {"bad": True}}), "结构不完整"),
    (httpx.Response(200, json=[]), "JSON 格式"),
    (httpx.Response(200, json={**reply("preview"), "provider_called": True}), "结构不完整"),
])
def test_http_and_malformed_responses_are_brief(report_dir, response, expected):
    code, output, errors = invoke(["preview"], report_dir, lambda _: response)
    assert code == 1 and expected in errors
    assert "Traceback" not in errors and "PRIVATE_RESPONSE_MARKER" not in errors + output
    brief = json.loads(output)
    assert brief["status"] == "error" and Path(brief["report"]).exists()


def test_default_client_ignores_environment_proxies_and_redirects(monkeypatch, report_dir):
    actual_client = httpx.Client
    constructor_calls = []

    def factory(**kwargs):
        constructor_calls.append(kwargs)
        return actual_client(**kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply("status"))))

    monkeypatch.setattr(diagnostic.httpx, "Client", factory)
    stdout, stderr = io.StringIO(), io.StringIO()
    assert diagnostic.main(["status"], output_dir=report_dir, stdout=stdout, stderr=stderr) == 0
    assert constructor_calls == [{"trust_env": False, "follow_redirects": False}]
    assert not stderr.getvalue()


def test_output_write_error_is_short(report_dir):
    blocked = report_dir / "not-a-directory"
    blocked.write_text("keep", encoding="utf-8")
    code, output, errors = invoke(["status", "--output", str(blocked / "report.json")], report_dir,
                                  lambda _: httpx.Response(200, json=reply("status")))
    assert code == 1 and not output and "无法保存诊断 JSON" in errors and "Traceback" not in errors
