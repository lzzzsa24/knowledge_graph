"""Source-bound native and raster table extraction, without repairing cell text.

Raster tables are accepted only when a ruled grid can establish every cell.
The OCR orientation classifier is disabled: PDF page coordinates already establish
the upright direction, and rotating short numeric cells can change their meaning.
"""
from __future__ import annotations

import math
import re
import threading

import pymupdf

from kg_pilot.pipeline import normalize


TABLE_PARSERS = frozenset({"pymupdf_table", "rapidocr_table"})
_CAPTION = re.compile(r"^\s*(TABLE\s+(?:[IVXLCDM]+|\d+)\s*[.:])(?:\s|$)", re.I)
_ENGINE = None
_OCR_LOCK = threading.Lock()
MAX_ROWS = 60
MAX_COLUMNS = 16


def _captions(page):
    captions = []
    for block in page.get_text("blocks"):
        text = " ".join(block[4].split())
        match = _CAPTION.match(text)
        if match:
            captions.append((pymupdf.Rect(block[:4]), text, match.group(1)))
    return captions


def _caption_for(rect, captions):
    nearby = []
    for bbox, text, label in captions:
        overlap = max(0, min(rect.x1, bbox.x1) - max(rect.x0, bbox.x0))
        gap = min(abs(rect.y0 - bbox.y1), abs(bbox.y0 - rect.y1))
        if overlap >= min(rect.width, bbox.width) * .4 and gap <= 60:
            nearby.append((gap, text, label, list(bbox)))
    if not nearby:
        return "", "", None
    _, text, label, bbox = min(nearby, key=lambda value: value[0])
    return text, label, bbox


def table_text(caption, columns, rows):
    """Exact cell strings with explicit row and column separators."""
    return "\n".join(([caption] if caption else []) + [" | ".join(row) for row in [columns, *rows]])


def _record(page, doc_id, sha, ordinal, rect, columns, rows, cell_bboxes, parser, captions, **extra):
    caption, label, caption_bbox = _caption_for(rect, captions)
    evidence_id = f"{doc_id}:p{page.number + 1}:table{ordinal}"
    # The overall evidence region includes the native caption when present.
    bbox = rect | pymupdf.Rect(caption_bbox) if caption_bbox else rect
    item = dict(evidence_id=evidence_id, table_id=evidence_id, doc_id=doc_id,
                pdf_page=page.number + 1, kind="table", parser=parser,
                document_sha256=sha, bbox=list(bbox), table_bbox=list(rect),
                table_caption=caption, table_label=label, caption_bbox=caption_bbox,
                columns=columns, rows=rows, cell_bboxes=cell_bboxes,
                row_bboxes=[list(pymupdf.Rect(min(b[0] for b in cells), min(b[1] for b in cells),
                                             max(b[2] for b in cells), max(b[3] for b in cells)))
                            for cells in cell_bboxes],
                review_status="ocr_source_requires_claim_level_review" if parser == "rapidocr_table"
                              else "source_requires_claim_level_review", **extra)
    item["text"] = table_text(caption, columns, rows)
    item["normalized_text"] = normalize(item["text"])
    validate_table_element(item, dict(pdf_page=page.number + 1, width=page.rect.width, height=page.rect.height,
                                      native_text=page.get_text()))
    return item


def _ocr_tokens(image):
    global _ENGINE
    import rapidocr
    from pathlib import Path
    from rapidocr import RapidOCR
    with _OCR_LOCK:
        if _ENGINE is None:
            directory = Path(rapidocr.__file__).parent / "models"
            models = {"Det.model_path": directory / "PP-OCRv6_det_small.onnx",
                      "Rec.model_path": directory / "PP-OCRv6_rec_small.onnx",
                      "Cls.model_path": directory / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"}
            if not all(path.is_file() for path in models.values()):
                raise RuntimeError("Local table OCR models are unavailable")
            _ENGINE = RapidOCR(params={**{key: str(value) for key, value in models.items()},
                                      "Global.log_level": "error"})
        output = _ENGINE(image, use_cls=False)
    return [dict(text=text, bbox=[float(box[:, 0].min()), float(box[:, 1].min()),
                                  float(box[:, 0].max()), float(box[:, 1].max())], confidence=float(score))
            for text, box, score in zip(output.txts or [], output.boxes if output.boxes is not None else [],
                                        output.scores or [])]


def _line_centers(indices, extent):
    groups = []
    for index in indices:
        if not groups or index > groups[-1][-1] + 2:
            groups.append([int(index)])
        else:
            groups[-1].append(int(index))
    # Broad white bands are page/cell backgrounds, not ruling lines.
    return [sum(group) / len(group) for group in groups if len(group) <= max(7, extent * .04)]


def _raster_grid(image):
    import cv2
    import numpy as np
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    candidates = []
    for lower, upper in ((0, 90), (240, 255)):
        binary = cv2.inRange(gray, lower, upper)
        horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, int(width * .75)), 1)))
        vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, int(height * .75)))))
        ys = _line_centers(np.flatnonzero((horizontal > 0).sum(axis=1) >= width * .75), height)
        xs = _line_centers(np.flatnonzero((vertical > 0).sum(axis=0) >= height * .75), width)
        if (3 <= len(xs) <= MAX_COLUMNS + 1 and 3 <= len(ys) <= MAX_ROWS + 2
                and xs[-1] - xs[0] >= width * .7 and ys[-1] - ys[0] >= height * .7
                and min(b - a for a, b in zip(xs, xs[1:])) >= 12
                and min(b - a for a, b in zip(ys, ys[1:])) >= 12):
            candidates.append((xs, ys))
    return max(candidates, key=lambda grid: len(grid[0]) * len(grid[1]), default=None)


