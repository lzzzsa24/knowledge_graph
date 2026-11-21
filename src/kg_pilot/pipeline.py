from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "pilot"


class LegacyRebuildError(RuntimeError):
    """Legacy one-off rebuilds must not discard subsequently ingested data."""


def ensure_legacy_rebuild_safe(pilot: Path | None = None):
    pilot = PILOT if pilot is None else Path(pilot)
    table_backfills_path = pilot / "catalog/table_evidence_backfills.json"
    if table_backfills_path.is_file():
        try:
            backfills = read_json(table_backfills_path)
            if not isinstance(backfills, dict) or not isinstance(backfills.get("documents"), dict):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise LegacyRebuildError("表格回填来源记录无法核对，已阻止旧试点重建流程。") from None
        if backfills["documents"]:
            raise LegacyRebuildError("文献库已补充表格证据，旧试点重建流程可能丢失补充数据，已停止；请使用批量入库或表格回填入口。现有合并数据仍可用 pipeline index 重建索引。")
    readiness_path = pilot / "catalog/ingestion_readiness.json"
    if not readiness_path.is_file():
        return
    try:
        readiness = read_json(readiness_path)
        if not isinstance(readiness, dict) or not isinstance(readiness.get("documents"), dict):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise LegacyRebuildError("批量入库状态无法核对，已阻止旧试点重建流程；请使用批量入库入口检查或恢复。") from None
    if readiness["documents"]:
        raise LegacyRebuildError("文献库已包含新增批次，旧试点重建流程可能丢失新资料，已停止；请使用批量入库入口。现有合并数据仍可用 pipeline index 重建索引。")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(s) for s in path.read_text(encoding="utf-8-sig").splitlines() if s.strip()]


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in records), encoding="utf-8")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def assemble():
    ensure_legacy_rebuild_safe()
    records, rejected, keys = [], [], set()
    for manifest in sorted((PILOT / "collection").glob("*/manifest.json")):
        items = read_json(manifest)
        if isinstance(items, dict):
            items = items.get("papers", items.get("records", items.get("documents", [])))
        for source in items:
            item = dict(source)
            item.pop("first_page_text", None)
            path = Path(item.get("local_path") or item.get("pdf_path") or "")
            if not path.is_absolute():
                path = manifest.parent / path
            if not path.is_file() or path.suffix.lower() != ".pdf":
                rejected.append({**item, "reason": "no_downloaded_pdf", "manifest": str(manifest)})
                continue
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            doi = (item.get("doi") or "").lower().strip().removeprefix("https://doi.org/")
            identity = doi or sha
            if identity in keys or sha in keys:
                rejected.append({**item, "reason": "duplicate_doi_or_sha256", "sha256": sha})
                continue
            source_type = str(item.get("source_type", "")).lower()
            if any(x in source_type for x in ("presentation", "slide", "poster")):
                rejected.append({**item, "reason": "not_a_paper"})
                continue
            scope = str(item.get("scope", ""))
            if scope == "background" or scope.startswith("supporting_process"):
                rejected.append({**item, "reason": "background_retained_outside_first_pilot"})
                continue
            try:
                with fitz.open(path) as doc:
                    pages = len(doc)
                    first = "\n".join(doc[i].get_text() for i in range(min(2, pages)))
                    total_chars = sum(len(p.get_text()) for p in doc)
            except Exception as exc:
                rejected.append({**item, "reason": f"pdf_open_error: {exc}"})
                continue
            doc_id = item.get("id") or f"{manifest.parent.name}_{path.stem}"
            keys.update([identity, sha])
            item.update(doc_id=doc_id, doi=doi or None, local_path=str(path.resolve()), sha256=sha,
                        page_count=pages, native_text_characters=total_chars,
                        manifest_path=str(manifest.relative_to(ROOT)), retrieval_date="2026-11-02")
            item["scope"] = item.get("scope", item.get("scope_class", "needs_scope_review"))
            records.append(item)
            preview = PILOT / "catalog" / "identity_previews" / f"{doc_id}.txt"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_text(first, encoding="utf-8")
    records.sort(key=lambda x: x["doc_id"])
    overrides_path = PILOT / "catalog" / "overrides.json"
    overrides = read_json(overrides_path) if overrides_path.exists() else {}
    for record in records:
        record.update(overrides.get(record["doc_id"], {}))
    write_json(PILOT / "catalog" / "papers.json", records)
    write_json(PILOT / "catalog" / "excluded.json", rejected)
    stats = {"unique_papers": len(records), "pages": sum(r["page_count"] for r in records),
             "reviews": sum(bool(r.get("is_review")) for r in records),
             "scope_counts": dict(Counter(str(r["scope"]) for r in records)), "excluded_entries": len(rejected)}
    write_json(PILOT / "catalog" / "statistics.json", stats)
    print(json.dumps(stats, ensure_ascii=False))


