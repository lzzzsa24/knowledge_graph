"""Regenerate the Markdown reading copies of published papers from local sources."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg_pilot import ingestion_publish as publication
from kg_pilot.pipeline import PILOT, jsonl, read_json
from kg_pilot.reading_sources import REVIEW_INPUTS, render_paper_reading_copy


def build_reading_copies(pilot=PILOT, doc_ids=None, *, check=False):
    pilot = Path(pilot).resolve()
    changed = {}
    source_hashes = {}
    with publication.dataset_lock(pilot):
        publication._ensure_idle(pilot)
        papers = read_json(pilot / "catalog/papers.json")
        if doc_ids:
            papers = [p for p in papers if p["doc_id"] in doc_ids]
            if {p["doc_id"] for p in papers} != set(doc_ids):
                raise ValueError("文献目录中找不到指定的 doc_id。")
        inputs = ["catalog/papers.json", *REVIEW_INPUTS]
        for paper in papers:
            doc_id = publication._safe_id(paper["doc_id"], "doc_id")
            relative = Path("parsed") / doc_id
            directory = publication._inside(pilot, relative)
            target = relative / "fulltext.md"
            inputs.extend(str(relative / name) for name in (
                "pages.json", "elements.jsonl", "tables.json", "elements_ocr.jsonl", "fulltext.md"))
            pages = read_json(directory / "pages.json")
            if isinstance(pages, dict):
                pages = pages["pages"]
            if sorted(p["pdf_page"] for p in pages) != list(range(1, paper["page_count"] + 1)):
                raise ValueError(f"{doc_id} 的解析页码与目录不符。")
            pdf = Path(paper["local_path"]).resolve()
            if not pdf.is_file():
                raise ValueError(f"{doc_id} 的原 PDF 路径无效。")
            digest = publication._sha(pdf)
            if paper.get("sha256") and paper["sha256"] != digest:
                raise ValueError(f"{doc_id} 的原 PDF 与入库时的文件不一致。")
            source_hashes[str(pdf)] = digest
            markdown = render_paper_reading_copy(pilot, paper, pages, jsonl(directory / "elements.jsonl"),
                markdown_path=pilot / target, tables=read_json(directory / "tables.json"),
                ocr_elements=jsonl(directory / "elements_ocr.jsonl"))
            content = markdown.encode("utf-8")
            if not (pilot / target).is_file() or (pilot / target).read_bytes() != content:
                changed[target] = content
        fingerprint = publication._fingerprint(pilot, inputs) if changed and not check else None
        if fingerprint is not None:
            fingerprint.update(source_hashes)
    summary = {"documents": len(papers), "pages": sum(p["page_count"] for p in papers), "changed": len(changed)}
    if check or not changed:
        return summary

    batch_id = "reading-markdown-" + uuid.uuid4().hex
    staging = publication._inside(pilot, Path("ingestion/staging") / batch_id)
    staging.mkdir(parents=True)
    for target, content in changed.items():
        path = staging / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    # Recheck the catalog, original inputs, and any existing reading copies
    # under the same lock before swapping just the Markdown files.
    publication._commit(staging, pilot, list(changed), fingerprint, batch_id)
    shutil.rmtree(staging)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-id", action="append", help="只检查或更新指定文献，可重复。")
    parser.add_argument("--check", action="store_true", help="只核对，不写文件。存在待更新文件时返回 1。")
    args = parser.parse_args()
    result = build_reading_copies(doc_ids=args.doc_id, check=args.check)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(int(args.check and bool(result["changed"])))
