"""Recoverable table additions run only against isolated synthetic libraries."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sqlite3

import numpy as np
import pymupdf
import pytest

from kg_pilot import ingestion_publish as publication
from kg_pilot import pipeline, table_backfill
from kg_pilot.pipeline import jsonl, normalize, read_json, write_json, write_jsonl


@pytest.fixture(autouse=True)
def validated_table_fixture(monkeypatch):
    # Table parsing itself is tested separately. These tests exercise publication
    # with a complete extraction bound to a real synthetic source page.
    from kg_pilot import table_evidence

    def extract(page, *, doc_id, document_sha256):
        boxes = [[[40, y, 190, y + 40], [190, y, 340, y + 40]] for y in (200, 240, 280)]
        text = "TABLE I. YIELD\nLayer | Yield\nLayer1 | 90%\nLayer2 | 60%"
        eid = f"{doc_id}:p{page.number + 1}:table0"
        return [dict(evidence_id=eid, table_id=eid, doc_id=doc_id, pdf_page=page.number + 1,
            kind="table", parser="pymupdf_table", document_sha256=document_sha256,
            bbox=[40, 150, 340, 320], table_bbox=[40, 200, 340, 320],
            table_caption="TABLE I. YIELD", table_label="TABLE I.", caption_bbox=[40, 150, 200, 180],
            columns=["Layer", "Yield"], rows=[["Layer1", "90%"], ["Layer2", "60%"]],
            cell_bboxes=boxes, row_bboxes=[[40, y, 340, y + 40] for y in (200, 240, 280)],
            review_status="source_requires_claim_level_review", text=text, normalized_text=normalize(text))]

    monkeypatch.setattr(table_evidence, "extract_table_evidence", extract)


def embeddings(texts):
    return np.asarray([[1., len(text) / 1000, .2, .3] for text in texts], dtype=np.float32)


def seed(tmp_path, wrapped_pages=False):
    pilot = tmp_path / "pilot"
    pdf = tmp_path / "old.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 40), "Original article text preserved with its separate OCR route.")
        page.insert_text((40, 170), "TABLE I. YIELD")
        for y in (200, 240, 280, 320):
            page.draw_line((40, y), (340, y))
        for x in (40, 190, 340):
            page.draw_line((x, 200), (x, 320))
        for y, left, right in ((225, "Layer", "Yield"), (265, "Layer1", "90%"), (305, "Layer2", "60%")):
            page.insert_text((50, y), left)
            page.insert_text((200, y), right)
        document.save(pdf)
    with pymupdf.open(pdf) as source:
        page = source[0]
        pages = [dict(pdf_page=1, width=page.rect.width, height=page.rect.height, text=page.get_text())]
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    paper = dict(doc_id="old_doc", title="Original paper", doi="10.1234/old", local_path=str(pdf),
                 sha256=sha, page_count=1)
    native = dict(evidence_id="old_doc:p1:b0", doc_id="old_doc", pdf_page=1,
        kind="text", parser="pymupdf", document_sha256=sha,
        text="Original native text", normalized_text="Original native text", bbox=[40, 20, 300, 60])
    ocr = dict(native, evidence_id="old_doc:p1:ocr0", parser="docling_rapidocr",
               text="Original separate OCR text", normalized_text="Original separate OCR text")
    write_json(pilot / "catalog/papers.json", [paper])
    write_json(pilot / "catalog/classifications.json", {"version": "1.0", "records": []})
    write_json(pilot / "catalog/statistics.json", {"unique_papers": 1, "excluded_entries": 4})
    write_jsonl(pilot / "annotations/claims.jsonl", [])
    write_jsonl(pilot / "parsed/evidence.jsonl", [native])
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", [ocr])
    write_json(pilot / "parsed/old_doc/pages.json", {"pages": pages} if wrapped_pages else pages)
    write_jsonl(pilot / "parsed/old_doc/elements.jsonl", [native])
    write_jsonl(pilot / "parsed/old_doc/elements_ocr.jsonl", [ocr])
    write_json(pilot / "parsed/old_doc/tables.json", [{"evidence_id": "old_doc:p1:t0", "cells": [["legacy candidate"]]}])
    (pilot / "parsed/old_doc/fulltext.txt").write_text("Untouched full text", encoding="utf-8")
    record = dict(vector_id=ocr["evidence_id"] + ":seg0", evidence_id=ocr["evidence_id"],
                  doc_id="old_doc", pdf_page=1, embedding_text=ocr["normalized_text"])
    write_json(pilot / "benchmarks/embedding/all_records.json", [record])
    np.save(pilot / "benchmarks/embedding/all_vectors.npy", embeddings([record["embedding_text"]]))
    return pilot, native, ocr


def preserved(pilot):
    names = ("catalog/papers.json", "catalog/classifications.json", "catalog/statistics.json",
             "annotations/claims.jsonl", "parsed/old_doc/pages.json",
             "parsed/old_doc/elements_ocr.jsonl", "parsed/old_doc/fulltext.txt")
    return {name: (pilot / name).read_bytes() for name in names}


@pytest.mark.parametrize("wrapped_pages", [False, True])
def test_preview_and_backfill_preserve_ocr_claims_and_vectors(tmp_path, wrapped_pages):
    pilot, native, ocr = seed(tmp_path, wrapped_pages)
    before = preserved(pilot)
    old_vectors = np.load(pilot / "benchmarks/embedding/all_vectors.npy").copy()
    preview = table_backfill.preview_backfill(["old_doc"], pilot)
    assert preview["status"] == "preview" and preview["added_evidence"] == 1
    assert preview["documents"][0]["tables"][0]["rows"] == [["Layer1", "90%"], ["Layer2", "60%"]]
    assert not (pilot / "ingestion/staging").exists()
    assert preserved(pilot) == before
    calls = []

    def embed(texts):
        calls.extend(texts)
        return embeddings(texts)

    result = table_backfill.backfill_tables(["old_doc"], pilot, embedder=embed)
    assert result["added_evidence"] == 1 and result["vector_count"] == 2
    assert result["added_claims"] == result["added_documents"] == 0
    assert len(calls) == 1 and "90%" in calls[0] and "Original separate OCR" not in calls[0]
    assert preserved(pilot) == before
    assert jsonl(pilot / "parsed/evidence.jsonl")[0] == native
    assert jsonl(pilot / "parsed/effective_evidence.jsonl")[0] == ocr
    assert read_json(pilot / "parsed/old_doc/tables.json")[0]["cells"] == [["legacy candidate"]]
    reading = (pilot / "parsed/old_doc/fulltext.md").read_text(encoding="utf-8")
    assert "90%" in reading and "60%" in reading
    assert "Original separate OCR text" in reading
    assert np.array_equal(np.load(pilot / "benchmarks/embedding/all_vectors.npy")[:1], old_vectors)
    assert (Path(result["backup_path"]) / "original/parsed/effective_evidence.jsonl").exists()
    with sqlite3.connect(pilot / "indexes/pilot.sqlite") as db:
        rows = db.execute("SELECT evidence_id FROM evidence_fts WHERE evidence_fts MATCH 'Yield'").fetchall()
    assert rows == [("old_doc:p1:table0",)]
    again = table_backfill.backfill_tables(["old_doc"], pilot,
        embedder=lambda _: pytest.fail("completed tables must not be re-encoded"))
    assert again["status"] == "already_backfilled" and again["added_evidence"] == 0
    assert again["updated_reading_documents"] == 0
    assert (pilot / "parsed/old_doc/fulltext.md").read_text(encoding="utf-8") == reading
    with pytest.raises(pipeline.LegacyRebuildError, match="表格"):
        pipeline.ensure_legacy_rebuild_safe(pilot)


@pytest.mark.parametrize("crash", [False, True])
def test_failed_swap_restores_old_artifacts_or_recovers_after_crash(tmp_path, monkeypatch, crash):
    pilot, _, _ = seed(tmp_path)
    before = {name: (pilot / name).read_bytes() for name in
              ("parsed/evidence.jsonl", "parsed/effective_evidence.jsonl", "parsed/old_doc/elements.jsonl", "parsed/old_doc/tables.json")}
    original = publication.os.replace
    failed = False

    def replace(source, target):
        nonlocal failed
        if not failed and Path(target) == pilot / "parsed/effective_evidence.jsonl" and "staging" in Path(source).parts:
            failed = True
            if crash:
                raise SystemExit("simulated process termination")
            raise OSError("simulated swap failure")
        return original(source, target)

    monkeypatch.setattr(publication.os, "replace", replace)
    expected = SystemExit if crash else publication.PublicationError
    with pytest.raises(expected):
        table_backfill.backfill_tables(["old_doc"], pilot, embedder=embeddings)
    if crash:
        assert publication.maintenance_status(pilot)["active"]
        assert publication.recover_publication(pilot)["status"] == "rolled_back"
    assert all((pilot / name).read_bytes() == content for name, content in before.items())
    assert not (pilot / table_backfill.REGISTRY).exists()
    assert not (pilot / "parsed/old_doc/fulltext.md").exists()
    assert not publication.maintenance_status(pilot)["active"]
    assert publication.recover_publication(pilot)["status"] == "rolled_back"


def test_changed_parsed_page_is_detected_before_commit(tmp_path):
    pilot, _, _ = seed(tmp_path)
    before = (pilot / "parsed/effective_evidence.jsonl").read_bytes()

    def race(stage):
        publication._build_indexes(stage)
        pages = read_json(pilot / "parsed/old_doc/pages.json")
        pages[0]["text"] += " concurrently changed"
        write_json(pilot / "parsed/old_doc/pages.json", pages)

    with pytest.raises(publication.DatasetBusy, match="已变化"):
        table_backfill.backfill_tables(["old_doc"], pilot, embedder=embeddings, index_builder=race)
    assert (pilot / "parsed/effective_evidence.jsonl").read_bytes() == before
    assert not (pilot / table_backfill.REGISTRY).exists()


def test_source_hash_mismatch_stops_before_extraction(tmp_path):
    pilot, _, _ = seed(tmp_path)
    paper = read_json(pilot / "catalog/papers.json")[0]
    Path(paper["local_path"]).write_bytes(b"changed source")
    with pytest.raises(publication.PublicationError, match="SHA-256"):
        table_backfill.preview_backfill(["old_doc"], pilot,
            extractor=lambda *args, **kwargs: pytest.fail("untrusted source must not be parsed"))


def test_conflicting_existing_table_is_never_replaced(tmp_path):
    pilot, _, _ = seed(tmp_path)
    preview = table_backfill.preview_backfill(["old_doc"], pilot)
    table = deepcopy(preview["documents"][0]["tables"][0])
    table["text"] += " stale extraction"
    effective = jsonl(pilot / "parsed/effective_evidence.jsonl")
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", effective + [table])
    with pytest.raises(publication.PublicationError, match="禁止覆盖"):
        table_backfill.backfill_tables(["old_doc"], pilot, embedder=embeddings)


def test_future_batch_publication_preserves_backfilled_tables(tmp_path):
    pilot, _, _ = seed(tmp_path)
    table_backfill.backfill_tables(["old_doc"], pilot, embedder=embeddings)
    registry = (pilot / table_backfill.REGISTRY).read_bytes()
    pdf = tmp_path / "future.pdf"
    with pymupdf.open() as source:
        page = source.new_page()
        page.insert_text((30, 50), "A different newly ingested article with valid native source text.")
        source.save(pdf)
    with pymupdf.open(pdf) as source:
        page = source[0]
        text = page.get_text()
        pages = [dict(pdf_page=1, width=page.rect.width, height=page.rect.height, text=text)]
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    element = dict(evidence_id="future_doc:p1:b0", doc_id="future_doc", pdf_page=1,
                   kind="text", parser="pymupdf", document_sha256=sha, bbox=[30, 30, 500, 65],
                   text=text, normalized_text=normalize(text))
    document = dict(paper=dict(doc_id="future_doc", title="Newly added article", doi="10.1234/future",
                              sha256=sha, page_count=1, local_path=str(pdf)),
                    pages=pages, elements=[element], claims=[], classification=dict(
                        doc_id="future_doc", source_sha256=sha,
                        classification_review="independent_agent_reviewed"))
    batch = tmp_path / "future_batch"
    write_json(batch / "prepared.json", dict(batch_id="future_batch", documents=[document]))
    result = publication.publish_batch(batch, pilot, embedder=embeddings)
    assert result["vector_count"] == 3 and result["added_documents"] == 1
    assert (pilot / table_backfill.REGISTRY).read_bytes() == registry
    assert any(item["evidence_id"] == "old_doc:p1:table0"
               for item in jsonl(pilot / "parsed/effective_evidence.jsonl"))


@pytest.mark.parametrize("existing", ["missing", "stale", "crlf"])
def test_completed_tables_refresh_only_missing_or_stale_reading_document(tmp_path, existing):
    pilot, _, _ = seed(tmp_path)
    table_backfill.backfill_tables(["old_doc"], pilot, embedder=embeddings)
    markdown_path = pilot / "parsed/old_doc/fulltext.md"
    expected = markdown_path.read_bytes()
    assert b"\r" not in expected
    if existing == "missing":
        old_bytes = None
        markdown_path.unlink()
    else:
        old_bytes = b"Outdated reading copy" if existing == "stale" else expected.replace(b"\n", b"\r\n")
        markdown_path.write_bytes(old_bytes)
    unchanged = preserved(pilot)
    names = ("parsed/evidence.jsonl", "parsed/effective_evidence.jsonl",
             "parsed/old_doc/elements.jsonl", "parsed/old_doc/tables.json",
             table_backfill.REGISTRY, "benchmarks/embedding/all_records.json",
             "benchmarks/embedding/all_vectors.npy", "indexes/pilot.sqlite")
    unchanged.update({name: (pilot / name).read_bytes() for name in names})
    result = table_backfill.backfill_tables(["old_doc"], pilot,
        embedder=lambda _: pytest.fail("reading refresh must not embed"),
        index_builder=lambda _: pytest.fail("reading refresh must not rebuild indexes"))
    assert result["status"] == "already_backfilled" and result["added_evidence"] == 0
    assert result["updated_reading_documents"] == 1
    assert markdown_path.read_bytes() == expected
    assert all((pilot / name).read_bytes() == content for name, content in unchanged.items())
    original = Path(result["backup_path"]) / "original/parsed/old_doc/fulltext.md"
    if existing == "missing":
        assert not original.exists()
    else:
        assert original.read_bytes() == old_bytes
