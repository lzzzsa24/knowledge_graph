"""Add table evidence to selected published papers without replacing their text.

The preview never loads an embedding model or changes published artifacts.
Apply reuses the normal dataset lock, staged index validation, retained backups,
publication journal, maintenance flag and crash recovery. No PDF/model download
is performed. Existing claims, classifications and paper metadata are preserved.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pymupdf

from kg_pilot import ingestion_publish as publication
from kg_pilot.pipeline import PILOT, jsonl, read_json, write_json, write_jsonl
from kg_pilot.reading_sources import REVIEW_INPUTS, render_paper_reading_copy


REGISTRY = "catalog/table_evidence_backfills.json"


def _pages(value):
    # Exported pilot pages are a list; a prepared/cached document may wrap it.
    records = value.get("pages") if isinstance(value, dict) else value
    if not isinstance(records, list) or not records:
        raise publication.PublicationError("文献缺少已发布的逐页解析。")
    by_page = {item["pdf_page"]: item for item in records}
    if len(by_page) != len(records):
        raise publication.PublicationError("文献逐页解析含重复页码。")
    return by_page


def _merge(existing, proposed):
    """Append only missing IDs; never silently replace a conflicting extraction."""
    result, ids = list(existing), {}
    for item in existing:
        key = item.get("evidence_id")
        if key:
            if key in ids:
                raise publication.PublicationError("已有证据含重复 ID，停止回填。")
            ids[key] = item
    added = []
    for item in proposed:
        key = item["evidence_id"]
        if key in ids:
            # Audit annotations may evolve after a corpus quality sweep; they
            # do not turn an otherwise identical source table into a conflict.
            old_content = {name: value for name, value in ids[key].items() if name != "text_quality"}
            new_content = {name: value for name, value in item.items() if name != "text_quality"}
            if old_content != new_content:
                raise publication.PublicationError(f"表格证据 ID 已存在但内容不同，禁止覆盖：{key}")
        else:
            result.append(item)
            ids[key] = item
            added.append(item)
    return result, added


def _read_snapshot(pilot, doc_ids, pages, extractor):
    from kg_pilot.table_evidence import extract_table_evidence, validate_table_element
    from kg_pilot.text_quality import repair_document_text

    extractor = extractor or extract_table_evidence
    doc_ids = list(doc_ids)
    if not doc_ids or len(set(doc_ids)) != len(doc_ids):
        raise publication.PublicationError("请明确选择不重复的已发布 doc_id。")
    for doc_id in doc_ids:
        publication._safe_id(doc_id, "doc_id")
    papers = read_json(pilot / "catalog/papers.json")
    catalog = {paper["doc_id"]: paper for paper in papers}
    if len(catalog) != len(papers) or not set(doc_ids).issubset(catalog):
        raise publication.PublicationError("所选 doc_id 不在当前唯一文献目录中。")
    selected_pages = set(pages) if pages is not None else None
    if selected_pages is not None and (not selected_pages or any(type(p) is not int or p < 1 for p in selected_pages)):
        raise publication.PublicationError("页码必须为从 1 开始的正整数。")
    source_names = list(publication._SOURCE_FILES) + list(REVIEW_INPUTS) + [REGISTRY]
    native = jsonl(pilot / "parsed/evidence.jsonl")
    effective_path = pilot / "parsed/effective_evidence.jsonl"
    effective = jsonl(effective_path) if effective_path.exists() else list(native)
    documents = []
    for doc_id in doc_ids:
        paper = catalog[doc_id]
        pdf = Path(paper["local_path"]).resolve()
        if not pdf.is_file() or publication._sha(pdf) != paper["sha256"]:
            raise publication.PublicationError(f"已发布 PDF 的 SHA-256 不符：{doc_id}")
        relative = Path("parsed") / doc_id
        by_page = _pages(read_json(pilot / relative / "pages.json"))
        expected_pages = set(range(1, paper["page_count"] + 1))
        if set(by_page) != expected_pages or (selected_pages is not None and not selected_pages.issubset(by_page)):
            raise publication.PublicationError(f"逐页解析或所选页码与文献不符：{doc_id}")
        proposed = []
        with pymupdf.open(pdf) as source:
            if source.page_count != paper["page_count"]:
                raise publication.PublicationError(f"已发布 PDF 页数不符：{doc_id}")
            for page_number in sorted(selected_pages or by_page):
                page = source[page_number - 1]
                parsed_page = by_page[page_number]
                if abs(float(parsed_page["width"]) - page.rect.width) > 0.01 or abs(float(parsed_page["height"]) - page.rect.height) > 0.01:
                    raise publication.PublicationError(f"原 PDF 与已发布页尺寸不符：{doc_id} p{page_number}")
                for item in extractor(page, doc_id=doc_id, document_sha256=paper["sha256"]):
                    if item.get("kind") != "table" or item.get("doc_id") != doc_id or item.get("document_sha256") != paper["sha256"] or item.get("pdf_page") != page_number:
                        raise publication.PublicationError("表格来源、页码或文献哈希不一致。")
                    try:
                        validate_table_element(item, parsed_page)
                    except ValueError as error:
                        raise publication.PublicationError(f"表格结构未通过校验：{error}") from error
                    proposed.append(item)
        if not proposed:
            raise publication.PublicationError(f"所选页面未提取到通过校验的表格：{doc_id}")
        _, proposed, _, quality = repair_document_text(pdf, [], proposed, document_sha256=paper["sha256"])
        for item in proposed:
            try:
                validate_table_element(item, by_page[item["pdf_page"]])
            except ValueError as error:
                raise publication.PublicationError(f"字形修复后的表格结构未通过校验：{error}") from error
        if publication._sha(pdf) != paper["sha256"]:
            raise publication.PublicationError(f"解析期间 PDF 已变化：{doc_id}")
        old_elements = jsonl(pilot / relative / "elements.jsonl")
        tables_path = pilot / relative / "tables.json"
        old_tables = read_json(tables_path) if tables_path.exists() else []
        if not isinstance(old_tables, list):
            raise publication.PublicationError(f"历史表格文件格式无效：{doc_id}")
        elements, added_elements = _merge(old_elements, proposed)
        tables, added_tables = _merge(old_tables, proposed)
        markdown_path = pilot / relative / "fulltext.md"
        markdown = render_paper_reading_copy(
            pilot, paper, list(by_page.values()), elements, markdown_path=markdown_path, tables=tables,
            ocr_elements=jsonl(pilot / relative / "elements_ocr.jsonl"))
        documents.append(dict(doc_id=doc_id, paper=paper, tables=tables,
                              elements=elements, proposed=proposed, text_quality=quality,
                              reading_markdown=markdown,
                              reading_changed=not markdown_path.is_file() or markdown_path.read_bytes() != markdown.encode("utf-8"),
                              changed=bool(added_elements or added_tables)))
        source_names.extend([str(relative / name) for name in (
            "pages.json", "elements.jsonl", "elements_ocr.jsonl", "tables.json", "fulltext.md", "table_text_quality.json")])
        source_names.append(str(pdf))
    proposed = [element for document in documents for element in document["proposed"]]
    merged_native, native_added = _merge(native, proposed)
    merged_effective, effective_added = _merge(effective, proposed)
    fingerprint = publication._fingerprint(pilot, source_names)
    # The expected PDF hash is the catalog identity, even if an external writer
    # changes the file between the last parsing check and fingerprint capture.
    for document in documents:
        fingerprint[str(Path(document["paper"]["local_path"]).resolve())] = document["paper"]["sha256"]
    return dict(papers=papers, claims=jsonl(pilot / "annotations/claims.jsonl"),
                documents=documents, native=merged_native, effective=merged_effective,
                native_added=native_added, effective_added=effective_added,
                registry=read_json(pilot / REGISTRY) if (pilot / REGISTRY).exists() else {"version": 1, "documents": {}},
                fingerprint=fingerprint)


def _report(snapshot):
    return {"doc_ids": [document["doc_id"] for document in snapshot["documents"]],
            "added_documents": 0, "added_claims": 0,
            "added_native_evidence": len(snapshot["native_added"]),
            "added_evidence": len(snapshot["effective_added"]),
            "documents": [{"doc_id": document["doc_id"],
                           "source_sha256": document["paper"]["sha256"],
                           "tables": document["proposed"]}
                          for document in snapshot["documents"]]}


def preview_backfill(doc_ids, pilot: Path = PILOT, *, pages=None, extractor=None):
    """Return full proposed tables for inspection, without BGE or index writes."""
    pilot = Path(pilot).resolve()
    with publication.dataset_lock(pilot):
        publication._ensure_idle(pilot)
        snapshot = _read_snapshot(pilot, doc_ids, pages, extractor)
    return {"status": "preview", **_report(snapshot)}


def backfill_tables(doc_ids, pilot: Path = PILOT, *, pages=None, embedder=None,
                    extractor=None, index_builder=None):
    """Append evidence, preserve all old text/OCR/claims, then atomically publish.

    The injected extractor/embedder/index builder are only for isolated tests.
    Default embeddings come from the existing local/offline BGE-M3 loader.
    If table evidence is already complete, a stale reading copy is refreshed
    through the same backup/journal without rebuilding vectors or indexes.
    """
    pilot = Path(pilot).resolve()
    batch_id = "table-backfill-" + uuid.uuid4().hex
    stage = pilot / "ingestion/staging" / batch_id
    stage.mkdir(parents=True)
    try:
        with publication.dataset_lock(pilot):
            publication._ensure_idle(pilot)
            snapshot = _read_snapshot(pilot, doc_ids, pages, extractor)
            evidence_changed = bool(snapshot["native_added"] or snapshot["effective_added"] or
                                    any(document["changed"] for document in snapshot["documents"]))
            reading_changes = [document for document in snapshot["documents"] if document["reading_changed"]]
            if not evidence_changed and not reading_changes:
                return {"status": "already_backfilled", "updated_reading_documents": 0,
                        **_report(snapshot)}
            # Freeze the exact vector matrix/records associated with this snapshot.
            if evidence_changed and (pilot / "benchmarks/embedding").exists():
                publication._copy(pilot / "benchmarks/embedding", stage / "benchmarks/embedding")
            if evidence_changed:
                for name in ("all_records.json", "all_vectors.npy"):
                    relative = Path("benchmarks/embedding") / name
                    if (pilot / relative).exists():
                        publication._copy(pilot / relative, stage / "source" / relative)
        if not evidence_changed:
            targets = []
            for document in reading_changes:
                relative = Path("parsed") / document["doc_id"] / "fulltext.md"
                (stage / relative).parent.mkdir(parents=True, exist_ok=True)
                (stage / relative).write_text(document["reading_markdown"], encoding="utf-8", newline="\n")
                targets.append(relative)
            backup = publication._commit(stage, pilot, targets, snapshot["fingerprint"], batch_id)
            result = {"status": "already_backfilled", "batch_id": batch_id,
                      "backup_path": str(backup), "updated_reading_documents": len(reading_changes),
                      **_report(snapshot)}
            publication._atomic_json(stage / "publication_result.json", result)
            return result
        write_json(stage / "catalog/papers.json", snapshot["papers"])
        write_jsonl(stage / "annotations/claims.jsonl", snapshot["claims"])
        write_jsonl(stage / "parsed/evidence.jsonl", snapshot["native"])
        write_jsonl(stage / "parsed/effective_evidence.jsonl", snapshot["effective"])
        targets = [Path(name) for name in ("parsed/evidence.jsonl", "parsed/effective_evidence.jsonl", REGISTRY, "indexes", "benchmarks/embedding")]
        registry = deepcopy(snapshot["registry"])
        if not isinstance(registry, dict) or not isinstance(registry.get("documents"), dict):
            raise publication.PublicationError("表格回填来源记录格式无效。")
        for document in snapshot["documents"]:
            doc_id = document["doc_id"]
            relative = Path("parsed") / doc_id
            write_jsonl(stage / relative / "elements.jsonl", document["elements"])
            write_json(stage / relative / "tables.json", document["tables"])
            write_json(stage / relative / "table_text_quality.json", document["text_quality"])
            (stage / relative / "fulltext.md").write_text(document["reading_markdown"], encoding="utf-8", newline="\n")
            targets.extend([relative / "elements.jsonl", relative / "tables.json", relative / "fulltext.md", relative / "table_text_quality.json"])
            previous = registry["documents"].get(doc_id, {})
            if previous.get("source_sha256") not in {None, document["paper"]["sha256"]}:
                raise publication.PublicationError("历史表格回填记录与文献哈希不符。")
            previous_ids = previous.get("evidence_ids", [])
            registry["documents"][doc_id] = dict(source_sha256=document["paper"]["sha256"],
                evidence_ids=sorted(set(previous_ids) | {item["evidence_id"] for item in document["proposed"]}),
                publication_batch_id=batch_id, updated_at=publication._now())
        write_json(stage / REGISTRY, registry)
        new_ids = {item["evidence_id"] for item in snapshot["effective_added"]}
        vector_count = publication._vectors(stage, pilot, snapshot["effective"], set(), embedder,
            vector_source=stage / "source", new_evidence_ids=new_ids)
        (index_builder or publication._build_indexes)(stage)
        publication._validate_stage(stage, quality_doc_ids={document["doc_id"]
            for document in snapshot["documents"] if document["changed"]})
        backup = publication._commit(stage, pilot, targets, snapshot["fingerprint"], batch_id)
        result = {"status": "backfilled", "batch_id": batch_id,
                  "backup_path": str(backup), "vector_count": vector_count,
                  "updated_reading_documents": len(reading_changes),
                  **_report(snapshot)}
        publication._atomic_json(stage / "publication_result.json", result)
        return result
    except Exception as error:
        publication._atomic_json(stage / "publication_failure.json", {
            "batch_id": batch_id, "status": "failed", "error_type": type(error).__name__,
            "time": publication._now()})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preview", "apply", "recover", "status"])
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument("--pages", nargs="+", type=int)
    parser.add_argument("--pilot", type=Path, default=PILOT)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args(argv)
    if args.action in {"preview", "apply"} and not args.doc_id:
        parser.error("preview/apply requires at least one explicit --doc-id")
    if args.action == "preview":
        result = preview_backfill(args.doc_id, args.pilot, pages=args.pages)
    elif args.action == "apply":
        result = backfill_tables(args.doc_id, args.pilot, pages=args.pages)
    elif args.action == "recover":
        result = publication.recover_publication(args.pilot)
    else:
        result = publication.maintenance_status(args.pilot)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
