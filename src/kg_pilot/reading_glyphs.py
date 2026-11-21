"""Decode mis-extracted glyphs in copies of text records, using the source PDF.

The same private-use code point means different things in Symbol, Wingdings,
and arbitrary subset fonts.  Nothing here is a general Unicode substitution:
each replacement is tied to a glyph ID in an embedded source font and aligned
to that glyph's source line.  Parsed evidence records are never modified.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import struct
import unicodedata

import pymupdf


def _private(char: str) -> bool:
    return unicodedata.category(char) == "Co"


def is_suspect_glyph(char: str) -> bool:
    category = unicodedata.category(char)
    return char == "\ufffd" or category in {"Co", "Cs"} or category == "Cc" and char not in "\t\r\n"


# Some subsets draw symbols that even their embedded glyph name misidentifies.
# Keys bind each visual transcription to PDF bytes, embedded font bytes, the
# extracted character and the glyph ID actually drawn at this position.
_REVIEWED_GLYPHS = {
    # Adeia 2023. SymbolMT draws µ and Ω at the misencoded ASCII slots;
    # actual millimetres are drawn with Calibri at different glyph IDs.
    "0086822b49e2d5df865817665501c8403bef4127469a8ce5d326c437342669df": {
        "3cf0834b23797c20f865a5521857ecbbf655d6482a5ee0de5944a6d62dcd95b3": {
            ("m", 80): "µ", ("W", 58): "Ω",
        },
    },
    # Small Science, PDF pp. 8 and 10. The tolerance is (−0/+10 °C).
    "c16978c3bf08750131ff13278238208a185e92471c73c38f7eceb9cab324440e": {
        "ee388b0e09fdaeba248b290e22b6572fc0372f0a3e44128621cd13c79a393414": {
            ("\x01", 2): "−", ("\x05", 5): "×", ("þ", 1): "+",
        },
    },
    # Balan slide deck, PDF pp. 9, 10, 14, 15 and 17. Composite Wingdings arrow.
    "a359fb15340c55cffc746132e6042a9af41934ad98548a6eddb3012051a1dba8": {
        "9920294b3c064d743d40851bc4b39c357034e5842eef56707b1fbd5b712158ae": {
            ("\uf0e0", 198): "→",
        },
    },
    # Figure 12, PDF p. 6. Two embedded fonts use the same broken code for
    # different ligatures in the labels "Bottom Die" and "Active".
    "29d3bca5289c0629cd22d213c7bed6c030fd2988a7ee3232a30a628c66ced6bf": {
        "e680cc28493a97e3e61e78531b7944f2cce945cbf86d2befd23fc7bada85b229": {
            ("\x02", 30): "tt",
        },
        "d8a9392c35c3eab2670b1bd7db90b4b74e16c02017e37682ca0553a4232736ea": {
            ("\x02", 61): "ti",
        },
    },
}
_REVIEW_INPUT_CHARS = {char for fonts in _REVIEWED_GLYPHS.values()
                       for mapping in fonts.values() for char, _ in mapping if not char.isprintable() or not char.isascii()}


def is_glyph_candidate(char: str, extra_candidates=()) -> bool:
    return is_suspect_glyph(char) or char in _REVIEW_INPUT_CHARS or char in extra_candidates


def _suspect(value, extra_candidates=()) -> bool:
    if isinstance(value, str):
        return any(is_glyph_candidate(c, extra_candidates) for c in value)
    if isinstance(value, (list, tuple)):
        return any(_suspect(v, extra_candidates) for v in value)
    return False


def _reviewed_input_characters(reviewed_fonts):
    return {char for mapping in (reviewed_fonts or {}).values() for char, _ in mapping}


def reviewed_input_characters(document_sha256):
    return _reviewed_input_characters(_REVIEWED_GLYPHS.get(document_sha256))


# Adobe Symbol / Microsoft Symbol character slots.  These are consulted only
# after the actual embedded font's Microsoft-symbol cmap maps the slot to the
# glyph ID drawn by this PDF.  In particular F06C is lambda here, but a bullet
# in Wingdings.  Technical bracket pieces preserve the source glyph, rather
# than inventing a complete equation around a fragment.
_SYMBOL = {
    0x28: "(", 0x29: ")", 0x2b: "+", 0x2d: "−", 0x3d: "=", 0x40: "≅",
    0x41: "Α", 0x42: "Β", 0x43: "Χ", 0x44: "Δ", 0x45: "Ε", 0x46: "Φ",
    0x47: "Γ", 0x48: "Η", 0x49: "Ι", 0x4a: "ϑ", 0x4b: "Κ", 0x4c: "Λ",
    0x4d: "Μ", 0x4e: "Ν", 0x4f: "Ο", 0x50: "Π", 0x51: "Θ", 0x52: "Ρ",
    0x53: "Σ", 0x54: "Τ", 0x55: "Υ", 0x56: "ς", 0x57: "Ω", 0x58: "Ξ",
    0x59: "Ψ", 0x5a: "Ζ", 0x61: "α", 0x62: "β", 0x63: "χ", 0x64: "δ",
    0x65: "ε", 0x66: "φ", 0x67: "γ", 0x68: "η", 0x69: "ι", 0x6a: "ϕ",
    0x6b: "κ", 0x6c: "λ", 0x6d: "μ", 0x6e: "ν", 0x6f: "ο", 0x70: "π",
    0x71: "θ", 0x72: "ρ", 0x73: "σ", 0x74: "τ", 0x75: "υ", 0x76: "ϖ",
    0x77: "ω", 0x78: "ξ", 0x79: "ψ", 0x7a: "ζ", 0xac: "←", 0xad: "↑",
    0xae: "→", 0xaf: "↓", 0xb0: "°", 0xb1: "±", 0xb3: "≥", 0xb4: "×",
    0xb6: "∂", 0xb7: "•", 0xb8: "÷", 0xb9: "≠", 0xba: "≡", 0xbb: "≈",
    0xbc: "…", 0xc5: "⊕", 0xc6: "∅", 0xc7: "∩", 0xc8: "∪", 0xce: "∈",
    0xcf: "∉", 0xd1: "∇", 0xd5: "∏", 0xd6: "√", 0xd7: "⋅", 0xd8: "¬",
    0xd9: "∧", 0xda: "∨", 0xdb: "⇔", 0xdc: "⇐", 0xdd: "⇑", 0xde: "⇒",
    0xdf: "⇓", 0xe0: "◊", 0xe2: "®", 0xe3: "©", 0xe4: "™", 0xe5: "∑",
    0xe6: "⎛", 0xe7: "⎜", 0xe8: "⎝", 0xe9: "⎡", 0xea: "⎢", 0xeb: "⎣",
    0xec: "⎧", 0xed: "⎨", 0xee: "⎩", 0xef: "⎪", 0xf1: "〉", 0xf2: "∫",
    0xf3: "⌠", 0xf4: "⎮", 0xf5: "⌡", 0xf6: "⎞", 0xf7: "⎟", 0xf8: "⎠",
    0xf9: "⎤", 0xfa: "⎥", 0xfb: "⎦", 0xfc: "⎫", 0xfd: "⎬", 0xfe: "⎭",
}
# Actual corpus source glyphs were rendered and checked.  These remain scoped
# to their respective font family and to the embedded symbol cmap / glyph ID.
_DINGBATS = {"wingdings": {0x4a: "☺", 0x6c: "●", 0xa7: "▪", 0xe0: "→", 0xfc: "✓"},
             "wingdingsregular": {0x4a: "☺", 0x6c: "●", 0xa7: "▪", 0xe0: "→", 0xfc: "✓"},
             "wingdings2": {0x97: "●"}}
_NAMED_PARTS = {
    "bracelefttp": "⎧", "braceleftmid": "⎨", "braceleftbt": "⎩", "braceex": "⎪",
    "bracerighttp": "⎫", "bracerightmid": "⎬", "bracerightbt": "⎭",
    "parenlefttp": "⎛", "parenleftex": "⎜", "parenleftbt": "⎝",
    "parenrighttp": "⎞", "parenrightex": "⎟", "parenrightbt": "⎠",
    "bracketlefttp": "⎡", "bracketleftex": "⎢", "bracketleftbt": "⎣",
    "bracketrighttp": "⎤", "bracketrightex": "⎥", "bracketrightbt": "⎦",
}


def _font_key(name: str) -> str:
    return re.sub(r"[\s_-]", "", re.sub(r"^[A-Z]{6}\+", "", name)).lower()


def _sfnt_tables(data):
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        return {}
    tables = {}
    count = struct.unpack_from(">H", data, 4)[0]
    if count > 256 or 12 + count * 16 > len(data):
        return {}
    for i in range(count):
        tag, _, start, size = struct.unpack_from(">4sIII", data, 12 + i * 16)
        if start + size <= len(data):
            tables[tag] = (start, size)
    return tables


def _sfnt_cmaps(data: bytes):
    """Read just cmap formats 4/12 from an embedded SFNT, without a new dependency.

    Returns Unicode cmaps separately from Microsoft symbol cmaps.  Unknown or
    malformed font formats supply no authority to repair a character.
    """
    unicode_maps, symbol_maps = [], []
    try:
        entry = _sfnt_tables(data).get(b"cmap")
        if entry is None:
            return unicode_maps, symbol_maps
        offset, size = entry
        table_end = offset + size
        if size < 4:
            return [], []
        count = struct.unpack_from(">H", data, offset + 2)[0]
        if count > 256 or 4 + count * 8 > size:
            return [], []
        for i in range(count):
            platform, encoding, relative = struct.unpack_from(">HHI", data, offset + 4 + i * 8)
            symbol = platform == 3 and encoding == 0
            if not (symbol or platform == 0 or platform == 3 and encoding in (1, 10)):
                continue
            start = offset + relative
            if start < offset + 4 + count * 8 or start + 4 > table_end:
                continue
            form = struct.unpack_from(">H", data, start)[0]
            cmap = {}
            if form == 4:
                length, _, twice_segments = struct.unpack_from(">HHH", data, start + 2)
                end = start + length
                if length < 16 or end > table_end:
                    continue
                segments = twice_segments // 2
                if twice_segments % 2 or segments > 8192 or 16 + segments * 8 > length:
                    continue
                ends = start + 14
                starts = ends + 2 * segments + 2
                deltas = starts + 2 * segments
                ranges = deltas + 2 * segments
                previous = -1
                for j in range(segments):
                    lo = struct.unpack_from(">H", data, starts + 2 * j)[0]
                    hi = struct.unpack_from(">H", data, ends + 2 * j)[0]
                    delta = struct.unpack_from(">h", data, deltas + 2 * j)[0]
                    relative_glyph = struct.unpack_from(">H", data, ranges + 2 * j)[0]
                    if lo > hi or lo <= previous:
                        cmap = {}
                        break
                    previous = hi
                    for code in range(lo, min(hi, 0xfffe) + 1):
                        if relative_glyph:
                            address = ranges + 2 * j + relative_glyph + (code - lo) * 2
                            if address + 2 > end:
                                continue
                            glyph = struct.unpack_from(">H", data, address)[0]
                            if glyph:
                                glyph = (glyph + delta) & 0xffff
                        else:
                            glyph = (code + delta) & 0xffff
                        if glyph:
                            cmap[code] = glyph
            elif form == 12:
                if start + 16 > table_end:
                    continue
                length = struct.unpack_from(">I", data, start + 4)[0]
                groups = struct.unpack_from(">I", data, start + 12)[0]
                if groups > 8192 or length < 16 + groups * 12 or start + length > table_end:
                    continue
                previous, mapped = -1, 0
                for j in range(groups):
                    lo, hi, glyph = struct.unpack_from(">III", data, start + 16 + j * 12)
                    mapped += hi - lo + 1
                    if lo > hi or lo <= previous or hi > 0x10ffff or mapped > 131072:
                        cmap = {}
                        break
                    previous = hi
                    cmap.update((code, glyph + code - lo) for code in range(lo, hi + 1))
            if cmap:
                (symbol_maps if symbol else unicode_maps).append(cmap)
    except (struct.error, IndexError, ValueError):
        return [], []
    return unicode_maps, symbol_maps


def _glyph_fingerprint(data, gid):
    """Exact simple-glyph outline bytes plus scale; never compare composites.

    Some PDF subsetters drop cmap and post names but leave the source outline.
    Composite glyph bytes contain other glyph IDs, so byte identity alone is
    insufficient for those and they deliberately do not qualify.
    """
    try:
        tables = _sfnt_tables(data)
        head, head_size = tables[b"head"]
        loca, loca_size = tables[b"loca"]
        glyf, glyf_size = tables[b"glyf"]
        maxp, maxp_size = tables[b"maxp"]
        if head_size < 54 or maxp_size < 6 or not 0 <= gid < struct.unpack_from(">H", data, maxp + 4)[0]:
            return None
        width = 4 if struct.unpack_from(">h", data, head + 50)[0] else 2
        if (gid + 2) * width > loca_size:
            return None
        form, multiplier = (">I", 1) if width == 4 else (">H", 2)
        lo = struct.unpack_from(form, data, loca + gid * width)[0] * multiplier
        hi = struct.unpack_from(form, data, loca + (gid + 1) * width)[0] * multiplier
        if lo >= hi or hi > glyf_size or hi - lo < 10:
            return None
        glyph = data[glyf + lo:glyf + hi]
        if struct.unpack_from(">h", glyph)[0] <= 0:
            return None
        return struct.unpack_from(">H", data, head + 18)[0], glyph.rstrip(b"\x00")
    except (struct.error, IndexError, KeyError):
        return None


@lru_cache(maxsize=4)
def _reference_shapes(family):
    mapping = _SYMBOL if family in {"symbol", "symbolmt"} else _DINGBATS.get(family, {})
    filenames = {"symbol": "symbol.ttf", "symbolmt": "symbol.ttf", "wingdings": "wingding.ttf",
                 "wingdingsregular": "wingding.ttf", "wingdings2": "wingdng2.ttf"}
    filename = filenames.get(family)
    if not filename:
        return {}
    try:
        data = (Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts" / filename).read_bytes()
    except OSError:
        return {}
    _, symbol_maps = _sfnt_cmaps(data)
    result = defaultdict(set)
    for cmap in symbol_maps:
        for code, gid in cmap.items():
            if 0xf000 <= code <= 0xf0ff and code - 0xf000 in mapping:
                shape = _glyph_fingerprint(data, gid)
                if shape is not None:
                    result[shape].add(mapping[code - 0xf000])
    return {shape: next(iter(values)) for shape, values in result.items() if len(values) == 1}


class _Font:
    def __init__(self, document, entry):
        self.family = _font_key(entry[3])
        self.font = None
        self.data = b""
        self.unicode_by_gid = defaultdict(set)
        self.symbol_slots = defaultdict(set)
        try:
            data = document.extract_font(entry[0])[3]
            self.data = data
            if not data:
                return
            self.font = pymupdf.Font(fontbuffer=data)
            unicode_maps, symbol_maps = _sfnt_cmaps(data)
            for cmap in unicode_maps:
                for code, gid in cmap.items():
                    if code and code != 0xfffd and not unicodedata.category(chr(code)).startswith("C"):
                        self.unicode_by_gid[gid].add(chr(code))
            for cmap in symbol_maps:
                for code, gid in cmap.items():
                    if 0xf000 <= code <= 0xf0ff:
                        self.symbol_slots[gid].add(code - 0xf000)
        except (RuntimeError, ValueError):
            pass

    def decode(self, code: int, gid: int) -> str | None:
        choices = self.unicode_by_gid.get(gid, set())
        if choices:
            return next(iter(choices)) if len(choices) == 1 else None
        slots = self.symbol_slots.get(gid, set())
        mapping = _SYMBOL if self.family in {"symbol", "symbolmt"} else _DINGBATS.get(self.family, {})
        decoded = {mapping[slot] for slot in slots if slot in mapping}
        if len(decoded) == 1 and len(slots) == 1:
            return next(iter(decoded))
        shape = _glyph_fingerprint(self.data, gid)
        if shape is not None:
            value = _reference_shapes(self.family).get(shape)
            if value:
                return value
        if self.font:
            try:
                name = pymupdf.mupdf.fz_get_glyph_name2(self.font.this, gid)
                if name in _NAMED_PARTS:
                    return _NAMED_PARTS[name]
                value = pymupdf.mupdf.fz_unicode_from_glyph_name_strict(name)
                if (0 < value <= 0x10ffff and value != 0xfffd
                        and not unicodedata.category(chr(value)).startswith("C")):
                    return chr(value)
            except (RuntimeError, ValueError, AttributeError):
                pass
        return None


@dataclass(frozen=True)
class _Line:
    text: str
    replacements: dict[int, str]
    bbox: tuple


def _page_lines(page, font_cache, reviewed_fonts=None):
    extra_candidates = _reviewed_input_characters(reviewed_fonts)
    fonts = defaultdict(list)
    reviewed = defaultdict(list)
    for entry in page.get_fonts(full=True):
        if entry[0] not in font_cache:
            font_cache[entry[0]] = _Font(page.parent, entry)
        font = font_cache[entry[0]]
        family = _font_key(entry[3])
        fonts[family].append(font)
        if reviewed_fonts:
            reviewed[family].append(reviewed_fonts.get(hashlib.sha256(font.data).hexdigest(), {}))
    drawn = defaultdict(set)
    for span in page.get_texttrace():
        for code, gid, origin, _ in span["chars"]:
            drawn[(_font_key(span["font"]), round(origin[0], 2), round(origin[1], 2))].add((code, gid))
    lines = []
    for block in page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]:
        for line in block.get("lines", []):
            text, changes = [], {}
            for span in line["spans"]:
                family = _font_key(span["font"])
                for char in span["chars"]:
                    value = char["c"]
                    if value.isspace() and not is_suspect_glyph(value):
                        continue
                    index = len(text)
                    text.append(value)
                    if not is_glyph_candidate(value, extra_candidates):
                        continue
                    key = family, round(char["origin"][0], 2), round(char["origin"][1], 2)
                    glyphs = drawn.get(key, set())
                    if len(glyphs) != 1:
                        continue
                    trace_code, gid = next(iter(glyphs))
                    if not gid:
                        continue
                    approved = {mapping.get((value, gid)) for mapping in reviewed[family]}
                    if len(approved) == 1 and None not in approved:
                        changes[index] = next(iter(approved))
                        continue
                    if not is_suspect_glyph(value):
                        continue
                    # MuPDF's drawing trace can resolve a symbol that its
                    # structured text extractor still exposes as legacy PUA.
                    # Same font + exact drawn origin bind these to one glyph.
                    trace_char = chr(trace_code)
                    if (trace_code != 0xfffd and not _private(trace_char)
                            and not unicodedata.category(trace_char).startswith("C")
                            and not trace_char.isspace()):
                        changes[index] = trace_char
                        continue
                    candidates = {font.decode(ord(value), gid) for font in fonts[family]}
                    if len(candidates) == 1 and None not in candidates:
                        changes[index] = next(iter(candidates))
            if text:
                lines.append(_Line("".join(text), changes, tuple(line["bbox"])))
    return lines


def _inside(line, box):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return True
    # Table / block records can round bounding boxes or exclude font ascenders.
    x0, y0, x1, y1 = line.bbox
    return (min(x1, box[2] + 2) > max(x0, box[0] - 2)
            and min(y1, box[3] + 2) > max(y0, box[1] - 2))


def _repair_text(text: str, lines: list[_Line], extra_candidates=()) -> str:
    if not _suspect(text, extra_candidates):
        return text
    offsets = [i for i, c in enumerate(text) if not c.isspace() or is_suspect_glyph(c)]
    compact = "".join(text[i] for i in offsets)
    proposals = defaultdict(set)
    for line in lines:
        # Full line in block/page, or short table cell in a source line.  Record
        # all alternatives; a conflicting font occurrence vetoes a repair.
        if line.text in compact:
            for match in re.finditer(re.escape(line.text), compact):
                for j, c in enumerate(line.text):
                    if is_glyph_candidate(c, extra_candidates):
                        proposals[match.start() + j].add(line.replacements.get(j))
        elif compact and compact in line.text:
            for match in re.finditer(re.escape(compact), line.text):
                for j, c in enumerate(compact):
                    if is_glyph_candidate(c, extra_candidates):
                        proposals[j].add(line.replacements.get(match.start() + j))
    result = list(text)
    for index, choices in proposals.items():
        if len(choices) == 1 and None not in choices:
            result[offsets[index]] = next(iter(choices))
    return "".join(result)


_FIELDS = ("text", "native_text", "normalized_text", "table_caption", "table_label", "columns", "rows", "cells")


def _native(record):
    parser = str(record.get("parser", "")).lower()
    mode = str(record.get("mode", "")).lower()
    return "ocr" not in parser and "ocr" not in mode and parser != "reviewed_transcription"


def repair_reading_pages(pdf_path, pages, elements, *, tables=()):
    """Return ``(pages, elements, tables)`` copies with proven glyphs decoded.

    Page numbers are physical and 1-based.  All sequences retain order/count;
    no input object or source file is changed.  OCR/reviewed transcriptions are
    excluded.  Unknown fonts, ambiguous alignment, missing/invalid PDFs, and
    symbols without semantic mappings are retained verbatim.  Only pages with
    suspect glyphs in the supplied records are inspected.
    """
    groups = [list(pages), list(elements), list(tables)]
    if not pdf_path or not Path(pdf_path).is_file():
        return tuple(groups)
    # Check the source identity before treating ordinary printable ASCII as a
    # repair candidate. Other PDFs retain their usual suspect-glyph scan.
    try:
        with Path(pdf_path).open("rb") as source_stream:
            reviewed_fonts = _REVIEWED_GLYPHS.get(hashlib.file_digest(source_stream, "sha256").hexdigest())
    except OSError:
        return tuple(groups)
    extra_candidates = _reviewed_input_characters(reviewed_fonts)
    wanted = {record.get("pdf_page") for group in groups for record in group
              if _native(record) and any(_suspect(record.get(field), extra_candidates) for field in _FIELDS)}
    wanted = {page for page in wanted if type(page) is int and page > 0}
    if not wanted:
        return tuple(groups)
    try:
        document = pymupdf.open(pdf_path)
    except (pymupdf.FileDataError, RuntimeError, ValueError, OSError):
        return tuple(groups)
    with document:
        cache = {}
        by_page = {}
        for number in sorted(wanted):
            if number <= len(document):
                try:
                    by_page[number] = _page_lines(document[number - 1], cache, reviewed_fonts)
                except (RuntimeError, ValueError, IndexError, OverflowError):
                    # One unreadable font/page cannot block other reading copies.
                    continue
        def repair(value, lines):
            if isinstance(value, str):
                return _repair_text(value, lines, extra_candidates)
            if isinstance(value, list):
                return [repair(v, lines) for v in value]
            if isinstance(value, tuple):
                return tuple(repair(v, lines) for v in value)
            return value
        for group in groups:
            for i, record in enumerate(group):
                if not _native(record):
                    continue
                lines = by_page.get(record.get("pdf_page"), [])
                box = record.get("table_bbox") or record.get("bbox")
                lines = [line for line in lines if _inside(line, box)]
                if not lines:
                    continue
                replacement = dict(record)
                for field in _FIELDS:
                    if field in record and _suspect(record[field], extra_candidates):
                        replacement[field] = repair(record[field], lines)
                if replacement != record:
                    group[i] = replacement
    return tuple(groups)
