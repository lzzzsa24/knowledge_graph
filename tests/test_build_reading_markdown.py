from pathlib import Path

from markdown_it import MarkdownIt
import pymupdf

from scripts.build_reading_markdown import build_reading_copies
from kg_pilot.pipeline import write_json
from kg_pilot.reading_sources import reading_tables


def test_staged_reading_copy_keeps_gao_body_in_one_paragraph_and_decodes_units(tmp_path):
    from kg_pilot.reading_sources import render_paper_reading_copy

    # New documents have an eventual catalog path and a separate staged PDF.
    # Read its real Symbol glyphs but keep links pointing to the eventual path.
    original = Path(__file__).resolve().parents[1] / "pilot/collection/reliability/gao_d2w_2021.pdf"
    with pymupdf.open(original) as pdf:
        page = pdf[1]
        paragraph = next(b[4] for b in page.get_text("blocks") if "shows the schematic layout" in b[4])
    assert "\uf06d" in paragraph
    record = {"pdf_page": 2, "text": paragraph, "width": 612, "height": 792}
    final_pdf = tmp_path / "collection" / "published.pdf"
    md = render_paper_reading_copy(
        tmp_path, {"doc_id": "staged", "title": "Example", "local_path": str(final_pdf)},
        [record], [], tables=[], markdown_path=tmp_path / "parsed/staged/fulltext.md", source_pdf=original)
    tokens = MarkdownIt().parse(md)
    body = [tokens[i + 1].content for i, token in enumerate(tokens) if token.type == "paragraph_open"
            and "shows the schematic layout" in tokens[i + 1].content]
    assert len(body) == 1
    assert body[0].endswith("measurement are on the host wafer.")
    for measurement in ("50 μm", "5 μm", "35 μm"):
        assert measurement in body[0].replace("µ", "μ")
    assert "\uf06d" not in md and "published.pdf#page=2" in md
    assert record["text"] == paragraph


def test_refresh_published_copies_preserves_source_and_is_repeatable(tmp_path):
    pilot = tmp_path / "pilot"
    pdf = pilot / "collection" / "example #1.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.touch()  # This exporter reads already-parsed sources.
    paper = dict(doc_id="old", local_path=str(pdf), title="Example", page_count=1)
    write_json(pilot / "catalog/papers.json", [paper])
    directory = pilot / "parsed/old"
    write_json(directory / "pages.json", [{"pdf_page": 1, "text": "Old result.", "width": 300, "height": 400}])
    write_json(directory / "tables.json", [])
    (directory / "elements.jsonl").touch()
    (directory / "fulltext.txt").write_bytes(b"legacy")
    assert build_reading_copies(pilot, check=True)["changed"] == 1
    assert not (directory / "fulltext.md").exists()
    assert build_reading_copies(pilot)["changed"] == 1
    md = (directory / "fulltext.md").read_text(encoding="utf-8")
    assert "Old result." in md and "example%20%231.pdf#page=1" in md
    assert build_reading_copies(pilot, check=True)["changed"] == 0
    write_json(directory / "pages.json", [{"pdf_page": 1, "text": "Corrected result.", "width": 300, "height": 400}])
    assert build_reading_copies(pilot, doc_ids=["old"])["changed"] == 1
    assert "Corrected result." in (directory / "fulltext.md").read_text(encoding="utf-8")
    assert (directory / "fulltext.txt").read_bytes() == b"legacy"


def test_reviewed_reading_table_requires_same_pdf_and_keeps_other_tables(tmp_path):
    write_json(tmp_path / "benchmarks/table_expected.json", {"tables": [{
        "doc_id": "old", "table_id": "verified", "pdf_path": "collection/journals/source.pdf",
        "pdf_page": 1, "caption": "Table 1", "grid": [["Layer", "Yield"], ["A", "1.24"]]}]})
    write_json(tmp_path / "collection/journals/manifest.json", [
        {"local_path": "archive/source.pdf", "sha256": "original"}])
    candidate = dict(pdf_page=1, review_status="parser_candidate_not_verified",
                     cells=[["Layer", "Yield"], ["A", "124 ."]], bbox=[0, 0, 40, 80])
    other = {"pdf_page": 2, "cells": [["Unrelated"]]}
    paper = {"doc_id": "old", "sha256": "original"}
    result = reading_tables(tmp_path, paper, [candidate, other])
    assert result[0] == other and result[1]["rows"] == [["A", "1.24"]]
    assert result[1]["bbox"] == candidate["bbox"]
    assert reading_tables(tmp_path, {**paper, "sha256": "changed"}, [candidate]) == [candidate]
