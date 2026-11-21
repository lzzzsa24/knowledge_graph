"""Use the original, source-checked table transcriptions in reading exports."""
from pathlib import Path
import re

from kg_pilot.effective_evidence import STALE_TEXT
from kg_pilot.pipeline import read_json
from kg_pilot.reading_fallback import apply_reading_fallback
from kg_pilot.reading_glyphs import repair_reading_pages
from kg_pilot.reading_markdown import render_fulltext_markdown


REVIEW_INPUTS = ("benchmarks/table_expected.json", "collection/journals/manifest.json")


def qualify_reading_copy(markdown, qualifications):
    """Attach the current review to its page, retaining the original PDF link."""
    if not qualifications:
        return markdown
    sections = re.split(r"(?m)^(## PDF 第 (\d+) 页\s*)$", markdown)
    for index in range(1, len(sections), 3):
        page = int(sections[index + 1])
        matching = [q for q in qualifications if page in q["pdf_pages"]]
        if any(q["status"] == "stale" for q in matching):
            link = re.search(r"(?m)^\[查看原 PDF 第 \d+ 页\]\([^)]+\)$", sections[index + 2])
            sections[index + 2] = "\n\n" + (link[0] + "\n\n" if link else "") + "> " + STALE_TEXT + "\n\n"
        elif matching:
            notice = "\n\n".join("> " + (q["summary"] + " " + q["usage_limit"]).replace("\n", "\n> ")
                                   for q in matching)
            sections[index + 2] = "\n\n" + notice + "\n\n" + sections[index + 2].lstrip("\n")
    return "".join(value for index, value in enumerate(sections) if index % 3 != 2)


def render_paper_reading_copy(pilot, paper, pages, elements, *, markdown_path,
                              tables, ocr_elements=(), source_pdf=None):
    """Use the original PDF's font information for presentation, without changing stored evidence."""
    tables = reading_tables(pilot, paper, tables)
    pages, elements, tables = repair_reading_pages(
        source_pdf or paper["local_path"], pages, elements, tables=tables)
    markdown = render_fulltext_markdown(paper, pages, elements, markdown_path=markdown_path,
                                        tables=tables, ocr_elements=ocr_elements)
    return apply_reading_fallback(markdown)


def reading_tables(pilot, paper, tables):
    """Prefer a checked grid over its legacy parser candidate for the same PDF."""
    registry, manifest = (Path(pilot) / rel for rel in REVIEW_INPUTS)
    if not registry.is_file() or not manifest.is_file():
        return tables
    doc_id = paper.get("doc_id") or paper.get("id")
    verified = [t for t in read_json(registry)["tables"] if t["doc_id"] == doc_id]
    if not verified:
        return tables
    # The original table audit identifies the PDF by filename; the acquisition
    # manifest binds that file to the hash in today's catalog.
    hashes = {Path(str(row["local_path"]).replace("\\", "/")).name: row.get("sha256")
              for row in read_json(manifest) if row.get("local_path")}
    result = list(tables)
    for table in verified:
        filename = Path(table["pdf_path"]).name
        if not paper.get("sha256") or hashes.get(filename) != paper["sha256"]:
            continue
        grid = table["grid"]
        page = table["pdf_page"]
        entry = dict(evidence_id=table["table_id"], doc_id=doc_id, pdf_page=page,
                     table_caption=table["caption"], kind="table", parser="reviewed_transcription",
                     review_status="source_page_checked", columns=grid[0], rows=grid[1:])
        # Each selected page in the audit has a single target with this shape.
        # Borrow its placement, never its unverified numerical cells.
        matches = [candidate for candidate in result
                   if candidate.get("pdf_page") == page and candidate.get("review_status") == "parser_candidate_not_verified"
                   and len(candidate.get("cells") or []) == len(grid)
                   and all(len(row) == len(grid[0]) for row in candidate["cells"])]
        if len(matches) == 1:
            entry["bbox"] = matches[0]["bbox"]
            result.remove(matches[0])
        # A later evidence backfill can add a second, structured extraction at
        # the very same location. Present that table only once in the reader.
        for candidate in result.copy():
            if candidate.get("pdf_page") != page:
                continue
            same_caption = " ".join(candidate.get("table_caption", "").casefold().split()) == " ".join(table["caption"].casefold().split())
            old_box = entry.get("bbox")
            new_box = candidate.get("table_bbox") or candidate.get("bbox")
            same_box = old_box and new_box and len(old_box) == len(new_box) == 4 and all(abs(a - b) <= 2 for a, b in zip(old_box, new_box))
            if same_caption or same_box:
                result.remove(candidate)
        result.append(entry)
    return result
