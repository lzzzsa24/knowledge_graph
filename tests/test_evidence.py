import json
from pathlib import Path
import pytest
from kg_pilot.pipeline import PILOT, jsonl, read_json
from kg_pilot.validation import Claim, quote_supported


def test_quote_does_not_accept_changed_number_or_unit():
    source = "The resistance was 1.5 mΩ after annealing."
    assert quote_supported("The resistance was\n1.5 mΩ", source)
    assert not quote_supported("The resistance was 15 mΩ", source)
    assert not quote_supported("The resistance was 1.5 mW", source)


def test_different_samples_remain_distinct():
    records = jsonl(PILOT / "annotations" / "claims.jsonl")
    observed = {c["sample_id"] for c in records if c["doc_id"] == "imec_11"}
    assert len(observed) >= 2
    original = next(c for c in records if c["claim_id"] == "i039")
    assert original["sample_id"] == "hybrid_chip_1"
    assert original["review_status"] == "independent_agent_reviewed"


def test_accepted_claims_have_independent_reviews_and_valid_schema():
    claims = jsonl(PILOT / "annotations" / "claims.jsonl")
    assert len(claims) >= 100
    for c in claims:
        Claim.model_validate(c)
        assert c["review_status"] in {"independent_agent_reviewed", "api_model_reviewed"}
    assert len({c["claim_id"] for c in claims}) == len(claims)


def test_ocr_preserves_original_and_has_unique_evidence_ids():
    native = jsonl(PILOT / "parsed" / "evidence.jsonl")
    effective = jsonl(PILOT / "parsed" / "effective_evidence.jsonl")
    assert len({e["evidence_id"] for e in effective}) == len(effective)
    original = [e for e in native if e["doc_id"] == "reliability_small_die_2022"]
    replacement = [e for e in effective if e["doc_id"] == "reliability_small_die_2022"]
    assert original and replacement
    assert all(e["parser"] == "docling_rapidocr" for e in replacement)
    assert "Die to Wafer Hybrid Bonding" in " ".join(e["text"] for e in replacement)


def test_finite_element_claims_are_not_relabeled_experiments():
    records = jsonl(PILOT / "annotations" / "claims.jsonl")
    model_records = [c for c in records if c["doc_id"] == "journals_mi15111332"]
    assert model_records and all(c["claim_type"] == "reported_simulation" for c in model_records)
    assert all(c["review_status"] == "independent_agent_reviewed" for c in model_records)


def test_review_correction_separates_contact_and_post_anneal():
    c = next(c for c in jsonl(PILOT / "annotations" / "claims.jsonl") if c["claim_id"] == "j015")
    text = json.dumps(c, ensure_ascii=False)
    assert "后退火" in text or "post" in text.lower()


def test_units_preserve_offsets_and_do_not_guess_ranges():
    from kg_pilot.units import normalize_single
    assert normalize_single("200", "°C")["value_si"] == pytest.approx(473.15)
    assert normalize_single("1.5", "mΩ")["value_si"] == pytest.approx(0.0015)
    assert normalize_single("200–350", "°C")["status"] == "not_a_single_explicit_numeric_quantity"


@pytest.mark.parametrize("readiness,blocked", [
    ({"documents": {}}, False),
    ({"documents": {"new_doc": {"batch_id": "b_test"}}}, True),
    ({"documents": []}, True),
])
def test_legacy_rebuild_requires_empty_verified_batch_state(monkeypatch, readiness, blocked):
    from kg_pilot import pipeline
    monkeypatch.setattr(Path, "is_file", lambda self: self.name == "ingestion_readiness.json")
    monkeypatch.setattr(pipeline, "read_json", lambda path: readiness)
    if blocked:
        with pytest.raises(pipeline.LegacyRebuildError, match="批量入库"):
            pipeline.ensure_legacy_rebuild_safe()
    else:
        pipeline.ensure_legacy_rebuild_safe()


def test_legacy_rebuild_guard_is_applied_before_any_old_work(monkeypatch):
    from kg_pilot import pipeline, validation, ocr_evidence
    calls = []

    def refuse(*args, **kwargs):
        calls.append(True)
        raise pipeline.LegacyRebuildError("请使用批量入库入口。")

    for module, function_names in ((pipeline, ("assemble", "parse")), (validation, ("merge",)), (ocr_evidence, ("main",))):
        monkeypatch.setattr(module, "ensure_legacy_rebuild_safe", refuse)
        for name in function_names:
            with pytest.raises(pipeline.LegacyRebuildError, match="批量入库"):
                getattr(module, name)()
    assert len(calls) == 4
