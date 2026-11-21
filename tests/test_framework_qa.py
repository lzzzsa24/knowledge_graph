import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kg_pilot import framework_qa as framework, qa
from web import server


def test_quote_must_occur_on_the_clicked_page():
    source = {"id": "S1", "pdf_page": 18, "text": "Temperature cycling | 2000 cycles | Pass"}
    answer = {"answer": "2,000 次通过。[S1]"}
    framework.bind_quotes(json.dumps({"quotes": {"S1": "2000 cycles"}}), answer, [source])
    assert source["verified_quote"] == "2000 cycles"
    with pytest.raises(qa.QAFormatError, match="短引"):
        framework.bind_quotes(json.dumps({"quotes": {"S1": "250 cycles"}}), answer, [source])
    with pytest.raises(qa.QAFormatError, match="页码"):
        framework.bind_quotes("{}", {"answer": "PDF 第 9 页 [S1]"}, [source])


def test_declared_scope_resolves_all_titles_or_fails_closed():
    a = "Low Temperature Hybrid Bonding for Die to Wafer Stacking Applications"
    b = "Hybrid Bond Interconnect and Advanced Packaging Solutions"
    assert qa._requested_papers(f"仅依据《{a}》和《{b}》回答：结果？") == set(framework.PDF_REVISIONS)
    with pytest.raises(qa.QAError, match="唯一匹配"):
        qa._requested_papers(f"仅依据《{a}》和《missing》回答：结果？")
    assert qa._requested_papers(f"将《{a}》与其他研究进行比较") is None


def test_no_framework_payload_for_another_endpoint_or_uncovered_scope(monkeypatch):
    def unexpected(*args):
        pytest.fail("A framework call was submitted for an unauthorized endpoint or unrelated scope")
    monkeypatch.setattr(framework._WORKER, "submit", unexpected)
    inputs = (qa.PILOT, "results?", [], [])
    alternate = {"provider_id": "openai_compatible", "base_url": "https://example.org/v1"}
    assert framework.page_sources(*inputs, alternate, "unused", "fixture", None)[0:2] == ([], {})
    assert framework.page_sources(*inputs, {"base_url": qa.BASE_URL}, "unused", "fixture", None,
                                  {"another-paper"}) == ([], {}, [], 0)


def test_multiple_quotes_are_verified_before_publishing_any_excerpt():
    sources = [{"id": "S1", "text": "First source original excerpt."},
               {"id": "S2", "text": "Second source. Another observation."}]
    answer = {"answer": "两个结果。[S1][S2]"}
    quotes = {"S1": "First source", "S2": ["Second source", "Not on this page"]}
    with pytest.raises(qa.QAFormatError):
        framework.bind_quotes(json.dumps({"quotes": quotes}), answer, sources)
    assert all("verified_quote" not in s for s in sources)
    quotes["S2"][1] = "Another observation"
    framework.bind_quotes(json.dumps({"quotes": quotes}), answer, sources)
    assert "Another observation" in sources[1]["verified_quote"]


@pytest.mark.parametrize("fail", [False, True])
def test_mixed_sources_and_framework_fallback(monkeypatch, fail):
    primary = {"id": "S1", "doc_id": "third", "pdf_page": 4, "text": "Third paper result", "title": "Third"}
    page = {"doc_id": "pilot", "pdf_page": 2, "text": "Original page result", "title": "Pilot", "framework_page": True}
    monkeypatch.setattr(qa, "retrieve_sources", lambda *args: ([primary.copy()], []))
    def supplement(*args):
        if fail:
            raise OSError("missing index")
        return [page.copy()], {}, [], 1
    monkeypatch.setattr(framework, "page_sources", supplement)
    monkeypatch.setattr(qa, "provider_status", lambda: {"configured": True})
    config = {"provider_id": "openai_compatible", "base_url": "http://127.0.0.1:1234/v1", "model": "fixture"}
    monkeypatch.setattr(qa, "_connection_snapshot", lambda: (config, "fixture-key"))
    monkeypatch.setattr(qa, "_format_event", lambda **kwargs: None)
    def local_generation(request):
        assert request.url.host == "127.0.0.1"
        body = json.loads(request.content)
        envelope = json.loads(body["messages"][-1]["content"])
        ids = [s["id"] for s in envelope["evidence"]]
        assert ids == (["S1"] if fail else ["S1", "S2"])
        answer = {"answer": " ".join(f"[{sid}]" for sid in ids),
                  "answerability": "supported", "quotes": {"S1": "Third paper result", "S2": "Original page result"}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}]})
    result = qa.answer_question("比较两篇", retrieval_mode="framework", transport=httpx.MockTransport(local_generation))
    assert result["retrieval"]["mode"] == ("library" if fail else "framework")
    assert [s["doc_id"] for s in result["sources"]] == (["third"] if fail else ["third", "pilot"])
    assert bool(result["warnings"]) == fail


def test_viewer_rejects_other_file_revision():
    client = TestClient(server.app)
    doc = next(iter(framework.PDF_REVISIONS))
    for suffix in ("pdf", "pages/3.png"):
        response = client.get(f"/api/papers/{doc}/{suffix}", params={"revision": "0" * 64})
        assert response.status_code == 409


def test_rejected_image_retries_with_text_and_counts_actual_requests(monkeypatch):
    page = {"doc_id": "pilot", "title": "Paper", "pdf_page": 2, "text": "Original result",
            "framework_page": True}
    monkeypatch.setattr(qa, "retrieve_sources", lambda *args: ([], []))
    monkeypatch.setattr(framework, "page_sources", lambda *args: ([page], {("pilot", 2): "fixture"}, [], 1))
    monkeypatch.setattr(qa, "provider_status", lambda: {"configured": True})
    monkeypatch.setattr(qa, "_connection_snapshot", lambda: ({"provider_id": "paratera", "model": "fixture"}, "fixture"))
    monkeypatch.setattr(qa, "_format_event", lambda **kwargs: None)
    def generate(req):
        body = json.loads(req.content)
        if isinstance(body["messages"][-1]["content"], list):
            return httpx.Response(415)
        return httpx.Response(200, json={"usage": {"total_tokens": 20}, "choices": [{
            "finish_reason": "stop", "message": {"content": '{"answer":"结果[S1]","quotes":{"S1":"Original result"}}'}}]})
    result = qa.answer_question("结果？", retrieval_mode="framework", transport=httpx.MockTransport(generate))
    assert result["retrieval"]["images"] == 0
    assert result["retrieval"]["usage"] == {"keyword_calls": 0, "generation_calls": 2,
                                            "reported_tokens": 20, "unreported_calls": 1}
    assert result["warnings"] and result["sources"][0]["verified_quote"] == "Original result"
