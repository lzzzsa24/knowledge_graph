"""Pure, source-bound Markdown reading copies of parsed PDF pages.

This module neither reads PDFs nor promotes extraction candidates to evidence.
Callers select the source records (including any reviewed OCR replacement) and
write the returned UTF-8 text.  Page numbers always mean physical PDF pages.
"""
from __future__ import annotations

import html
import ntpath
import os
from pathlib import Path
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import quote


Record = Mapping[str, Any]
_GRID_PARSERS = {"pymupdf_table", "rapidocr_table", "reviewed_transcription"}
_UNSAFE_STATUS = re.compile(r"not_verified|unverified|rejected|ambiguous|candidate", re.I)
_NUMBERED_HEADING = re.compile(r"^(?:[IVXLCDM]+[.]|\d+(?:\.\d+)*[.]?|[A-Z][.])\s+(.+)$")
_NAMED_HEADING = re.compile(
    r"^(?:abstract|keywords|introduction|conclusions?|references|acknowledg(?:e)?ments?|"
    r"摘要|关键词|引言|结论|参考文献)\s*[:：]?\s*$", re.I)
_SECTION_TITLE_START = re.compile(
    r"^(?:abstract|introduction|background|materials?|methods?|methodology|"
    r"experiments?|experimental|results?|discussion|conclusions?|references|"
    r"acknowledg(?:e)?ments?|appendix|appendices)\b", re.I)
_MEASUREMENT_START = re.compile(
    r"^(?:[fpnumkMGTµμ]?(?:m|s|g|V|W|Hz|Pa|J|N|F|H|K|Ω)|"
    r"ohms?|volts?|watts?|kelvin|degrees?)\b")
_REFERENCE_CONTINUATION = re.compile(
    r"\b(?:fig(?:ure)?s?|tables?|eq(?:uation)?s?|refs?|references?|sec(?:tion)?s?)\.$", re.I)
_REFERENCES_HEADING = re.compile(r"^(?:references|参考文献)\s*[:：]?\s*$", re.I)


