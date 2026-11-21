"""Source font repair must affect ingestion evidence, not merely its reading copy."""
from copy import deepcopy
import hashlib
import re

import pymupdf
import pytest

from kg_pilot import ingestion as ing
from kg_pilot import ingestion_publish as publication, pipeline, table_backfill
from kg_pilot.pipeline import normalize, write_json, write_jsonl
from kg_pilot.text_quality import (QUALITY_VERSION, guard_claim_text_quality,
                                   repair_document_text, scan_text_quality, _changes)


def source_pdf(tmp_path, *, unknown=False):
    path = tmp_path / "source.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_font(fontname="embedded", fontbuffer=pymupdf.Font("helv").buffer)
        page.insert_text((40, 170), "Measured sample width was 5 \xb5m and the source was inspected.", fontname="embedded")
        mapping = {gid: 0xf06d if chr(code) == "\xb5" else code
                   for span in page.get_texttrace() for code, gid, _, _ in span["chars"]}
        cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
                "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def\n"
                "/CMapName /Test-UCS def\n/CMapType 2 def\n"
                "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
                f"{len(mapping)} beginbfchar\n" +
                "\n".join(f"<{gid:04x}> <{code:04x}>" for gid, code in mapping.items()) +
                "\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend")
        font = page.get_fonts()[0][0]
        xref = int(document.xref_get_key(font, "ToUnicode")[1].split()[0])
        document.update_stream(xref, cmap.encode("ascii"))
        document.save(path)
    with pymupdf.open(path) as document:
        page = document[0]
        text = page.get_text()
        box = list(page.get_text("blocks")[0][:4])
    return path, text, box


