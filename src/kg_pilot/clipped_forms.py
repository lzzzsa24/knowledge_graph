"""Layout-only repair for embedded PDF Forms whose hidden contents leak into parsers."""
from collections import defaultdict
from pathlib import Path
import re

import pymupdf as fitz


# A searchable marker in the in-memory caption pass identifies a Form's own
# placement clip among unrelated rectangles used for page layout or shading.
_MARKER = b"q 0.03125 0.21875 0.59375 RG 0.25 w 1 1 m 2 2 l S Q"
_MARKER_RGB = (0.03125, 0.21875, 0.59375)


def _flatten_overflow(doc, xref, box):
    # get_xobjects reports the transformed BBox. Restrict this rewrite to
    # identity matrices so that we can place the image in native Form space.
    matrix_type, matrix = doc.xref_get_key(xref, "Matrix")
    if matrix_type != "null" and (matrix_type != "array" or
            tuple(map(float, matrix.strip("[]").split())) != (1, 0, 0, 1, 0, 0)):
        return False
    if doc.xref_get_key(xref, "Resources")[0] not in ("dict", "xref") or box.is_empty or box.is_infinite:
        return False
    if min(box.width, box.height) < 1:
        return False
    page = doc.new_page(width=box.width, height=box.height)
    try:
        doc.xref_set_key(page.xref, "Resources", f"<< /XObject << /Placed {xref} 0 R >> >>")
        stream = doc.get_new_xref()
        doc.update_object(stream, "<< >>")
        doc.update_stream(stream, f"q 1 0 0 1 {-box.x0} {-box.y0} cm /Placed Do Q".encode())
        page.set_contents(stream)
        bounds = page.rect + (-2, -2, 2, 2)
        if not any(op in ("fill-text", "fill-image") and (fitz.Rect(area) & bounds).is_empty
                   for op, area in page.get_bboxlog()):
            return False
        zoom = min(2.5, 2200 / max(box.width, box.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
        image_ref = page.insert_image(page.rect, stream=pix.tobytes("png"))
        doc.xref_set_key(xref, "Resources", f"<< /XObject << /Visible {image_ref} 0 R >> >>")
        doc.update_stream(xref, (
            f"q {box.width} 0 0 {box.height} {box.x0} {box.y0} cm /Visible Do Q"
        ).encode())
        return True
    finally:
        doc.delete_page(-1)


def _captioned_boxes(page):
    """Match a printed caption to one complete, rectangular figure immediately above it."""
    if page.rotation:
        return []
    boxes = []
    clip = None
    for path in page.get_drawings(extended=True):
        if path["level"] == 0:
            clip = None
            if path["type"] == "clip" and len(path["items"]) == 1 and path["items"][0][0] == "re":
                clip = path["scissor"] & page.rect
        elif (path["level"] == 1 and path.get("color") == _MARKER_RGB and clip is not None
              and not clip.is_empty and clip not in boxes):
            boxes.append(clip)
    matches = []
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    for block in page.get_text("dict", flags=flags)["blocks"]:
        lines = block.get("lines", [])
        if not lines:
            continue
        parts = ["".join(s["text"] for s in line["spans"]).strip() for line in lines]
        if not re.match(r"^(?:Figure|Fig\.?)\s*\d+[.:](?:\s|$)", parts[0], re.I):
            continue
        first = fitz.Rect(lines[0]["bbox"])
        near = [b for b in boxes if -1 <= first.y0 - b.y1 <= 18 and
                min(first.x1, b.x1) > max(first.x0, b.x0)]
        # A panel with its own clip must not replace the full multi-panel figure.
        if len(near) == 1 and not any(b != near[0] and abs(b.y1 - near[0].y1) <= 6
                                       and abs(b.y0 - near[0].y0) <= 10 for b in boxes):
            matches.append((near[0], " ".join(parts)))
    counts = defaultdict(int)
    for box, _ in matches:
        counts[tuple(box)] += 1
    return [(box, caption) for box, caption in matches if counts[tuple(box)] == 1]


def visible_layout_copy(pdf_path, directory):
    """Return the parser input, pages needing new layout, and printed figure anchors.

    Only the analysis copy is modified. Page geometry, visible body text and
    table cells stay in place. The caller renders every final crop from the source.
    """
    with fitz.open(pdf_path) as doc:
        roots = {}
        pages_using = defaultdict(set)
        for page in doc:
            for xref, _, parent, bbox in page.get_xobjects():
                pages_using[xref].add(page.number + 1)
                if not parent:
                    roots[xref] = bbox
        affected = set()
        for xref, bbox in roots.items():
            if _flatten_overflow(doc, xref, fitz.Rect(bbox)):
                affected.update(pages_using[xref])
        output = None
        if affected:
            output = Path(directory) / "visible-layout.pdf"
            doc.save(output, garbage=3, deflate=True)
    if not roots:
        return output, affected, {}
    # Use the source revision's object numbers. Saving a compact parser copy
    # above can renumber objects. Captions inside embedded artwork are excluded.
    with fitz.open(pdf_path) as doc:
        for xref in roots:
            doc.update_stream(xref, _MARKER)
        anchors = {}
        for n in sorted(set().union(*(pages_using[x] for x in roots))):
            figures = _captioned_boxes(doc[n - 1])
            if figures:
                anchors[n] = figures
        return output, affected, anchors
