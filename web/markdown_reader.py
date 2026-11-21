"""Render the on-disk reading copy without executing document-supplied HTML.

The Markdown is also offered unchanged for download. Only links to the current
paper's verified local PDF become page-navigation links; remote images are text.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml


def _safe_link(href: str, *, markdown_path: Path, pdf_path: Path | None,
               doc_id: str, page_count: int) -> tuple[str | None, int | None]:
    try:
        parts = urlsplit(href)
    except ValueError:
        return None, None
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        return href, None
    if parts.scheme or parts.netloc or parts.query or pdf_path is None:
        return None, None
    match = re.fullmatch(r"page=([1-9]\d*)", parts.fragment)
    if not match or not parts.path:
        return None, None
    page = int(match[1])
    if page > page_count:
        return None, None
    try:
        target = (markdown_path.parent / unquote(parts.path)).resolve()
    except (OSError, ValueError):
        return None, None
    if target != pdf_path.resolve():
        return None, None
    return f"/api/papers/{quote(doc_id, safe='')}/pdf#page={page}", page


def render_markdown(source: str, *, markdown_path: Path, pdf_path: Path | None,
                    doc_id: str, page_count: int) -> str:
    # HTML is disabled in both blocks and inline content. No plugins emit raw
    # HTML. Tables are part of the installed markdown-it-py distribution.
    parser = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    parser.add_render_rule("image", lambda renderer, tokens, index, options, env:
                           escapeHtml(f"[图片：{tokens[index].content}]"))
    tokens = parser.parse(source)

    def secure(items):
        links = []
        for token in items:
            if token.type == "link_open":
                href, page = _safe_link(token.attrGet("href") or "", markdown_path=markdown_path,
                                        pdf_path=pdf_path, doc_id=doc_id, page_count=page_count)
                links.append(href is not None)
                if href is None:
                    token.tag, token.attrs = "span", {}
                else:
                    token.attrs = {"href": href, "target": "_blank", "rel": "noopener noreferrer"}
                    if page is not None:
                        token.attrSet("data-pdf-page", str(page))
            elif token.type == "link_close" and links:
                if not links.pop():
                    token.tag = "span"
            if token.children:
                secure(token.children)

    secure(tokens)
    return parser.renderer.render(tokens, parser.options, {})