def _raster_table(page, rect, image_ocr):
    import numpy as np
    scale = min(4., 2400 / max(rect.width, rect.height))
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
    grid = _raster_grid(image)
    if grid is None:
        return None
    xs, ys = grid
    cells = [[[] for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
    confidences = []
    for token in image_ocr(image):
        text, box = token["text"].strip(), token["bbox"]
        if not text:
            continue
        x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        column = next((i for i in range(len(xs) - 1) if xs[i] <= x < xs[i + 1]), None)
        row = next((i for i in range(len(ys) - 1) if ys[i] <= y < ys[i + 1]), None)
        if row is None or column is None:
            continue
        # A text box straddling a rule cannot be assigned safely to one cell.
        if (box[0] < xs[column] - 3 or box[2] > xs[column + 1] + 3
                or box[1] < ys[row] - 3 or box[3] > ys[row + 1] + 3):
            return None
        confidence = float(token.get("confidence", 1))
        if confidence < .85:
            return None
        confidences.append(confidence)
        cells[row][column].append((box[1], box[0], text))
    rows = [[" ".join(token[2] for token in sorted(cell)) for cell in row] for row in cells]
    if any(not value for row in rows for value in row):
        return None
    # Pixel origins can be rounded by the renderer; scale back using its actual
    # canvas dimensions, retaining source-page coordinates for every cell.
    boxes = [[[rect.x0 + xs[c] * rect.width / pixmap.width,
               rect.y0 + ys[r] * rect.height / pixmap.height,
               rect.x0 + xs[c + 1] * rect.width / pixmap.width,
               rect.y0 + ys[r + 1] * rect.height / pixmap.height]
              for c in range(len(xs) - 1)] for r in range(len(ys) - 1)]
    return rows[0], rows[1:], boxes, dict(ocr_confidence_min=min(confidences),
        ocr_orientation_classifier=False, extraction_method="ruled_grid_cell_assignment")


def extract_table_evidence(page, *, doc_id, document_sha256, image_ocr=None):
    """Extract tables from one original PDF page; ordinals are deterministic.

    No document title, identity, layer labels, measurements, or result values are
    special-cased. Unruled/ambiguous raster tables remain available in the PDF.
    """
    captions = _captions(page)
    candidates = []
    # Use visible ruling rather than the layout model's inferred grid: the
    # latter can introduce narrow, empty columns even in a two-column fixture.
    for table in page.find_tables(use_layout=False).tables:
        if table.header.external:
            continue  # do not relabel the first data row as an inferred header
        raw_rows = table.extract()
        # Known native-font failures displace decimal dots, exponents and scale
        # units to separate lines (e.g. '012\n.'). Never join such fragments into
        # an apparently valid numeric cell or guess the intended multiplier.
        if any(re.search(r"\d", value or "") and "\n" in (value or "")
               or any(re.fullmatch(r"[\s.,+−±^()·-]+", part) for part in (value or "").splitlines())
               for row in raw_rows for value in row):
            continue
        rows = [[" ".join((value or "").split()) for value in row] for row in raw_rows]
        if not rows or len(rows) < 2 or not 2 <= len(rows[0]) <= MAX_COLUMNS or len(rows) > MAX_ROWS + 1:
            continue
        boxes = [list(row.cells) for row in table.rows]
        if any(cell is None for row in boxes for cell in row):
            continue  # merged cells require an explicit richer schema
        if any(not value for row in rows for value in row):
            continue
        candidates.append((pymupdf.Rect(table.bbox), rows[0], rows[1:], boxes, "pymupdf_table", {}))
    seen_images = set()
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"]) & page.rect
        identity = tuple(round(value, 2) for value in rect)
        if identity in seen_images or rect.width < 60 or rect.height < 35:
            continue
        seen_images.add(identity)
        if any((rect & candidate[0]).get_area() > rect.get_area() * .7 for candidate in candidates):
            continue
        # Without a native caption, only a complete ruled grid can be accepted.
        raster = _raster_table(page, rect, image_ocr or _ocr_tokens)
        if raster is not None:
            columns, rows, boxes, extra = raster
            candidates.append((rect, columns, rows, boxes, "rapidocr_table", extra))
    result = []
    for rect, columns, rows, boxes, parser, extra in sorted(candidates, key=lambda c: (c[0].y0, c[0].x0)):
        try:
            result.append(_record(page, doc_id, document_sha256, len(result), rect,
                                  columns, rows, boxes, parser, captions, **extra))
        except ValueError:
            continue
    return result


def validate_table_element(item, page):
    """Validate the complete source-bound table schema; raise ValueError on drift."""
    if item.get("kind") != "table" or item.get("parser") not in TABLE_PARSERS:
        raise ValueError("Invalid table kind or parser")
    page_no = page.get("pdf_page")
    if type(page_no) is not int or item.get("pdf_page") != page_no:
        raise ValueError("Table page does not match source page")
    if not isinstance(item.get("doc_id"), str) or not item["doc_id"].strip():
        raise ValueError("Table document identity is required")
    expected = rf"{re.escape(item['doc_id'])}:p{page_no}:table\d+"
    if not re.fullmatch(expected, str(item.get("evidence_id", ""))) or item.get("table_id") != item["evidence_id"]:
        raise ValueError("Invalid table evidence identity")
    if not re.fullmatch(r"[a-f0-9]{64}", str(item.get("document_sha256", ""))):
        raise ValueError("Table is not bound to a document hash")
    columns, rows = item.get("columns"), item.get("rows")
    if (not isinstance(columns, list) or not 2 <= len(columns) <= MAX_COLUMNS
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS):
        raise ValueError("Invalid table dimensions")
    matrix = [columns, *rows]
    if any(not isinstance(row, list) or len(row) != len(columns)
           or any(not isinstance(cell, str) or not cell.strip() or len(cell) > 2000 for cell in row) for row in matrix):
        raise ValueError("Incomplete table cells")
    caption = item.get("table_caption", "")
    if not isinstance(caption, str) or item.get("text") != table_text(caption, columns, rows):
        raise ValueError("Table text does not match its cells")
    if item.get("normalized_text") != normalize(item["text"]):
        raise ValueError("Table normalized text does not match")
    width, height = page.get("width"), page.get("height")
    def check_box(box, parent=None):
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or any(type(value) not in {int, float} or not math.isfinite(value) for value in box)
                or not 0 <= box[0] < box[2] <= width + .1 or not 0 <= box[1] < box[3] <= height + .1):
            raise ValueError("Table bounding box is outside source page")
        if parent and not (parent[0] - .1 <= box[0] and parent[1] - .1 <= box[1]
                           and box[2] <= parent[2] + .1 and box[3] <= parent[3] + .1):
            raise ValueError("Cell bounding box is outside table")
    if type(width) not in {int, float} or type(height) not in {int, float}:
        raise ValueError("Source page dimensions are required")
    check_box(item.get("bbox"))
    check_box(item.get("table_bbox"), item["bbox"])
    boxes, row_boxes = item.get("cell_bboxes"), item.get("row_bboxes")
    if not isinstance(boxes, list) or len(boxes) != len(matrix) or not isinstance(row_boxes, list) or len(row_boxes) != len(matrix):
        raise ValueError("Missing table cell geometry")
    for cells, row_box in zip(boxes, row_boxes):
        if not isinstance(cells, list) or len(cells) != len(columns):
            raise ValueError("Cell geometry does not match table columns")
        check_box(row_box, item["table_bbox"])
        for box in cells:
            check_box(box, row_box)
        if any(left[2] > right[0] + .1 for left, right in zip(cells, cells[1:])):
            raise ValueError("Table columns overlap or are out of order")
    if any(upper[3] > lower[1] + .1 for upper, lower in zip(row_boxes, row_boxes[1:])):
        raise ValueError("Table rows overlap or are out of order")
    if caption:
        match = _CAPTION.match(caption)
        native = " ".join(page.get("native_text", page.get("text", "")).split())
        if not match or item.get("table_label") != match.group(1) or caption not in native:
            raise ValueError("Table caption is not present in source text")
        check_box(item.get("caption_bbox"), item["bbox"])
    elif item.get("table_label") or item.get("caption_bbox") is not None:
        raise ValueError("Table caption metadata is inconsistent")
