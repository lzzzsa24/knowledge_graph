"""Glyph decoding needs the actual font and source occurrence, never PUA alone."""
from collections import defaultdict
from copy import deepcopy
import hashlib
import re
import struct

import pymupdf
import pytest

from kg_pilot.reading_glyphs import (_Font, _Line, _page_lines, _repair_text, _sfnt_cmaps,
                                    repair_reading_pages)


def _source_pdf(tmp_path, lines, *, alias="\xb5"):
    """A real embedded CFF source whose ToUnicode incorrectly uses PUA.

    The drawn font still names each actual glyph; changing ToUnicode does not
    change the visible PDF. No local/system font or corpus PDF is required.
    """
    path = tmp_path / "source.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_font(fontname="embedded", fontbuffer=pymupdf.Font("helv").buffer)
        for i, text in enumerate(lines):
            page.insert_text((40, 60 + 24 * i), text, fontname="embedded")
        mapping = {}
        for span in page.get_texttrace():
            for code, gid, _, _ in span["chars"]:
                mapping[gid] = 0xf06d if chr(code) == alias else code
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


def test_source_glyph_repairs_copies_and_table_fields_without_mutation(tmp_path):
    pdf, text, box = _source_pdf(tmp_path, ["Measured 5 \xb5m."])
    pages = [{"pdf_page": 1, "text": text}]
    elements = [{"pdf_page": 1, "text": text, "bbox": box, "parser": "pymupdf",
                 "normalized_text": re.sub(r"\s+", " ", text).strip(),
                 "evidence_id": "original", "document_sha256": "unchanged"}]
    tables = [{"pdf_page": 1, "parser": "pymupdf_table", "bbox": box,
               "table_caption": text.strip(), "columns": ["Distance"],
               "rows": [["5 \uf06dm"]], "cells": [["5 \uf06dm"]]}]
    original = deepcopy((pages, elements, tables))
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    new_pages, new_elements, new_tables = repair_reading_pages(pdf, pages, elements, tables=tables)
    assert "5 \xb5m" in new_pages[0]["text"]
    assert "\uf06d" not in str((new_pages, new_elements, new_tables))
    assert new_tables[0]["rows"] == [["5 \xb5m"]]
    assert new_elements[0]["evidence_id"] == "original"
    assert new_elements[0]["document_sha256"] == "unchanged"
    assert (pages, elements, tables) == original
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == digest


def test_same_pua_is_decoded_as_its_actual_letter_not_mu(tmp_path):
    pdf, text, _ = _source_pdf(tmp_path, ["Value 5 Am."], alias="A")
    new_pages, _, _ = repair_reading_pages(pdf, [{"pdf_page": 1, "text": text}], [])
    assert new_pages[0]["text"] == "Value 5 Am.\n"


@pytest.mark.parametrize("bad_code", [2, 0xfffd])
def test_corrupt_control_or_replacement_uses_drawn_glyph(tmp_path, bad_code):
    pdf, text, _ = _source_pdf(tmp_path, ["Value 5 Am."])
    with pymupdf.open(pdf) as doc:
        page = doc[0]
        class DamagedTextLayer:
            # Emulate a bad structured-text mapping, keeping the real font,
            # coordinate and independent drawing trace from the fixture.
            parent = doc
            get_fonts = page.get_fonts
            get_texttrace = page.get_texttrace

            def get_text(self, *args, **kwargs):
                data = page.get_text(*args, **kwargs)
                for line in data["blocks"][0]["lines"]:
                    for span in line["spans"]:
                        for ch in span["chars"]:
                            if ch["c"] == "A":
                                ch["c"] = chr(bad_code)
                return data

        lines = _page_lines(DamagedTextLayer(), {})
    assert _repair_text(text.replace("A", chr(bad_code)), lines) == text


def test_reviewed_readable_character_requires_source_font_glyph_and_code(tmp_path, monkeypatch):
    from kg_pilot import reading_glyphs
    pdf, text, _ = _source_pdf(tmp_path, ["Value þ."])
    page = {"pdf_page": 1, "text": text}
    with pymupdf.open(pdf) as doc:
        font = doc.extract_font(doc[0].get_fonts()[0][0])[3]
        gid = next(gid for span in doc[0].get_texttrace()
                   for code, gid, _, _ in span["chars"] if chr(code) == "þ")
    source_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    font_sha = hashlib.sha256(font).hexdigest()
    for sha, embedded, drawn, expected in [
        ("different PDF", font_sha, gid, text),
        (source_sha, "different font", gid, text),
        (source_sha, font_sha, gid + 1, text),
        (source_sha, font_sha, gid, text.replace("þ", "+")),
    ]:
        monkeypatch.setattr(reading_glyphs, "_REVIEWED_GLYPHS", {sha: {embedded: {("þ", drawn): "+"}}})
        assert repair_reading_pages(pdf, [page], [])[0][0]["text"] == expected


