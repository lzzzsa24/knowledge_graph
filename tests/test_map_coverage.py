"""Additional coverage is scoped, source-bound and additive."""
import json

import pytest

from kg_pilot.map_coverage import load_coverage_reviews
from kg_pilot.map_relations import claims_digest
from kg_pilot.research_map import build_research_map


def paper(doc_id="a", route="W2W"):
    return {"doc_id": doc_id, "sha256": "a" * 64, "page_count": 3,
            "classification": {"routes": [route]}}


def claim(claim_id, statement, quantity, **extra):
    return {"claim_id": claim_id, "doc_id": "a", "quantity": quantity,
            "statement": statement, "material_system": "Cu/SiO2", "conditions": {},
            "claim_type": "reported_experiment", "review_status": "independent_agent_reviewed",
            "evidence": [{"pdf_page": 2, "quote": "The measured result is for the specified specimen."}],
            **extra}


def review(claim_id):
    return {"doc_id": "a", "claim_id": claim_id, "row": "surface_passivation", "column": "voids",
            "relation": "condition", "review_status": "automatic_agent_reviewed",
            "scope": "Observed region of the stated sample.", "limit": "Qualitative only.",
            "process_evidence": {"pdf_page": 1, "quote": "The sample was passivated with ruthenium."},
            "result_evidence": {"pdf_page": 2, "quote": "The selected section exhibited defects."}}


def test_unmapped_explains_claims_without_creating_edges_from_context():
    records = [
        claim("input", "CMP采用1 psi压力。", "CMP_pressure"),
        claim("long_quote", "测得电阻为1 mΩ。", "contact_resistance",
              evidence=[{"pdf_page": 2, "quote": "CMP, plasma and annealing are frequently studied."}]),
        claim("negated", "未进行退火，测得电阻为1 mΩ。", "contact_resistance"),
        claim("method", "比较两台仪器的Cu凹陷预测误差。", "Cu凹陷预测误差"),
        claim("review", "本文回顾了退火研究进展。", "literature_scope", claim_type="review_summary"),
        claim("spec", "厂商建议采用20秒的等离子处理。", "处理时间", claim_type="recommended_process"),
        claim("held", "CMP后测得接触电阻为1 mΩ。", "contact_resistance"),
    ]
    withheld = {("held", "surface_planarization", "electrical_resistance")}
    result = build_research_map([paper()], records, withheld=withheld)
    assert result["record_details"] == {}
    assert result["unmapped"] == {
        "input": "no_outcome", "long_quote": "no_process_factor", "negated": "no_process_factor",
        "method": "no_outcome", "review": "background", "spec": "spec_only", "held": "ambiguous",
    }


def test_new_links_fill_only_unmapped_records_and_honor_filters_and_withholds():
    records = [
        claim("new", "预热后溅射Ru的组出现键合缺陷。", "键合性能"),
        claim("old", "CMP后测得接触电阻为1 mΩ。", "contact_resistance"),
        claim("signed", "所列试样经过剪切试验。", "定性观察"),
        claim("held", "预热后溅射Ru的组出现键合缺陷。", "键合性能"),
        claim("other_route", "测得电阻为1 mΩ。", "contact_resistance", doc_id="b"),
        claim("other_system", "CMP后测得接触电阻为1 mΩ。", "contact_resistance", material_system="Cu–Cu"),
    ]
    signed = {**review("signed"), "row": "alignment_contact", "column": "bond_strength"}
    result = build_research_map(
        [paper(), paper("b", "D2W")], records, route="W2W", system="cu_oxide", reviewed=[signed],
        coverage_reviewed=[review(cid) for cid in ("new", "old", "signed", "held")],
        withheld={("held", "surface_passivation", "voids")},
    )
    assert result["totals"]["claims"] == 5
    assert result["unmapped"] == {"held": "ambiguous", "other_system": "out_of_scope"}
    assert {(cell["row"], cell["column"]): cell["records"] for cell in result["cells"] if cell["records"]} == {
        ("surface_passivation", "voids"): ["new"],
        ("surface_planarization", "electrical_resistance"): ["old"],
        ("alignment_contact", "bond_strength"): ["signed"],
    }
    edge = next(cell["reviewed_records"]["new"] for cell in result["cells"] if "new" in cell["reviewed_records"])
    assert edge["review_status"] == "automatic_agent_reviewed"
    assert edge["result_evidence"]["pdf_page"] == 2


@pytest.mark.parametrize("invalid", ["paper_hash", "claim_digest", "out_of_page", "wrong_result_page",
                                     "review_status", "axis", "duplicate", "relation"])
def test_coverage_review_is_bound_to_current_pdf_catalog_and_claim(tmp_path, invalid):
    p = paper()
    c = claim("new", "预热后溅射Ru的组出现键合缺陷。", "键合性能")
    edge = review("new")
    bound = {"doc_id": "a", "source_sha256": p["sha256"], "claims_digest": claims_digest([c]), "links": [edge]}
    path = tmp_path / "research_map/coverage_reviews.json"
    path.parent.mkdir()

    def write():
        path.write_text(json.dumps({"schema": 1, "documents": [bound]}), encoding="utf-8")

    write()
    links, audit = load_coverage_reviews(tmp_path, [p], [c])
    assert links == [edge] and audit["current_reviewed_claims"] == 1
    if invalid == "paper_hash":
        p["sha256"] = "new_hash"
    elif invalid == "claim_digest":
        c["statement"] = "该研究的陈述已更新。"
    elif invalid == "out_of_page":
        edge["process_evidence"]["pdf_page"] = 4
    elif invalid == "wrong_result_page":
        edge["result_evidence"]["pdf_page"] = 3
    elif invalid == "review_status":
        edge["review_status"] = "expert_confirmed"
    elif invalid == "axis":
        edge["column"] = "alignment_error"
    elif invalid == "duplicate":
        bound["links"].append(edge.copy())
    else:
        edge["relation"] = ["invalid"]
    write()
    assert load_coverage_reviews(tmp_path, [p], [c])[0] == []
