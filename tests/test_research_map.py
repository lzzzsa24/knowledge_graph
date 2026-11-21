"""Assertions are coverage anchors, never paper-tag or causal combinations."""
from copy import deepcopy
from pathlib import Path
import json

from fastapi.testclient import TestClient

from kg_pilot.classification import classified_papers
from kg_pilot.research_map import build_research_map
from web import server


def paper(doc_id, *, nature="original_research", routes=None, doi=None):
    return {"doc_id": doc_id, "title": doc_id, "doi": doi,
            "classification": {"content_nature": nature, "routes": routes or ["W2W"],
                               "methods": ["experimental"], "primary_category": "integrated_flow"}}


def claim(cid, doc_id, statement, quantity, **overrides):
    return {"claim_id": cid, "doc_id": doc_id, "statement": statement, "quantity": quantity,
            "material_system": "Cu/SiO2", "sample_id": "sample", "conditions": {},
            "claim_type": "reported_experiment", "review_status": "independent_agent_reviewed",
            "evidence": [{"evidence_id": f"{doc_id}:p1:b1", "pdf_page": 1, "quote": statement}], **overrides}


def cell(data, row, column):
    return next(c for c in data["cells"] if c["row"] == row and c["column"] == column)


def test_does_not_cartesian_join_separate_claims_or_paper_tags():
    ps = [paper("a")]
    ps[0]["classification"].update(related_stages=["surface_planarization"], topics=["空洞", "电阻"])
    cs = [claim("process", "a", "CMP采用1 psi压力。", "CMP_pressure"),
          claim("response", "a", "样品的电阻为1 mΩ。", "contact_resistance")]
    result = build_research_map(ps, cs)
    assert not any(c["claims"] for c in result["cells"])
    assert result["totals"]["claims"] == 2


def test_same_assertion_counts_documents_and_keeps_all_records():
    cs = [claim("c1", "a", "CMP后接触电阻为1 mΩ。", "contact_resistance"),
          claim("c2", "a", "CMP后另一组接触电阻为2 mΩ。", "contact_resistance")]
    cs.append(deepcopy(cs[0]))
    result = build_research_map([paper("a")], cs)
    found = cell(result, "surface_planarization", "electrical_resistance")
    assert (found["documents"], found["claims"], found["experiments"]) == (1, 2, 1)
    assert found["records"] == ["c1", "c2"]
    assert result["record_details"]["c1"]["evidence"][0]["pdf_page"] == 1


def test_long_quotes_disclaimers_and_test_methods_do_not_create_links():
    cs = [claim("quote", "a", "样品电阻为1 mΩ。", "contact_resistance",
                evidence=[{"pdf_page": 1, "quote": "CMP and activation improve resistance and TDDB."}]),
          claim("scope", "a", "样品电阻为1 mΩ；这不是清洗或钝化后的击穿可靠性验证。", "contact_resistance",
                conditions={"scope": "CMP plasma activation passivation annealing TDDB"}),
          claim("method", "a", "CMP样品的接触电阻用四探针测量。", "接触电阻测量方法", claim_type="test_method")]
    assert not build_research_map([paper("a")], cs)["record_details"]


def test_measured_roughness_is_not_automatically_a_process_factor():
    cs = [claim("rough", "a", "表面粗糙度为0.2 nm。", "surface_roughness"),
          claim("cmp", "a", "CMP后表面粗糙度为0.2 nm。", "surface_roughness")]
    found = cell(build_research_map([paper("a")], cs), "surface_planarization", "surface_state")
    assert found["records"] == ["cmp"]


def test_process_inputs_and_measurement_bias_do_not_become_results_or_factors():
    cs = [claim("input", "a", "退火模型从25°C升温到300°C后冷却。", "simulation_thermal_cycle", claim_type="reported_simulation"),
          claim("bias", "a", "SiO2击穿场强为3.4 MV/cm，作者注明错位测量使该值偏低。", "SiO2击穿场强",
                conditions={"作者注明的测量局限": "错位测量导致数值偏低"}),
          claim("before", "a", "未经退火的接头电阻为10 mΩ。", "contact_resistance")]
    result = build_research_map([paper("a")], cs)
    assert "input" not in result["record_details"]
    assert not cell(result, "alignment_contact", "electrical_reliability")["records"]
    assert not cell(result, "annealing", "electrical_resistance")["records"]


def test_separates_measured_simulation_review_vendor_and_guidance_documents():
    ps = [paper("experiment"), paper("simulation"), paper("review", nature="review"),
          paper("vendor", nature="product_specification"), paper("guide", nature="technical_guidance")]
    types = ["reported_experiment", "reported_simulation", "reported_experiment", "manufacturer_specification", "reported_experiment"]
    cs = [claim(p["doc_id"], p["doc_id"], "退火后接触电阻为1 mΩ。", "contact_resistance", claim_type=t)
          for p, t in zip(ps, types)]
    found = cell(build_research_map(ps, cs), "annealing", "electrical_resistance")
    assert (found["documents"], found["experiments"], found["simulations"]) == (5, 1, 1)
    assert found["source_counts"] == {"experiment": 1, "simulation": 1, "review_or_cited": 1, "manufacturer": 1, "technical_guidance": 1}


