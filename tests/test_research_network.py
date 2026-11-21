from kg_pilot.research_network import build_network


def test_graph_groups_documents_and_limits_comparison_edges_to_attributed_measurements():
    point = {"pdf_page":1,"quote":"Source text."}
    papers = [{"doc_id":str(i),"page_count":2,"classification":{"primary_category":"surface","evidence":[point],
               "content_nature":"original_research"}} for i in range(7)]
    details = {p["doc_id"]:{"doc_id":p["doc_id"],"source_kind":"experiment","system":"cu_oxide"} for p in papers}
    relations = {cid:{"relation":"method" if cid=="0" else "condition","result_evidence":point} for cid in details}
    matrix = {"record_details":details,"rows":[{"id":"r","label":"CMP"}],"columns":[{"id":"c","label":"Surface"}],
              "cells":[{"row":"r","column":"c","records":list(details),"reviewed_records":relations}]}
    cited = {"edges":[{"citing":"0","cited":"1",**point},{"citing":"2","cited":"1","pdf_page":8,"quote":"Wrong page."}]}
    notes = {"cases":[],"opportunities":[],"study_connections":[]}
    data = build_network(papers,[{"id":"surface","label":"Surface"}],matrix,cited,notes)
    assert len(data["clusters"])==1 and data["clusters"][0]["doc_ids"]==list(details)
    assert not data["papers"][0]["has_measured"] and data["papers"][1]["has_measured"]
    assert [e["source"] for e in data["links"] if e["kind"]=="citation"]==["0"]
    weak = [e for e in data["links"] if e["kind"]=="shared_process"]
    assert weak and all({a["doc_id"] for a in e["evidence"]}=={e["source"],e["target"]} for e in weak)
    assert all("0" not in (e["source"],e["target"]) for e in weak)
    assert max(sum(p["doc_id"] in (e["source"],e["target"]) for e in weak) for p in papers)<=3
    assert {(e["kind"],e["basis"]) for e in data["links"]} == {("citation","source_text"),("shared_process","rule")}
    assert all(e["basis_note"] for e in data["links"])
    cluster = data["clusters"][0]
    assert cluster["coverage"] == {"documents":7,"mapped_documents":7,"measured_documents":6,"contrast_documents":0}
    assert cluster["comparison_gap"]["measured_documents"] == 6
    assert cluster["comparison_gap"]["contrast_documents"] == 0


def test_network_distinguishes_source_connections_from_agent_comparisons():
    papers = [{"doc_id": doc_id, "page_count": 2} for doc_id in ("a", "b")]
    anchor = {"pdf_page": 1, "quote": "Original text."}
    members = [{"doc_id": p["doc_id"], "evidence": [anchor]} for p in papers]
    kinds = ("repeated_data", "reused_figure", "documented_extension")
    connections = [{"id": kind, "kind": kind, "title": kind, "finding": "Finding.",
                    "scope_limit": "Scope.", "members": members} for kind in kinds]
    case = {"id": "test", "title": "Comparison", "comparison_limit": "Limited scope.",
            "rows": [{"doc_id": p["doc_id"], **anchor} for p in papers]}
    notes = {"cases": [case], "opportunities": [], "study_connections": connections}
    matrix = {"rows": [], "columns": [], "cells": []}

    links = build_network(papers, [], matrix, {}, notes)["links"]

    expected = dict.fromkeys(kinds, "source_text") | {"study_comparison": "agent_review"}
    assert {edge["kind"]: edge["basis"] for edge in links} == expected
    for edge in links:
        assert edge["basis_note"]
        assert edge["evidence"] == [{"doc_id": p["doc_id"], **anchor} for p in papers]
