"""Legacy parsing writes its reading copy only in an isolated temporary pilot."""
import hashlib

import pymupdf

from kg_pilot import pipeline
from kg_pilot.pipeline import read_json, write_json


def test_legacy_parse_creates_markdown_and_preserves_existing_plain_text(tmp_path, monkeypatch):
    pilot = tmp_path / "pilot"
    pdf = tmp_path / "original.pdf"
    with pymupdf.open() as source:
        page = source.new_page()
        page.insert_text((40, 60), "Paragraph one preserves 25 MPa as measured.")
        source.save(pdf)
    paper = dict(doc_id="legacy_doc", title="Legacy article", local_path=str(pdf),
                 sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), page_count=1)
    write_json(pilot / "catalog/papers.json", [paper])
    old_plaintext = pilot / "parsed/legacy_doc/fulltext.txt"
    old_plaintext.parent.mkdir(parents=True)
    old_plaintext.write_text("Preserved historical plaintext", encoding="utf-8")
    monkeypatch.setattr(pipeline, "PILOT", pilot)
    pipeline.parse()
    markdown = (pilot / "parsed/legacy_doc/fulltext.md").read_text(encoding="utf-8")
    assert "Legacy article" in markdown and "25 MPa" in markdown
    assert "original.pdf" in markdown
    assert b"\r" not in (pilot / "parsed/legacy_doc/fulltext.md").read_bytes()
    assert old_plaintext.read_text(encoding="utf-8") == "Preserved historical plaintext"
    assert read_json(pilot / "parsed/legacy_doc/pages.json")[0]["text"].strip() == "Paragraph one preserves 25 MPa as measured."
