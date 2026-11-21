from __future__ import annotations
import json
import re
from collections import Counter
from typing import Any, Literal
import pymupdf
from pydantic import BaseModel, ConfigDict, Field
from kg_pilot.pipeline import (PILOT, LegacyRebuildError, ensure_legacy_rebuild_safe,
                               jsonl, read_json, write_json, write_jsonl)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    pdf_page: int = Field(ge=1)
    quote: str = Field(min_length=8)
    evidence_id: str | None = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="allow")
    claim_id: str
    doc_id: str
    statement: str
    material_system: str
    sample_id: str
    stage: Literal["surface_preparation", "contact_bonding", "post_bond_annealing", "measurement",
                   "reliability_test", "material_characterization", "background"]
    quantity: str
    value_text: str
    unit: str | None
    conditions: dict[str, Any]
    evidence: list[Evidence] = Field(min_length=1)
    claim_type: Literal["reported_experiment", "reported_simulation", "author_interpretation", "review_summary", "cited_prior_work",
                        "manufacturer_specification", "recommended_process", "standard_requirement", "test_method"]
    review_status: str


def whitespace(text):
    return re.sub(r"\s+", " ", text).strip()


def quote_supported(quote: str, source_text: str) -> bool:
    # Deliberately do not normalize symbols or digits: verbatim quotations are auditable.
    return whitespace(quote) in whitespace(source_text)


def load_source_pages():
    from kg_pilot.text_quality import repair_document_text
    pages = {}
    for paper in read_json(PILOT / "catalog" / "papers.json"):
        with pymupdf.open(paper["local_path"]) as doc:
            records = [dict(pdf_page=i+1, text=page.get_text()) for i, page in enumerate(doc)]
        repaired, _, _, _ = repair_document_text(paper["local_path"], records, [], document_sha256=paper["sha256"])
        pages[paper["doc_id"]] = {record["pdf_page"]: record["text"] for record in repaired}
    ocr_path = PILOT / "benchmarks" / "docling" / "reliability_small_die_2022_p1-7" / "document.json"
    ocr_pages = {}
    if ocr_path.exists():
        for item in read_json(ocr_path).get("texts", []):
            for prov in item.get("prov", []):
                ocr_pages.setdefault(prov["page_no"], []).append(item.get("text", ""))
    pages["reliability_small_die_2022:ocr"] = {p: "\n".join(t) for p, t in ocr_pages.items()}
    return pages


def merge():
    ensure_legacy_rebuild_safe(PILOT)
    claims = [c for group in ("imec", "journals", "reliability") for c in jsonl(PILOT / "annotations" / f"claims_{group}.jsonl")]
    reviews = [r for group in ("imec", "journals", "reliability") for r in jsonl(PILOT / "annotations" / f"reviews_{group}.jsonl")]
    review_by_id = {r["claim_id"]: r for r in reviews}
    sources = load_source_pages()
    accepted, quarantine, audit, seen = [], [], [], set()
    for original in claims:
        c = json.loads(json.dumps(original, ensure_ascii=False))
        review = review_by_id.get(c.get("claim_id"))
        errors = []
        if c.get("claim_id") in seen:
            errors.append("duplicate_claim_id")
        seen.add(c.get("claim_id"))
        if review and review.get("proposed_patch"):
            c.update(review["proposed_patch"])
        try:
            Claim.model_validate(c)
            from kg_pilot.text_quality import guard_claim_text_quality
            guard_claim_text_quality(c)
        except Exception as exc:
            errors.append(f"schema: {exc}")
        for ev in c.get("evidence", []):
            key = c["doc_id"]
            if ev.get("parser") == "docling_rapidocr":
                key += ":ocr"
            page_text = sources.get(key, {}).get(ev["pdf_page"], "")
            if not quote_supported(ev["quote"], page_text):
                errors.append(f"quote_not_exact_on_page:{ev['pdf_page']}")
        if not review:
            errors.append("no_independent_review")
        elif review["status"] == "rejected":
            errors.append("independent_reviewer_rejected")
        elif review["status"] == "needs_fix" and not review.get("proposed_patch"):
            errors.append("unresolved_review_issue")
        if errors:
            quarantine.append({"claim": c, "errors": errors, "review": review})
        else:
            c["review_status"] = "independent_agent_reviewed"
            c["verification_label"] = "automated_review_not_human_gold"
            accepted.append(c)
        audit.append({"claim_id": c["claim_id"], "schema_and_quote_pass": not errors,
                      "errors": errors, "review": review, "patch_applied": bool(review and review.get("proposed_patch"))})
    write_jsonl(PILOT / "annotations" / "claims.jsonl", accepted)
    write_json(PILOT / "annotations" / "quarantine.json", quarantine)
    write_json(PILOT / "reports" / "claim_validation_audit.json", audit)
    summary = {"candidate_claims": len(claims), "independent_reviews": len(reviews), "accepted_claims": len(accepted),
               "quarantined_claims": len(quarantine), "accepted_documents": len({c['doc_id'] for c in accepted}),
               "corrected_by_reviewer": sum(a["patch_applied"] for a in audit),
               "claim_types": dict(Counter(c["claim_type"] for c in accepted)),
               "stages": dict(Counter(c["stage"] for c in accepted)), "label": "automatic_agent_review"}
    write_json(PILOT / "reports" / "claim_validation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    try:
        merge()
    except LegacyRebuildError as error:
        raise SystemExit(str(error)) from None
