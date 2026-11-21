import json

import httpx
import pytest

from kg_pilot import shared_ingestion as shared


def test_page_conversion_preserves_source_revision_units_and_order():
    paper = {"doc_id":"a", "sha256":"verified", "page_count":2}
    pages = [{"pdf_page":1, "text":"Abstract"}, {"pdf_page":2, "text":"Cu 0.80 µm"}]
    table = {"doc_id":"a", "pdf_page":2, "document_sha256":"verified",
             "table_caption":"Table 1", "text":"Sn thickness | Pressure\n1 µm | 1 MPa"}
    items = shared.page_items(paper, pages, [table])
    assert [i["page_idx"] for i in items] == [0, 1, 1]
    assert items[-1]["table_body"] == table["text"] and items[-1]["table_caption"] == ["Table 1"]
    with pytest.raises(ValueError, match="revision"):
        shared.page_items(paper, pages, [dict(table, document_sha256="different")])
    with pytest.raises(ValueError, match="sequence"):
        shared.page_items(paper, pages[::-1], [])


def test_model_and_endpoint_pin_precede_any_external_request(tmp_path, monkeypatch):
    model = "DeepSeek-V4.1-Flash-1017"
    cfg = dict(model=model, provider_id="paratera", base_url=shared.llm.PARATERA_BASE_URL)
    monkeypatch.setattr(shared.runners, "_effective_config", lambda: cfg)
    monkeypatch.setattr(shared.runners, "_api_key", lambda _: "synthetic-test-key")
    monkeypatch.setattr(shared.runners, "_check_key", lambda _: None)
    run = shared.ApiRun(tmp_path / "usage.jsonl", model, 100)
    transport = run.transport("framework_build")
    for url, name, error in [
        ("https://example.org/v1/chat/completions", model, "destination"),
        (run.endpoint+"/chat/completions", "other", "destination"),
        (run.endpoint+"/chat/completions", model, "limit")]:
        req = httpx.Request("POST", url, json={"model":name, "messages":[], "max_tokens":100})
        with pytest.raises(ValueError, match=error):
            transport.handle_request(req)
    with pytest.raises(ValueError, match="CLI"):
        run.intake_task("codex", "extract", {}, tmp_path)
    assert not run.log.exists()


def test_usage_is_reloaded_without_adding_cached_input_twice(tmp_path, monkeypatch):
    model = "DeepSeek-V4.1-Flash-1017"
    cfg = dict(provider_id="paratera", base_url=shared.llm.PARATERA_BASE_URL, model=model)
    monkeypatch.setattr(shared.runners, "_effective_config", lambda: cfg)
    monkeypatch.setattr(shared.runners, "_api_key", lambda _: "independent-test-key")
    monkeypatch.setattr(shared.runners, "_check_key", lambda _: None)
    seen = []

    def reply(client, method, url, *, headers, json):
        seen.append(json)
        return httpx.Response(200, json=dict(model=model, usage={
            "prompt_tokens":20,"completion_tokens":5,"total_tokens":25,
            "prompt_tokens_details":{"cached_tokens":10}},
            choices=[{"message":{"content":"ok"},"finish_reason":"stop"}]))

    monkeypatch.setattr(httpx.Client, "request", reply)
    run = shared.ApiRun(tmp_path / "usage.jsonl", model, 50000)
    request = httpx.Request("POST", run.endpoint+"/chat/completions", headers={"Authorization":"Bearer sample"},
                            json=dict(model=model, messages=[{"role":"user","content":"test question"}]))
    assert run.transport("framework_build").handle_request(request).json()["model"] == model
    assert seen[0]["thinking"] == {"type":"disabled"}
    log = run.log.read_text(encoding="utf8")
    assert json.loads(log)["charged_tokens"] == 25
    assert "test question" not in log and "Bearer" not in log
    assert shared.ApiRun(run.log, model, 50000).charged == 25
