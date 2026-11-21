"""Classification integrity and filtering semantics; no model calls or credentials."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kg_pilot.classification import (CATEGORY_LABELS, MISSING, classified_papers,
                                     filter_papers, taxonomy)
from scripts.build_classification import validate_records, load_classification_inputs, prepare_record
from kg_pilot.pipeline import PILOT, read_json, jsonl
from web.server import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def examples():
    return [
        {"doc_id": "a", "year": 2024, "title": "Annealing experiment", "classification": {
            "primary_category": "post_bond_annealing", "related_stages": ["alignment_contact"],
            "routes": ["W2W"], "relevance": "core", "methods": ["experimental"]}},
        {"doc_id": "b", "year": 2025, "title": "Cleaning simulation", "classification": {
            "primary_category": "cleaning_activation", "related_stages": ["post_bond_annealing"],
            "routes": ["D2W"], "relevance": "core", "methods": ["simulation"]}},
        {"doc_id": "c", "year": 2025, "title": "Measurement method", "classification": {
            "primary_category": "characterization_evaluation", "related_stages": [],
            "routes": ["W2W", "D2W"], "relevance": "support", "methods": ["experimental"]}},
        {"doc_id": "d", "year": 2025, "title": "Unspecified route", "classification": {
            "primary_category": "integrated_flow", "related_stages": [],
            "routes": [], "relevance": "core", "methods": ["literature_review"]}},
    ]


def ids(rows):
    return [row["doc_id"] for row in rows]


def test_or_within_facet_and_across_facets(examples):
    assert ids(filter_papers(examples, facets={"routes": ["W2W", "D2W"], "relevance": ["core"]})) == ["a", "b"]
    assert ids(filter_papers(examples, facets={"routes": ["D2W"], "methods": ["experimental"]})) == ["c"]
    assert ids(filter_papers(examples, facets={"routes": [MISSING]})) == ["d"]


def test_stage_associations_do_not_duplicate_or_replace_primary(examples):
    assert ids(filter_papers(examples, categories=["post_bond_annealing"])) == ["a"]
    assert ids(filter_papers(examples, categories=["post_bond_annealing", "alignment_contact"], include_related=True)) == ["a", "b"]
    assert taxonomy(examples)["category_counts"]["post_bond_annealing"] == 1
    assert taxonomy(examples)["related_category_counts"]["post_bond_annealing"] == 2


def test_taxonomy_lists_defined_options_with_zero_counts(examples):
    options = {facet["id"]: facet["options"] for facet in taxonomy(examples)["facets"]}
    assert [(option["value"], option["count"]) for option in options["relevance"]] == [
        ("core", 3), ("support", 1), ("background", 0), ("peripheral", 0),
    ]
    assert [(option["value"], option["count"]) for option in options["routes"]] == [
        ("W2W", 2), ("D2W", 2), ("D2D", 0), (MISSING, 1),
    ]


def test_empty_selection_reset_unknown_and_keyword_year(examples):
    assert filter_papers(examples, facets={"routes": []}, categories=[]) == examples
    assert filter_papers(examples, facets={"routes": ["unknown"]}) == []
    assert filter_papers(examples, categories=["unknown"]) == []
    assert ids(filter_papers(examples, query="measurement", year=2025)) == ["c"]
    assert filter_papers(examples, query="measurement", year=2024) == []
    with pytest.raises(ValueError):
        filter_papers(examples, facets={"invented": ["x"]})


def test_replaced_pdf_does_not_inherit_old_labels(tmp_path):
    (tmp_path / "catalog").mkdir()
    record = {"doc_id": "a", "source_sha256": "old", "primary_category": "alignment_contact"}
    (tmp_path / "catalog/classifications.json").write_text(json.dumps({"version": "1.0", "records": [record]}), encoding="utf-8")
    assert classified_papers([{"doc_id": "a", "sha256": "new"}], [], tmp_path)[0]["classification"] is None
    assert taxonomy(classified_papers([{"doc_id": "new"}], [], tmp_path))["unclassified"] == 1


def test_real_catalog_coverage_and_document_claim_boundary():
    client = TestClient(app)
    catalog = read_json(PILOT / "catalog/papers.json")
    rows = client.get("/api/papers").json()
    assert len(rows) == len(catalog)
    assert {row["doc_id"] for row in rows} == {row["doc_id"] for row in catalog}
    assert all(row["classification"]["primary_category"] in CATEGORY_LABELS for row in rows)
    by_id = {row["doc_id"]: row for row in rows}
    assert by_id["imec_05"]["classification"]["primary_category"] == "characterization_evaluation"
    assert by_id["imec_05"]["classification"]["relevance"] == "support"
    assert by_id["imec_02"]["classification"]["relevance"] == "support"
    assert by_id["reliability_yap_yield_2025"]["classification"]["publication_status"] == "preprint"
    claims = client.get("/api/claims").json()
    saved_claims = jsonl(PILOT / "annotations/claims.jsonl")
    assert len(claims) == len(saved_claims)
    assert {claim["claim_id"] for claim in claims} == {claim["claim_id"] for claim in saved_claims}
    assert {claim["stage"] for claim in claims} >= {"measurement", "reliability_test", "surface_preparation"}
    assert all("classification" not in claim for claim in claims)
    definitions = client.get("/api/taxonomy").json()
    assert definitions["classified"] == definitions["total"] == len(catalog)
    assert sum(definitions["category_counts"].values()) == len(catalog)
    topics = next(f for f in definitions["facets"] if f["id"] == "topics")
    assert len(topics["options"]) == 21
    pumping = by_id["imec_06"]["classification"]
    assert "表面形貌与均匀性" in pumping["topics"] and pumping["keywords"]
    assert by_id["reliability_gao_d2w_2021"]["classification"]["routes"] == ["D2W"]


def test_api_repeated_facets_stage_and_pdf_links():
    client = TestClient(app)
    params = [("category", "characterization_evaluation"), ("relevance", "support"),
              ("relevance", "core"), ("methods", "experimental")]
    response = client.get("/api/papers", params=params)
    assert response.status_code == 200
    rows = response.json()
    assert any(row["doc_id"] == "imec_05" for row in rows)
    for row in rows:
        c = row["classification"]
        assert c["primary_category"] == "characterization_evaluation"
        assert c["relevance"] in {"core", "support"} and "experimental" in c["methods"]
        assert row["has_pdf"]
        assert "source_sha256" not in c and "local_path" not in row
    assert client.get("/api/papers", params={"year": "not-a-year"}).status_code == 422
    assert client.get("/api/papers", params={"category": "absent"}).json() == []


def test_validator_rejects_missing_coverage_and_fabricated_quote():
    raw = json.loads((ROOT / "pilot/catalog/classifications.json").read_text(encoding="utf-8"))["records"]
    row = next(r for r in raw if r["doc_id"] == "imec_05")
    paper = {"doc_id": "imec_05", "page_count": 30}
    assert any("覆盖" in error for error in validate_records([paper], [], {}, check_files=False))
    assert any("原文未匹配" in error for error in validate_records([paper], [row], {}, check_files=False))


@pytest.mark.parametrize("review_status", ["independent_agent_reviewed", "api_reviewed", "api_model_reviewed"])
def test_validator_accepts_review_markers_from_published_batches(review_status):
    row = next(row for row in read_json(PILOT / "catalog/classifications.json")["records"] if row["doc_id"] == "imec_05")
    row["classification_review"] = review_status
    paper = next(row for row in read_json(PILOT / "catalog/papers.json") if row["doc_id"] == "imec_05")
    sources = {(row["doc_id"], evidence["pdf_page"]): [evidence["quote"]] for evidence in row["evidence"]}
    # Multiple quotes on the same physical page must all be available.
    for evidence in row["evidence"]:
        sources[(row["doc_id"], evidence["pdf_page"])].append(evidence["quote"])
    assert not validate_records([paper], [row], sources, check_files=False)


def test_topic_rebuild_preserves_batch_keywords_and_original_review_date():
    vocabulary = {"表面形貌与均匀性": ["Cu凹陷", "铜凹陷"]}
    paper = {"doc_id": "example", "sha256": "source-sha"}
    legacy = {"doc_id": "example", "topics": ["Cu凹陷", "铜凹陷"]}
    previous = {"source_sha256": "source-sha", "classified_date": "2025-01-02"}
    result = prepare_record(legacy, paper, vocabulary, previous=previous, today="2026-11-03")
    assert result["topics"] == ["表面形貌与均匀性"]
    assert result["keywords"] == ["Cu凹陷", "铜凹陷"]
    assert result["classified_date"] == "2025-01-02"
    grouped = dict(legacy, topics=["表面形貌与均匀性"], keywords=["新批次的精确细词"], classified_date="2026-03-04")
    result = prepare_record(grouped, paper, vocabulary, previous=previous, today="2026-11-03")
    assert result["topics"] == ["表面形貌与均匀性"]
    assert result["keywords"] == ["新批次的精确细词"]
    assert result["classified_date"] == "2026-03-04"
    assert grouped["keywords"] == ["新批次的精确细词"]


def test_all_rebuild_inputs_cover_live_catalog_without_writing():
    papers = read_json(PILOT / "catalog/papers.json")
    records = load_classification_inputs()
    assert len(records) == len(papers)
    assert {row["doc_id"] for row in records} == {paper["doc_id"] for paper in papers}


def test_classification_inputs_include_published_batch_sidecars(monkeypatch):
    from scripts import build_classification as builder
    fixtures = {
        "classification_process.json": [{"doc_id": "original_process"}],
        "classification_reliability.json": [{"doc_id": "original_reliability"}],
        "new_batch.json": {"version": "1.0", "records": [{"doc_id": "new_document"}]},
    }
    monkeypatch.setattr(builder, "read_json", lambda path: fixtures[path.name])
    monkeypatch.setattr(Path, "glob", lambda self, pattern: [Path("new_batch.json")])
    assert [row["doc_id"] for row in builder.load_classification_inputs()] == [
        "original_process", "original_reliability", "new_document"]
