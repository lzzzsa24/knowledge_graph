"""Expose reviewed evidence restrictions through the same projection in each reader."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import unicodedata

from .map_relations import claims_digest
from .map_sample_bindings import _physical_hash
from .pipeline import jsonl, read_json


REGISTRY = "catalog/evidence_qualifications.json"
FIELD = "evidence_qualifications"
STALE_TEXT = "已知此处有源证据复核记录，当前 PDF 或主张版本已变化，需重新核验原页。"
_KINDS = {"transcription_correction", "source_conflict", "source_limit"}
_AUDIT_FIELDS = {"raw_text_fields", "raw_quote", "raw_text", "raw_native_text",
                 "native_text", "text_quality", "text_quality_audit"}


def _alias_text(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _without_audit(record):
    if _AUDIT_FIELDS.isdisjoint(record):
        return record
    return {key: value for key, value in record.items() if key not in _AUDIT_FIELDS}



def _physical_pdf(pilot: Path, paper: dict) -> str:
    raw = paper.get("local_path")
    if not raw:
        return ""
    path = Path(raw)
    # Catalog paths can outlive the workspace they were assembled in.
    parts = str(raw).replace("\\", "/").split("/")
    lowered = [part.casefold() for part in parts]
    if "pilot" in lowered:
        path = pilot.joinpath(*parts[lowered.index("pilot") + 1:])
    elif not path.is_absolute():
        path = pilot.parent / path
    try:
        stat = path.stat()
        return _physical_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return ""


def physical_revision(pilot: Path, registry_path: Path):
    """Track bound files even when the on-disk catalog has not yet changed."""
    data = read_json(registry_path)
    owners = {doc["doc_id"] for doc in data["documents"]}
    papers = read_json(pilot / "catalog/papers.json")
    matches = sorted((p["doc_id"], _physical_pdf(pilot, p)) for p in papers if p["doc_id"] in owners)
    return repr(matches)


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _checked_entry(row: dict):
    pages = row.get("pdf_pages")
    claim_ids = row.get("claim_ids")
    evidence = row.get("evidence")
    if not (
        _nonempty_text(row.get("id"))
        and row.get("kind") in _KINDS
        and _nonempty_text(row.get("summary"))
        and _nonempty_text(row.get("usage_limit"))
        and isinstance(pages, list) and pages and all(type(n) is int and n > 0 for n in pages)
        and isinstance(claim_ids, list) and all(_nonempty_text(cid) for cid in claim_ids)
        and isinstance(evidence, list) and evidence
        and all(isinstance(e, dict) and e.get("pdf_page") in pages and _nonempty_text(e.get("quote"))
                for e in evidence)
    ):
        raise ValueError("源证据复核记录缺少有效的对象、页码、结论或依据。")
    allowed = ("id", "kind", "claim_ids", "pdf_pages", "summary", "usage_limit", "evidence",
               "disputed_value", "related_claim_only")
    for key in ("disputed_value", "related_claim_only"):
        if key in row and type(row[key]) is not bool:
            raise ValueError("源证据复核范围必须为布尔值。")
    aliases = row.get("query_aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(group, list) and group and
                                                all(_nonempty_text(term) for term in group) for group in aliases):
        raise ValueError("源证据复核检索词必须为词组数组。")
    return {key: row[key] for key in allowed if key in row}


class EffectiveEvidence:
    def __init__(self, *, by_doc=None, aliases=None):
        self._by_doc = by_doc or {}
        self._aliases = aliases or {}

    def qualifications(self, doc_id, *, pdf_page=None, claim_id=None):
        return [q for q in self._by_doc.get(doc_id, ())
                if (pdf_page is None and claim_id is None)
                or (pdf_page is not None and pdf_page in q["pdf_pages"])
                or (claim_id is not None and claim_id in q["claim_ids"])]

    def matches(self, query):
        text = _alias_text(query)
        return [(doc_id, qualification) for doc_id, entries in self._by_doc.items()
                for qualification in entries
                if any(all(term in text for term in group)
                       for group in self._aliases.get((doc_id, qualification["id"]), []))]

    def source(self, source: dict) -> dict:
        source = _without_audit(source)
        if not source.get("doc_id") or type(source.get("pdf_page")) is not int:
            return source
        qualifications = self.qualifications(source["doc_id"], pdf_page=source["pdf_page"])
        if not qualifications:
            return source
        projected = {**source, FIELD: qualifications}
        projected.pop("embedding_text", None)
        if any(q["status"] == "stale" for q in qualifications):
            for field in ("text", "quote"):
                if field in projected:
                    projected[field] = STALE_TEXT
            projected.pop("parts", None)
        return projected

    def claim(self, claim: dict) -> dict:
        claim = _without_audit(claim)
        doc_id, claim_id = claim["doc_id"], claim["claim_id"]
        evidence = claim.get("evidence") or []
        visible_evidence = [_without_audit(entry) for entry in evidence]
        if visible_evidence != evidence:
            claim = {**claim, "evidence": visible_evidence}
        evidence = visible_evidence
        qualifications = self.qualifications(doc_id, claim_id=claim_id)
        pages = {e.get("pdf_page") for e in evidence}
        by_page = [q for q in self.qualifications(doc_id) if pages.intersection(q["pdf_pages"])]
        qualifications.extend(q for q in by_page if q not in qualifications)
        if not qualifications:
            return claim
        stale = [q for q in qualifications if q["status"] == "stale"]
        stale_pages = {page for q in stale for page in q["pdf_pages"]}
        stale_claim = any(claim_id in q["claim_ids"] and not q.get("related_claim_only") for q in stale)
        affected = stale_claim or bool(pages & stale_pages)
        if not affected:
            disputed = next((q for q in qualifications if q["status"] == "active" and q.get("disputed_value")
                             and claim_id in q["claim_ids"]), None)
            projection = {**claim, FIELD: qualifications}
            if disputed:
                projection.update(statement=disputed["summary"], value_text=None, unit=None)
            return projection
        return {**claim, FIELD: qualifications, "statement": STALE_TEXT, "sample_id": "",
                "conditions": {}, "value_text": None, "unit": None, "evidence": [
                    {**e, "quote": STALE_TEXT} if stale_claim or e.get("pdf_page") in stale_pages else e
                    for e in evidence]}

    def anchors(self, item, *, doc_id=None):
        """Project source anchors in map notes and links after their raw validation."""
        if isinstance(item, list):
            return [self.anchors(entry, doc_id=doc_id) for entry in item]
        if not isinstance(item, dict):
            return item
        item = _without_audit(item)
        doc_id = item.get("doc_id", doc_id)
        projected = {k: self.anchors(v, doc_id=doc_id) if k != FIELD else v for k, v in item.items()}
        if doc_id and type(item.get("pdf_page")) is int and "quote" in item:
            source = self.source({"doc_id": doc_id, **projected})
            return source if "doc_id" in item else {k: v for k, v in source.items() if k != "doc_id"}
        return projected


def load_effective_evidence(pilot, *, papers=None, claims=None) -> EffectiveEvidence:
    pilot = Path(pilot)
    path = pilot / REGISTRY
    if not path.is_file():
        return EffectiveEvidence()
    data = read_json(path)
    if data.get("schema") != 1 or not isinstance(data.get("documents"), list):
        raise ValueError("源证据复核文件的结构无效。")
    if papers is None:
        papers = read_json(pilot / "catalog/papers.json")
    if claims is None:
        claims = jsonl(pilot / "annotations/claims.jsonl")
    catalog = {p["doc_id"]: p for p in papers}
    by_doc, aliases, grouped = {}, {}, defaultdict(list)
    owners = {d["doc_id"] for d in data["documents"]}
    for claim in claims:
        if claim["doc_id"] in owners:
            grouped[claim["doc_id"]].append(claim)
    for document in data["documents"]:
        doc_id = document["doc_id"]
        paper = catalog.get(doc_id, {})
        rows = document["qualifications"]
        if doc_id in by_doc or not isinstance(rows, list):
            raise ValueError(f"源证据复核文件重复或结构无效：{doc_id}")
        expected = document.get("source_sha256")
        active = bool(expected and expected == paper.get("sha256")
                      and document.get("claims_digest") == claims_digest(grouped[doc_id])
                      and expected == _physical_pdf(pilot, paper))
        by_doc[doc_id] = []
        seen = set()
        for raw in rows:
            entry = _checked_entry(raw)
            if entry["id"] in seen:
                raise ValueError(f"源证据复核记录有重复标识：{doc_id}")
            seen.add(entry["id"])
            aliases[(doc_id, entry["id"])] = [[_alias_text(term) for term in group]
                                               for group in raw.get("query_aliases", [])]
            if active:
                entry["status"] = "active"
            else:
                entry.update(status="stale", summary=STALE_TEXT, usage_limit=STALE_TEXT,
                             evidence=[{"pdf_page": page} for page in entry["pdf_pages"]])
            by_doc[doc_id].append(entry)
    return EffectiveEvidence(by_doc=by_doc, aliases=aliases)
