"""Exercise source-bound repairs and publication on an isolated dataset."""
from contextlib import closing
from pathlib import Path
import sqlite3

import numpy as np
import pymupdf
import pytest

from kg_pilot import ingestion_publish as publication
from kg_pilot import quality_review_publish as review
from kg_pilot.pipeline import PILOT, jsonl, read_json, normalize, write_json, write_jsonl


CHANGED_PAGES = {4, 6, 7, 8, 9, 10, 11, 13, 14, 16, 17, 18, 19}
RAW_QUOTES = {
    "c002": "Alignment skew: • 8 x 12 mm test vehicles • Pad size ranging from 2 - 15 mm",
    "c004": "Test Vehicle: 8 x 12 mm Die with 10 mm bond pads, thinned to 50 or 200 mm",
    "c005": "Stacking Test Vehicle • 8 x 12 mm x 50 mm thick die with 15 mm bond pads",
    "c007": "Test Vehicle • 8 x 12 mm x 200 mm thick Die with 10 mm bond pads",
}


def _encode(texts):
    return np.asarray([[1., len(text) / 1000, .2, .3] for text in texts], dtype=np.float32)


@pytest.fixture
def reviewed_dataset(tmp_path):
    pdf = PILOT / "collection/batches/b_82c9ff73209e4a488d5a" / (review.DOC_ID + ".pdf")
    if not pdf.is_file():
        pytest.skip("The reviewed Adeia presentation is not bundled in this checkout.")
    paper = dict(doc_id=review.DOC_ID, title="Adeia", local_path=str(pdf), sha256=review.SOURCE_SHA256, page_count=19)
    pages = []
    evidence = []
    with pymupdf.open(pdf) as document:
        for number, page in enumerate(document, 1):
            text = page.get_text()
            pages.append(dict(pdf_page=number, text=text, native_text=text,
                              width=page.rect.width, height=page.rect.height))
            evidence.append(dict(evidence_id=f"{review.DOC_ID}:p{number}:b0_0", doc_id=review.DOC_ID,
                pdf_page=number, kind="text", parser="pymupdf", document_sha256=review.SOURCE_SHA256,
                bbox=list(page.rect), text=text, normalized_text=normalize(text)))
    claims = []
    for suffix, rule in review.CLAIM_CHANGES.items():
        source = evidence[rule["page"] - 1]
        claim = dict(claim_id=f"{review.DOC_ID}_{suffix}", doc_id=review.DOC_ID, material_system="Cu",
                     stage="measurement", quantity="measurement", value_text="", unit=None,
                     evidence=[dict(pdf_page=source["pdf_page"], evidence_id=source["evidence_id"], quote=RAW_QUOTES[suffix])])
        claim.update({field: pair[0] for field, pair in rule["fields"].items()})
        claim["conditions"] = {field: pair[0] for field, pair in rule["conditions"].items()}
        claim["conditions"]["die_dimensions"] = "8 x 12 mm"
        claims.append(claim)
    pilot = tmp_path / "pilot"
    write_json(pilot / "catalog/papers.json", [paper])
    write_json(pilot / "catalog/classifications.json", {"records": []})
    write_json(pilot / "parsed" / review.DOC_ID / "pages.json", pages)
    write_jsonl(pilot / "parsed" / review.DOC_ID / "elements.jsonl", evidence)
    write_jsonl(pilot / "parsed/evidence.jsonl", evidence)
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", evidence)
    write_jsonl(pilot / "annotations/claims.jsonl", claims)
    records = [dict(vector_id=e["evidence_id"] + ":seg0", evidence_id=e["evidence_id"], doc_id=review.DOC_ID,
                    pdf_page=e["pdf_page"], embedding_text=e["normalized_text"]) for e in evidence]
    write_json(pilot / "benchmarks/embedding/all_records.json", records)
    np.save(pilot / "benchmarks/embedding/all_vectors.npy", _encode([r["embedding_text"] for r in records]))
    return pilot, pdf