def records(tmp_path):
    path, text, box = source_pdf(tmp_path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    pages = [dict(pdf_page=1, text=text, native_text=text, width=595, height=842)]
    elements = [dict(pdf_page=1, text=text, normalized_text=normalize(text), bbox=box,
                     parser="pymupdf", kind="text", evidence_id="paper:p1:b0",
                     document_sha256=sha, doc_id="paper")]
    return path, sha, pages, elements


def test_source_repairs_preserve_original_and_are_idempotent(tmp_path):
    pdf, sha, pages, elements = records(tmp_path)
    before = deepcopy((pages, elements))
    fixed_pages, fixed_elements, _, audit = repair_document_text(pdf, pages, elements, document_sha256=sha)
    assert "5 µm" in fixed_pages[0]["text"]
    assert fixed_pages[0]["native_text"] == pages[0]["native_text"]
    assert fixed_pages[0]["raw_text_fields"]["text"] == pages[0]["text"]
    assert fixed_elements[0]["evidence_id"] == elements[0]["evidence_id"]
    assert fixed_elements[0]["document_sha256"] == sha
    assert audit["status"] == "repaired" and not audit["unresolved_critical"]
    assert (pages, elements) == before
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == sha
    repeated = repair_document_text(pdf, fixed_pages, fixed_elements, document_sha256=sha)
    assert repeated[:2] == (fixed_pages, fixed_elements)
    assert repeated[3] == audit


def test_unmatched_corruption_retained_and_hash_cannot_authorize_another_source(tmp_path):
    pdf, sha, pages, elements = records(tmp_path)
    pages[0]["text"] = "Unknown sample is 12 \ue001m, not present in the source."
    elements[0]["text"] = pages[0]["text"]
    fixed_pages, fixed_elements, _, audit = repair_document_text(pdf, pages, elements, document_sha256=sha)
    assert fixed_pages[0]["text"] == pages[0]["text"]
    assert fixed_elements[0]["text_quality"]["unresolved_critical"]
    assert audit["pages"][0]["quantitative_context_issues"]
    with pytest.raises(ValueError, match="SHA-256"):
        repair_document_text(pdf, pages, elements, document_sha256="0" * 64)


def test_scan_both_common_corruption_and_valid_scientific_unicode():
    assert not scan_text_quality("5 μm; 7 µm; 20 °C; 3σ; 1×10⁻⁶; −2; ■ item; Xperi’s")
    kinds = {issue["kind"] for issue in scan_text_quality("5 \uf06dm; \ufffd; (cid:13); \x01; 20 Âµm")}
    assert kinds == {"private_use", "replacement_character", "cid_fragment", "invalid_control", "possible_mojibake"}


@pytest.mark.parametrize("field", ["unit", "statement", "conditions", "quote"])
def test_quantitative_gate_checks_values_and_actual_quote(field):
    claim = dict(statement="The width is 5 µm.", value_text="5", unit="µm", conditions={},
                 evidence=[dict(pdf_page=1, quote="The measured width is 5 µm.")])
    if field == "conditions":
        claim[field] = {"temperature": "20 \ufffdC"}
    elif field == "quote":
        claim["evidence"][0]["quote"] = "The measured width is 5 \uf06dm."
    else:
        claim[field] = "5 \uf06dm"
    with pytest.raises(ValueError, match="未确认乱码"):
        guard_claim_text_quality(claim)
    claim = dict(statement="The width is 5 µm.", value_text="5", unit="µm", conditions={},
                 evidence=[dict(pdf_page=1, quote="The measured width is 5 µm.")])
    guard_claim_text_quality(claim, pages=[{"pdf_page": 1, "text": "Unrelated \ue001 marker"}])


def test_parse_repairs_before_ocr_and_retains_audit(tmp_path, monkeypatch):
    pdf, sha, pages, elements = records(tmp_path)
    from kg_pilot import table_evidence
    monkeypatch.setattr(table_evidence, "extract_table_evidence", lambda *args, **kwargs: [])
    parsed = ing.parse_document(dict(source_path=str(pdf), sha256=sha, doc_id="paper", filename=pdf.name),
                                ocr=lambda page: pytest.fail("A source-proven unit does not need OCR"))
    assert parsed["parser_version"] == ing.PARSER_VERSION
    assert parsed["pages"][0]["native_text"] == pages[0]["native_text"]
    assert "5 µm" in parsed["pages"][0]["text"]
    assert "5 µm" in parsed["elements"][0]["text"]
    assert parsed["text_quality"]["status"] == "repaired"


def test_publication_entry_rechecks_old_prepared_claims_without_mutation(tmp_path):
    pdf, sha, pages, elements = records(tmp_path)
    claim = dict(claim_id="paper_c1", doc_id="paper", statement="The width is 5 \uf06dm.",
                 material_system="Cu", sample_id="s", stage="measurement", quantity="width",
                 value_text="5", unit="\uf06dm", conditions={}, claim_type="reported_experiment",
                 review_status="independent_agent_reviewed",
                 evidence=[dict(pdf_page=1, quote=pages[0]["text"].strip(), evidence_id="paper:p1:b0")])
    prepared = dict(batch_id="batch1", documents=[dict(paper=dict(doc_id="paper", sha256=sha,
        local_path=str(pdf), page_count=1), pages=pages, elements=elements, claims=[claim],
        classification=dict(doc_id="paper", source_sha256=sha, classification_review="independent_agent_reviewed"))])
    directory = tmp_path / "batch"
    write_json(directory / "prepared.json", prepared)
    original = (directory / "prepared.json").read_bytes()
    with pytest.raises(publication.PublicationError, match="未确认乱码"):
        publication.publish_batch(directory, tmp_path / "pilot", embedder=lambda values: pytest.fail("Do not embed corrupt units"))
    assert (directory / "prepared.json").read_bytes() == original
    assert not (tmp_path / "pilot/catalog/papers.json").exists()


def test_legacy_index_preflight_leaves_existing_sqlite_untouched(tmp_path, monkeypatch):
    write_jsonl(tmp_path / "annotations/claims.jsonl", [dict(claim_id="unsafe", statement="5 \uf06dm", value_text="5", unit="\uf06dm")])
    index = tmp_path / "indexes/pilot.sqlite"
    index.parent.mkdir()
    index.write_bytes(b"existing index sentinel")
    monkeypatch.setattr(pipeline, "PILOT", tmp_path)
    monkeypatch.setattr(pipeline.sqlite3, "connect", lambda *args: pytest.fail("Do not open indexes before gate"))
    with pytest.raises(pipeline.LegacyRebuildError, match="未确认乱码"):
        pipeline.index()
    assert index.read_bytes() == b"existing index sentinel"


def test_table_dedup_ignores_audit_only_and_rejects_real_changes():
    old = dict(evidence_id="table", text="5 µm", raw_text_fields={"text": "5 \uf06dm"},
               text_quality={"version": "old", "status": "repaired"})
    proposed = dict(old, text_quality={"version": QUALITY_VERSION, "status": "repaired"})
    merged, added = table_backfill._merge([old], [proposed])
    assert merged == [old] and not added
    with pytest.raises(publication.PublicationError, match="禁止覆盖"):
        table_backfill._merge([old], [dict(proposed, text="50 µm")])


def test_ocr_text_is_flagged_without_borrowing_native_font_repair(tmp_path):
    pdf, sha, pages, elements = records(tmp_path)
    elements[0]["parser"] = "rapidocr"
    _, fixed, _, audit = repair_document_text(pdf, [], elements, document_sha256=sha)
    assert fixed[0]["text"] == elements[0]["text"]
    assert audit["unresolved_critical"] and not audit["repaired_characters"]


def test_ligature_audit_counts_source_glyph_without_shifted_suffix():
    change = _changes("Bo\x02om Die", "Bottom Die", "text")[0]
    assert change["changed_characters"] == 1
    assert change["output_characters"] == 2
    assert change["replacements"] == [dict(before="\x02", after="tt", count=1)]


def test_final_normalized_text_regeneration_preserves_its_original(tmp_path):
    record = dict(pdf_page=1, text="Width is 5 µm", normalized_text="Width is 5 µm", parser="pymupdf")
    _, elements, _, _ = repair_document_text(None, [], [record])
    fixed = elements[0]
    assert fixed["normalized_text"] == "Width is 5 μm"
    assert fixed["raw_text_fields"]["normalized_text"] == record["normalized_text"]
    assert fixed["text_quality"]["repairs"][0]["replacements"] == [dict(before="µ", after="μ", count=1)]
