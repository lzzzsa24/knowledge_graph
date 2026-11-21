"""All publication tests use temporary libraries and explicitly fake test embeddings."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pymupdf
import pytest

from kg_pilot import ingestion_publish as publication
from kg_pilot.pipeline import jsonl, normalize, read_json, write_json, write_jsonl


TEXT = "The measured bond strength was 25 MPa after annealing at 200 degrees C. The samples were carefully prepared and inspected by microscopy."


def test_embedder(texts):
    """Deterministic 4-D vectors ONLY for temporary test libraries."""
    return np.asarray([[1.0, (len(t) % 7 + 1) / 9, 0.2, 0.3] for t in texts], dtype=np.float32)


test_embedder.__test__ = False


def document(tmp_path, doc_id="new_paper", doi="10.1234/new"):
    pdf = tmp_path / (doc_id + ".pdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(20, 20, 550, 200), TEXT)
    doc.save(pdf)
    doc.close()
    with pymupdf.open(pdf) as doc:
        source = doc[0].get_text()
        width, height = doc[0].rect.width, doc[0].rect.height
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    element = {"evidence_id": doc_id + ":p1:b0", "doc_id": doc_id, "pdf_page": 1,
               "text": source, "normalized_text": normalize(source), "bbox": [20, 20, 550, 200],
               "document_sha256": sha, "kind": "text", "parser": "pymupdf"}
    claim = {"claim_id": doc_id + "_claim", "doc_id": doc_id, "statement": "The strength was 25 MPa.",
             "material_system": "Cu-Cu", "sample_id": "sample1", "stage": "measurement",
             "quantity": "bond_strength", "value_text": "25", "unit": "MPa", "conditions": {},
             "evidence": [{"pdf_page": 1, "quote": "The measured bond strength was 25 MPa", "evidence_id": element["evidence_id"]}],
             "claim_type": "reported_experiment", "review_status": "independent_agent_reviewed"}
    return {"paper": {"doc_id": doc_id, "title": "A paper " + doc_id, "doi": doi, "year": 2026,
                       "authors": ["Test Author"], "source_type": "journal_paper", "is_review": False,
                       "page_count": 1, "local_path": str(pdf), "sha256": sha, "scope": "core"},
            "pages": [{"pdf_page": 1, "text": source, "width": width, "height": height}],
            "elements": [element], "classification": {"doc_id": doc_id, "source_sha256": sha,
                "primary_category": "characterization_evaluation", "classification_review": "independent_agent_reviewed",
                "topics": ["bond_strength"], "keywords": ["bond strength"]}, "claims": [claim]}


def batch(tmp_path, docs=None, batch_id="batch1"):
    path = tmp_path / batch_id
    write_json(path / "prepared.json", {"batch_id": batch_id, "documents": docs or [document(tmp_path)]})
    return path


def seed_old(pilot, tmp_path, *, vectors=True):
    old = document(tmp_path, "old_paper", "10.1234/old")
    native = old["elements"][0]
    ocr = dict(native, evidence_id="old_paper:p1:ocr0", parser="docling_rapidocr", text=TEXT + " OCR preserved.", normalized_text=normalize(TEXT + " OCR preserved."))
    old["claims"][0]["evidence"][0]["evidence_id"] = ocr["evidence_id"]
    write_json(pilot / "catalog/papers.json", [old["paper"]])
    write_json(pilot / "catalog/classifications.json", {"version": "1.0", "records": [old["classification"]]})
    write_json(pilot / "catalog/statistics.json", {"unique_papers": 1, "excluded_entries": 3})
    write_jsonl(pilot / "parsed/evidence.jsonl", [native])
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", [ocr])
    write_jsonl(pilot / "annotations/claims.jsonl", old["claims"])
    write_json(pilot / "parsed/old_paper/pages.json", old["pages"])
    if vectors:
        record = {"vector_id": ocr["evidence_id"] + ":seg0", "evidence_id": ocr["evidence_id"], "doc_id": "old_paper", "pdf_page": 1, "embedding_text": ocr["normalized_text"]}
        out = pilot / "benchmarks/embedding"
        write_json(out / "all_records.json", [record])
        np.save(out / "all_vectors.npy", test_embedder([record["embedding_text"]]))
        (out / "trial_note.txt").write_text("preserve me", encoding="utf-8")
    return old, ocr


def test_publish_preserves_old_ocr_and_reuses_vectors(tmp_path):
    pilot = tmp_path / "pilot"
    old, ocr = seed_old(pilot, tmp_path)
    before = (pilot / "parsed/old_paper/pages.json").read_bytes()
    calls = []

    def embedding(texts):
        calls.extend(texts)
        return test_embedder(texts)

    result = publication.publish_batch(batch(tmp_path), pilot, embedder=embedding)
    assert result["added_documents"] == result["added_claims"] == result["added_evidence"] == 1
    assert result["vector_count"] == 2
    assert len(calls) == 1 and "OCR preserved" not in calls[0]
    assert jsonl(pilot / "parsed/effective_evidence.jsonl")[0] == ocr
    assert (pilot / "parsed/old_paper/pages.json").read_bytes() == before
    assert (pilot / "benchmarks/embedding/trial_note.txt").read_text() == "preserve me"
    assert Path(result["backup_path"]).is_dir()
    assert (Path(result["backup_path"]) / "original/catalog/papers.json").is_file()
    assert len(read_json(pilot / "catalog/papers.json")) == 2
    assert read_json(pilot / "catalog/statistics.json")["excluded_entries"] == 3
    assert not publication.maintenance_status(pilot)["active"]
    assert read_json(pilot / "catalog/ingestion_readiness.json")["documents"]["new_paper"]["semantic_ready"]
    assert read_json(pilot / "collection/batches/manifest.json")["papers"][0]["doc_id"] == "new_paper"
    assert read_json(pilot / "catalog/classification_batches/batch1.json")["records"][0]["doc_id"] == "new_paper"
    reading = (pilot / "parsed/new_paper/fulltext.md").read_text(encoding="utf-8")
    assert "A paper new\\_paper" in reading and "25 MPa" in reading
    assert "collection/batches/batch1/new_paper.pdf" in reading
    assert "staging" not in reading
    assert b"\r" not in (pilot / "parsed/new_paper/fulltext.md").read_bytes()
    assert not (pilot / "parsed/new_paper/fulltext.txt").exists()


@pytest.mark.parametrize("duplicate", ["id", "doi", "hash", "within_batch"])
def test_duplicate_rejected_without_touching_old_files(tmp_path, duplicate):
    pilot = tmp_path / "pilot"
    old, _ = seed_old(pilot, tmp_path)
    new = document(tmp_path)
    if duplicate == "id":
        new["paper"]["doc_id"] = "old_paper"
    elif duplicate == "doi":
        new["paper"]["doi"] = "https://doi.org/10.1234/OLD"
    elif duplicate == "hash":
        new["paper"]["sha256"] = old["paper"]["sha256"]
    docs = [new, new] if duplicate == "within_batch" else [new]
    before = (pilot / "catalog/papers.json").read_bytes()
    with pytest.raises(publication.PublicationError):
        publication.publish_batch(batch(tmp_path, docs), pilot, embedder=test_embedder)
    assert (pilot / "catalog/papers.json").read_bytes() == before
    assert not (pilot / publication.MAINTENANCE).exists()


def test_initial_library_without_any_old_indexes(tmp_path):
    pilot = tmp_path / "pilot"
    result = publication.publish_batch(batch(tmp_path), pilot, embedder=test_embedder)
    assert result["vector_count"] == 1
    assert (pilot / "indexes/pilot.sqlite").is_file()
    assert (pilot / "indexes/pilot.graphml").is_file()
    assert (pilot / "indexes/qdrant").is_dir()


def test_new_document_does_not_reject_unrelated_unchanged_legacy_corruption(tmp_path):
    pilot = tmp_path / "pilot"
    old, _ = seed_old(pilot, tmp_path)
    old["claims"][0]["statement"] = "Legacy width is 5 \ue001m."
    write_jsonl(pilot / "annotations/claims.jsonl", old["claims"])
    result = publication.publish_batch(batch(tmp_path), pilot, embedder=test_embedder)
    assert result["added_documents"] == 1
    assert jsonl(pilot / "annotations/claims.jsonl")[0] == old["claims"][0]
    # A full-library audit still catches the pre-existing unsafe record.
    with pytest.raises(publication.PublicationError, match="未确认乱码"):
        publication._validate_stage(pilot)


def test_existing_library_without_old_vectors_rebuilds_from_effective(tmp_path):
    pilot = tmp_path / "pilot"
    _, ocr = seed_old(pilot, tmp_path, vectors=False)
    calls = []

    def embedding(texts):
        calls.extend(texts)
        return test_embedder(texts)

    result = publication.publish_batch(batch(tmp_path), pilot, embedder=embedding)
    assert result["vector_count"] == 2 and len(calls) == 2
    assert ocr["normalized_text"] in calls


def test_identical_historical_vector_duplicates_are_deduplicated(tmp_path):
    pilot = tmp_path / "pilot"
    seed_old(pilot, tmp_path)
    records_path = pilot / "benchmarks/embedding/all_records.json"
    records = read_json(records_path)
    write_json(records_path, records + records)
    vectors_path = pilot / "benchmarks/embedding/all_vectors.npy"
    vectors = np.load(vectors_path)
    np.save(vectors_path, np.concatenate([vectors, vectors]))
    result = publication.publish_batch(batch(tmp_path), pilot, embedder=test_embedder)
    assert result["vector_count"] == 2


def test_second_publication_replaces_complete_old_indexes(tmp_path):
    pilot = tmp_path / "pilot"
    first = publication.publish_batch(batch(tmp_path), pilot, embedder=test_embedder)
    old_sqlite = (pilot / "indexes/pilot.sqlite").read_bytes()
    new_doc = document(tmp_path, "second_paper", "10.1234/second")
    second = publication.publish_batch(batch(tmp_path, [new_doc], "batch2"), pilot, embedder=test_embedder)
    assert first["vector_count"] == 1 and second["vector_count"] == 2
    assert (Path(second["backup_path"]) / "original/indexes/pilot.sqlite").read_bytes() == old_sqlite
    assert len(read_json(pilot / "collection/batches/manifest.json")["papers"]) == 2


def test_successful_publication_retry_is_idempotent(tmp_path):
    pilot = tmp_path / "pilot"
    path = batch(tmp_path)
    first = publication.publish_batch(path, pilot, embedder=test_embedder)
    before = (pilot / "catalog/papers.json").read_bytes()
    second = publication.publish_batch(path, pilot, embedder=lambda _: pytest.fail("must not re-embed completed batch"))
    assert second["status"] == "already_published" and second["already_published"]
    assert second["added_documents"] == 0 and second["backup_path"] == first["backup_path"]
    assert (pilot / "catalog/papers.json").read_bytes() == before


def test_rapidocr_page_and_api_review_are_publishable(tmp_path):
    pilot = tmp_path / "pilot"
    doc = document(tmp_path)
    doc["pages"][0].update(parser="rapidocr", native_text="")
    doc["elements"][0]["parser"] = "rapidocr"
    doc["claims"][0]["review_status"] = "api_model_reviewed"
    result = publication.publish_batch(batch(tmp_path, [doc]), pilot, embedder=test_embedder)
    assert result["added_documents"] == 1
    assert jsonl(pilot / "parsed/effective_evidence.jsonl")[0]["parser"] == "rapidocr"


def test_rapidocr_evidence_cannot_use_native_page_layer(tmp_path):
    pilot = tmp_path / "pilot"
    doc = document(tmp_path)
    doc["elements"][0]["parser"] = "rapidocr"
    with pytest.raises(publication.PublicationError, match="解析层"):
        publication.publish_batch(batch(tmp_path, [doc]), pilot, embedder=test_embedder)


def test_index_builder_failure_does_not_touch_live_library(tmp_path):
    pilot = tmp_path / "pilot"
    seed_old(pilot, tmp_path)
    before = (pilot / "catalog/papers.json").read_bytes()

    def fail(stage):
        raise RuntimeError("injected index failure")

    with pytest.raises(RuntimeError, match="injected"):
        publication.publish_batch(batch(tmp_path), pilot, embedder=test_embedder, index_builder=fail)
    assert (pilot / "catalog/papers.json").read_bytes() == before
    assert not (pilot / publication.MAINTENANCE).exists()


def fail_swap_once(monkeypatch, pilot, exception):
    original = publication.os.replace
    failed = False

    def replace(source, target):
        nonlocal failed
        if not failed and Path(target) == pilot / "catalog/classifications.json" and "staging" in Path(source).parts:
            failed = True
            raise exception
        return original(source, target)

    monkeypatch.setattr(publication.os, "replace", replace)


def test_mid_commit_failure_rolls_back_every_file(tmp_path, monkeypatch):
    pilot = tmp_path / "pilot"
    seed_old(pilot, tmp_path)
    before = {p: (pilot / p).read_bytes() for p in ["catalog/papers.json", "catalog/classifications.json", "parsed/effective_evidence.jsonl", "annotations/claims.jsonl"]}
    fail_swap_once(monkeypatch, pilot, OSError("injected swap failure"))
    with pytest.raises(publication.PublicationError, match="已恢复原库"):
        publication.publish_batch(batch(tmp_path), pilot, embedder=test_embedder)
    assert all((pilot / p).read_bytes() == content for p, content in before.items())
    assert not (pilot / "parsed/new_paper").exists()
    assert not publication.maintenance_status(pilot)["active"]
    assert read_json(pilot / publication.JOURNAL)["status"] == "rolled_back"


def test_crash_journal_can_be_recovered_repeatedly(tmp_path, monkeypatch):
    pilot = tmp_path / "pilot"
    seed_old(pilot, tmp_path)
    before = (pilot / "catalog/papers.json").read_bytes()
    fail_swap_once(monkeypatch, pilot, SystemExit("simulated process termination"))
    with pytest.raises(SystemExit):
        publication.publish_batch(batch(tmp_path), pilot, embedder=test_embedder)
    assert publication.maintenance_status(pilot)["active"]
    assert publication.recover_publication(pilot)["status"] == "rolled_back"
    assert (pilot / "catalog/papers.json").read_bytes() == before
    assert publication.recover_publication(pilot)["status"] == "rolled_back"
    assert not publication.maintenance_status(pilot)["active"]


def test_cross_process_lock(tmp_path):
    pilot = tmp_path / "pilot"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    code = "from pathlib import Path; from kg_pilot.ingestion_publish import dataset_lock, DatasetBusy; import sys\ntry:\n with dataset_lock(Path(sys.argv[1])): pass\nexcept DatasetBusy:\n print('BUSY'); sys.exit(7)\n"
    with publication.dataset_lock(pilot):
        result = subprocess.run([sys.executable, "-c", code, str(pilot)], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 7 and "BUSY" in result.stdout
    with publication.dataset_lock(pilot):
        pass