def parse():
    ensure_legacy_rebuild_safe()
    from kg_pilot.reading_sources import render_paper_reading_copy
    from kg_pilot.text_quality import repair_document_text

    all_evidence, stats = [], []
    for paper in read_json(PILOT / "catalog" / "papers.json"):
        started = time.perf_counter()
        doc_id = paper["doc_id"]
        blocks, pages, table_records = [], [], []
        with fitz.open(paper["local_path"]) as doc:
            for page_index, page in enumerate(doc):
                text = page.get_text("text", sort=True)
                pages.append({"pdf_page": page_index + 1, "text": text,
                              "width": page.rect.width, "height": page.rect.height})
                for block_index, block in enumerate(page.get_text("blocks", sort=True)):
                    if len(block) < 7 or block[6] != 0 or not str(block[4]).strip():
                        continue
                    content = str(block[4])
                    record = {"evidence_id": f"{doc_id}:p{page_index + 1}:b{block_index}", "doc_id": doc_id,
                              "pdf_page": page_index + 1, "bbox": list(block[:4]), "kind": "text",
                              "text": content, "normalized_text": normalize(content),
                              "document_sha256": paper["sha256"], "parser": "pymupdf",
                              "parser_version": fitz.VersionBind}
                    blocks.append(record)
                if re.search(r"\bTable\s+\d", text):
                    try:
                        finder = page.find_tables()
                        for table_index, table in enumerate(finder.tables):
                            cells = table.extract()
                            table_records.append({"evidence_id": f"{doc_id}:p{page_index + 1}:t{table_index}",
                                                  "doc_id": doc_id, "pdf_page": page_index + 1,
                                                  "bbox": list(table.bbox), "kind": "table", "cells": cells,
                                                  "review_status": "parser_candidate_not_verified"})
                    except Exception as exc:
                        table_records.append({"doc_id": doc_id, "pdf_page": page_index + 1, "error": str(exc)})
        pages, blocks, table_records, quality = repair_document_text(paper["local_path"], pages, blocks,
            tables=table_records, document_sha256=paper["sha256"])
        write_json(PILOT / "parsed" / doc_id / "text_quality.json", quality)
        write_json(PILOT / "parsed" / doc_id / "pages.json", pages)
        write_jsonl(PILOT / "parsed" / doc_id / "elements.jsonl", blocks)
        write_json(PILOT / "parsed" / doc_id / "tables.json", table_records)
        markdown_path = PILOT / "parsed" / doc_id / "fulltext.md"
        markdown_path.write_text(render_paper_reading_copy(
            PILOT, paper, pages, blocks, markdown_path=markdown_path,
            tables=table_records), encoding="utf-8", newline="\n")
        all_evidence.extend(blocks)
        text = "\n".join(p["text"] for p in pages)
        stats.append({"doc_id": doc_id, "pages": len(pages), "elements": len(blocks),
                      "table_candidates": sum("cells" in t for t in table_records), "characters": len(text),
                      "replacement_characters": text.count("\ufffd"), "cid_fragments": text.count("(cid:"),
                      "text_quality_status": quality["status"], "unresolved_critical": quality["unresolved_critical"],
                      "seconds": round(time.perf_counter() - started, 3),
                      "status": "needs_visual_review"})
    write_jsonl(PILOT / "parsed" / "evidence.jsonl", all_evidence)
    write_json(PILOT / "reports" / "pymupdf_parse_metrics.json", stats)
    print(json.dumps({"parsed_documents": len(stats), "evidence_elements": len(all_evidence),
                      "seconds": round(sum(s["seconds"] for s in stats), 2)}, ensure_ascii=False))


