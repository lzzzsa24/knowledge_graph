"""Audit a frozen research map against a source-first, purposive benchmark.

This deliberately does not score keyword overlap as scientific correctness.
The membership and count checks are machine-checkable; scientific boundaries
and cross-paper conclusions require an explicit source-based review file.
No service is started, stopped, restarted, or mutated by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "pilot/reports/research_map_20261107"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ids(records: list[Any]) -> list[str]:
    result = []
    for item in records:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("claim_id"), str):
            result.append(item["claim_id"])
        else:
            raise ValueError(f"Record must be claim_id or a dict with claim_id: {str(item)[:200]}")
    return result


def bound_source_reviews(path: Path, papers: dict, claims: dict) -> dict:
    """Independently validate SHA-bound provenance sidecars; never trust API labels."""
    data = read(path) if path.is_file() else {}
    bound = {"documents": {}, "claims": {}}
    if data.get("schema") != 1:
        return bound
    for scope, key in (("documents", "doc_id"), ("claims", "claim_id")):
        for row in data.get(scope, []):
            paper = papers.get(row.get("doc_id"), {})
            valid = bool(row.get("reason") and row.get("evidence") and paper.get("sha256")
                         and row.get("pdf_sha256") == paper.get("sha256"))
            valid &= all(type(e.get("pdf_page")) is int and 1 <= e["pdf_page"] <= paper.get("page_count", 0)
                         and bool(e.get("quote")) for e in row.get("evidence", []))
            if scope == "claims":
                c = claims.get(row.get("claim_id"))
                digest = hashlib.sha256(json.dumps([c], ensure_ascii=False, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest() if c else None
                valid &= bool(c and c["doc_id"] == row.get("doc_id") and digest == row.get("claim_digest"))
            if valid:
                bound[scope][row[key]] = row
    return bound


def independent_kind(claim: dict, paper: dict, classification: dict, reviews: dict) -> str:
    override = reviews.get("claims", {}).get(claim["claim_id"]) or reviews.get("documents", {}).get(claim["doc_id"])
    if override:
        return override["kind"]
    ctype, nature = claim.get("claim_type"), classification.get("content_nature")
    forms = {paper.get("source_type"), classification.get("document_form")}
    if "patent" in forms:
        return "patent_disclosure"
    if paper.get("is_review") or nature == "review" or ctype in {"review_summary", "cited_prior_work"}:
        return "review_or_cited"
    if ctype == "test_method" or "test_method" in forms:
        return "test_method"
    if ctype == "standard_requirement" or "standard" in forms:
        return "technical_guidance"
    if ctype == "manufacturer_specification" or "datasheet" in forms or nature == "product_specification":
        return "manufacturer"
    if nature in {"technical_guidance", "industry_overview", "normative_document"}:
        return "technical_guidance"
    return {"reported_experiment": "experiment", "reported_simulation": "simulation",
            "author_interpretation": "interpretation"}.get(ctype, "guidance_or_requirement")


def anchor_check(anchor: dict, memberships: dict, papers: dict, claims: dict, verify_pages: bool) -> dict:
    """A PDF-page anchor need not already have a claim or a matrix membership."""
    doc = papers.get(anchor.get("doc_id"), {})
    page = anchor.get("pdf_page")
    if anchor.get("doc_id") and type(page) is int:
        okay = bool(doc and 1 <= page <= doc.get("page_count", 0) and anchor.get("quote")
                    and anchor.get("pdf_sha256") == doc.get("sha256"))
        c = claims.get(anchor.get("claim_id")) if anchor.get("claim_id") else None
        if anchor.get("claim_id"):
            okay &= bool(c and c["doc_id"] == anchor.get("doc_id"))
        result = dict(anchor=anchor, kind="source_pdf_page", passed=okay, native_quote_verified=None)
        if okay and verify_pages:
            import pymupdf as fitz
            actual_pdf_hash_ok = sha(Path(doc["local_path"])) == doc.get("sha256")
            result["pdf_file_sha256_verified"] = actual_pdf_hash_ok
            def norm(s):
                return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s).casefold())
            with fitz.open(doc["local_path"]) as pdf:
                normalized_quote = norm(anchor["quote"])
                quote_found = len(normalized_quote) >= 8 and normalized_quote in norm(pdf[page-1].get_text())
            result["native_quote_verified"] = quote_found
            # A failed native-text match needs visual audit; do not invent a pass.
            result["passed"] = okay and actual_pdf_hash_ok and quote_found
        return result
    okay = anchor.get("claim_id") in memberships.get((anchor.get("row"), anchor.get("column")), set())
    return dict(anchor=anchor, kind="matrix_screening_link", passed=okay)


def audit(payload: dict, benchmark: dict, claims: dict, manual: dict | None = None,
          papers: dict | None = None, classifications: dict | None = None,
          source_reviews: dict | None = None, verify_pages: bool = False) -> dict:
    issues: list[dict] = []

    def issue(kind, **details):
        issues.append(dict(kind=kind, **details))

    rows = {r["id"] for r in payload.get("rows", [])}
    columns = {r["id"] for r in payload.get("columns", [])}
    memberships: dict[tuple[str, str], set[str]] = {}
    count_checks = []
    for cell in payload.get("cells", []):
        key = (cell.get("row"), cell.get("column"))
        if key[0] not in rows or key[1] not in columns:
            issue("undeclared_axis", row=key[0], column=key[1])
        if key in memberships:
            issue("duplicate_cell", row=key[0], column=key[1])
        try:
            record_ids = ids(cell.get("records", []))
        except ValueError as exc:
            issue("invalid_record", row=key[0], column=key[1], message=str(exc))
            record_ids = []
        cids = set(record_ids)
        memberships[key] = cids
        if len(cids) != len(record_ids):
            issue("duplicate_record", row=key[0], column=key[1])
        missing = sorted(cids - set(claims))
        if missing:
            issue("unknown_claims", row=key[0], column=key[1], claim_ids=missing)
        known = [claims[c] for c in cids if c in claims]
        cell_kinds = {}
        for c in known:
            kind = independent_kind(c, (papers or {}).get(c["doc_id"], {}),
                                    (classifications or {}).get(c["doc_id"], {}), source_reviews or {})
            relation = cell.get("reviewed_records", {}).get(c["claim_id"], {}).get("relation")
            if kind in {"experiment", "simulation"} and relation in {"method", "interpretation"}:
                kind = relation
            cell_kinds[c["claim_id"]] = kind
        experimental_records = [c for c in known if cell_kinds[c["claim_id"]] == "experiment"]
        # Contract clarification after the first run: experiments counts distinct
        # primary research documents, not records. This changes no frozen cell
        # expectations. Keep experimental record count separately observable.
        experimental_docs = {c["doc_id"] for c in experimental_records}
        expected = dict(
            claims=len(cids), documents=len({c["doc_id"] for c in known}),
            experiments=len(experimental_docs),
        )
        if "contrasts" in cell:
            expected["contrasts"] = len({c["doc_id"] for c in experimental_records
                if cell.get("reviewed_records", {}).get(c["claim_id"], {}).get("relation") == "contrast"})
        actual = {k: cell.get(k) for k in expected}
        check = dict(row=key[0], column=key[1], expected=expected, actual=actual,
                     experimental_record_count=len(experimental_records),
                     passed=all(actual[k] == expected[k] for k in expected))
        count_checks.append(check)
        for k in expected:
            if actual[k] != expected[k]:
                issue("cell_count_mismatch", row=key[0], column=key[1], field=k,
                      expected=expected[k], actual=actual[k])

    positives = []
    for item in benchmark["positives"]:
        key = (item["row"], item["column"])
        passed = item["claim_id"] in memberships.get(key, set())
        positives.append(dict(id=item["id"], claim_id=item["claim_id"], row=key[0],
                              column=key[1], passed=passed,
                              relation_scope=item["relation_scope"]))
    recall = sum(p["passed"] for p in positives) / len(positives)
    negatives = []
    for item in benchmark["negatives"]:
        rule = item.get("automatic_rule") or {}
        violations = []
        for forbidden in rule.get("forbid_memberships", []):
            for (row, col), cids in memberships.items():
                if (forbidden["claim_id"] in cids
                    and ("row" not in forbidden or forbidden["row"] == row)
                    and ("column" not in forbidden or forbidden["column"] == col)):
                    violations.append(dict(row=row, column=col, claim_id=forbidden["claim_id"]))
        manual_item = (manual or {}).get("negatives", {}).get(item["id"], {})
        state = manual_item.get("status", "unverified")
        has_audit = bool(state == "pass" and manual_item.get("evidence") and manual_item.get("notes"))
        negatives.append(dict(
            id=item["id"], name=item["name"], automatic_violations=violations,
            automatic_status=("fail" if violations else "pass" if rule else "not_applicable"),
            scientific_boundary_status="pass" if has_audit else state,
            scientific_boundary_verified=has_audit,
            review=manual_item,
        ))
    forbidden_count = sum(len(n["automatic_violations"]) for n in negatives)

    anchors_checked = []
    for index, direction in enumerate(payload.get("directions", [])):
        for field in ("question", "rationale", "controls", "measurement", "anchors"):
            if not direction.get(field):
                issue("incomplete_direction", index=index, field=field)
        for anchor in direction.get("anchors", []):
            checked = anchor_check(anchor, memberships, papers or {}, claims, verify_pages)
            anchors_checked.append(dict(direction=index, **checked))
            if not checked["passed"]:
                issue("invalid_direction_anchor", index=index, anchor=anchor)

    screening_checks = []
    published_ids = {d.get("id") for d in payload.get("directions", []) if d.get("id")}
    for index, candidate in enumerate(payload.get("screening_candidates", [])):
        checks = [anchor_check(a, memberships, papers or {}, claims, verify_pages) for a in candidate.get("anchors", [])]
        passed = bool(candidate.get("status") == "needs_fulltext_review" and checks
                      and all(c["passed"] for c in checks) and candidate.get("id") not in published_ids)
        screening_checks.append(dict(index=index, status=candidate.get("status"), anchors=checks, passed=passed))
        if not passed:
            issue("invalid_screening_candidate", index=index)

    comparison_audits = []
    for task in benchmark["comparisons"]:
        review = (manual or {}).get("comparisons", {}).get(task["id"], {})
        criterion_reviews = review.get("criteria", [])
        all_criteria = (len(criterion_reviews) == len(task["rubric"])
                        and all(c.get("status") == "pass" and c.get("evidence") and c.get("notes") for c in criterion_reviews))
        passed = bool(review.get("status") == "pass" and all_criteria and review.get("response_artifact")
                      and review.get("primary_document_count", 0) >= 2 and review.get("no_forbidden_inference") is True)
        comparison_audits.append(dict(id=task["id"], passed=passed,
                                     status="pass" if passed else review.get("status", "unverified"), review=review))

    totals = payload.get("totals", {})
    union = set().union(*memberships.values()) if memberships else set()
    mapped_docs = {claims[c]["doc_id"] for c in union if c in claims}
    for field, expected in (("mapped_documents", len(mapped_docs)), ("mapped_claims", len(union)),
                            ("documents", benchmark["provenance"]["corpus"]["documents"]),
                            ("claims", benchmark["provenance"]["corpus"]["claims"])):
        if totals.get(field) != expected:
            issue("totals_mismatch", field=field, expected=expected, actual=totals.get(field))
    membership_pass = recall >= benchmark["acceptance"]["minimum_positive_membership_recall"]
    structural_pass = not issues and forbidden_count == 0
    boundaries_pass = all(n["scientific_boundary_verified"] for n in negatives)
    comparison_pass = all(c["passed"] for c in comparison_audits)
    directions_review = (manual or {}).get("directions", {})
    directions_pass = bool(directions_review.get("status") == "pass" and directions_review.get("evidence")
                           and directions_review.get("notes"))
    scientific_pass = boundaries_pass and comparison_pass and directions_pass
    return dict(
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        benchmark_version=benchmark["benchmark_version"],
        label="purposive_source_first_benchmark_not_human_expert_gold",
        counter_contract="Post-freeze API clarification: experiments is distinct source-verified experimental documents, applying SHA-bound source-kind overrides first and excluding per-cell method/interpretation records. Contrasts is distinct experimental documents with reviewed contrast relation. Experimental record count is separate. Source-page potential anchors do not require an extracted claim. Screening links are audited separately. Frozen benchmark unchanged.",
        summary=dict(
            positive_hits=sum(p["passed"] for p in positives), positive_total=len(positives),
            sampled_membership_recall=recall, membership_acceptance=membership_pass,
            forbidden_memberships=forbidden_count, structural_acceptance=structural_pass,
            scientific_boundaries_verified=sum(n["scientific_boundary_verified"] for n in negatives),
            scientific_boundaries_total=len(negatives),
            comparisons_verified=sum(c["passed"] for c in comparison_audits), comparisons_total=len(comparison_audits),
            scientific_acceptance=scientific_pass,
            overall_acceptance=membership_pass and structural_pass and scientific_pass,
        ),
        limits=["24 source-first cases are purposive, not representative of all1439claims.",
                "Membership recall does not measure precision of every mapped record or causal correctness.",
                "Count separation is checked, but experimental scope and comparative conclusions still need source-based audit.",
                "No automatic scorer can certify global novelty from low local-corpus support."],
        positives=positives, negatives=negatives, cell_count_checks=count_checks,
        direction_anchors=anchors_checked, comparison_audits=comparison_audits,
        screening_candidate_checks=screening_checks,
        direction_audit=directions_review, structural_issues=issues,
    )


def review_template(benchmark: dict) -> dict:
    return dict(
        reviewer="", review_kind="independent_agent_source_audit_not_human_expert_gold",
        negatives={n["id"]: dict(status="unverified", notes="", evidence=[]) for n in benchmark["negatives"]},
        comparisons={c["id"]: dict(status="unverified", response_artifact="", primary_document_count=0,
                                   no_forbidden_inference=None,
                                   criteria=[dict(criterion=t, status="unverified", notes="", evidence=[]) for t in c["rubric"]])
                     for c in benchmark["comparisons"]},
        directions=dict(status="unverified", notes="", evidence=[]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, default=REPORTS / "benchmark.json")
    parser.add_argument("--map-json", type=Path)
    parser.add_argument("--base-url", help="Existing API server only; script never starts/stops servers")
    parser.add_argument("--manual-review", type=Path)
    parser.add_argument("--write-review-template", type=Path)
    parser.add_argument("--output", type=Path, default=REPORTS / "benchmark_evaluation.json")
    parser.add_argument("--verify-pdfs", action="store_true")
    parser.add_argument("--verify-anchor-pages", action="store_true")
    parser.add_argument("--source-kind-reviews", type=Path, default=ROOT / "pilot/research_map/source_kind_reviews.json")
    args = parser.parse_args()
    benchmark = read(args.benchmark)
    hashfile = args.benchmark.with_suffix(".sha256")
    expected_sha = hashfile.read_text(encoding="ascii").split()[0]
    if sha(args.benchmark) != expected_sha:
        raise SystemExit("Frozen benchmark SHA256 mismatch")
    if args.write_review_template:
        if args.write_review_template.exists():
            raise SystemExit("Review template destination exists; will not overwrite review")
        args.write_review_template.write_text(json.dumps(review_template(benchmark), ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(f"Wrote unverified audit template: {args.write_review_template}")
        return
    for rel, expected in benchmark["provenance"]["inputs"].items():
        if sha(ROOT / rel) != expected:
            raise SystemExit(f"Frozen source changed: {rel}; record corpus drift before evaluating")
    if args.verify_pdfs:
        for doc in benchmark["source_documents"]:
            if sha(Path(doc["pdf_path"])) != doc["sha256"]:
                raise SystemExit(f"Source PDF changed: {doc['doc_id']}")
    if bool(args.map_json) == bool(args.base_url):
        parser.error("Choose exactly one --map-json or --base-url")
    if args.map_json:
        payload = read(args.map_json)
        origin = str(args.map_json.resolve())
    else:
        origin = args.base_url.rstrip("/") + "/api/research-map"
        with urllib.request.urlopen(origin, timeout=60) as response:
            payload = json.load(response)
        raw = args.output.with_name(args.output.stem + "_raw_map.json")
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    claims = {c["claim_id"]: c for c in map(json.loads, (ROOT / "pilot/annotations/claims.jsonl").read_text(encoding="utf-8").splitlines())}
    manual = read(args.manual_review) if args.manual_review else None
    if manual and manual.get("benchmark_sha256") not in {None, sha(args.benchmark)}:
        raise SystemExit("Manual review is bound to a different frozen benchmark")
    papers = {p["doc_id"]: p for p in read(ROOT / "pilot/catalog/papers.json")}
    classifications = {p["doc_id"]: p for p in read(ROOT / "pilot/catalog/classifications.json").get("records", [])}
    source_reviews = bound_source_reviews(args.source_kind_reviews, papers, claims)
    report = audit(payload, benchmark, claims, manual, papers, classifications, source_reviews, args.verify_anchor_pages)
    report["source"] = dict(origin=origin, benchmark_sha256=sha(args.benchmark),
                           map_payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
                           manual_review_path=str(args.manual_review) if args.manual_review else None)
    report["source"]["source_kind_reviews"] = dict(path=str(args.source_kind_reviews), sha256=sha(args.source_kind_reviews) if args.source_kind_reviews.is_file() else None,
        bound_document_reviews=len(source_reviews["documents"]), bound_claim_reviews=len(source_reviews["claims"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