def test_unmatched_source_context_and_out_of_box_characters_remain(tmp_path):
    pdf, text, _ = _source_pdf(tmp_path, ["Known 5 \xb5m."])
    pages = [{"pdf_page": 1, "text": "Invented 7 \uf06dm."}]
    elements = [{"pdf_page": 1, "text": text, "bbox": [400, 400, 500, 500]}]
    assert repair_reading_pages(pdf, pages, elements) == (pages, elements, [])


def test_ocr_and_reviewed_records_are_preserved_in_every_sequence(tmp_path):
    pdf, text, box = _source_pdf(tmp_path, ["Measured 5 \xb5m."])
    ocr = {"pdf_page": 1, "text": text, "bbox": box, "parser": "rapidocr"}
    checked = {"pdf_page": 1, "text": text, "parser": "reviewed_transcription",
               "table_caption": text, "rows": [["\uf06d"]]}
    assert repair_reading_pages(pdf, [ocr], [ocr], tables=[checked]) == ([ocr], [ocr], [checked])


def test_ambiguous_cell_cannot_borrow_a_meaning_from_another_occurrence():
    lines = [_Line("5\uf06dm", {1: "μ"}, (0, 0, 20, 10)),
             _Line("6\uf06dm", {1: "A"}, (0, 20, 20, 30))]
    assert _repair_text("\uf06d", lines) == "\uf06d"
    assert _repair_text("5 \uf06dm", lines) == "5 μm"
    assert _repair_text("6 \uf06dm", lines) == "6 Am"
    assert _repair_text("5 \uf06dm", [_Line("5\uf06dm", {}, (0, 0, 20, 10))]) == "5 \uf06dm"


def test_font_family_and_actual_cmap_gid_are_both_required():
    font = object.__new__(_Font)
    font.family = "symbolmt"
    font.font = None
    font.data = b""
    font.unicode_by_gid = defaultdict(set)
    font.symbol_slots = defaultdict(set, {80: {0x6d}, 86: {0x73}, 79: {0x6c}})
    assert font.decode(0xf06d, 80) == "μ"
    assert font.decode(0xf073, 86) == "σ"
    assert font.decode(0xf06d, 1) is None
    font.family = "arbitraryprivatefont"
    assert font.decode(0xf06d, 80) is None
    font.family = "wingdings"
    assert font.decode(0xf06c, 79) == "●"
    font.family = "symbolmt"
    assert font.decode(0xf06c, 79) == "λ"
    # Compatibility-equivalent cmap alternatives still have distinct source
    # values; NFKC is not authority to choose one.
    font.unicode_by_gid[80] = {"µ", "μ"}
    assert font.decode(0xf06d, 80) is None


def test_native_text_is_repaired_but_ocr_mode_is_preserved(tmp_path):
    pdf, text, _ = _source_pdf(tmp_path, ["Measured 5 \xb5m."])
    native = {"pdf_page": 1, "text": "OCR-independent wrapper", "native_text": text, "mode": "native"}
    ocr = {"pdf_page": 1, "native_text": text, "mode": "rapidocr"}
    pages, _, _ = repair_reading_pages(pdf, [native, ocr], [])
    assert pages[0]["native_text"] == "Measured 5 \xb5m.\n"
    assert pages[0]["text"] == native["text"]
    assert pages[1] == ocr


def test_one_unreadable_page_does_not_prevent_safe_output(tmp_path, monkeypatch):
    from kg_pilot import reading_glyphs
    pdf, text, _ = _source_pdf(tmp_path, ["Measured 5 \xb5m."])
    def broken(*args):
        raise RuntimeError("Source font is malformed")
    monkeypatch.setattr(reading_glyphs, "_page_lines", broken)
    pages = [{"pdf_page": 1, "text": text}]
    assert repair_reading_pages(pdf, pages, []) == (pages, [], [])


