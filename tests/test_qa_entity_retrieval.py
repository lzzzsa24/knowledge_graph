"""Offline regressions for identity-aware retrieval of reviewed evidence."""
import json
import sqlite3

import httpx
import pytest

from kg_pilot import qa


@pytest.fixture(autouse=True)
def isolated_provider_configuration(tmp_path, monkeypatch):
    """Synthetic generation checks must never read the user's API settings."""
    for name in ("PARATERA_API_KEY", "KG_LLM_API_KEY", "PARATERA_MODEL", "OPENAI_API_KEY",
                 "KG_LLM_BASE_URL", "KG_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(qa, "CONFIG_PATH", tmp_path / "llm_config.json")
    monkeypatch.setattr(qa, "SECRET_PATH", tmp_path / "secrets" / "fake.key.dpapi")

    def forbid_outbound_http(*args, **kwargs):
        pytest.fail("Entity retrieval tests must use MockTransport; outbound HTTP is forbidden")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_outbound_http)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    papers = [
        {"doc_id": "metrology", "title": "Surface metrology application note",
         "provenance": {"publisher": "Novara Instruments", "document_number": "NI-M207",
                        "version": "Rev C", "document_type": "application_note"}},
        {"doc_id": "qualification", "title": "Module qualification",
         "publisher": "Automotive Qualification Consortium (AQC)", "document_number": "AQC-R731",
         "version": "Rev B", "source_type": "standard"},
        {"doc_id": "other", "title": "Hybrid bonding research", "publisher": "Elara Research"},
        {"doc_id": "generic", "title": "Another hybrid bonding study", "publisher": "Hybrid Bonding Materials"},
    ]
    late_quote = "Profiling throughput exceeds 31 wafers per hour at six measurement sites."
    body = "Introductory material. " * 260 + late_quote + " Further instrument context."
    rows = [("note:title", "metrology", 1, "Surface metrology application note. Contents."),
            ("note:end", "metrology", 9, "Conclusions and references."),
            ("note:result", "metrology", 6, body),
            ("note:conditions", "metrology", 7, "Use six measurement sites at the wafer edge."),
            ("rule:title", "qualification", 1, "Module qualification. Contents."),
            ("rule:scope", "qualification", 4, "This standard applies to completed automotive modules."),
            ("other:page", "other", 3, "A separate research source describing bonding experiments.")]
    claims = []
    # The relevant sixth assertion shares a source with five irrelevant ones.
    for i in range(5):
        claims.append({"claim_id": f"unrelated-{i}", "doc_id": "metrology", "statement": "扫描探针安装方法。",
                       "quantity": "安装方法", "conditions": {},
                       "evidence": [{"evidence_id": "note:result", "quote": "Introductory material."}]})
    claims += [
        {"claim_id": "throughput", "doc_id": "metrology", "statement": "仪器的晶圆吞吐量超过31片每小时。",
         "quantity": "晶圆吞吐量", "conditions": {"sites": "六个测量点", "location": "晶圆边缘"},
         "claim_type": "manufacturer_specification", "evidence": [
             {"evidence_id": "note:result", "quote": late_quote, "pdf_page": 6},
             {"evidence_id": "note:conditions", "quote": "Use six measurement sites at the wafer edge.", "pdf_page": 7}]},
        {"claim_id": "module-scope", "doc_id": "qualification", "statement": "规范适用对象为完成制造的汽车模块。",
         "quantity": "规范适用对象", "conditions": {"scope": "成品模块级"}, "claim_type": "standard_requirement",
         "evidence": [{"evidence_id": "rule:scope", "quote": "This standard applies to completed automotive modules.", "pdf_page": 4}]},
        {"claim_id": "other-result", "doc_id": "other", "statement": "独立研究报告键合实验。",
         "quantity": "键合实验", "conditions": {},
         "evidence": [{"evidence_id": "other:page", "quote": "A separate research source describing bonding experiments.", "pdf_page": 3}]},
    ]
    (tmp_path / "catalog").mkdir()
    (tmp_path / "annotations").mkdir()
    (tmp_path / "indexes").mkdir()
    (tmp_path / "catalog/papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (tmp_path / "annotations/claims.jsonl").write_text("\n".join(json.dumps(c) for c in claims), encoding="utf-8")
    with sqlite3.connect(tmp_path / "indexes/pilot.sqlite") as db:
        db.execute("CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY,doc_id TEXT,pdf_page INTEGER,text TEXT)")
        db.executemany("INSERT INTO evidence VALUES(?,?,?,?)", rows)
    monkeypatch.setattr(qa, "PILOT", tmp_path)
    monkeypatch.setattr(qa, "retrieval_model", lambda: object())
    hits = [{"evidence_id": "note:title", "doc_id": "metrology"},
            {"evidence_id": "note:end", "doc_id": "metrology"},
            {"evidence_id": "other:page", "doc_id": "other"}]
    calls = []

    def semantic(query, model, limit):
        calls.append((query, limit))
        # The actual result and condition pages are absent from all 30 hits.
        return (hits * 10)[:limit]

    monkeypatch.setattr(qa, "retrieve", semantic)
    return {"papers": {p["doc_id"]: p for p in papers}, "claims": claims, "calls": calls,
            "quote": late_quote, "body": body}


@pytest.mark.parametrize("question", [
    "Novara的晶圆吞吐量适用于什么条件？",
    "NI M207的晶圆吞吐量适用于什么条件？",
    "What throughput does Novara report, and at how many sites?",
])
def test_reviewed_record_recovers_all_evidence_outside_semantic_top30(corpus, question):
    sources, warnings = qa.retrieve_sources(question)
    assert not warnings and corpus["calls"][-1][1] == 30
    by_eid = {s["evidence_id"]: s for s in sources}
    assert {"note:result", "note:conditions", "other:page"} <= by_eid.keys()
    assert sources[0]["evidence_id"] == "note:result"
    for eid in ("note:result", "note:conditions"):
        assert "throughput" in {c["claim_id"] for c in by_eid[eid]["reviewed_claims"]}
        reason = next(r for r in by_eid[eid]["retrieval_reasons"] if r["method"] == "reviewed_claim_terms")
        assert reason["terms"] and reason["entity_match"] is True
    assert len(sources) <= 20


def test_matched_sixth_claim_and_late_verbatim_quote_survive_context(corpus):
    sources, _ = qa.retrieve_sources("Novara的晶圆吞吐量是多少？")
    source = next(s for s in sources if s["evidence_id"] == "note:result")
    assert source["text"] == corpus["body"]  # Retain the original SQLite source.
    entry = qa._source_context(source)
    assert corpus["quote"] in entry["text"]
    assert entry["text_truncated"] is True and len(entry["text"]) <= 3500
    matched = next(c for c in entry["reviewed_claims"] if c["claim_id"] == "throughput")
    assert matched["evidence_quotes"] == [corpus["quote"]]
    assert (entry["publisher"], entry["document_number"], entry["version"], entry["document_type"]) == (
        "Novara Instruments", "NI-M207", "Rev C", "application_note")


@pytest.mark.parametrize("question", ["AQC-R731适用于什么对象？", "What is the scope of AQC-R731?"])
def test_document_identity_returns_scope_page_with_original_standard_type(corpus, question):
    sources, _ = qa.retrieve_sources(question)
    source = next(s for s in sources if s["evidence_id"] == "rule:scope")
    assert source["document_type"] == "standard" and source["version"] == "Rev B"
    assert source["reviewed_claims"][0]["claim_type"] == "standard_requirement"


def test_generic_subject_is_not_an_entity_and_keeps_semantic_diversity(corpus):
    question = "hybrid bonding材料的键合实验有哪些？"
    assert qa._entity_documents(question, corpus["papers"])[0] == set()
    sources, _ = qa.retrieve_sources(question)
    assert sources[0]["evidence_id"] == "note:title"
    assert {"metrology", "other"} <= {s["doc_id"] for s in sources}


def test_comparison_resolves_both_named_entities_without_filtering_others(corpus):
    question = "比较Novara的晶圆吞吐量与AQC-R731规范适用对象。"
    docs, _ = qa._entity_documents(question, corpus["papers"])
    assert docs == {"metrology", "qualification"}
    sources, _ = qa.retrieve_sources(question)
    assert {"note:result", "note:conditions", "rule:scope", "other:page"} <= {s["evidence_id"] for s in sources}


def test_full_publisher_name_can_identify_an_institution_without_acronym_in_question():
    papers = {"institute": {"publisher": "National Physical Laboratory (NPL)"},
              "generic": {"publisher": "Hybrid Bonding Materials"}}
    assert qa._entity_documents("National Physical Laboratory的指南", papers)[0] == {"institute"}
    assert qa._entity_documents("hybrid bonding材料", papers)[0] == set()


def test_assistant_history_cannot_supply_identity_or_claim_matches(corpus):
    sources, _ = qa.retrieve_sources("hybrid bonding实验", [{"role": "assistant", "content": "Novara吞吐量"}])
    assert corpus["calls"][-1][0] == "hybrid bonding实验"
    assert sources[0]["evidence_id"] == "note:title"
    assert "note:result" not in {s["evidence_id"] for s in sources}


def test_quote_matching_allows_only_whitespace_changes():
    text = "Prefix. Throughput\n>31 WPH\twith six sites. Suffix."
    span = qa._quote_span(text, "Throughput >31 WPH with six sites.")
    assert span and text[slice(*span)] == "Throughput\n>31 WPH\twith six sites."
    assert qa._quote_span(text, "Throughput >45 WPH with six sites.") is None


def test_full_title_separates_identity_from_sample_intent_and_publisher_words(corpus, monkeypatch):
    title = "Chemical Mechanical Profiling of Modiﬁed Interfaces for Advanced Chip Assembly"
    corpus["papers"]["metrology"]["title"] = title
    corpus["papers"]["other"]["publisher"] = "American Chemical Society"
    (qa.PILOT / "catalog/papers.json").write_text(
        json.dumps(list(corpus["papers"].values())), encoding="utf-8")
    quote = "Six-inch wafers were diced into 7 mm × 9 mm chips for bonding experiments."
    claim = {"claim_id": "sample-geometry", "doc_id": "metrology",
             "statement": "处理后试样成功连接。", "quantity": "连接结果",
             "sample_id": "6英寸晶圆切出的7×9mm芯片试样", "material_system": "金属/介质",
             "conditions": {}, "claim_type": "reported_experiment",
             "evidence": [{"evidence_id": "note:sample", "pdf_page": 8, "quote": quote}]}
    claims = [*corpus["claims"], claim]
    (qa.PILOT / "annotations/claims.jsonl").write_text(
        "\n".join(json.dumps(c) for c in claims), encoding="utf-8")
    with sqlite3.connect(qa.PILOT / "indexes/pilot.sqlite") as db:
        db.execute("INSERT INTO evidence VALUES(?,?,?,?)", ("note:sample", "metrology", 8, quote))
    question = "《Chemical Mechanical Profiling of Modified Interfaces for Advanced Chip Assembly》实际试样尺寸是整片晶圆还是芯片？"
    docs, intent = qa._title_documents(question, corpus["papers"])
    assert docs == {"metrology"} and "chemical" not in intent
    assert qa._entity_documents(intent, corpus["papers"])[0] == set()
    sources, warnings = qa.retrieve_sources(question)
    assert not warnings and corpus["calls"][-1][0] == question
    assert sources[0]["evidence_id"] == "note:sample"
    assert {"method": "exact_document_title"} in sources[0]["retrieval_reasons"]
    matched = next(c for c in sources[0]["reviewed_claims"] if c["claim_id"] == "sample-geometry")
    assert matched["evidence_quotes"] == [quote]
    assert "other:page" in {s["evidence_id"] for s in sources}  # Identity is a boost, not a filter.
    monkeypatch.setattr(qa, "provider_status", lambda: {"configured": True, "provider": "fake", "model": "fake-v4"})
    monkeypatch.setattr(qa, "_api_key", lambda config=None: "fake-key")
    monkeypatch.setenv("PARATERA_MODEL", "fake-v4")
    monkeypatch.setattr(qa, "_format_event", lambda **kwargs: None)

    def handler(request):
        contexts = json.loads(json.loads(request.content)["messages"][-1]["content"])["evidence"]
        assert contexts[0]["doc_id"] == "metrology" and quote in contexts[0]["text"]
        assert sum(len(json.dumps(c, ensure_ascii=False)) for c in contexts) <= 42000
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"answer": "试样为切割后的芯片。[S1]", "answerability": "supported"})}}]})

    result = qa.answer_question(question, transport=httpx.MockTransport(handler))
    assert result["generated"] and result["sources"][0]["evidence_id"] == "note:sample"


