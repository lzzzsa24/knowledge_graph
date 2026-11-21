"""Load additional, source-checked links without replacing earlier reviews."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .map_relations import claims_digest
from .research_map import COLUMNS, ROWS


_ROWS = {axis["id"] for axis in ROWS}
_COLUMNS = {axis["id"] for axis in COLUMNS}
_REVIEW_STATUS = "automatic_agent_reviewed"
_LINK_KINDS = {
    "reported_experiment": {"condition", "contrast"},
    "reported_simulation": {"condition", "contrast"},
    "author_interpretation": {"interpretation"},
}


def _citation(value, pages):
    return (isinstance(value, dict) and type(value.get("pdf_page")) is int
            and 1 <= value["pdf_page"] <= pages and isinstance(value.get("quote"), str)
            and 8 <= len(value["quote"].strip()) <= 450)


def load_coverage_reviews(pilot: Path, papers: list[dict], claims: list[dict]) -> tuple[list[dict], dict]:
    path = pilot / "research_map/coverage_reviews.json"
    audit = {"scope": "current_document_selection", "status": _REVIEW_STATUS,
             "current_reviewed_claims": 0, "current_reviewed_pairs": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [], audit
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("documents"), list):
        return [], audit

    papers_by_id = {paper["doc_id"]: paper for paper in papers}
    claims_by_doc = defaultdict(list)
    for claim in claims:
        claims_by_doc[claim["doc_id"]].append(claim)
    documents = [doc for doc in data["documents"] if isinstance(doc, dict) and isinstance(doc.get("doc_id"), str)]
    occurrences = Counter(doc["doc_id"] for doc in documents)
    accepted = []
    for doc in documents:
        doc_id = doc["doc_id"]
        paper = papers_by_id.get(doc_id)
        if (occurrences[doc_id] != 1 or not paper or not doc.get("source_sha256")
                or paper.get("sha256") != doc["source_sha256"]
                or claims_digest(claims_by_doc[doc_id]) != doc.get("claims_digest")
                or not isinstance(doc.get("links"), list) or type(paper.get("page_count")) is not int):
            continue
        by_id = {c["claim_id"]: c for c in claims_by_doc[doc_id]}
        links = [r for r in doc["links"] if isinstance(r, dict)
                 and all(isinstance(r.get(key), str) for key in ("claim_id", "row", "column"))]
        keys = Counter((r["claim_id"], r["row"], r["column"]) for r in links)
        for link in links:
            claim = by_id.get(link["claim_id"])
            pair = link["claim_id"], link["row"], link["column"]
            if (keys[pair] != 1 or not claim or link.get("doc_id") != doc_id
                    or link["row"] not in _ROWS or link["column"] not in _COLUMNS
                    or link.get("review_status") != _REVIEW_STATUS
                    or not isinstance(link.get("relation"), str)
                    or link.get("relation") not in _LINK_KINDS.get(claim.get("claim_type"), set())
                    or not all(isinstance(link.get(k), str) and link[k].strip() for k in ("scope", "limit"))
                    or not all(_citation(link.get(k), paper["page_count"]) for k in ("process_evidence", "result_evidence"))):
                continue
            if link["result_evidence"]["pdf_page"] in {e.get("pdf_page") for e in claim.get("evidence", [])}:
                accepted.append(link)
    audit.update(current_reviewed_claims=len({r["claim_id"] for r in accepted}), current_reviewed_pairs=len(accepted))
    return accepted, audit