def test_unreviewed_or_unlocated_assertions_cannot_populate_map():
    cs = [claim("unreviewed", "a", "CMP后电阻为1 mΩ。", "contact_resistance", review_status="pending"),
          claim("unlocated", "a", "CMP后电阻为1 mΩ。", "contact_resistance", evidence=[])]
    assert build_research_map([paper("a")], cs)["totals"]["mapped_claims"] == 0


def test_routes_only_filter_source_snapshot():
    ps = [paper("w", routes=["W2W"]), paper("d", routes=["D2W"])]
    cs = [claim("w", "w", "CMP后电阻为1 mΩ。", "contact_resistance"),
          claim("d", "d", "CMP后电阻为1 mΩ。", "contact_resistance")]
    result = build_research_map(ps, cs, route="D2W")
    assert result["totals"] == {"documents": 1, "claims": 1, "mapped_documents": 1, "mapped_claims": 1}
    assert set(result["record_details"]) == {"d"}


def direction_fixture(*, same_doc=False, same_doi=False, other_system=False, disjoint_route=False):
    ps = [paper("a", doi="10.test/same" if same_doi else None),
          paper("b", doi="https://doi.org/10.test/same" if same_doi else None, routes=["D2W"] if disjoint_route else None)]
    cs = [claim("shape", "a", "CMP后表面粗糙度为0.2 nm。", "surface_roughness"),
          claim("reliability", "a" if same_doc else "b", "退火后漏电流为1 pA。", "leakage_current",
                material_system="Cu-Cu" if other_system else "Cu/SiO2")]
    return ps, cs


def test_candidate_requires_two_complementary_independent_same_system_anchors():
    ps, cs = direction_fixture()
    result = build_research_map(ps, cs)
    direction = next(d for d in result["directions"] if (d["row"], d["column"]) == ("surface_planarization", "electrical_reliability"))
    assert direction["system"] == "cu_oxide"
    assert {a["role"] for a in direction["anchors"]} == {"same_process", "same_response"}
    assert {a["doc_id"] for a in direction["anchors"]} == {"a", "b"}
    assert direction["question"] and direction["controls"] and direction["measurement"]
    assert "本库" in direction["boundary"] and "创新性" in direction["boundary"]


def test_same_document_duplicate_doi_incompatible_system_or_route_cannot_be_two_anchors():
    for kwargs in ({"same_doc": True}, {"same_doi": True}, {"other_system": True}, {"disjoint_route": True}):
        ps, cs = direction_fixture(**kwargs)
        result = build_research_map(ps, cs)
        assert not any(d["row"] == "surface_planarization" and d["column"] == "electrical_reliability" for d in result["directions"])


def test_no_arbitrary_empty_cell_recommendations_or_high_coverage_candidates():
    assert build_research_map([paper("a")], [claim("a", "a", "CMP后粗糙度为1 nm。", "surface_roughness")])["directions"] == []
    ps, cs = direction_fixture()
    for n in range(3):
        doc = f"direct{n}"
        ps.append(paper(doc))
        cs.append(claim(doc, doc, "CMP后漏电流为1 pA。", "leakage_current"))
    result = build_research_map(ps, cs)
    assert not any(d["row"] == "surface_planarization" and d["column"] == "electrical_reliability" for d in result["directions"])


def test_pure_metal_and_dielectric_coupons_remain_distinct_from_hybrid_systems():
    ps = [paper("a")]
    cs = [claim("metal", "a", "独立Cu–Cu同质试样经活化后拉伸强度为4 MPa。", "拉伸强度"),
          claim("oxide", "a", "氧化层模型样品经活化后拉伸强度为4 MPa。", "拉伸强度"),
          claim("hybrid", "a", "图形化Cu/SiO2混合结构经活化后拉伸强度为4 MPa。", "拉伸强度")]
    result = build_research_map(ps, cs)
    groups = {d["system"] for d in result["record_details"].values()}
    assert groups == {"cu_only", "dielectric_oxide", "cu_oxide"}


def reviewed_pair(cid, doc_id, row, column, relation="condition"):
    return {"claim_id": cid, "doc_id": doc_id, "row": row, "column": column,
            "relation": relation, "scope": "same checked specimen", "limit": "association only",
            "process_evidence": {"pdf_page": 2, "quote": "The same test specimens were annealed."},
            "result_evidence": {"pdf_page": 4, "quote": "The contact resistance was 1 mOhm."}}


def test_reviewed_context_adds_only_explicit_pair_and_keeps_both_pages():
    cs = [claim("result", "a", "样品接触电阻为1 mΩ。", "contact_resistance")]
    relation = reviewed_pair("result", "a", "annealing", "electrical_resistance")
    result = build_research_map([paper("a")], cs, reviewed=[relation])
    assert sum(c["claims"] for c in result["cells"]) == 1
    found = cell(result, "annealing", "electrical_resistance")
    assert found["records"] == ["result"] and found["experiments"] == 1
    assert found["reviewed_records"]["result"] == relation
    assert result["record_details"]["result"]["process_matches"] == {}