def test_adeia_publish_keeps_raw_text_and_millimetres_and_reencodes_changed_pages(reviewed_dataset):
    pilot, pdf = reviewed_dataset
    sidecar = "catalog/evidence_qualifications.json"
    extra = {sidecar: {"expected_sha256": None, "value": {"version": 1, "records": []}}}
    claims_path = pilot / "annotations/claims.jsonl"
    original = claims_path.read_bytes()
    evidence_before = jsonl(pilot / "parsed/evidence.jsonl")
    preview = review.preview_source_review(pilot, extra_updates=extra)
    assert preview["changed_pages"] == len(CHANGED_PAGES)
    assert preview["changed_claims"] == 4
    assert claims_path.read_bytes() == original and not (pilot / sidecar).exists()
    assert not (pilot / "ingestion/staging").exists()

    calls = []

    def embed(texts):
        calls.extend(texts)
        return _encode(texts)

    result = review.publish_source_review(pilot, extra_updates=extra, embedder=embed)
    claims_after = preview["documents"][0]["source_review"]["claims_after"]
    assert result["status"] == "backfilled" and result["vector_count"] == 19
    assert len(calls) == len(CHANGED_PAGES)
    assert read_json(pilot / sidecar) == extra[sidecar]["value"]
    assert jsonl(claims_path) == claims_after
    records = read_json(pilot / "benchmarks/embedding/all_records.json")
    assert {r["pdf_page"] for r in records[-len(CHANGED_PAGES):]} == CHANGED_PAGES
    assert next(r for r in records if r["pdf_page"] == 9)["embedding_text"].find("~10 mΩ per link") >= 0
    evidence_after = jsonl(pilot / "parsed/evidence.jsonl")
    assert {r["pdf_page"] for a, r in zip(evidence_before, evidence_after) if a["text"] != r["text"]} == CHANGED_PAGES
    assert "8 x 12 mm" in evidence_after[8]["text"] and "2 - 15 µm" in evidence_after[8]["text"]
    assert "~10 mW per link" in evidence_after[8]["raw_text_fields"]["text"]
    assert "2–15 µm pads" in evidence_after[18]["text"]
    assert "50 mm2 center array" in evidence_after[6]["text"]
    assert all(r["conditions"]["die_dimensions"] == "8 x 12 mm" for r in claims_after)
    assert all(r["evidence"][0]["raw_quote"] == RAW_QUOTES[r["claim_id"].rsplit("_", 1)[1]] for r in claims_after)

    with closing(sqlite3.connect(pilot / "indexes/pilot.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM claims").fetchone() == (4,)
        assert db.execute("SELECT count(*) FROM evidence_fts WHERE evidence_fts MATCH ?", ('"10 mΩ per link"',)).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM evidence_fts WHERE evidence_fts MATCH ?", ('"10 mW per link"',)).fetchone() == (0,)
    assert (Path(result["backup_path"]) / "original/annotations/claims.jsonl").read_bytes() == original
    assert publication._sha(pdf) == review.SOURCE_SHA256
    repeat = review.publish_source_review(pilot, extra_updates=extra,
        embedder=lambda _: pytest.fail("already published sources must not be embedded again"),
        index_builder=lambda _: pytest.fail("already published indexes must not be rebuilt"))
    assert repeat["status"] == "already_backfilled"
    assert repeat["changed_claims"] == repeat["changed_file_count"] == 0


def test_review_sidecar_refuses_stale_or_out_of_scope_updates(reviewed_dataset):
    pilot, _ = reviewed_dataset
    with pytest.raises(publication.DatasetBusy, match="已变化"):
        review.preview_source_review(pilot, extra_updates={"catalog/evidence_qualifications.json": {
            "expected_sha256": "0" * 64, "value": {}}})
    with pytest.raises(publication.PublicationError, match="无法识别"):
        review.preview_source_review(pilot, extra_updates={"catalog/papers.json": {
            "expected_sha256": None, "value": []}})
