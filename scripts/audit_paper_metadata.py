"""Check the public metadata projection and the sources of reviewed overrides."""

import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg_pilot.paper_metadata import PAPER_METADATA_OVERRIDES, apply_paper_metadata, load_paper_metadata_overrides


def normalized_quote(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().replace("ﬁ", "fi")


def main() -> int:
    pilot = ROOT / "pilot"
    raw_catalog = (pilot / "catalog/papers.json").read_bytes()
    original = json.loads(raw_catalog.decode("utf-8-sig"))
    overrides = load_paper_metadata_overrides(pilot)
    declared = json.loads((pilot / PAPER_METADATA_OVERRIDES).read_text(encoding="utf-8-sig"))
    if not overrides or overrides != declared.get("documents"):
        raise ValueError("Missing or invalid paper metadata overrides")

    papers = apply_paper_metadata(original, pilot)
    by_id = {paper["doc_id"]: paper for paper in original}
    errors = []
    verified_quotes = visual_quotes = publisher_sources = 0
    for doc_id, entry in overrides.items():
        paper = by_id.get(doc_id)
        if not paper or entry.get("sha256") != paper.get("sha256"):
            errors.append(f"{doc_id}: override does not match catalog PDF identity")
            continue
        if not entry.get("reason") or not entry.get("evidence"):
            errors.append(f"{doc_id}: override lacks evidence or rationale")
            continue
        path = Path(paper["local_path"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                errors.append(f"{doc_id}: actual PDF bytes differ from override")
                continue
        with pymupdf.open(path) as document:
            for source in entry["evidence"]:
                if source["kind"] == "publisher_metadata":
                    if not source.get("url", "").startswith("https://"):
                        errors.append(f"{doc_id}: missing primary metadata URL")
                    publisher_sources += 1
                    continue
                if source["kind"] != "pdf" or not 1 <= source["page"] <= len(document) or not source["quote"]:
                    errors.append(f"{doc_id}: invalid PDF page evidence")
                    continue
                if source.get("visual"):
                    visual_quotes += 1
                    continue
                page_text = document[source["page"] - 1].get_text()
                if normalized_quote(source["quote"]) not in normalized_quote(page_text):
                    errors.append(f"{doc_id}: quote absent from PDF page {source['page']}")
                else:
                    verified_quotes += 1

    for raw, paper in zip(original, papers, strict=True):
        doc_id = raw["doc_id"]
        authors = paper["authors"]
        if not raw.get("authors") or isinstance(raw["authors"], str):
            if doc_id not in overrides:
                errors.append(f"{doc_id}: unreviewed missing or string author field")
        if not isinstance(authors, list) or any(not isinstance(name, str) or not name.strip() for name in authors):
            errors.append(f"{doc_id}: invalid author list")
        if not authors and not paper.get("organization"):
            errors.append(f"{doc_id}: no personal or institutional attribution")
        if not paper["short_label"].strip() or paper["short_label"].startswith("Unknown "):
            errors.append(f"{doc_id}: missing label")
        if any(paper.get(key) != value for key, value in raw.items() if key not in ("authors", "organization", "short_label")):
            errors.append(f"{doc_id}: projection changed another catalog field")

    print(json.dumps({
        "catalog_sha256": hashlib.sha256(raw_catalog).hexdigest(),
        "papers": len(papers),
        "original_missing_authors": sum(not paper.get("authors") for paper in original),
        "original_string_authors": sum(isinstance(paper.get("authors"), str) for paper in original),
        "papers_with_personal_authors": sum(bool(paper["authors"]) for paper in papers),
        "papers_with_institutional_attribution": sum(not paper["authors"] and bool(paper.get("organization")) for paper in papers),
        "undated_labels": [paper["doc_id"] for paper in papers if paper["short_label"].endswith(" n.d.")],
        "reviewed_overrides": len(overrides),
        "verified_pdf_text_quotes": verified_quotes,
        "visually_reviewed_pdf_quotes": visual_quotes,
        "primary_metadata_urls": publisher_sources,
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