def test_reviewed_method_and_interpretation_are_not_measured_evidence():
    cs = [claim("result", "a", "退火后接触电阻为1 mΩ。", "contact_resistance")]
    for relation_type in ("method", "interpretation"):
        relation = reviewed_pair("result", "a", "annealing", "electrical_resistance", relation_type)
        found = cell(build_research_map([paper("a")], cs, reviewed=[relation]), "annealing", "electrical_resistance")
        assert found["experiments"] == 0
        assert found["source_counts"][relation_type] == 1
        assert found["systems"][0]["experiments"] == 0


def test_reviewed_relations_respect_route_doc_identity_and_evidence_shape():
    ps = [paper("a", routes=["D2W"])]
    cs = [claim("result", "a", "接触电阻为1 mΩ。", "contact_resistance")]
    relation = reviewed_pair("result", "a", "annealing", "electrical_resistance")
    assert build_research_map(ps, cs, route="W2W", reviewed=[relation])["totals"]["mapped_claims"] == 0
    for broken in ({**relation, "doc_id": "other"}, {**relation, "row": "unknown"},
                   {**relation, "process_evidence": {"pdf_page": 0, "quote": "bad"}}):
        assert build_research_map(ps, cs, reviewed=[broken])["totals"]["mapped_claims"] == 0


def test_film_resistance_does_not_anchor_a_joint_resistance_candidate():
    ps = [paper("a"), paper("b")]
    cs = [claim("process", "a", "SiO2样品活化后键合能为2 J/m²。", "bonding_energy"),
          claim("film", "b", "Cu/SiO2膜经退火后的片电阻为1 Ω/sq。", "sheet_resistance")]
    result = build_research_map(ps, cs)
    assert result["record_details"]["film"]["specimen_scope"] == "thin_film_transport"
    assert not any(d["row"] == "dielectric_interface" and d["column"] == "electrical_resistance" for d in result["directions"])


def test_metric_grammar_keeps_input_roughness_separate_from_resistance_response():
    cs = [claim("a", "a", "CMP后接触电阻随铜凹陷增加。", "接触电阻率随铜凹陷的变化关系")]
    result = build_research_map([paper("a")], cs)
    assert cell(result, "surface_planarization", "electrical_resistance")["records"] == ["a"]
    assert not cell(result, "surface_planarization", "surface_state")["records"]


def test_live_corpus_contract_and_traceability():
    root = Path(__file__).resolve().parents[1]
    papers = json.loads((root / "pilot/catalog/papers.json").read_text("utf-8-sig"))
    claims = [json.loads(line) for line in (root / "pilot/annotations/claims.jsonl").read_text("utf-8-sig").splitlines() if line.strip()]
    result = build_research_map(classified_papers(papers, claims), claims)
    assert result["totals"]["documents"] == len(papers)
    assert result["totals"]["claims"] == len(claims)
    assert len(result["cells"]) == len(result["rows"]) * len(result["columns"])
    ids = {claim["claim_id"]: claim for claim in claims}
    for found in result["cells"]:
        assert found["claims"] == len(found["records"]) == len(set(found["records"]))
        assert found["documents"] == len({ids[cid]["doc_id"] for cid in found["records"]})
        assert found["experiments"] <= found["documents"]
        assert all(result["record_details"][cid]["evidence"] for cid in found["records"])
    # Real false-positive regressions: numerical thermal loading is an input,
    # and a measurement-alignment caveat is not a studied alignment factor.
    assert "j038" not in result["record_details"]
    assert "doc_d2626ee7a51ee374adb6_c007" not in cell(result, "alignment_contact", "electrical_reliability")["records"]
    assert "doc_3a1fddf247811b81710f_c004" not in cell(result, "surface_planarization", "voids")["records"]
    for cid, row, column in [("j005", "annealing", "voids"), ("i014", "surface_planarization", "voids"),
                             ("i024", "annealing", "surface_state"), ("i025", "annealing", "surface_state"),
                             ("j028", "surface_passivation", "surface_state"),
                             ("doc_95ce2634ffbe629e815d_c003", "cleaning_activation", "electrical_resistance")]:
        assert cid in cell(result, row, column)["records"]


def test_api_contract_routes_and_publication_guard(monkeypatch):
    client = TestClient(server.app)
    response = client.get("/api/research-map")
    assert response.status_code == 200
    result = response.json()
    assert {"rows", "columns", "cells", "directions", "totals", "method"} <= result.keys()
    assert client.get("/api/research-map", params={"route": "bad"}).status_code == 422
    filtered = client.get("/api/research-map", params={"route": "D2W"})
    assert filtered.status_code == 200 and filtered.json()["totals"]["documents"] <= result["totals"]["documents"]
    from kg_pilot import ingestion_publish
    monkeypatch.setattr(ingestion_publish, "maintenance_status", lambda pilot: {"active": True})
    assert client.get("/api/research-map").status_code == 503
