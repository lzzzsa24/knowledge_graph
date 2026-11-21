"""Backfill publication safety is verified against isolated real source PDFs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
import pymupdf
import pytest

from kg_pilot import ingestion_publish as publication
from kg_pilot import text_quality_backfill as backfill
from kg_pilot.pipeline import jsonl, normalize, read_json, write_json, write_jsonl


def embeddings(texts):
    return np.asarray([[1., len(text) / 1000, .2, .3] for text in texts], dtype=np.float32)


def _source_pdf(path):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_font(fontname="embedded", fontbuffer=pymupdf.Font("helv").buffer)
        page.insert_text((40, 60), "Measured spacing is 5 \xb5m for the test array.", fontname="embedded")
        mapping = {gid: 0xf06d if code == 0xb5 else code
                   for span in page.get_texttrace() for code, gid, _, _ in span["chars"]}
        cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
                "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def\n"
                "/CMapName /Test-UCS def\n/CMapType 2 def\n1 begincodespacerange\n"
                "<0000> <FFFF>\nendcodespacerange\n" + f"{len(mapping)} beginbfchar\n" +
                "\n".join(f"<{gid:04x}> <{code:04x}>" for gid, code in mapping.items()) +
                "\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend")
        xref = int(document.xref_get_key(page.get_fonts()[0][0], "ToUnicode")[1].split()[0])
        document.update_stream(xref, cmap.encode("ascii"))
        document.save(path)


def seed(tmp_path):
    pilot = tmp_path / "pilot"
    pdf = tmp_path / "source.pdf"
    _source_pdf(pdf)
    with pymupdf.open(pdf) as document:
        page = document[0]
        text, bbox = page.get_text(), list(page.get_text("blocks")[0][:4])
        pages = [{"pdf_page": 1, "text": text, "width": page.rect.width, "height": page.rect.height}]
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    paper = dict(doc_id="paper", title="Synthetic micro spacing", doi="10.1234/quality",
                 local_path=str(pdf), sha256=sha, page_count=1)
    evidence = dict(evidence_id="paper:p1:b0", doc_id="paper", pdf_page=1, kind="text",
                    parser="pymupdf", document_sha256=sha, bbox=bbox, text=text, normalized_text=normalize(text))
    claim = dict(claim_id="spacing", doc_id="paper", statement="The measured spacing was 5 \uf06dm.",
                 material_system="Cu", sample_id="array", stage="measurement", quantity="spacing",
                 value_text="5", unit="\uf06dm", conditions={"pitch": "5 \uf06dm"},
                 evidence=[dict(pdf_page=1, evidence_id=evidence["evidence_id"], quote=text.strip())],
                 claim_type="reported_experiment", review_status="independent_agent_reviewed")
    write_json(pilot / "catalog/papers.json", [paper])
    write_json(pilot / "catalog/classifications.json", {"records": []})
    write_json(pilot / "catalog/statistics.json", {"unique_papers": 1})
    write_jsonl(pilot / "annotations/claims.jsonl", [claim])
    write_json(pilot / "parsed/paper/pages.json", pages)
    write_jsonl(pilot / "parsed/paper/elements.jsonl", [evidence])
    write_jsonl(pilot / "parsed/evidence.jsonl", [evidence])
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", [evidence])
    write_json(pilot / "parsed/paper/tables.json", [])
    (pilot / "parsed/paper/fulltext.txt").write_text(text, encoding="utf-8")
    record = dict(vector_id=evidence["evidence_id"] + ":seg0", evidence_id=evidence["evidence_id"],
                  doc_id="paper", pdf_page=1, embedding_text=evidence["normalized_text"])
    write_json(pilot / "benchmarks/embedding/all_records.json", [record])
    np.save(pilot / "benchmarks/embedding/all_vectors.npy", embeddings([record["embedding_text"]]))
    return pilot, pdf, evidence, claim


def test_preview_is_readonly_then_apply_updates_all_active_layers_and_is_idempotent(tmp_path):
    pilot, pdf, evidence, claim = seed(tmp_path)
    before = {path: path.read_bytes() for path in pilot.rglob("*") if path.is_file()}
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    preview = backfill.preview_text_quality(pilot)
    assert preview["checked_documents"] == preview["changed_pages"] == preview["changed_blocks"] == 1
    assert preview["changed_claims"] == 1 and preview["quarantined_claims"] == 0
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not (pilot / "ingestion/staging").exists()
    calls = []

    def embed(texts):
        calls.extend(texts)
        return embeddings(texts)

    result = backfill.backfill_text_quality(pilot, embedder=embed)
    assert result["status"] == "backfilled" and result["vector_count"] == 1
    assert len(calls) == 1 and "5 μm" in calls[0]
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == digest
    for name in ("parsed/evidence.jsonl", "parsed/effective_evidence.jsonl", "parsed/paper/elements.jsonl"):
        row = jsonl(pilot / name)[0]
        assert "5 µm" in row["text"] and "5 μm" in row["normalized_text"]
        assert row["raw_text_fields"]["text"] == evidence["text"]
    updated = jsonl(pilot / "annotations/claims.jsonl")[0]
    assert updated["unit"] == "µm" and updated["statement"].endswith("5 µm.")
    assert updated["conditions"]["pitch"] == "5 µm"
    assert updated["raw_text_fields"]["unit"] == claim["unit"]
    assert updated["evidence"][0]["raw_quote"] == claim["evidence"][0]["quote"]
    assert (pilot / "parsed/paper/fulltext.raw.txt").read_text("utf-8") == evidence["text"]
    assert "5 µm" in (pilot / "parsed/paper/fulltext.txt").read_text("utf-8")
    with sqlite3.connect(pilot / "indexes/pilot.sqlite") as db:
        assert "5 µm" in db.execute("SELECT text FROM evidence").fetchone()[0]
        assert json.loads(db.execute("SELECT payload FROM claims").fetchone()[0])["unit"] == "µm"
    backup = Path(result["backup_path"]) / "original"
    assert (backup / "annotations/claims.jsonl").read_bytes() == before[pilot / "annotations/claims.jsonl"]
    after = {path: path.read_bytes() for path in pilot.rglob("*") if path.is_file()}
    again = backfill.backfill_text_quality(pilot,
        embedder=lambda _: pytest.fail("idempotent backfill must not encode"),
        index_builder=lambda _: pytest.fail("idempotent backfill must not build"))
    assert again["status"] == "already_backfilled"
    assert all(path.read_bytes() == content for path, content in after.items())


def test_unresolved_quantitative_claim_is_quarantined_without_disabling_clean_claim(tmp_path):
    pilot, _, evidence, claim = seed(tmp_path)
    bad = deepcopy(claim)
    bad.update(claim_id="unresolved", unit="\ue001m", statement="Unresolved 8 \ue001m", value_text="8")
    bad["evidence"][0]["quote"] = "Unknown unit 8 \ue001m cannot be decoded."
    write_jsonl(pilot / "annotations/claims.jsonl", [claim, bad])
    result = backfill.backfill_text_quality(pilot, embedder=embeddings)
    assert result["quarantined_claims"] == 1
    assert [row["claim_id"] for row in jsonl(pilot / "annotations/claims.jsonl")] == ["spacing"]
    quarantine = jsonl(pilot / backfill.QUARANTINE)
    assert quarantine[0]["claim"]["unit"] == "\ue001m"
    assert quarantine[0]["status"] == "text_quality_needs_review"
    assert backfill.backfill_text_quality(pilot, embedder=embeddings)["status"] == "already_backfilled"


def test_quote_without_verbatim_source_cannot_authorize_unit_replacement(tmp_path):
    pilot, pdf, evidence, claim = seed(tmp_path)
    claim["evidence"][0]["quote"] = "Invented measurement 9 \uf06dm"
    pages = read_json(pilot / "parsed/paper/pages.json")
    result, _ = backfill.repair_claim_text_quality(pdf, [claim], pages, [evidence])
    assert result[0]["unit"] == "\uf06dm"
    assert result[0]["statement"] == claim["statement"]


def test_partial_lines_transfer_by_exact_source_offsets_and_veto_ambiguous_occurrences():
    original = "Full first line\nends in 5 \uf06dm and some\nextra trailing text."
    corrected = original.replace("\uf06d", "µ")
    quote = "first line ends in 5 \uf06dm and some extra"
    assert backfill._transfer_quote(quote, original, corrected) == quote.replace("\uf06d", "µ")
    assert backfill._transfer_quote("5 \uf06dm", "5 \uf06dm; 5 \uf06dm", "5 µm; 5 Am") is None
    assert backfill._transfer_quote("7 \uf06dm", original, corrected) is None


def test_multichar_source_ligatures_and_divergent_font_meanings_remain_reference_bound():
    old = [dict(evidence_id="bottom", pdf_page=1, text="The Bo\x02om Die was checked."),
           dict(evidence_id="invention", pdf_page=2, text="The inven\x02on was reported.")]
    new = [dict(old[0], text="The Bottom Die was checked."),
           dict(old[1], text="The invention was reported.")]
    claim = dict(claim_id="ligatures", statement="Bottom Die and invention at 1 nm.", unit="nm", value_text="1",
                 conditions={"unresolved_model_text": "Bo\x02om inven\x02on"},
                 evidence=[dict(evidence_id=row["evidence_id"], pdf_page=row["pdf_page"], quote=row["text"]) for row in old])
    result, _ = backfill.repair_claim_text_quality(None, [claim], [], new, original_elements=old)
    assert result[0]["evidence"][0]["quote"] == new[0]["text"]
    assert result[0]["evidence"][1]["quote"] == new[1]["text"]
    assert result[0]["statement"] == claim["statement"]
    # The same broken scalar represents tt and ti in the two embedded fonts.
    # A derived model field lacks that font identity and must stay unresolved.
    assert result[0]["conditions"] == claim["conditions"]
    assert backfill._transfer_quote("Bo\x02om Die", old[0]["text"], new[0]["text"]) == "Bottom Die"
    assert backfill._transfer_quote("Bo\x02om", "Bo\x02om; Bo\x02om", "Bottom; Botiom") is None


def test_transfer_rejects_unrelated_normal_source_changes():
    assert backfill._transfer_quote("Value 5 \uf06dm", "Value 5 \uf06dm", "Value 6 µm") is None


def test_ascii_repair_only_changes_a_source_aligned_quote():
    before = {"evidence_id": "x", "pdf_page": 1, "text": "Pad is 5 mm; die is 12 mm."}
    after = dict(before, text="Pad is 5 µm; die is 12 mm.")
    claim = dict(claim_id="ascii", unit="mm", conditions={"die": "12 mm", "pad": "5 mm"},
                 evidence=[dict(before, quote="Pad is 5 mm")])
    repaired, _ = backfill.repair_claim_text_quality(None, [claim], [], [after],
        original_elements=[before], extra_candidates={"m"})
    assert repaired[0]["evidence"][0]["quote"] == "Pad is 5 µm"
    assert repaired[0]["evidence"][0]["raw_quote"] == "Pad is 5 mm"
    assert repaired[0]["conditions"] == claim["conditions"]
    assert repaired[0]["unit"] == "mm"
    assert backfill._transfer_quote("Pad is 5 mm", before["text"], after["text"]) is None


def test_selected_backfill_preserves_unaudited_legacy_claim(tmp_path):
    pilot, _, evidence, claim = seed(tmp_path)
    papers = read_json(pilot / "catalog/papers.json")
    papers.append(dict(papers[0], doc_id="legacy", title="Unaudited legacy source"))
    write_json(pilot / "catalog/papers.json", papers)
    legacy_evidence = dict(evidence, doc_id="legacy", evidence_id="legacy:p1:b0")
    legacy_claim = deepcopy(claim)
    legacy_claim.update(doc_id="legacy", claim_id="legacy_claim")
    legacy_claim["evidence"][0]["evidence_id"] = legacy_evidence["evidence_id"]
    write_jsonl(pilot / "annotations/claims.jsonl", [claim, legacy_claim])
    write_jsonl(pilot / "parsed/evidence.jsonl", [evidence, legacy_evidence])
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", [evidence, legacy_evidence])
    result = backfill.backfill_text_quality(pilot, doc_ids=["paper"], embedder=embeddings)
    assert result["checked_documents"] == 1 and result["quarantined_claims"] == 0
    assert next(row for row in jsonl(pilot / "annotations/claims.jsonl") if row["claim_id"] == "legacy_claim") == legacy_claim


def test_classification_quote_repair_preserves_all_decisions(tmp_path):
    pilot, _, _, claim = seed(tmp_path)
    classification = dict(doc_id="paper", primary_category="alignment_contact", relevance="core",
                          topics=["pitch"], classification_reason="A reviewed classification decision.",
                          evidence=deepcopy(claim["evidence"]))
    write_json(pilot / "catalog/classifications.json", {"version": "1", "records": [classification]})
    result = backfill.backfill_text_quality(pilot, embedder=embeddings)
    assert result["changed_classifications"] == 1
    updated = read_json(pilot / "catalog/classifications.json")["records"][0]
    assert {key: value for key, value in updated.items() if key != "evidence"} == {
        key: value for key, value in classification.items() if key != "evidence"}
    assert "5 µm" in updated["evidence"][0]["quote"]
    assert updated["evidence"][0]["raw_quote"] == classification["evidence"][0]["quote"]
    assert backfill.backfill_text_quality(pilot, embedder=embeddings)["status"] == "already_backfilled"


def test_unchanged_vectors_are_reused_exactly(tmp_path):
    pilot, _, evidence, _ = seed(tmp_path)
    plain = dict(evidence, evidence_id="paper:p1:b1", text="A clean original observation.",
                 normalized_text="A clean original observation.")
    write_jsonl(pilot / "parsed/paper/elements.jsonl", [evidence, plain])
    write_jsonl(pilot / "parsed/evidence.jsonl", [evidence, plain])
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", [evidence, plain])
    records = read_json(pilot / "benchmarks/embedding/all_records.json")
    records.append(dict(records[0], vector_id=plain["evidence_id"] + ":seg0",
                        evidence_id=plain["evidence_id"], embedding_text=plain["text"]))
    write_json(pilot / "benchmarks/embedding/all_records.json", records)
    original_vectors = embeddings([r["embedding_text"] for r in records])
    np.save(pilot / "benchmarks/embedding/all_vectors.npy", original_vectors)
    calls = []

    def embed(texts):
        calls.extend(texts)
        return embeddings(texts)

    backfill.backfill_text_quality(pilot, embedder=embed)
    result_records = read_json(pilot / "benchmarks/embedding/all_records.json")
    vectors = np.load(pilot / "benchmarks/embedding/all_vectors.npy")
    index = next(i for i, row in enumerate(result_records) if row["evidence_id"] == plain["evidence_id"])
    assert np.array_equal(vectors[index], original_vectors[1])
    assert len(calls) == 1 and plain["text"] not in calls


@pytest.mark.parametrize("crash", [False, True])
def test_failed_commit_restores_every_original_or_recovers_after_crash(tmp_path, monkeypatch, crash):
    pilot, _, _, _ = seed(tmp_path)
    before = {name: (pilot / name).read_bytes() for name in (
        "annotations/claims.jsonl", "parsed/evidence.jsonl", "parsed/effective_evidence.jsonl", "parsed/paper/pages.json")}
    original, failed = publication.os.replace, False

    def replace(source, target):
        nonlocal failed
        if not failed and Path(target) == pilot / "parsed/effective_evidence.jsonl" and "staging" in Path(source).parts:
            failed = True
            if crash:
                raise SystemExit("interrupted publication")
            raise OSError("failed publication")
        return original(source, target)

    monkeypatch.setattr(publication.os, "replace", replace)
    with pytest.raises(SystemExit if crash else publication.PublicationError):
        backfill.backfill_text_quality(pilot, embedder=embeddings)
    if crash:
        assert publication.maintenance_status(pilot)["active"]
        assert publication.recover_publication(pilot)["status"] == "rolled_back"
    assert all((pilot / name).read_bytes() == value for name, value in before.items())
    assert not (pilot / backfill.REGISTRY).exists()
    assert not publication.maintenance_status(pilot)["active"]


def test_source_mutation_is_caught_before_commit(tmp_path):
    pilot, pdf, _, _ = seed(tmp_path)
    before = (pilot / "annotations/claims.jsonl").read_bytes()

    def mutate(stage):
        publication._build_indexes(stage)
        pdf.write_bytes(pdf.read_bytes() + b"\nchanged")

    with pytest.raises(publication.DatasetBusy, match="已变化"):
        backfill.backfill_text_quality(pilot, embedder=embeddings, index_builder=mutate)
    assert (pilot / "annotations/claims.jsonl").read_bytes() == before


def test_pdf_hash_mismatch_rejects_preview(tmp_path):
    pilot, pdf, _, _ = seed(tmp_path)
    pdf.write_bytes(pdf.read_bytes() + b"changed")
    with pytest.raises(publication.PublicationError, match="SHA-256"):
        backfill.preview_text_quality(pilot)