def test_full_title_matching_keeps_both_comparison_targets_and_rejects_partial_topics():
    papers = {"one": {"title": "Optical Profiling of Wafer Surfaces for Assembly"},
              "two": {"title": "Electrical Evaluation of Thin Film Contacts for Integration"},
              "generic": {"title": "Hybrid Bonding"}}
    question = "比较《Optical Profiling of Wafer Surfaces for Assembly》与《Electrical Evaluation of Thin Film Contacts for Integration》的试样。"
    docs, intent = qa._title_documents(question, papers)
    assert docs == {"one", "two"} and "试样" in intent
    assert qa._title_documents("hybrid bonding以及Optical Profiling的结果", papers)[0] == set()


def test_context_budget_and_existing_request_contract_are_preserved(monkeypatch):
    sources = [{"id": f"S{i}", "doc_id": f"d{i}", "title": "A study", "pdf_page": 2,
                "text": "原文" * 3000, "reviewed_claims": []} for i in range(1, 21)]
    monkeypatch.setattr(qa, "retrieve_sources", lambda *args: (sources, []))
    monkeypatch.setattr(qa, "provider_status", lambda: {"configured": True, "provider": "fake", "model": "fake-v4"})
    monkeypatch.setattr(qa, "_api_key", lambda config=None: "fake-key")
    monkeypatch.setenv("PARATERA_MODEL", "fake-v4")
    monkeypatch.setattr(qa, "_format_event", lambda **kwargs: None)
    payloads = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        entries = json.loads(payload["messages"][-1]["content"])["evidence"]
        assert sum(len(json.dumps(e, ensure_ascii=False)) for e in entries) <= 42000
        assert 0 < len(entries) < len(sources)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["stream"] is False and payload["temperature"] == 0.1
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"answer": "有来源的回答。[S1]", "answerability": "supported"})}}]})

    result = qa.answer_question("问题", transport=httpx.MockTransport(handler))
    assert result["generated"] and len(payloads) == 1
    ids = {e["id"] for e in json.loads(payloads[0]["messages"][-1]["content"])["evidence"]}
    assert {s["id"] for s in result["sources"]} == ids
