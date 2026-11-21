"""Serve versioned research comparisons and screened leads from the current catalog."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def corpus_digest(papers: list[dict]) -> str:
    if any(not p.get("sha256") for p in papers):
        return ""
    pairs = sorted((p["doc_id"], p["sha256"]) for p in papers)
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def read_optional(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError,OSError):
        return {"_invalid":True}
    return data if isinstance(data,dict) else {"_invalid":True}


def attach_first_steps(ideas, registry):
    """Expose practical starting points only for an unchanged reviewed protocol."""
    for idea in ideas:
        entry = registry.get("items", {}).get(idea["id"])
        if not isinstance(entry, dict):
            continue
        protocol = {key: idea.get(key) for key in ("feasibility", "controls")}
        encoded = json.dumps(protocol, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        if hashlib.sha256(encoded).hexdigest() != entry.get("protocol_sha256"):
            continue
        anchors = {(s.get("doc_id"), s.get("pdf_page")) for s in idea.get("evidence", [])}
        sources, needs = entry.get("sources"), entry.get("needs")
        if (not isinstance(sources,list) or not sources or not all(isinstance(s,dict) and (s.get("doc_id"),s.get("pdf_page")) in anchors for s in sources)
                or not isinstance(needs,list) or not needs or not all(isinstance(n,str) and n.strip() for n in needs)
                or not isinstance(entry.get("first_step"),str) or not entry["first_step"].strip()):
            continue
        idea["start_here"] = {"needs":needs, "first_step":entry["first_step"], "sources":sources, "basis_fields":["feasibility","controls"]}


def checked_study_connections(root, docs, by_claim, bound):
    pack = read_optional(root / "study_connections.json")
    if pack.get("schema") != 1:
        return []
    accepted = []
    for entry in pack.get("connections", []):
        members = entry.get("members", []) if isinstance(entry,dict) else []
        if (len(members) != 2 or not all(isinstance(m,dict) for m in members)
                or len({m.get("doc_id") for m in members}) != 2
                or entry.get("kind") not in {"repeated_data","reused_figure","documented_extension"}
                or not all(isinstance(entry.get(k),str) and entry[k] for k in ("id","title","finding","scope_limit"))):
            continue
        validated = []
        for m in members:
            doc_id = m.get("doc_id")
            anchors, ids = m.get("evidence"), m.get("claim_ids", [])
            if (doc_id not in docs or not m.get("pdf_sha256") or not isinstance(anchors,list) or not anchors
                    or not all(isinstance(e,dict) and bound({**e, "doc_id":doc_id},m["pdf_sha256"]) for e in anchors)
                    or not isinstance(ids,list) or not all(by_claim.get(cid,{}).get("doc_id") == doc_id for cid in ids)):
                break
            # Changes to one extracted result must not mark a new record as a
            # repeated measurement solely for sharing its previous identifier.
            exact_ids = []
            for cid in ids:
                data = json.dumps(by_claim[cid], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                if hashlib.sha256(data).hexdigest() == m.get("claim_digests", {}).get(cid):
                    exact_ids.append(cid)
            validated.append({"doc_id":doc_id,"claim_ids":exact_ids,"evidence":[{k:e[k] for k in ("quote","pdf_page","locator") if k in e} for e in anchors]})
        if len(validated) == 2:
            accepted.append({k:entry[k] for k in ("id","kind","title","finding","scope_limit")} | {"members":validated,"cells":[]})
    return accepted


def apply_public_reviews(root, potential, prior):
    path = root / "opportunity_reviews.json"
    audit_path = root / "external_prior_art.json"
    audit = read_optional(audit_path)
    if (not audit.get("reviews") or not path.is_file() or
            audit.get("source_review_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
        return potential, prior
    by_id = {r.get("id"):r for r in audit["reviews"] if isinstance(r,dict)}
    protocol_file = read_optional(root / "opportunity_protocols.json")
    protocols = {}
    if protocol_file.get("schema") == 1 and protocol_file.get("prior_art_sha256") == hashlib.sha256(audit_path.read_bytes()).hexdigest():
        protocols = {r.get("id"):r for r in protocol_file.get("revisions",[]) if isinstance(r,dict)}
    exposed = []
    prior = list(prior)
    for idea in potential:
        check = by_id.get(idea["id"])
        if not check or check.get("verdict") not in {"refine","prior_work","continue"}:
            exposed.append(idea)
            continue
        sources = [s for s in check.get("external_sources",[]) if isinstance(s,dict)
                   and str(s.get("url","")).startswith(("https://","http://")) and s.get("title") and s.get("finding")]
        if not sources or not all(check.get(k) for k in ("revised_question","revised_gap","revised_prior_work")):
            exposed.append(idea)
            continue
        revised = {**idea,"question":check["revised_question"],"title":check.get("revised_title") or idea["title"],
                   "gap":check["revised_gap"],"prior_work":check["revised_prior_work"],
                   "public_review":{"date":audit.get("review_date"),"verdict":check["verdict"],
                                    "conclusion":check.get("conclusion"),"experiment_change":check.get("experiment_change")}}
        combined = {}
        for entry in [*sources,*idea.get("external_sources",[])]:
            key = (str(entry.get("doi") or "").casefold().removeprefix("https://doi.org/") or entry.get("url"))
            if key and key not in combined: combined[key] = entry
        revised["external_sources"] = list(combined.values())
        protocol = protocols.get(idea["id"], {})
        controls = protocol.get("controls")
        if (isinstance(controls,list) and controls and all(isinstance(c,str) and c.strip() for c in controls)
                and isinstance(protocol.get("measurement"),str) and protocol["measurement"].strip()):
            revised["controls"] = controls
            for field in ("measurement","success_criterion","counterexample","feasibility"):
                if isinstance(protocol.get(field),str) and protocol[field].strip():
                    revised[field] = protocol[field]
            revised["public_review"]["protocol_integrated"] = True
        if check["verdict"]=="prior_work":
            revised["status"]="prior_work"
            prior.append(revised)
        else:
            exposed.append(revised)
    return exposed,prior


def research_notes(pilot: Path, papers: list[dict], claims: list[dict]) -> dict:
    root = pilot / "research_map"
    study_files = [read_optional(root / name) for name in ("study_comparisons.json","additional_studies.json")]
    reviews = read_optional(root / "opportunity_reviews.json")
    docs = {p["doc_id"]: p for p in papers}
    by_claim = {c["claim_id"]: c for c in claims}
    sources = {p["doc_id"]: p.get("sha256") for group in study_files for p in group.get("sources", [])}
    dataset = corpus_digest(papers)

    def bound(entry, expected=None):
        doc = docs.get(entry.get("doc_id"))
        page = entry.get("pdf_page")
        cid = entry.get("claim_id")
        if not doc or type(page) is not int or not 1 <= page <= (doc.get("page_count") or 0):
            return False
        if expected is not None and expected != doc["sha256"]:
            return False
        return (not cid or (cid in by_claim and by_claim[cid]["doc_id"] == doc["doc_id"])) and bool(entry.get("quote"))

    cases = []
    for case in (case for group in study_files for case in group.get("cases", [])):
        rows = case.get("rows", [])
        fingerprints = case.get("source_fingerprints") or sources
        def current_row(row):
            doc_id = row.get("doc_id")
            expected = fingerprints.get(doc_id)
            if not expected or row.get("source_sha256", expected) != expected or not bound(row, expected):
                return False
            return all(bound({"doc_id":doc_id, **extra}, expected) for extra in row.get("additional_evidence", []))
        if not rows or not all(current_row(row) for row in rows):
            continue
        cases.append({k: case[k] for k in ("id", "title", "question", "conclusion", "comparison_limit",
                      "next_test", "row_ids", "column_ids", "rows", "calculation", "lead_ids", "prior_work_ids") if k in case})

    current = bool(dataset) and bool(reviews) and reviews.get("corpus", {}).get("source_digest") == dataset
    stack_source = root / "stack_opportunities.json"
    stack_review = read_optional(root / "stack_release_review.json")
    if stack_source.is_file() and stack_review.get("source_review_sha256") == hashlib.sha256(stack_source.read_bytes()).hexdigest():
        case_by_id = {case["id"]: case for case in cases}
        for judgement in stack_review.get("reviews", []):
            case = case_by_id.get(judgement.get("study_id"))
            target = judgement.get("study_anchor") or {}
            if not case or not judgement.get("study_note") or judgement.get("decision") != "withhold":
                continue
            # A design lesson can survive a withheld proposal only when the
            # source of the lesson is still this exact, accessible PDF edition.
            for anchor in judgement.get("local_evidence", []):
                if (anchor.get("doc_id") == target.get("doc_id") and anchor.get("pdf_page") == target.get("pdf_page")
                        and anchor.get("pdf_sha256") and bound(anchor, anchor["pdf_sha256"])):
                    case["design_review"] = {"note": judgement["study_note"], "source": anchor}
                    break

    def screened(key, source=None):
        entries = []
        for r in (reviews.get(key, []) if source is None else source):
            evidence = r.get("evidence", [])
            if not evidence or not all(e.get("pdf_sha256") and bound(e, e["pdf_sha256"]) for e in evidence): continue
            # Never expose absolute workstation paths from editorial audit metadata.
            item = {k:r[k] for k in ("id","title","row","column","system","system_label","route_scope",
                "route_basis","status","question","prior_work","gap","controls","measurement","success_criterion","counterexample",
                "feasibility","evidence","external_sources","search_summary","search_limit","reason","next_step") if k in r}
            search = r.get("search") or {}
            counts = search.get("results") or {}
            item["search"] = {k:search[k] for k in ("groups","limitations","external_queries") if k in search}
            item["search"]["counts"] = {k:counts[k] for k in ("individual_group_document_counts",
                "all_groups_same_document_count","all_groups_same_page_count") if k in counts}
            role = "lead_ids" if key=="opportunities" else "prior_work_ids"
            item["study_ids"] = [study["id"] for study in cases if item["id"] in study.get(role, [])]
            entries.append(item)
        return entries

    potential,prior = apply_public_reviews(root,screened("opportunities") if current else [],screened("rejected"))
    # External review can withhold one material proposal and release another.
    # Publishing a new material dossier must not invalidate earlier reviews.
    material_path = root / "material_opportunities.json"
    release = read_optional(root / "material_release_review.json")
    if (dataset and release.get("corpus_digest") == dataset and release.get("schema") == 1
            and material_path.is_file() and release.get("source_review_sha256") == hashlib.sha256(material_path.read_bytes()).hexdigest()):
        dossier = read_optional(material_path)
        proposals = {p["id"] for p in dossier.get("proposals", []) if isinstance(p,dict) and p.get("id")}
        seen = {item["id"] for item in [*potential, *prior]}
        for decision in release.get("reviews", []):
            if not isinstance(decision,dict) or decision.get("decision") != "release_refined":
                continue
            card = decision.get("card")
            if (not isinstance(card,dict) or card.get("id") not in proposals or card["id"] in seen
                    or decision.get("id") != card["id"] or card.get("status") != "potential"):
                continue
            if (not all(card.get(k) for k in ("title", "row", "column", "question", "prior_work", "gap", "measurement", "controls", "success_criterion", "counterexample", "feasibility", "evidence", "external_sources"))
                    or not decision.get("conclusion") or not all(isinstance(s,dict) and str(s.get("url","")).startswith("https://") and s.get("finding") for s in card["external_sources"])):
                continue
            for item in screened("opportunities", [card]):
                item["public_review"] = {"date":release.get("review_date"),"verdict":"refine","conclusion":decision["conclusion"],
                                         "protocol_integrated":True}
                potential.append(item)
                seen.add(item["id"])
    attach_first_steps(potential, read_optional(root / "direction_starting_points.json"))
    return {"cases": cases, "opportunities":potential,
            "study_connections":checked_study_connections(root, docs, by_claim, bound),
            "prior_work":prior, "reviewed_on": reviews.get("reviewed_on"),
            "source_issues": [name for name,group in zip(("原始对照","新增对照","方向复查"),[*study_files,reviews]) if group.get("_invalid")],
            "last_review_documents":reviews.get("corpus",{}).get("documents"),
            "review_status": "unavailable" if reviews.get("_invalid") else "current" if current else "requires_refresh" if reviews else "pending",
            "documents":len(docs), "pages":sum(d.get("page_count") or 0 for d in docs.values()), "corpus_digest":dataset}


def attach_connection_cells(connections: list[dict], matrix: dict) -> list[dict]:
    """Locate only the connection's validated claims, never a paper's topics.

    Cells are the union of the records explicitly attached to this connection;
    this locates its discussion, without asserting both papers measured every
    cell. Connections supported only by a figure or prose remain unlocated.
    """
    by_claim = {}
    for cell in matrix["cells"]:
        for cid in cell["records"]:
            by_claim.setdefault(cid, set()).add((cell["row"], cell["column"]))
    return [{**connection, "cells": [{"row": row, "column": column} for row, column in sorted({
                pair for member in connection.get("members", []) for cid in member.get("claim_ids", [])
                for pair in by_claim.get(cid, ())})]}
            for connection in connections]


def attach_screened_directions(result: dict, notes: dict, route: str | None):
    # Sparse cells are leads for a future review. Only reviewed, corpus-bound
    # questions belong in the list that readers see as potential innovations.
    result["screening_candidates"] = result["directions"]
    for candidate in result["screening_candidates"]:
        candidate["status"] = "needs_fulltext_review"
    result["directions"] = []
    result["study_connections"] = attach_connection_cells(notes.get("study_connections", []), result)
    for item in notes["opportunities"]:
        if (item.get("status") != "potential" or (route and route not in item.get("route_scope", []))
                or (result.get("system") and item.get("system") != result["system"])):
            continue
        result["directions"].append({k:item[k] for k in ("id","title","row","column","system","system_label","question",
            "prior_work","gap","controls","measurement","status","route_scope") if k in item}
            | {"rationale":item["gap"],"anchors":item["evidence"],"reviewed_on":notes["reviewed_on"],
               "prior_art":[{k:source[k] for k in ("title","url") if k in source} for source in item.get("external_sources",[])[:3]]})
    result["prior_work"] = [{k:item[k] for k in ("id","title","row","column","system","prior_work","question","status") if k in item}
                            for item in notes["prior_work"] if (not route or route in item.get("route_scope", []))
                            and (not result.get("system") or item.get("system")==result["system"])]
    # Link a study to a cell only through a shared *record*, not a cross-product
    # of the topic labels in an editorial case file.
    study_by_claim = {}
    for study in notes["cases"]:
        for row in study["rows"]:
            if row.get("claim_id"):
                study_by_claim.setdefault(row["claim_id"],set()).add(study["id"])
    for cell in result["cells"]:
        cell["study_ids"] = sorted({s for cid in cell["records"] for s in study_by_claim.get(cid, ())})
    found = {s for cell in result["cells"] for s in cell["study_ids"]}
    result["studies"] = [{k:s[k] for k in ("id","title","conclusion")} for s in notes["cases"] if s["id"] in found]
    result["catalog_digest"] = notes.get("corpus_digest")
    result["review_status"] = notes["review_status"]
    # Human-facing labels clarify the already-reviewed axis definition. Keep
    # the frozen scientific rubric used for signed decisions independent.
    for row in result.get("rows", []):
        if row["id"] == "annealing":
            row["label"] = "退火与热预算"
            row["description"] = "材料预退火、键合后独立热处理，以及明确以退火温度或热历程为变量的模型。单步热压键合见“对准与接触条件”。"
        elif row["id"] == "alignment_contact":
            row["description"] = "错位、对准、接触压力，以及施压键合时同步加热的条件。"
    result["method"] = ("每格显示涉及该工艺与结果的不同资料数。原创实测只计报告该项实际测量的资料；"
                        "实测对照还要求原文改变了该工艺因素并比较结果，同时改变的条件写在连线依据中。"
                        "路线按文献分类，材料按当前记录所指的样品归类。"
                        "潜在创新方向经过原页与既有工作复查，具体问题可以来自文献密集的交叉。")
    return result

