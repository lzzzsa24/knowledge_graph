"""Offline retrieval checks using an independent synthetic technical corpus."""
import hashlib
import json
import sqlite3

import pytest

from kg_pilot import retrieval
from kg_pilot.bilingual_retrieval import concept_coverage, plan_query, title_documents


@pytest.fixture
def corpus(tmp_path):
    for directory in ("catalog", "annotations", "indexes"):
        (tmp_path / directory).mkdir()
    papers = [
        {"doc_id": "stack", "title": "Assembly and Investigation of Thin Stacked Components"},
        {"doc_id": "other", "title": "Survey of Novel Layered Semiconductor Materials", "authors": ["Jules Vale"]},
        {"doc_id": "rough", "title": "Topography Characterization of Processed Substrates"},
    ]
    rows = [
        ("stack:p1", "stack", 1, "Assembly and Investigation of Thin Stacked Components. Jules Vale and Mira Lee."),
        ("stack:p3", "stack", 3, "The three-layer stack has a cumulative void-free yield of 67%."),
        ("stack:p4", "stack", 4, "Each die is 45 micrometers thick. TSV diameter is 8 micrometers and via pitch is 24 micrometers."),
        ("other:p1", "other", 1, "Survey of Novel Layered Semiconductor Materials. Jules Vale."),
        ("other:p2", "other", 2, "The thermal conductivity of the layered material increased after annealing."),
        ("rough:p2", "rough", 2, "The roughness after polishing was 0.7 nm RMS."),
        ("rough:p3", "rough", 3, "Introduction to hybrid bonding and semiconductor assembly."),
    ]
    (tmp_path / "catalog/papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (tmp_path / "annotations/claims.jsonl").write_text("", encoding="utf-8")
    with sqlite3.connect(tmp_path / "indexes/pilot.sqlite") as db:
        db.execute("CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY,doc_id TEXT,pdf_page INTEGER,text TEXT)")
        db.execute("CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED,doc_id UNINDEXED,text,tokenize='unicode61')")
        db.executemany("INSERT INTO evidence VALUES(?,?,?,?)", rows)
        db.executemany("INSERT INTO evidence_fts VALUES(?,?,?)", [(r[0], r[1], r[3]) for r in rows])
    return tmp_path, papers, rows


def test_chinese_quantity_recall_without_model_or_reviewed_claims(corpus):
    pilot, _, rows = corpus
    before = hashlib.sha256((pilot / "indexes/pilot.sqlite").read_bytes()).hexdigest()
    hits = retrieval.retrieve("三层堆叠的累计无空洞良率是多少？", pilot=pilot, limit=5)
    assert hits[0]["evidence_id"] == "stack:p3"
    assert hits[0]["text"] == rows[1][3]
    assert {r["method"] for r in hits[0]["retrieval_reasons"]} >= {"bilingual_fts", "domain_query_rerank"}
    assert hashlib.sha256((pilot / "indexes/pilot.sqlite").read_bytes()).hexdigest() == before


def test_dense_and_lexical_use_one_vote_each_and_rehydrate_source(corpus):
    pilot, _, rows = corpus
    dense = [{"evidence_id": "rough:p3", "doc_id": "wrong", "text": "embedding wrapper", "score": .8},
             {"evidence_id": "stack:p3", "doc_id": "wrong", "text": "summary, not evidence", "score": .7}]
    hits = retrieval.retrieve("三层堆叠无空洞良率", dense_results=dense, pilot=pilot, limit=6)
    assert hits[0]["evidence_id"] == "stack:p3"
    assert hits[0]["text"] == rows[1][3] and hits[0]["doc_id"] == "stack"
    assert len({hit["evidence_id"] for hit in hits}) == len(hits)
    assert all(hit["rrf_score"] <= 2 / 61 for hit in hits)
    assert "rough:p3" in {hit["evidence_id"] for hit in hits}


def test_title_scope_excludes_title_words_from_intent(corpus):
    pilot, papers, _ = corpus
    title = papers[0]["title"]
    plan = plan_query(f"《{title}》的芯片多厚，通孔直径和间距是多少？", papers)
    assert plan.title_docs == {"stack"} and "assembly" not in plan.intent
    hits = retrieval.retrieve(plan.original, pilot=pilot)
    assert hits[0]["evidence_id"] == "stack:p4"
    assert {"method": "document_scoped_fts", "exact_title": True} in hits[0]["retrieval_reasons"]


def test_known_catalog_author_can_recover_legacy_missing_metadata(corpus):
    pilot, _, _ = corpus
    hits = retrieval.retrieve("Vale那篇论文的芯片多厚，通孔直径和间距是多少？", pilot=pilot)
    assert hits[0]["evidence_id"] == "stack:p4"
    assert any(r["method"] == "document_scoped_fts" for r in hits[0]["retrieval_reasons"])
    # Identity is a boost rather than a corpus filter.
    assert "other:p2" in {h["evidence_id"] for h in retrieval.retrieve("Vale退火后热导如何变化？", pilot=pilot)}


def test_latest_explicit_identity_supersedes_history(corpus):
    _, papers, _ = corpus
    question = f"《{papers[0]['title']}》的良率？\n《{papers[2]['title']}》的粗糙度？"
    plan = plan_query(question, papers)
    assert plan.entity_docs == {"rough"} and plan.title_docs == {"rough"}
    assert "良率" not in plan.intent


def test_quantity_bridge_uses_labels_not_numeric_answers_or_quotes():
    claim = {"quantity": "偏振角度_polarization_angle", "value_text": "73 degrees",
             "statement": "任意陈述", "evidence": [{"quote": "Secret answer is 73 degrees."}]}
    plan = plan_query("偏振角度是多少？", claims=[claim])
    assert "polarization" in plan.phrases and "angle" in plan.phrases
    assert not any("73" in p or "secret" in p for p in plan.phrases)
    assert concept_coverage(plan, "The polarization angle was measured.")[0] > 0


def test_english_semantic_order_and_original_values_are_preserved(monkeypatch):
    dense = [{"evidence_id": "b", "text": "original ﬁlm", "score": .2},
             {"evidence_id": "a", "text": "original second", "score": .1}]
    calls = []
    monkeypatch.setattr(retrieval, "semantic", lambda q, model, limit: calls.append((q, model, limit)) or dense)
    hits = retrieval.retrieve("What is the roughness?", "model", limit=2)
    assert calls == [("What is the roughness?", "model", 30)]
    assert [h["evidence_id"] for h in hits] == ["b", "a"]
    assert hits[0]["text"] == dense[0]["text"] and hits[0]["score"] == .2


def test_dense_failure_is_visible_and_does_not_trigger_hidden_fallback(monkeypatch):
    def fail(*args):
        raise RuntimeError("embedding unavailable")
    monkeypatch.setattr(retrieval, "semantic", fail)
    monkeypatch.setattr(retrieval, "lexical", lambda *a: pytest.fail("hidden fallback"))
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        retrieval.retrieve("粗糙度是多少？", object())


def test_fts_operator_like_question_remains_literal(corpus):
    pilot, _, _ = corpus
    hits = retrieval.retrieve('粗糙度 OR " ); DROP TABLE evidence; --', pilot=pilot)
    assert any(h["evidence_id"] == "rough:p2" for h in hits)
    with sqlite3.connect(pilot / "indexes/pilot.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 7


def test_missing_fts_preserves_dense_evidence_and_empty_offline_fallback(corpus):
    pilot, _, _ = corpus
    with sqlite3.connect(pilot / "indexes/pilot.sqlite") as db:
        db.execute("DROP TABLE evidence_fts")
    dense = [{"evidence_id": "rough:p2", "text": "wrapper"}]
    assert retrieval.retrieve("粗糙度", dense_results=dense, pilot=pilot)[0]["text"].startswith("The roughness")
    assert retrieval.retrieve("粗糙度", pilot=pilot) == []


def test_shared_title_matcher_folds_ligatures_and_keeps_comparison():
    papers = {"one": {"title": "Modiﬁed Surfaces for Heterogeneous Integration"},
              "two": {"title": "Chemical Treatment of Compound Semiconductor Surfaces"}}
    docs, intent = title_documents("Modified Surfaces for Heterogeneous Integration 与 Chemical Treatment of Compound Semiconductor Surfaces 的试样", papers)
    assert docs == {"one", "two"} and "试样" in intent
    assert title_documents("compound semiconductor", papers)[0] == set()


def test_phrase_scoring_preserves_repeated_words_and_real_adjacency():
    plan = plan_query("接触电阻")
    assert concept_coverage(plan, "contact resistance at the first and second contact resistance")[0] == 1
    assert concept_coverage(plan, "contact force measurements and electrical resistance")[0] == 0


def test_material_quantity_translations_remain_distinct():
    assert "resistivity" in plan_query("电阻率").phrases
    assert "resistance" not in plan_query("电阻率").phrases
    assert "thermal resistance" in plan_query("热阻").phrases
    assert "thermal conductivity" not in plan_query("热阻").phrases
    assert "temperature cycling" in plan_query("温度循环").phrases


def test_result_prefix_is_stable_across_web_qa_budgets(corpus):
    pilot, _, _ = corpus
    query = "三层堆叠的累计无空洞良率是多少？"
    small = retrieval.retrieve(query, pilot=pilot, limit=3)
    large = retrieval.retrieve(query, pilot=pilot, limit=50)
    assert [h["evidence_id"] for h in small] == [h["evidence_id"] for h in large[:3]]


def test_strong_lexical_passage_beats_broad_dual_channel_agreement(corpus):
    pilot, _, _ = corpus
    broad = "The stack yield is a common assembly topic."
    with sqlite3.connect(pilot / "indexes/pilot.sqlite") as db:
        db.execute("UPDATE evidence SET text=? WHERE evidence_id='rough:p3'", (broad,))
        db.execute("UPDATE evidence_fts SET text=? WHERE evidence_id='rough:p3'", (broad,))
    dense = [{"evidence_id": "rough:p3", "doc_id": "rough", "text": broad, "score": .9}]
    hits = retrieval.retrieve("三层堆叠的累计无空洞良率", dense_results=dense, pilot=pilot)
    assert hits[0]["evidence_id"] == "stack:p3"


def test_rare_catalog_model_is_identity_but_generic_materials_are_not():
    papers = {"instrument": {"title": "PXV570 Automated Processing System"},
              "science": {"title": "D2W Hybrid Bonding of SiO2 Interfaces"}}
    assert plan_query("PXV570的清洗过程", papers).identifier_docs == {"instrument"}
    assert not plan_query("D2W和SiO2的键合", papers).identifier_docs


def test_author_and_year_soft_priority_uses_catalog_metadata_only(corpus):
    pilot, papers, _ = corpus
    papers[0].update(authors=["Jules Vale"], year=2022)
    papers[1].update(year=2023)
    (pilot / "catalog/papers.json").write_text(json.dumps(papers), encoding="utf-8")
    # Both sources discuss the same quantity; a year mentioned in source text
    # is not a publication-year match and cannot steal the named paper.
    text = "The die thickness is 45 micrometers. A 2022 reference is cited."
    with sqlite3.connect(pilot / "indexes/pilot.sqlite") as db:
        for eid in ("stack:p4", "other:p2"):
            db.execute("UPDATE evidence SET text=? WHERE evidence_id=?", (text, eid))
            db.execute("UPDATE evidence_fts SET text=? WHERE evidence_id=?", (text, eid))
    hits = retrieval.retrieve("Vale在2022年那篇的芯片多厚？", pilot=pilot)
    assert hits[0]["doc_id"] == "stack"
    assert {"method": "catalog_publication_year", "year": 2022} in hits[0]["retrieval_reasons"]
    assert not any(r["method"] == "catalog_publication_year" for h in hits if h["doc_id"] == "other" for r in h["retrieval_reasons"])
    assert not plan_query("2022年论文的厚度？", papers).publication_years
