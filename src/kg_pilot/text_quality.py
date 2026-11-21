"""Source-bound PDF text repairs and an auditable quantitative-evidence gate.

The font decoder is deliberately the same one used for reading copies.  A
private-use code point is never interpreted from its value alone.  Other
corruption is detected and retained for review rather than guessed away.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
import re
import unicodedata

from kg_pilot.reading_glyphs import repair_reading_pages


QUALITY_VERSION = "source-font-quality-1"
_FIELDS = ("text", "normalized_text", "table_caption", "table_label", "columns", "rows", "cells")
_MOJIBAKE = re.compile(r"(?:â[€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ\x80-\xff]{1,2}|Ã[\x80-\xBF]|Â[\x80-\xBF]|ï¿½)")


def scan_text_quality(value):
    """Return all suspicious source occurrences; valid Greek/SI text is clean.

    Offsets are Python string offsets.  An unresolved occurrence is considered
    critical when used in quantitative evidence: even a single bad prefix can
    change a measurement by orders of magnitude.
    """
    if not isinstance(value, str):
        return []
    issues = []
    for offset, char in enumerate(value):
        category = unicodedata.category(char)
        kind = ("private_use" if category == "Co" else
                "replacement_character" if char == "\ufffd" else
                "invalid_control" if category in {"Cc", "Cs"} and char not in "\n\r\t" else None)
        if kind:
            issues.append(dict(kind=kind, offset=offset, codepoint=f"U+{ord(char):04X}",
                               critical=True, context=value[max(0, offset-35):offset+36]))
    for kind, expression in (("cid_fragment", re.compile(r"\(cid:\s*\d+\)", re.I)),
                             ("possible_mojibake", _MOJIBAKE)):
        for match in expression.finditer(value):
            issues.append(dict(kind=kind, offset=match.start(), token=match.group(), critical=True,
                               context=value[max(0, match.start()-35):match.end()+35]))
    for issue in issues:
        issue["quantitative_context"] = bool(re.search(r"\d|[=<>≤≥±×σμµ]", issue["context"]))
    return sorted(issues, key=lambda item: item["offset"])


def _strings(value, path):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")


def _changes(before, after, path):
    if isinstance(before, str) and isinstance(after, str):
        if before != after:
            # A single ligature can expand to several Unicode characters.
            # Positional zip would count its entire following suffix as edits.
            pieces = [(before[i:j], after[k:l]) for opcode, i, j, k, l in
                      SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes() if opcode != "equal"]
            replacements = Counter(pieces)
            return [dict(field=path, replacements=[dict(before=a, after=b, count=count)
                                                  for (a, b), count in sorted(replacements.items())],
                         changed_characters=sum(len(a) for a, _ in pieces),
                         output_characters=sum(len(b) for _, b in pieces))]
    elif isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        return [change for index, (a, b) in enumerate(zip(before, after))
                for change in _changes(a, b, f"{path}[{index}]")]
    return []


def repair_document_text(pdf_path, pages, elements, *, tables=(), document_sha256=None):
    """Return copied canonical ``(pages, elements, tables, audit)``.

    Identity, page and geometry fields are preserved.  Changed raw fields are
    saved in ``raw_text_fields``.  ``native_text`` always remains the original
    extractor output; its repaired counterpart is ``decoded_native_text``.
    Neither input records nor the PDF are modified.  A supplied hash is checked
    before consulting the font source, so a stale PDF cannot authorize repair.
    """
    source = Path(pdf_path) if pdf_path else None
    actual_sha = hashlib.sha256(source.read_bytes()).hexdigest() if source and source.is_file() else None
    if document_sha256 and actual_sha != document_sha256:
        raise ValueError("字形修复的来源 PDF SHA-256 不符。")
    originals = deepcopy((list(pages), list(elements), list(tables)))
    repaired = repair_reading_pages(source, *originals[:2], tables=originals[2])
    groups = deepcopy(repaired)
    page_audit = {}
    for group_index, group in enumerate(groups):
        for before, record in zip(originals[group_index], group):
            # Capture the final derived value, including compatibility Unicode
            # normalization, in both the raw-field record and compact audit.
            if "normalized_text" in record and isinstance(record.get("text"), str):
                record["normalized_text"] = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", record["text"])).strip()
            changes = []
            for field in (*_FIELDS, "native_text"):
                if field in before and field in record:
                    changes.extend(_changes(before[field], record[field], field))
            previous_raw = deepcopy(before.get("raw_text_fields", {}))
            for field in _FIELDS:
                if field in before and before.get(field) != record.get(field):
                    previous_raw.setdefault(field, deepcopy(before[field]))
            if previous_raw:
                record["raw_text_fields"] = previous_raw
            if "native_text" in before:
                if record.get("native_text") != before["native_text"]:
                    record["decoded_native_text"] = record["native_text"]
                record["native_text"] = before["native_text"]
            issues = [dict(issue, field=path) for field in _FIELDS if field in record
                      for path, value in _strings(record[field], field) for issue in scan_text_quality(value)]
            # Keep audit state stable when a prepared document is revalidated.
            previous = before.get("text_quality", {})
            prior_changes = previous.get("repairs", []) if previous.get("version") == QUALITY_VERSION else []
            # Revalidation can improve the audit format (for example ligature
            # counts) without double-counting the same stored source field.
            by_field = {change["field"]: change for change in prior_changes}
            by_field.update({change["field"]: change for change in changes})
            changes = list(by_field.values())
            quality = dict(version=QUALITY_VERSION, status="needs_review" if issues else "repaired" if changes else "clean",
                           unresolved_critical=any(issue["critical"] for issue in issues),
                           issue_count=len(issues), quantitative_context_issues=sum(issue["quantitative_context"] for issue in issues),
                           issues=issues[:80], repairs=changes,
                           repaired_characters=sum(change["changed_characters"] for change in changes))
            record["text_quality"] = quality
            page = record.get("pdf_page")
            summary = page_audit.setdefault(page, dict(pdf_page=page, repaired_characters=0,
                repaired_records=0, issue_count=0, quantitative_context_issues=0,
                unresolved_critical=False, issue_kinds=Counter()))
            summary["repaired_characters"] += quality["repaired_characters"]
            summary["repaired_records"] += bool(changes)
            summary["issue_count"] += len(issues)
            summary["quantitative_context_issues"] += quality["quantitative_context_issues"]
            summary["unresolved_critical"] |= quality["unresolved_critical"]
            summary["issue_kinds"].update(issue["kind"] for issue in issues)
    summaries = [page_audit[key] for key in sorted(page_audit, key=lambda key: (key is None, key or 0))]
    for summary in summaries:
        summary["issue_kinds"] = dict(summary["issue_kinds"])
    audit = dict(version=QUALITY_VERSION, source_sha256=actual_sha,
                 status="needs_review" if any(p["unresolved_critical"] for p in summaries) else
                        "repaired" if any(p["repaired_records"] for p in summaries) else "clean",
                 unresolved_critical=any(p["unresolved_critical"] for p in summaries),
                 pages=summaries, repaired_characters=sum(p["repaired_characters"] for p in summaries),
                 issue_count=sum(p["issue_count"] for p in summaries),
                 policy="Only source-bound font/glyph repairs; unresolved quantitative evidence must not be published.")
    return (*groups, audit)


def guard_claim_text_quality(claim, *, pages=(), elements=()):
    """Reject a numeric claim whose own values/conditions/quotes are corrupt.

    A clean quotation may still be used on a page with an unrelated bad glyph.
    The gate therefore follows the quoted evidence instead of disabling every
    measurement on the physical page.  ``pages/elements`` are accepted for
    callers that also maintain record-level audit context.
    """
    fields = ("statement", "value_text", "unit", "conditions", "material_system")
    quantitative = bool(claim.get("unit")) or any(re.search(r"\d", text)
        for field in ("statement", "value_text", "conditions") for _, text in _strings(claim.get(field), field))
    if not quantitative:
        return
    issues = [dict(issue, field=path) for field in fields
              for path, value in _strings(claim.get(field), field) for issue in scan_text_quality(value)]
    issues.extend(dict(issue, field=f"evidence[{index}].quote")
                  for index, reference in enumerate(claim.get("evidence", []))
                  for issue in scan_text_quality(reference.get("quote")))
    if issues:
        raise ValueError("定量记录仍含未确认乱码或单位符号，已阻止发布；请查看对应原页并重新抽取。")
