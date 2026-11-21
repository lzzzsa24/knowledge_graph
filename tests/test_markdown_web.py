"""Reading-copy routes and untrusted Markdown handling, using isolated files."""
from __future__ import annotations

import json
import shutil
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import pymupdf
import pytest
from fastapi.testclient import TestClient

from web import server
from web.markdown_reader import render_markdown


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture
def reading_copy(monkeypatch):
    # Use inherited ACLs: this Windows environment cannot reopen pytest's
    # default 0o700 temp directories. Never touch the production pilot files.
    parent = Path(__file__).resolve().parents[1] / ".cache"
    parent.mkdir(exist_ok=True)
    root = parent / ("markdown-web-" + uuid.uuid4().hex)
    pilot = root / "pilot"
    md = pilot / "parsed" / "paper_1" / "fulltext.md"
    pdf = pilot / "collection" / "中文 source.pdf"
    md.parent.mkdir(parents=True)
    pdf.parent.mkdir(parents=True)
    with pymupdf.open() as document:
        document.new_page()
        document.new_page()
        document.save(pdf)
    paper = {"doc_id": "paper_1", "title": "Reading copy", "local_path": str(pdf), "page_count": 2}
    (pilot / "catalog").mkdir()
    (pilot / "catalog/papers.json").write_text(json.dumps([paper]), encoding="utf-8")
    (pilot / "annotations").mkdir()
    (pilot / "annotations/claims.jsonl").write_text("", encoding="utf-8")
    source = ("# Reading copy\n\n## PDF 第 2 页\n\n"
              f"[查看原 PDF 第 2 页](../../collection/{quote(pdf.name)}#page=2)\n\n"
              "| 条件 | 数值 |\n| --- | --- |\n| Temperature | 200 °C |\n")
    md.write_text(source, encoding="utf-8", newline="\n")
    monkeypatch.setattr(server, "ROOT", root)
    monkeypatch.setattr(server, "PILOT", pilot)
    try:
        yield paper, md, pdf, source
    finally:
        assert root.resolve().parent == parent.resolve()
        shutil.rmtree(root)


def test_markdown_download_matches_desktop_file_and_render_has_table_and_physical_page(reading_copy):
    paper, md, _, source = reading_copy
    with TestClient(server.app) as client:
        response = client.get("/api/papers/paper_1/markdown")
        assert response.status_code == 200
        assert response.content == md.read_bytes() == source.encode("utf-8")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in response.headers["content-disposition"]
        rendered = client.get("/api/papers/paper_1/markdown/rendered")
        assert rendered.status_code == 200
        html = rendered.json()["html"]
        tags = Tags(html).tags
        assert any(tag == "table" for tag, _ in tags)
        assert any(tag == "td" for tag, _ in tags)
        link = next(attrs for tag, attrs in tags if tag == "a")
        assert link["href"] == "/api/papers/paper_1/pdf#page=2"
        assert link["data-pdf-page"] == "2"
        assert "noopener" in link["rel"]
        assert str(md) not in json.dumps(rendered.json())
        assert client.get("/api/papers/paper_1/pdf").status_code == 200
        assert client.get("/api/papers/missing/markdown").status_code == 404
        assert server.public_paper(paper)["has_markdown"] is True


def test_missing_markdown_retains_pdf(reading_copy):
    paper, md, _, _ = reading_copy
    md.unlink()
    assert server.public_paper(paper)["has_markdown"] is False
    with TestClient(server.app) as client:
        assert client.get("/api/papers/paper_1/markdown").status_code == 404
        assert client.get("/api/papers/paper_1/markdown/rendered").status_code == 404
        assert client.get("/api/papers/paper_1/pdf").status_code == 200


def test_raw_html_images_and_non_source_files_are_inert(reading_copy):
    _, md, pdf, _ = reading_copy
    source = ("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n"
              "![remote image](https://tracker.invalid/a.png)\n\n"
              "[attack](javascript:alert(1)) [data](data:text/html,hello)\n\n"
              "[other file](../../catalog/papers.json#page=1)\n\n"
              "[unrelated pdf](../../collection/other.pdf#page=1)\n\n"
              f"[invalid page](../../collection/{quote(pdf.name)}#page=3)\n\n"
              "[external](https://example.org/paper)\n\n"
              "| Raw | Values |\n| --- | --- |\n| <svg onload=bad()> | **safe** |\n")
    html = render_markdown(source, markdown_path=md, pdf_path=pdf, doc_id="paper_1", page_count=2)
    tags = Tags(html).tags
    assert not any(tag in {"script", "img", "svg", "iframe", "style"} for tag, _ in tags)
    assert not any(any(name.lower().startswith("on") for name in attrs) for _, attrs in tags)
    assert [attrs["href"] for tag, attrs in tags if tag == "a"] == ["https://example.org/paper"]
    assert "&lt;script&gt;" in html
    assert "[图片：remote image]" in html
    assert "<strong>safe</strong>" in html


def test_markdown_path_rejects_catalog_traversal(reading_copy):
    assert server.local_markdown({"doc_id": "../../catalog"}) is None
    assert server.local_markdown({"doc_id": "../paper_1"}) is None
    assert server.local_markdown({"doc_id": "paper_1/fulltext.md"}) is None