def index():
    import networkx as nx
    records = jsonl(PILOT / "annotations" / "claims.jsonl")
    # Validate before opening SQLite or clearing any existing index tables.
    from kg_pilot.text_quality import guard_claim_text_quality
    for record in records:
        try:
            guard_claim_text_quality(record)
        except ValueError as error:
            raise LegacyRebuildError(f"{record.get('claim_id')}：{error}") from error
    papers = read_json(PILOT / "catalog" / "papers.json")
    evidence_path = PILOT / "parsed" / "effective_evidence.jsonl"
    evidence = jsonl(evidence_path if evidence_path.exists() else PILOT / "parsed" / "evidence.jsonl")
    path = PILOT / "indexes" / "pilot.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS papers(doc_id TEXT PRIMARY KEY, title TEXT, doi TEXT, metadata TEXT);
        CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY, doc_id TEXT, pdf_page INTEGER, text TEXT, bbox TEXT);
        CREATE TABLE IF NOT EXISTS claims(claim_id TEXT PRIMARY KEY, doc_id TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS graph_nodes(node_id TEXT PRIMARY KEY, kind TEXT, attributes TEXT);
        CREATE TABLE IF NOT EXISTS graph_edges(edge_id TEXT PRIMARY KEY, source TEXT, target TEXT, kind TEXT, attributes TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(evidence_id UNINDEXED,doc_id UNINDEXED,text,tokenize='unicode61');
        DELETE FROM papers; DELETE FROM evidence; DELETE FROM claims; DELETE FROM graph_nodes; DELETE FROM graph_edges; DELETE FROM evidence_fts;
        """)
        graph = nx.MultiDiGraph()
        for p in papers:
            db.execute("INSERT INTO papers VALUES (?,?,?,?)", (p["doc_id"], p["title"], p.get("doi"), json.dumps(p, ensure_ascii=False)))
            graph.add_node(p["doc_id"], kind="document", title=p["title"])
        for e in evidence:
            db.execute("INSERT INTO evidence VALUES (?,?,?,?,?)", (e["evidence_id"], e["doc_id"], e["pdf_page"], e["text"], json.dumps(e["bbox"])))
            db.execute("INSERT INTO evidence_fts VALUES (?,?,?)", (e["evidence_id"], e["doc_id"], e["normalized_text"]))
        for c in records:
            db.execute("INSERT INTO claims VALUES (?,?,?)", (c["claim_id"], c["doc_id"], json.dumps(c, ensure_ascii=False)))
            graph.add_node(c["claim_id"], kind="claim", statement=c["statement"], stage=c.get("stage", "not_reported"))
            graph.add_edge(c["claim_id"], c["doc_id"], kind="reported_in")
            sample_id = c["doc_id"] + ":sample:" + c.get("sample_id", "unspecified")
            graph.add_node(sample_id, kind="sample", material=c.get("material_system", "not_reported"))
            graph.add_edge(c["claim_id"], sample_id, kind="about_sample")
            for ev in c.get("evidence", []):
                eid = ev.get("evidence_id") or f"{c['doc_id']}:p{ev['pdf_page']}"
                graph.add_node(eid, kind="evidence", pdf_page=ev["pdf_page"])
                graph.add_edge(c["claim_id"], eid, kind="supported_by")
        for node, attrs in graph.nodes(data=True):
            db.execute("INSERT INTO graph_nodes VALUES (?,?,?)", (node, attrs["kind"], json.dumps(attrs, ensure_ascii=False)))
        for i, (a, b, attrs) in enumerate(graph.edges(data=True)):
            db.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?)", (f"edge_{i}", a, b, attrs["kind"], json.dumps(attrs, ensure_ascii=False)))
        db.commit()
    nx.write_graphml(graph, PILOT / "indexes" / "pilot.graphml")
    from kg_pilot.retrieval import build_claim_index
    build_claim_index()
    print(json.dumps({"papers": len(papers), "evidence": len(evidence), "claims": len(records),
                      "graph_nodes": len(graph), "graph_edges": graph.number_of_edges()}, ensure_ascii=False))


def search(query: str, limit=8):
    from kg_pilot.effective_evidence import load_effective_evidence

    # The caller supplies an explicit English retrieval query; no hidden query translation.
    terms = re.findall(r"[\w]+", query, re.UNICODE)
    expression = " OR ".join('"' + t.replace('"', '') + '"' for t in terms)
    if not expression:
        return []
    with sqlite3.connect(PILOT / "indexes" / "pilot.sqlite") as db:
        rows = db.execute("SELECT f.evidence_id,f.doc_id,e.pdf_page,e.text,bm25(evidence_fts) "
                          "FROM evidence_fts f JOIN evidence e ON f.evidence_id=e.evidence_id "
                          "WHERE evidence_fts MATCH ? ORDER BY bm25(evidence_fts) LIMIT ?", (expression, limit)).fetchall()
    view = load_effective_evidence(PILOT)
    return [view.source(dict(zip(("evidence_id", "doc_id", "pdf_page", "text", "bm25"), row))) for row in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["assemble", "parse", "index", "search"])
    parser.add_argument("query", nargs="?")
    args = parser.parse_args()
    try:
        if args.stage == "search":
            print(json.dumps(search(args.query or ""), ensure_ascii=False, indent=2))
        else:
            globals()[args.stage]()
    except LegacyRebuildError as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