def _flat(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _escape(value: Any, *, cell: bool = False) -> str:
    """Escape source text, including raw HTML and Markdown link syntax."""
    value = html.escape(_flat(value), quote=False).replace("\\", "\\\\")
    for char in "`*_{}[]<>()#!|~":
        value = value.replace(char, "\\" + char)
    # A source line beginning with '-' or '+' must not become an invented list.
    if not cell:
        value = re.sub(r"^([+-])(?=\s)", r"\\\1", value)
        value = re.sub(r"^(\d+)\.(?=\s)", r"\1\\.", value)
    return value


def relative_pdf_url(paper: Record, markdown_path: str | Path | None = None,
                     *, page: int | None = None) -> str:
    """Return a URL-encoded local PDF link relative to ``fulltext.md``.

    Explicit ``markdown_path`` is preferred. The default uses the repository's
    ``parsed/<doc_id>/fulltext.md`` to ``collection/...`` layout. No file is read.
    """
    source = str(paper.get("local_path") or paper.get("pdf_path") or "")
    if not source:
        return ""
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", source) or source.startswith("\\\\"))
    if markdown_path is not None:
        target = str(markdown_path)
        if windows:
            relative = ntpath.relpath(source, ntpath.dirname(target))
        else:
            relative = os.path.relpath(source, Path(target).parent)
    else:
        parts = source.replace("\\", "/").split("/")
        if "collection" in parts:
            relative = "../../" + "/".join(parts[parts.index("collection"):])
        elif not (windows or source.startswith("/")):
            relative = source
        else:
            raise ValueError("An explicit markdown_path is required for this PDF location")
    url = quote(relative.replace("\\", "/"), safe="/.-_~")
    if page is not None:
        if type(page) is not int or page < 1:
            raise ValueError("PDF page must be a positive physical page number")
        url += f"#page={page}"
    return url


@dataclass(frozen=True)
class _SourceSpan:
    first: int
    last: int

    def start(self):
        return self.first

    def end(self):
        return self.last


def _source_match(text: str, value: str):
    tokens = str(value).split()
    if not tokens:
        return None
    match = re.search(r"\s+".join(re.escape(token) for token in tokens), text, re.I)
    if match:
        return match
    # Typographic PDF ligatures (e.g. 'speciﬁc') and superscripts differ from a
    # checked transcription's spelling. Compatibility normalization is only for
    # locating exact source spans; the original prose itself is never rewritten.
    normalized, offsets = [], []
    for index, char in enumerate(text):
        compatible = unicodedata.normalize("NFKC", char)
        normalized.extend(compatible)
        offsets.extend([index] * len(compatible))
    tokens = unicodedata.normalize("NFKC", str(value)).split()
    match = re.search(r"\s+".join(re.escape(token) for token in tokens), "".join(normalized), re.I)
    if match and match.end() > match.start():
        return _SourceSpan(offsets[match.start()], offsets[match.end()-1]+1)
    return None


def _rect(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            x0, y0, x1, y1 = map(float, value)
            if x1 > x0 and y1 > y0:
                return x0, y0, x1, y1
        except (ValueError, TypeError):
            pass
    return None


def _overlap(a, b) -> float:
    if not a or not b:
        return 0
    area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return area / min((a[2]-a[0]) * (a[3]-a[1]), (b[2]-b[0]) * (b[3]-b[1]))


def _is_grid(table: Record) -> bool:
    columns, rows = table.get("columns"), table.get("rows")
    status = str(table.get("review_status", ""))
    if table.get("parser") not in _GRID_PARSERS or _UNSAFE_STATUS.search(status):
        return False
    if table.get("parser") == "reviewed_transcription" and status != "source_page_checked":
        return False
    if any(table.get(key) for key in ("merged_cells", "cell_spans", "spans", "has_merged_cells")):
        return False
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
        return False
    return (isinstance(rows, list) and bool(rows) and all(
        isinstance(row, list) and len(row) == len(columns)
        and all(isinstance(cell, str) for cell in row) for row in rows))


def _table_key(table: Record):
    return (_flat(table.get("table_caption", "")),
            repr(table.get("columns", table.get("cells", []))), repr(table.get("rows", [])))


def _tables(records: Iterable[Record], doc_id: str) -> list[Record]:
    result: list[Record] = []
    for table in records:
        if table.get("kind") != "table" or (table.get("doc_id") and doc_id and table["doc_id"] != doc_id):
            continue
        if type(table.get("pdf_page")) is not int or table["pdf_page"] < 1:
            continue
        duplicate = None
        for index, previous in enumerate(result):
            if previous["pdf_page"] != table["pdf_page"]:
                continue
            same_id = table.get("evidence_id") and table.get("evidence_id") == previous.get("evidence_id")
            same_caption = (table.get("table_caption") and _flat(table["table_caption"]).casefold()
                            == _flat(previous.get("table_caption", "")).casefold())
            if same_id or same_caption or _overlap(_rect(table.get("bbox")), _rect(previous.get("bbox"))) > .9 or _table_key(table) == _table_key(previous):
                duplicate = index
                break
        if duplicate is None:
            result.append(table)
        elif (_is_grid(table), table.get("parser") == "reviewed_transcription") > (
                _is_grid(result[duplicate]), result[duplicate].get("parser") == "reviewed_transcription"):
            result[duplicate] = table
    return result


def _fence(text: str) -> str:
    runs = [len(match.group()) for match in re.finditer(r"`+", text)]
    delimiter = "`" * max(3, max(runs, default=0) + 1)
    return f"{delimiter}text\n{text}\n{delimiter}"


def _table_markdown(table: Record, url: str, *, unlocated: bool = False) -> str:
    caption = str(table.get("table_caption") or table.get("table_label") or "表格抽取")
    parts = [f"**{_escape(caption)}**"]
    if _is_grid(table):
        if table.get("parser") == "rapidocr_table":
            note = "图片表格 OCR 抽取，保留原始行标签；单元格文字与数值仍需核对原页。"
        elif table.get("parser") == "reviewed_transcription":
            note = "按原页核对的阅读版转录。"
        else:
            note = "原生表格抽取；单元格文字与数值仍需核对原页。"
        parts += [f"> {note}",
                  "| " + " | ".join(_escape(c, cell=True) for c in table["columns"]) + " |",
                  "| " + " | ".join("---" for _ in table["columns"]) + " |"]
        parts.extend("| " + " | ".join(_escape(c, cell=True) for c in row) + " |" for row in table["rows"])
        # Keep table rows adjacent, with paragraph breaks only around the grid.
        grid_start = 2
        parts = parts[:grid_start] + ["\n".join(parts[grid_start:])]
    else:
        parts.append("> 表格候选：行列、表头或合并单元格尚未可靠核验，以下保留原始抽取，不补齐或推断数值。")
        text = str(table.get("text") or "")
        if not text:
            cells = table.get("cells") or [table.get("columns", []), *table.get("rows", [])]
            text = "\n".join(" | ".join("[未识别]" if cell is None else str(cell) for cell in row)
                             if isinstance(row, (list, tuple)) else str(row) for row in cells)
        parts.append(_fence(text.strip() or "（未得到可展示的单元格文字）"))
    if unlocated:
        parts.append("> 已定位到本 PDF 页；正文内位置未能可靠匹配，按页附列。")
    if url:
        parts.append(f"[核对表格原页]({url})")
    return "\n\n".join(parts)


def _remove_table_suffix(text: str, tables: list[Record]) -> str:
    # New intake appends exact table.text blocks to page.text. Only a suffix is
    # removed here: a matching caption in the original body remains an anchor.
    changed = True
    while changed:
        changed = False
        for table in tables:
            value = str(table.get("text") or "").strip()
            if value and text.rstrip().endswith(value):
                prefix = text.rstrip()[:-len(value)].rstrip()
                caption = str(table.get("table_caption") or "")
                # A table at the original end of the page still needs its
                # caption anchor; an appended duplicate already has one above.
                if caption and value != caption and not _source_match(prefix, caption):
                    prefix += "\n\n" + caption
                text = prefix
                changed = True
    return text


def _strip_native_table_text(text: str, table: Record, elements: list[Record]) -> str:
    box = _rect(table.get("table_bbox") or table.get("bbox"))
    if not box or not _is_grid(table):
        return text
    caption = _flat(table.get("table_caption", ""))
    contained = []
    for element in elements:
        rect, value = _rect(element.get("bbox")), str(element.get("text") or "").strip()
        if (element.get("kind") == "text" and rect and value and _flat(value) != caption
                and rect[0] >= box[0]-3 and rect[1] >= box[1]-3
                and rect[2] <= box[2]+3 and rect[3] <= box[3]+3):
            contained.append(element)
    caption_match = _source_match(text, caption) if caption else None
    source_chars = "".join(re.sub(r"\s+", "", str(item["text"])) for item in contained)
    if caption_match and source_chars:
        # A contiguous native table may be sorted by rows in pages.json but by
        # columns in its original blocks. Exact character coverage around the
        # caption proves that the whole region can be replaced without relying
        # on the parser's proposed row/column order or guessing header text.
        positions = [index for index, char in enumerate(text) if not char.isspace()]
        size = len(source_chars)
        following = [index for index in positions if index >= caption_match.end()]
        preceding = [index for index in positions if index < caption_match.start()]
        ranges = []
        if len(following) >= size:
            ranges.append((following[0], following[size-1]+1))
        if len(preceding) >= size:
            ranges.append((preceding[-size], preceding[-1]+1))
        for start, end in ranges:
            compact = re.sub(r"\s+", "", text[start:end])
            if Counter(compact) == Counter(source_chars) and all(
                    re.sub(r"\s+", "", line) in compact for item in contained
                    for line in str(item["text"]).splitlines() if line.strip()):
                return text[:start] + "\n\n" + text[end:]
    for element in elements:
        rect, value = _rect(element.get("bbox")), str(element.get("text") or "").strip()
        if element.get("kind") != "text" or not rect or not value or _flat(value) == caption:
            continue
        # Require the entire text block to be inside the extracted table region.
        # Legacy line-based table boxes can end at the baseline while a glyph's
        # descender extends ~2pt farther. Keep the allowance small and explicit.
        if rect[0] < box[0] - 3 or rect[1] < box[1] - 3 or rect[2] > box[2] + 3 or rect[3] > box[3] + 3:
            continue
        match = _source_match(text, value)
        if match:
            text = text[:match.start()] + "\n" + text[match.end():]
        else:
            # sort=True can reorder header lines within a native text block.
            # Remove a native line only if it has one unambiguous occurrence
            # outside the caption. Short numeric labels remain untouched here.
            for line in value.splitlines():
                line = line.strip()
                if len(line) < 3 or not any(char.isalpha() for char in line):
                    continue
                pattern = r"\s+".join(re.escape(token) for token in line.split())
                if line[0].isalnum():
                    pattern = r"(?<!\w)" + pattern
                if line[-1].isalnum():
                    pattern += r"(?!\w)"
                caption_match = _source_match(text, caption) if caption else None
                matches = [item for item in re.finditer(pattern, text, re.I)
                           if not caption_match or item.end() <= caption_match.start() or item.start() >= caption_match.end()]
                if len(matches) == 1:
                    item = matches[0]
                    text = text[:item.start()] + "\n" + text[item.end():]
    # Some historical records lack individual text geometry. An exact contiguous
    # sequence of all known cells can still be removed without deleting prose.
    cells = [table["columns"], *table["rows"]]
    for matrix in (cells, list(map(list, zip(*cells)))):
        value = " ".join(_flat(cell) for row in matrix for cell in row if str(cell).strip())
        match = _source_match(text, value)
        if match and len(value) >= 12:
            text = text[:match.start()] + "\n" + text[match.end():]
    return text


def _position(text: str, table: Record, elements: list[Record]) -> tuple[int, int, bool]:
    caption = str(table.get("table_caption") or "")
    match = _source_match(text, caption) if caption else None
    if match:
        return match.start(), match.end(), False
    box = _rect(table.get("table_bbox") or table.get("bbox"))
    if box:
        candidates = []
        for element in elements:
            rect = _rect(element.get("bbox"))
            if element.get("kind") != "text" or not rect:
                continue
            overlap_x = max(0, min(rect[2], box[2]) - max(rect[0], box[0]))
            if overlap_x < min(rect[2]-rect[0], box[2]-box[0]) * .5:
                continue
            value = str(element.get("text") or "")
            match = _source_match(text, value)
            if not match:
                continue
            if rect[3] <= box[1] + 1:
                candidates.append((max(0, box[1]-rect[3]), match.end()))
            elif rect[1] >= box[3] - 1:
                candidates.append((max(0, rect[1]-box[3]), match.start()))
        if candidates:
            _, offset = min(candidates)
            return offset, offset, False
    return len(text), len(text), True


def _column_reading_order(text: str, page: Record, elements: list[Record]) -> tuple[str, bool]:
    """Use paragraph geometry only for fully covered, unambiguous two-column pages.

    No paragraphs are synthesized or dropped. Crossing/full-width blocks,
    incomplete native records, overlapping blocks, or a narrow/unclear gutter
    leave the original page text unchanged.
    """
    blocks = [item for item in elements if item.get("kind") == "text" and str(item.get("text") or "").strip()]
    try:
        width, height = float(page["width"]), float(page["height"])
    except (KeyError, TypeError, ValueError):
        return text, False
    if width <= 0 or height <= 0 or len(blocks) < 4:
        return text, False
    compact = re.sub(r"\s+", "", text)
    if Counter(compact) != Counter("".join(re.sub(r"\s+", "", str(item["text"])) for item in blocks)):
        return text, False
    # As well as full character coverage, every original extraction line must
    # occur verbatim (modulo whitespace) in the page's source text.
    if any(re.sub(r"\s+", "", line) not in compact
           for item in blocks for line in str(item["text"]).splitlines() if line.strip()):
        return text, False
    columns: list[list[tuple[tuple, Record]]] = [[], []]
    for item in blocks:
        rect = _rect(item.get("bbox"))
        if not rect or rect[0] < 0 or rect[1] < 0 or rect[2] > width + 1 or rect[3] > height + 1:
            return text, False
        # Do not apply a native-coordinate heuristic to OCR coordinate systems.
        if item.get("parser") not in {None, "pymupdf"}:
            return text, False
        if rect[2] <= width / 2:
            columns[0].append((rect, item))
        elif rect[0] >= width / 2:
            columns[1].append((rect, item))
        else:
            return text, False
    if any(len(column) < 2 or sum(len(_flat(item["text"])) for _, item in column) < 120 for column in columns):
        return text, False
    if min(rect[0] for rect, _ in columns[1]) - max(rect[2] for rect, _ in columns[0]) < width * .015:
        return text, False
    vertical_overlap = min(max(rect[3] for rect, _ in column) for column in columns) - max(
        min(rect[1] for rect, _ in column) for column in columns)
    if vertical_overlap < height * .2:
        return text, False
    for column in columns:
        column.sort(key=lambda value: (value[0][1], value[0][0]))
        if any(a[0][3] > b[0][1] + 2 for a, b in zip(column, column[1:])):
            return text, False
    return "\n\n".join(str(item["text"]).strip() for column in columns for _, item in column), True


def _heading(line: str) -> bool:
    if _NAMED_HEADING.fullmatch(line):
        return True
    match = _NUMBERED_HEADING.fullmatch(line)
    if not match or len(line) > 110:
        return False
    title = match[1]
    label = line[:match.start(1)].strip().rstrip(".")
    # A quantity, year, address, or author list is not a section number. In
    # particular, PDF Symbol-font PUA glyphs must not count as CJK letters just
    # because their codepoints are larger than U+3400.
    if re.fullmatch(r"\d+(?:\.\d+)*", label):
        if int(label.split(".")[0]) > 99 or _MEASUREMENT_START.match(title):
            return False
        if ("." not in line[:match.start(1)]
                and not _SECTION_TITLE_START.match(title)
                and unicodedata.category(title[0]) != "Lo"):
            return False
    elif re.fullmatch(r"[IVXLCDM]+", label):
        values = dict(I=1, V=5, X=10, L=50, C=100, D=500, M=1000)
        total = sum(-values[char] if index + 1 < len(label) and values[char] < values[label[index+1]]
                    else values[char] for index, char in enumerate(label))
        if total > 99:
            return False
    if (re.search(r"\bet\s+al\b|https?://|\bdoi\b", title, re.I)
            or re.match(r"^[A-Z][.]\s|^\S+\s+[A-Z](?:[.,]|\s[A-Z][.,])", title)
            or title.count(",") >= 2
            or re.search(r"[.!?。！？]\s+\w", title)):
        return False
    if len(title.split()) == 1 and not (unicodedata.category(title[0]) == "Lo"
            or _NAMED_HEADING.fullmatch(title)
            or title.casefold() in {"methods", "results", "discussion", "experiments", "background", "methodology",
                                    "scope", "procedure", "notes", "calculations", "fabrication", "characterization",
                                    "annealing", "analysis"}):
        return False
    return (len(title.split()) <= 14 and not re.search(r"[.;。；]$", title)
            and (title[0].isupper() or unicodedata.category(title[0]) == "Lo"))


def _continuation_lines(elements: list[Record], page: Record) -> set[str]:
    """Find source lines that only occur inside a native paragraph block.

    This is a veto on guessed headings/captions, not a new reading order. A line
    also seen at a block/paragraph start is ambiguous and receives no veto.
    """
    starts, continuations = set(), set()
    try:
        width, height = float(page["width"]), float(page["height"])
    except (KeyError, TypeError, ValueError):
        return set()
    if width <= 0 or height <= 0:
        return set()
    for element in elements:
        if element.get("kind") != "text" or element.get("parser") not in {None, "pymupdf"}:
            continue
        rect = _rect(element.get("bbox"))
        # Historical "text blocks" can be full-page transcription containers.
        # Their internal lines do not prove a common source paragraph.
        if (not rect or rect[0] < 0 or rect[1] < 0 or rect[2] > width + 1
                or rect[3] > height + 1 or rect[3] - rect[1] > height * .4
                or rect[2] - rect[0] > width * .85):
            continue
        beginning = True
        for raw in str(element.get("text") or "").splitlines():
            line = _flat(raw)
            if not line:
                beginning = True
            else:
                (starts if beginning else continuations).add(line)
                # A section and its first subsection can share one native
                # block. Their adjacency is title structure, not wrapped prose.
                beginning = _heading(line)
    return continuations - starts


@dataclass
class _ProseState:
    in_references: bool = False


def _prose(text: str, *, continuation_lines: set[str] | None = None,
           state: _ProseState | None = None) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    continuation_lines = continuation_lines or set()
    state = state if state is not None else _ProseState()
    def flush():
        if paragraph:
            blocks.append(_escape(" ".join(paragraph)))
            paragraph.clear()
    for raw in text.splitlines():
        line = raw.strip()
        match = _NUMBERED_HEADING.fullmatch(line)
        title = match[1] if match else line
        references = bool(_REFERENCES_HEADING.fullmatch(title))
        named = bool(_NAMED_HEADING.fullmatch(title))
        guessed_heading = _heading(line)
        if guessed_heading and not named:
            guessed_heading = not (state.in_references or _flat(line) in continuation_lines
                                  or (paragraph and _REFERENCE_CONTINUATION.search(paragraph[-1])))
        if not line:
            flush()
        elif guessed_heading:
            flush()
            blocks.append("### " + _escape(line))
            if named:
                state.in_references = references
        elif (re.match(r"^(?:Fig(?:ure)?[.]?|Table)\s*(?:\d+|[IVXLCDM]+)[.:：]"
                       r"|^(?:图|表)\s*\d+[.:：\s]", line, re.I)
              and _flat(line) not in continuation_lines):
            flush()
            paragraph.append(line)
        else:
            paragraph.append(line)
    flush()
    return "\n\n".join(blocks)


def render_fulltext_markdown(paper: Record, pages: Iterable[Record],
                             evidence: Iterable[Record] | None = None, *,
                             markdown_path: str | Path | None = None,
                             tables: Iterable[Record] | None = None,
                             ocr_elements: Iterable[Record] | None = None) -> str:
    """Render supplied page/evidence records without changing any input.

    ``ocr_elements`` explicitly replaces text on the pages it covers. It is never
    selected merely because a second OCR file happens to exist on disk. Legacy
    ``cells`` candidates remain visibly unverified rather than inferred grids.
    When supplied, ``tables`` is the authoritative table list; otherwise table
    elements are selected from ``evidence``.
    """
    doc_id = str(paper.get("doc_id") or paper.get("id") or "")
    records = [item for item in (evidence or [])
               if not doc_id or not item.get("doc_id") or item["doc_id"] == doc_id]
    table_records = _tables(tables if tables is not None else records, doc_id)
    ocr = [item for item in (ocr_elements or []) if item.get("kind") == "text"
           and (not doc_id or not item.get("doc_id") or item["doc_id"] == doc_id)]
    sections = ["# " + _escape(paper.get("title") or doc_id or "PDF 阅读版"),
                "> 按 PDF 物理页整理的阅读版。正文可能存在排版或识别误差；引用、图示、公式及表格请核对原 PDF。"]
    page_records = sorted(pages, key=lambda value: value["pdf_page"])
    seen_pages = set()
    prose_state = _ProseState()
    for page in page_records:
        number = page.get("pdf_page")
        if type(number) is not int or number < 1 or number in seen_pages:
            raise ValueError("pages must contain unique positive physical PDF page numbers")
        seen_pages.add(number)
        url = relative_pdf_url(paper, markdown_path, page=number)
        sections.append(f"## PDF 第 {number} 页")
        if url:
            sections.append(f"[查看原 PDF 第 {number} 页]({url})")
        page_elements = [item for item in records if item.get("pdf_page") == number]
        page_ocr = [item for item in ocr if item.get("pdf_page") == number]
        page_tables = [item for item in table_records if item["pdf_page"] == number]
        is_ocr = bool(page_ocr) or "ocr" in str(page.get("parser", "")).lower()
        if page_ocr:
            text = "\n\n".join(str(item.get("text") or "") for item in page_ocr)
            page_elements = page_ocr
        elif not is_ocr and isinstance(page.get("native_text"), str):
            text = page["native_text"]
        else:
            text = str(page.get("text") or "")
        if is_ocr:
            sections.append("> 本页正文使用 OCR 识别；保留识别结果，文字、符号及阅读顺序需结合原页复核。")
        text = _remove_table_suffix(text, page_tables)
        if not is_ocr:
            text, reordered = _column_reading_order(text, page, page_elements)
            if reordered:
                sections.append("> 本页按原页双栏顺序整理。")
        for table in page_tables:
            text = _strip_native_table_text(text, table, page_elements)
        # Insertions are applied from right to left against the unchanged source
        # offsets, then source prose and generated table syntax render separately.
        insertions = []
        for index, table in enumerate(page_tables):
            start, end, unlocated = _position(text, table, page_elements)
            token = f"\x00KG_TABLE_{index}\x00"
            insertions.append((start, end, token, _table_markdown(table, url, unlocated=unlocated)))
        for start, end, token, _ in sorted(insertions, key=lambda item: item[0], reverse=True):
            text = text[:start] + "\n\n" + token + "\n\n" + text[end:]
        markup = {token: rendered for _, _, token, rendered in insertions}
        parts = re.split(r"(\x00KG_TABLE_\d+\x00)", text)
        continuations = _continuation_lines(page_elements, page) if not is_ocr else set()
        rendered = "\n\n".join(markup[part] if part in markup else _prose(
                                      part, continuation_lines=continuations, state=prose_state)
                                  for part in parts if part.strip())
        sections.append(rendered or "（本页未提供可展示文字，请查看原 PDF。）")
    return "\n\n".join(sections).rstrip() + "\n"


render_reading_markdown = render_fulltext_markdown
