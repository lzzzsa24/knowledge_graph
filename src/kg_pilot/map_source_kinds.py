"""Provenance and endpoint corrections for the research map, without changing annotations."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .map_relations import claims_digest


_KINDS = {"experiment", "simulation", "manufacturer", "review_or_cited", "technical_guidance",
          "patent_disclosure", "test_method", "process_setting", "unverified_attribution"}
_SHA = re.compile(r"[0-9a-f]{64}")


def load_source_kind_reviews(path: Path) -> dict:
    """Index signed document and claim reviews. Missing/unsupported sidecars yield an empty index."""
    path = Path(path)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        return {}
    index = {"documents": {}, "claims": {}}
    for scope, key in (("documents", "doc_id"), ("claims", "claim_id")):
        for row in data.get(scope, []):
            if (row.get("kind") not in _KINDS or not row.get(key) or not row.get("doc_id")
                    or not _SHA.fullmatch(row.get("pdf_sha256") or "")
                    or not row.get("reason") or not row.get("evidence")):
                raise ValueError(f"Invalid source kind review in {scope}")
            if scope == "claims" and not _SHA.fullmatch(row.get("claim_digest") or ""):
                raise ValueError(f"Missing claim binding for {row[key]}")
            if any(not isinstance(e.get("pdf_page"), int) or e["pdf_page"] < 1 or not e.get("quote")
                   for e in row["evidence"]):
                raise ValueError(f"Invalid page anchor for {row[key]}")
            if row[key] in index[scope]:
                raise ValueError(f"Conflicting duplicate source review for {row[key]}")
            index[scope][row[key]] = row
    return index


def source_kind_review(paper: dict, claim: dict, review_index: dict | None = None) -> dict | None:
    """Return the page-grounded review only when the live document and claim match."""
    doc_id = paper.get("doc_id")
    if not review_index or not doc_id or doc_id != claim.get("doc_id") or not paper.get("sha256"):
        return None
    specific = review_index.get("claims", {}).get(claim.get("claim_id"))
    if (specific and specific["doc_id"] == doc_id and specific["pdf_sha256"] == paper["sha256"]
            and specific["claim_digest"] == claims_digest([claim])):
        return specific
    whole = review_index.get("documents", {}).get(doc_id)
    if whole and whole["pdf_sha256"] == paper["sha256"]:
        return whole
    return None


def resolve_map_source_kind(paper: dict, claim: dict, review_index: dict | None = None) -> str | None:
    """Return an override, or None for the ordinary research-map classifier.

    Signed claim-level exceptions can distinguish a measured demonstration from
    a specification within the same equipment deck. Unreviewed presentations,
    corporate authors, keywords and titles are never grounds to downgrade a result.
    """
    if paper.get("doc_id") != claim.get("doc_id"):
        return None
    reviewed = source_kind_review(paper, claim, review_index)
    if reviewed:
        return reviewed["kind"]
    classification = paper.get("classification") or {}
    forms = {paper.get("source_type"), classification.get("document_form")}
    kind = claim.get("claim_type")
    if "patent" in forms:
        return "patent_disclosure"
    if (kind in {"cited_prior_work", "review_summary"} or paper.get("is_review")
            or classification.get("content_nature") == "review"):
        return "review_or_cited"
    if kind == "test_method" or "test_method" in forms:
        return "test_method"
    if kind == "standard_requirement" or "standard" in forms:
        return "technical_guidance"
    if (kind == "manufacturer_specification" or "datasheet" in forms
            or classification.get("content_nature") == "product_specification"):
        return "manufacturer"
    return None
