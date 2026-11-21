import json
from copy import deepcopy
from pathlib import Path

import pytest

from kg_pilot.map_relations import claims_digest
from kg_pilot.map_source_kinds import (
    load_source_kind_reviews,
    resolve_map_source_kind,
    source_kind_review,
)


def test_untouched_documents_use_conservative_metadata():
    def resolve(form, claim_type, **fields):
        paper = {"doc_id": "paper", "classification": {"document_form": form}, **fields}
        claim = {"doc_id": "paper", "claim_type": claim_type}
        return resolve_map_source_kind(paper, claim)

    assert resolve("patent", "reported_experiment") == "patent_disclosure"
    assert resolve("journal_paper", "author_interpretation", source_type="patent") == "patent_disclosure"
    assert resolve("datasheet", "reported_experiment") == "manufacturer"
    assert resolve("standard", "reported_experiment") == "technical_guidance"
    assert resolve("test_method", "reported_experiment") == "test_method"
    assert resolve("journal_paper", "test_method") == "test_method"
    assert resolve("presentation", "reported_experiment") is None
    assert resolve("journal_paper", "reported_experiment", is_review=True) == "review_or_cited"
    assert resolve("presentation", "cited_prior_work") == "review_or_cited"


def test_review_is_bound_to_paper_and_live_claim_content(monkeypatch):
    paper = {"doc_id": "d", "sha256": "a" * 64, "source_type": "datasheet"}
    claim = {"doc_id": "d", "claim_id": "c", "claim_type": "reported_experiment",
             "evidence": [{"pdf_page": 2, "quote": "We measured sample A."}]}
    doc_review = {"doc_id": "d", "pdf_sha256": paper["sha256"], "kind": "manufacturer",
                  "reason": "equipment sheet", "evidence": [{"pdf_page": 1, "quote": "specifications"}]}
    claim_review = {**doc_review, "kind": "experiment", "claim_id": "c",
                    "claim_digest": claims_digest([claim]), "evidence": claim["evidence"]}
    path = Path("source_kind_reviews.json")
    content = json.dumps({"schema": 1, "documents": [doc_review], "claims": [claim_review]})
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "read_text", lambda self, encoding: content)
    index = load_source_kind_reviews(path)
    assert resolve_map_source_kind(paper, claim, index) == "experiment"
    assert source_kind_review(paper, claim, index)["evidence"][0]["pdf_page"] == 2

    changed = deepcopy(claim)
    changed["evidence"][0]["quote"] = "Changed result"
    assert resolve_map_source_kind(paper, changed, index) == "manufacturer"
    changed_source = {**paper, "sha256": "b" * 64}
    assert source_kind_review(changed_source, claim, index) is None
    assert resolve_map_source_kind(changed_source, claim, index) == "manufacturer"
    assert resolve_map_source_kind({**paper, "doc_id": "other"}, claim, index) is None


def test_missing_and_invalid_sidecars(monkeypatch):
    path = Path("no-file.json")
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    assert load_source_kind_reviews(path) == {}
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    content = '{"schema":99}'
    monkeypatch.setattr(Path, "read_text", lambda self, encoding: content)
    assert load_source_kind_reviews(path) == {}
    content = json.dumps({"schema": 1, "documents": [{"doc_id": "d", "kind": "experiment"}]})
    with pytest.raises(ValueError, match="Invalid source kind review"):
        load_source_kind_reviews(path)


def test_real_mixed_source_separates_prior_results_and_new_reliability():
    pilot = Path(__file__).resolve().parents[1] / "pilot"
    index = load_source_kind_reviews(pilot / "research_map/source_kind_reviews.json")
    papers = {p["doc_id"]: p for p in json.loads((pilot / "catalog/papers.json").read_text(encoding="utf-8"))}
    claims = {c["claim_id"]: c for c in map(json.loads, (pilot / "annotations/claims.jsonl").read_bytes().splitlines())}
    cases = {"doc_ca6dd996597df814c837_c005": "review_or_cited",
             "doc_ca6dd996597df814c837_c006": "experiment",
             "doc_2512dc8278919a464c96_c005": "unverified_attribution",
             "doc_2512dc8278919a464c96_c002": None,
             "doc_8e41f3f50ea027998e50_c004": "experiment",
             "doc_bb979bc5a7fa4742c47b_c003": "patent_disclosure",
             "j009": "test_method", "i033": "process_setting"}
    for claim_id, expected in cases.items():
        claim = claims[claim_id]
        assert resolve_map_source_kind(papers[claim["doc_id"]], claim, index) == expected
