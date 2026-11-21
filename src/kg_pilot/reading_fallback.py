"""Conservative display fallback for undecodable characters in reading copies.

This is a final, pure Markdown presentation pass. It does not decode fonts or
alter PDF/parse evidence. A fallback is emitted only inside a physical-page
section with its own existing relative PDF link.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit


_UNKNOWN = re.compile("[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd\ufffd]+")
_PAGE = re.compile(r"^## PDF 第 ([1-9]\d*) 页[ \t]*$")
_SOURCE = re.compile(r"^\[查看原\s*PDF\s*第\s*([1-9]\d*)\s*页\]\(([^\r\n]+)\)[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_NUMBER = re.compile(r"(\\?\([ \t]*[1-9]\d{0,2}[ \t]*\\?\))[ \t]*$")
_DESTINATION = re.compile(r"(?<!\\)\]\((?:\\.|[^)\r\n])*\)")
_MARKER = "〔原页符号〕"


def _fence_state(line: str, active: tuple[str, int] | None):
    match = _FENCE.match(line.rstrip("\r\n"))
    if not match:
        return active
    run, rest = match.groups()
    if active is None:
        return run[0], len(run)
    if run[0] == active[0] and len(run) >= active[1] and not rest.strip():
        return None
    return active


def _source_url(section: str, page: int) -> str | None:
    active = None
    for line in section.splitlines():
        if active is None:
            match = _SOURCE.fullmatch(line)
            if match and int(match[1]) == page:
                url = match[2]
                try:
                    parts = urlsplit(url)
                except ValueError:
                    continue
                if (not parts.scheme and not parts.netloc and not parts.query
                        and parts.path and not parts.path.startswith(("/", "\\"))
                        and parts.fragment == f"page={page}"
                        and not re.search(r"[\s()<>]", url)):
                    return url
        active = _fence_state(line, active)
    return None


def _blocks(section: str):
    """Keep original separators and fenced blocks, including their blank lines."""
    pending: list[str] = []
    active = None
    for line in section.splitlines(keepends=True):
        if not line.strip() and active is None:
            if pending:
                yield "".join(pending)
                pending.clear()
            yield line
            continue
        pending.append(line)
        previous = active
        active = _fence_state(line, active)
        if previous is not None and active is None:
            yield "".join(pending)
            pending.clear()
    if pending:
        yield "".join(pending)


def _damaged_formula(block: str) -> tuple[bool, str]:
    """Collapse only almost-entirely undecodable, standalone formula fragments.

    Real prose, numbers (except a final parenthesized equation label), and
    existing non-ASCII mathematical symbols force the less destructive inline
    fallback. Single Latin fragments adjoining an unknown glyph can be remnants
    of a broken formula, such as the original CambriaMatho B(...) expression.
    """
    text = block.strip()
    if not text or re.match(r"^(?:#{1,6}\s|[>|]|`{3,}|~{3,})", text):
        return False, ""
    number = _NUMBER.search(text)
    label = number[1] if number else ""
    body = text[:number.start()] if number else text
    runs = list(_UNKNOWN.finditer(body))
    unknown_count = sum(len(match[0]) for match in runs)
    if unknown_count < 4 or unknown_count / max(1, len(re.sub(r"\s", "", body))) < .5:
        return False, ""
    # A readable quantity or Greek/math symbol must not disappear with the run.
    known = _UNKNOWN.sub("", body)
    if any(char.isnumeric() or (ord(char) > 127 and not char.isspace()) for char in known):
        return False, ""
    for match in re.finditer(r"[A-Za-z]+", body):
        if len(match[0]) != 1 or not (
                (match.end() < len(body) and _UNKNOWN.match(body, match.end()))
                or (match.start() > 0 and unicodedata.category(body[match.start()-1]) == "Co")):
            return False, ""
    return True, label


def _replace_runs(block: str) -> str:
    # Do not rewrite an existing Markdown link destination or its escaping.
    parts, end = [], 0
    for match in _DESTINATION.finditer(block):
        parts.extend((_UNKNOWN.sub(_MARKER, block[end:match.start()]), match[0]))
        end = match.end()
    parts.append(_UNKNOWN.sub(_MARKER, block[end:]))
    return "".join(parts)


def _fallback_page(section: str, page: int) -> str:
    url = _source_url(section, page)
    if url is None or not _UNKNOWN.search(section):
        return section
    newline = "\r\n" if "\r\n" in section else "\n"
    result = []
    inline_note_added = False
    for block in _blocks(section):
        if not _UNKNOWN.search(block):
            result.append(block)
            continue
        formula, label = _damaged_formula(block)
        ending = newline if block.endswith("\n") else ""
        if formula:
            result.append(f"[该公式排版见原页]({url})" + (" " + label if label else "") + ending)
            continue
        replacement = _replace_runs(block)
        if replacement != block and not inline_note_added:
            replacement = (replacement.rstrip("\r\n") + newline + newline
                           + f"[未解码符号见本页原文]({url})" + ending)
            inline_note_added = True
        result.append(replacement)
    return "".join(result)


def apply_reading_fallback(markdown: str) -> str:
    """Replace residual PUA/replacement runs with explicitly linked fallbacks.

    Ordinary squares/bullets and all other Unicode characters are untouched.
    Sections without their matching physical-page link stay unchanged because
    the pass cannot provide a reliable source destination for them.
    """
    if not _UNKNOWN.search(markdown):
        return markdown
    result, section = [], []
    page = None
    active = None
    for line in markdown.splitlines(keepends=True):
        match = _PAGE.fullmatch(line.rstrip("\r\n")) if active is None else None
        if match:
            value = "".join(section)
            result.append(_fallback_page(value, page) if page is not None else value)
            section = []
            page = int(match[1])
        section.append(line)
        active = _fence_state(line, active)
    value = "".join(section)
    result.append(_fallback_page(value, page) if page is not None else value)
    return "".join(result)