def test_trace_unicode_must_match_the_raw_glyph_font_and_origin():
    class Page:
        code, font, origin = 0x25aa, "Wingdings-Regular", (40.0, 60.0)
        def get_fonts(self, **kwargs):
            return []
        def get_texttrace(self):
            return [{"font": self.font, "chars": [(self.code, 131, self.origin, (40, 50, 45, 60))]}]
        def get_text(self, kind, **kwargs):
            assert not kwargs["flags"] & pymupdf.TEXT_PRESERVE_IMAGES
            return {"blocks": [{"lines": [{"bbox": (40, 50, 45, 60), "spans": [
                {"font": "Wingdings-Regular", "chars": [{"c": "\uf0a7", "origin": (40.0, 60.0)}]}
            ]}]}]}
    page = Page()
    assert _page_lines(page, {})[0].replacements == {0: "▪"}
    page.origin = (60.0, 60.0)
    assert _page_lines(page, {})[0].replacements == {}
    page.origin, page.font = (40.0, 60.0), "UnrelatedFont"
    assert _page_lines(page, {})[0].replacements == {}
    page.font, page.code = "Wingdings-Regular", 0xfffd
    assert _page_lines(page, {})[0].replacements == {}


def test_real_unicode_and_normal_bullets_take_fast_path(tmp_path, monkeypatch):
    path = tmp_path / "not-really-a-pdf.pdf"
    path.touch()
    def unexpected(*args, **kwargs):
        raise AssertionError("A page without private-use characters must not open a PDF")
    monkeypatch.setattr(pymupdf, "open", unexpected)
    pages = [{"pdf_page": 1, "text": "25 µm; 3σ; ■ item; □ option; Xperi’s"}]
    assert repair_reading_pages(path, pages, []) == (pages, [], [])


def test_unknown_pua_missing_or_invalid_pdf_and_page_are_preserved(tmp_path):
    records = [{"pdf_page": 1, "text": "Unknown \ue001"},
               {"pdf_page": 0, "text": "Invalid page \uf06d"}]
    assert repair_reading_pages(tmp_path / "missing.pdf", records, []) == (records, [], [])
    invalid = tmp_path / "invalid.pdf"
    invalid.write_bytes(b"not a PDF")
    assert repair_reading_pages(invalid, records, []) == (records, [], [])


def test_sfnt_cmap_separates_symbol_slots_from_real_unicode():
    # Tiny cmap-only SFNT validates the table reader; drawing-glyph checks are
    # exercised with real embedded CFF sources above.
    def format12(code, gid):
        return struct.pack(">HHIIIII", 12, 0, 28, 0, 1, code, code) + struct.pack(">I", gid)
    unicode_map, symbol_map = format12(0x3bc, 80), format12(0xf06d, 80)
    cmap = (struct.pack(">HH", 0, 2) + struct.pack(">HHI", 3, 10, 20) +
            struct.pack(">HHI", 3, 0, 20 + len(unicode_map)) + unicode_map + symbol_map)
    sfnt = (b"\x00\x01\x00\x00" + struct.pack(">HHHH", 1, 16, 0, 0) +
            struct.pack(">4sIII", b"cmap", 0, 28, len(cmap)) + cmap)
    assert _sfnt_cmaps(sfnt) == ([{0x3bc: 80}], [{0xf06d: 80}])
    assert _sfnt_cmaps(sfnt[:25]) == ([], [])


def test_malformed_or_overlapping_cmap_ranges_do_not_expand():
    def font(groups, *, declared=None):
        entries = b"".join(struct.pack(">III", *g) for g in groups)
        subtable = struct.pack(">HHIII", 12, 0, 16 + len(entries), 0,
                               len(groups) if declared is None else declared) + entries
        cmap = struct.pack(">HHHHI", 0, 1, 3, 10, 12) + subtable
        return (b"\x00\x01\x00\x00" + struct.pack(">HHHH", 1, 16, 0, 0) +
                struct.pack(">4sIII", b"cmap", 0, 28, len(cmap)) + cmap)
    assert _sfnt_cmaps(font([(65, 80, 1), (70, 90, 2)])) == ([], [])
    assert _sfnt_cmaps(font([(1, 0x10ffff, 1)])) == ([], [])
    assert _sfnt_cmaps(font([], declared=0xffffffff)) == ([], [])
