"""Offline source-local result/table/method retrieval regressions.

All texts and values below are synthetic. No provider, embedding model or pilot
data is read or modified by these tests.
"""
import json
import sqlite3

import pytest

from kg_pilot import qa


QUESTION = "哪些研究报告了五层堆叠的累计无空洞良率？整叠良率是多少，用什么方法测的？如有单层良率请单独列出。"


@pytest.fixture
def yield_corpus(tmp_path, monkeypatch):
    papers = [{"doc_id": name, "title": f"Synthetic study {name}"} for name in ("one", "two", "diverse")]
    result_one = "Table IV shows per-layer void free yield. The cumulative void free yield of the five-layer stack is 58%."
    result_two = "The cumulative void free yield of the five-layer stack is 72%. The per-layer yield ranged from 86% to 98%."
    table = "TABLE IV. VOID FREE YIELD PER LAYER\nLayer | Yield\nLayer 1 | 96%\nLayer 2 | 92%\nLayer 2 (CAP) | 88%"
    method_one = "A-VM imaging was performed to identify voids at the bonding interfaces and measure void free yield."
    method_two = "U-SAM was performed to identify voiding at any die interface. The stack was then examined by U-SAM."
    rows = [
        ("one:p3:b2", "one", 3, result_one),
        ("two:p4:b8", "two", 4, result_two),
        ("diverse:p1:b0", "diverse", 1, "Independent semantic evidence on another assembly study."),
        ("one:p3:table0", "one", 3, table),
        ("one:p3:table1", "one", 3, "TABLE II. cumulative void free yield\nAn unrelated earlier experiment | 41%"),
        ("one:p4:table0", "one", 4, table),
        ("two:p4:table0", "two", 4, "TABLE I. Shear strength\nSample | Strength\nA | 11 MPa"),
        ("one:p3:b1", "one", 3, method_one),
        ("two:p4:b3", "two", 4, method_two),
        ("two:p5:b3", "two", 5, method_two),
        ("one:p3:b9", "one", 3, "A-VM instrument purchasing and installation history."),
    ]
    claims = []
    for name, eid, quote, method in [("one", "one:p3:b2", result_one, "AVM"),
                                      ("two", "two:p4:b8", result_two, "USAM")]:
        claims.append({"claim_id": name + "-yield", "doc_id": name,
                       "statement": "五层堆叠累计无空洞良率，逐层良率分别统计。",
                       "quantity": "cumulative_void_free_yield", "conditions": {"method": method, "stack_die_count": 5},
                       "evidence": [{"evidence_id": eid, "quote": quote}]})
    for directory in ("catalog", "annotations", "indexes"):
        (tmp_path / directory).mkdir()
    (tmp_path / "catalog/papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (tmp_path / "annotations/claims.jsonl").write_text("\n".join(json.dumps(c) for c in claims), encoding="utf-8")
    with sqlite3.connect(tmp_path / "indexes/pilot.sqlite") as db:
        db.execute("CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY,doc_id TEXT,pdf_page INTEGER,text TEXT)")
        db.executemany("INSERT INTO evidence VALUES(?,?,?,?)", rows)
    monkeypatch.setattr(qa, "PILOT", tmp_path)
    monkeypatch.setattr(qa, "retrieval_model", lambda: object())
    hits = [{"evidence_id": row[0], "doc_id": row[1]} for row in rows[:3]]
    monkeypatch.setattr(qa, "retrieve", lambda query, model, limit: hits)
    return {"rows": {r[0]: r for r in rows}, "table": table, "claims": claims}


def test_recovers_referenced_whole_table_and_actual_method_paragraphs(yield_corpus):
    sources, warnings = qa.retrieve_sources(QUESTION)
    by_eid = {source["evidence_id"]: source for source in sources}
    assert not warnings
    assert {"one:p3:table0", "one:p3:b1", "two:p4:b3"} <= by_eid.keys()
    assert by_eid["one:p3:table0"]["text"] == yield_corpus["table"]
    assert qa._source_context(by_eid["one:p3:table0"])["text"] == yield_corpus["table"]
    table_reason = next(r for r in by_eid["one:p3:table0"]["retrieval_reasons"]
                        if r["method"] == "same_page_result_table")
    assert table_reason["explicit_table_reference"] is True
    assert table_reason["anchor_evidence_id"] == "one:p3:b2"
    for eid in ("one:p3:b1", "two:p4:b3"):
        assert any(r["method"] == "same_page_measurement_method" for r in by_eid[eid]["retrieval_reasons"])
    # Companion discovery is not a review/annotation operation: do not attach
    # an assertion to a paragraph it did not originally cite.
    assert by_eid["two:p4:b3"]["reviewed_claims"] == []
    assert {"one:p3:table1", "one:p4:table0", "two:p4:table0", "two:p5:b3", "one:p3:b9"}.isdisjoint(by_eid)
    # Both original sources and semantic diversity survive the local expansion.
    assert {"one:p3:b2", "two:p4:b8", "diverse:p1:b0"} <= by_eid.keys()
    assert len(sources) <= 20 and [s["id"] for s in sources] == [f"S{i}" for i in range(1, len(sources) + 1)]
    assert sources.index(by_eid["one:p3:table0"]) < sources.index(by_eid["two:p4:b8"])
    for source in sources:
        assert source["text"] == yield_corpus["rows"][source["evidence_id"]][3]


def test_method_expansion_requires_method_intent(yield_corpus):
    sources, _ = qa.retrieve_sources("哪些研究报告了五层累计无空洞良率和单层良率？")
    by_eid = {source["evidence_id"]: source for source in sources}
    assert "one:p3:table0" in by_eid
    assert {"one:p3:b1", "two:p4:b3"}.isdisjoint(by_eid)


def test_companions_need_a_matched_assertion_not_only_a_semantic_hit(yield_corpus):
    sources, _ = qa.retrieve_sources("What is the laboratory address?")
    assert [source["evidence_id"] for source in sources] == ["one:p3:b2", "two:p4:b8", "diverse:p1:b0"]


def test_unreferenced_table_needs_metric_overlap(yield_corpus):
    with sqlite3.connect(qa.PILOT / "indexes/pilot.sqlite") as db:
        db.execute("UPDATE evidence SET text=? WHERE evidence_id=?",
                   ("The cumulative void free yield of the stack is 58%.", "one:p3:b2"))
        db.execute("UPDATE evidence SET text=? WHERE evidence_id=?",
                   ("TABLE II. Throughput per layer\nLayer | Wafers per hour\n1 | 33", "one:p3:table1"))
    sources, _ = qa.retrieve_sources(QUESTION)
    by_eid = {source["evidence_id"]: source for source in sources}
    assert "one:p3:table0" in by_eid and "one:p3:table1" not in by_eid
    reason = next(r for r in by_eid["one:p3:table0"]["retrieval_reasons"] if r["method"] == "same_page_result_table")
    assert reason["explicit_table_reference"] is False


def test_table_number_alone_does_not_make_an_unrelated_metric_relevant(yield_corpus):
    with sqlite3.connect(qa.PILOT / "indexes/pilot.sqlite") as db:
        db.execute("UPDATE evidence SET text=? WHERE evidence_id=?",
                   ("TABLE IV. Shear strength\nSample | Strength\nA | 11 MPa", "one:p3:table0"))
    sources, _ = qa.retrieve_sources(QUESTION)
    assert {"one:p3:table0", "one:p3:table1"}.isdisjoint(s["evidence_id"] for s in sources)


def test_companions_replace_low_priority_tail_when_original_twenty_slots_are_full(yield_corpus, monkeypatch):
    # Semantic sources plus multi-page reviewed assertions fill all 20 slots
    # before any source-local expansion. Relevant companions still need space.
    papers = json.loads((qa.PILOT / "catalog/papers.json").read_text(encoding="utf-8"))
    claims = list(yield_corpus["claims"])
    hits = []
    with sqlite3.connect(qa.PILOT / "indexes/pilot.sqlite") as db:
        for index in range(8):
            doc_id = f"semantic-{index}"
            papers.append({"doc_id": doc_id, "title": "Independent synthetic study"})
            eid = doc_id + ":p1:b0"
            db.execute("INSERT INTO evidence VALUES(?,?,?,?)", (eid, doc_id, 1, "Other semantic source text."))
            hits.append({"evidence_id": eid, "doc_id": doc_id})
        for index in range(6):
            doc_id = f"result-{index}"
            papers.append({"doc_id": doc_id, "title": "Another synthetic result"})
            eid = doc_id + ":p2:b0"
            quote = "Table V gives the cumulative void free yield of the five-layer stack."
            db.executemany("INSERT INTO evidence VALUES(?,?,?,?)", [
                (eid, doc_id, 2, quote),
                (doc_id + ":p3:b0", doc_id, 3, "Separate original sample conditions."),
                (doc_id + ":p2:table0", doc_id, 2, "TABLE V. VOID FREE YIELD\nLayer | Yield\n1 | 91%"),
                (doc_id + ":p2:b1", doc_id, 2, "METRO was used to measure the void free yield of each stack.")])
            claims.append({"claim_id": doc_id, "doc_id": doc_id, "statement": "五层堆叠累计无空洞良率，逐层良率分别统计。",
                           "quantity": "cumulative_void_free_yield", "conditions": {"method": "METRO"},
                           "evidence": [{"evidence_id": eid, "quote": quote},
                                        {"evidence_id": doc_id + ":p3:b0", "quote": "Separate original sample conditions."}]})
    (qa.PILOT / "catalog/papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (qa.PILOT / "annotations/claims.jsonl").write_text("\n".join(json.dumps(c) for c in claims), encoding="utf-8")
    monkeypatch.setattr(qa, "retrieve", lambda query, model, limit: hits)
    with monkeypatch.context() as baseline:
        baseline.setattr(qa, "_claim_companions", lambda *args, **kwargs: [])
        original_sources, _ = qa.retrieve_sources(QUESTION)
    assert len(original_sources) == 20
    original_anchors = {source["evidence_id"] for source in original_sources
                        if any(r["method"] == "reviewed_claim_terms" for r in source["retrieval_reasons"])}
    sources, _ = qa.retrieve_sources(QUESTION)
    companions = [s for s in sources if any(r["method"].startswith("same_page_") for r in s["retrieval_reasons"])]
    assert len(sources) == 20 and len(companions) == 4
    assert all(sum(s["doc_id"] == doc for s in companions) <= 2 for doc in {s["doc_id"] for s in companions})
    selected = {s["evidence_id"] for s in sources}
    assert original_anchors <= selected
    assert {"one:p3:table0", "one:p3:b1"} <= selected
    assert {hit["evidence_id"] for hit in hits[:4]} <= selected
    assert {hit["evidence_id"] for hit in hits[4:]}.isdisjoint(selected)


def test_already_retrieved_companions_gain_reason_and_move_beside_anchor(yield_corpus, monkeypatch):
    order = ["one:p3:table0", "one:p3:b1", "two:p4:b3", "diverse:p1:b0", "one:p3:b2", "two:p4:b8"]
    hits = [{"evidence_id": eid, "doc_id": yield_corpus["rows"][eid][1]} for eid in order]
    monkeypatch.setattr(qa, "retrieve", lambda query, model, limit: hits)
    sources, _ = qa.retrieve_sources(QUESTION)
    by_eid = {source["evidence_id"]: source for source in sources}
    assert len(by_eid) == len(sources) == len(hits)
    result_order = [source["evidence_id"] for source in sources]
    anchor = result_order.index("one:p3:b2")
    assert result_order[anchor + 1:anchor + 3] == ["one:p3:table0", "one:p3:b1"]
    assert result_order[result_order.index("two:p4:b8") + 1] == "two:p4:b3"
    for eid, method in [("one:p3:table0", "same_page_result_table"),
                        ("one:p3:b1", "same_page_measurement_method"),
                        ("two:p4:b3", "same_page_measurement_method")]:
        reasons = {reason["method"] for reason in by_eid[eid]["retrieval_reasons"]}
        assert {"semantic_or_fts", method} <= reasons
