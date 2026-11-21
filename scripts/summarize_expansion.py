"""Write a source inventory and measured counts after the campaign is audited."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot.classification import FACET_DEFINITIONS, CATEGORY_LABELS
from kg_pilot.pipeline import read_json
from kg_pilot.ingestion import now, write_atomic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-total", type=int, required=True)
    args = parser.parse_args()
    pilot = ROOT / "pilot"
    reports = pilot / "reports/expansion_20261103"
    audit = read_json(reports / f"audit_{args.expected_total}.json")
    papers = read_json(pilot / "catalog/papers.json")
    classifications = {c["doc_id"]: c for c in read_json(pilot / "catalog/classifications.json")["records"]}
    claims = [json.loads(line) for line in (pilot / "annotations/claims.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if audit["status"] != "passed" or len(papers) != args.expected_total or audit["counts"]["claims"] != len(claims):
        raise ValueError("Current corpus does not match a passing audit.")
    selected = {r["sha256"]: r for r in read_json(reports / "source_selection.json")["selected"]}
    baseline = {p["doc_id"] for p in read_json(reports / "baseline/catalog/papers.json")}
    forms = next(labels for field, _, _, labels in FACET_DEFINITIONS if field == "document_form")
    claim_counts = Counter(c["doc_id"] for c in claims)
    inventory = []
    for paper in papers:
        c = classifications[paper["doc_id"]]
        source = selected.get(paper["sha256"], {})
        inventory.append({"doc_id": paper["doc_id"], "title": paper["title"], "baseline": paper["doc_id"] in baseline,
            "source_id": source.get("id"), "year": paper.get("year"), "publisher": paper.get("publisher"),
            "document_form": c["document_form"], "document_form_label": forms.get(c["document_form"], c["document_form"]),
            "primary_category": c["primary_category"], "primary_category_label": CATEGORY_LABELS[c["primary_category"]],
            "relevance": c["relevance"], "page_count": paper["page_count"], "claim_count": claim_counts[paper["doc_id"]],
            "document_number": paper.get("document_number"), "version": paper.get("version"), "status": paper.get("status"),
            "source_url": paper.get("source_url") or paper.get("publisher_url"), "pdf_url": paper.get("pdf_url"),
            "sha256": paper["sha256"], "local_path": paper["local_path"], "ingestion_batch_id": paper.get("ingestion_batch_id")})
    new = [p for p in inventory if not p["baseline"]]
    report = {"created_at": now(), "status": "audited", "audit": str((reports / f"audit_{args.expected_total}.json").resolve()),
        "counts": {"documents": len(papers), "new_documents": len(new), "pages": sum(p["page_count"] for p in papers),
                   "new_pages": sum(p["page_count"] for p in new), "claims": len(claims), "new_claims": sum(p["claim_count"] for p in new)},
        "all_document_forms": dict(Counter(p["document_form_label"] for p in inventory)),
        "new_document_forms": dict(Counter(p["document_form_label"] for p in new)),
        "all_primary_categories": dict(Counter(p["primary_category_label"] for p in inventory)),
        "claim_types": dict(Counter(c["claim_type"] for c in claims)),
        "batch_ids": audit["batch_ids"], "vectors": audit["vectors"],
        "boundary": "重点知识抽取与独立agent自动复核；非全文穷尽、非专家金标准；标准和厂商指标保留适用对象与条件。",
        "inventory": inventory}
    output = reports / f"summary_{args.expected_total}.json"
    write_atomic(output, report)
    print(json.dumps({k: report[k] for k in ["counts", "all_document_forms", "new_document_forms", "all_primary_categories", "claim_types"]}, ensure_ascii=False, indent=2))
    print(str(output.resolve()))


if __name__ == "__main__":
    main()
