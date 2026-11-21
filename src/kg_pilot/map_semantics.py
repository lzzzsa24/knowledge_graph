"""Apply claim- and evidence-bound adjudications to the two upstream map passes."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from .map_relations import claims_digest
from .research_map import COLUMNS, ROWS, build_research_map


SCHEMA = 1
VERSION = "map-stage-endpoint-v1"
ROW_IDS = {row["id"] for row in ROWS}
COLUMN_IDS = {column["id"] for column in COLUMNS}
RELATIONS = {"condition", "contrast", "method", "interpretation"}
STATUSES = {"kept", "replaced", "withheld", "uncertain"}


def digest(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def pair(item):
    return item["claim_id"], item["row"], item["column"]


AXES_DIGEST = digest([ROWS, COLUMNS])


def mapped_candidates(papers, claims, links, withheld):
    """Enumerate the actual pre-semantic map, including rule-only associations."""
    mapping = build_research_map(papers, claims, reviewed=links, withheld=withheld)
    grouped = defaultdict(list)
    for cell in mapping["cells"]:
        for cid in cell["records"]:
            doc_id = mapping["record_details"][cid]["doc_id"]
            previous = cell["reviewed_records"].get(cid)
            item = dict(doc_id=doc_id, claim_id=cid, row=cell["row"], column=cell["column"], previous=previous)
            item["input_digest"] = digest([AXES_DIGEST, item])
            grouped[doc_id].append(item)
    return {doc: sorted(items, key=pair) for doc, items in grouped.items()}


def _citation(evidence, page_count):
    return (isinstance(evidence, dict) and type(evidence.get("pdf_page")) is int
            and 1 <= evidence["pdf_page"] <= page_count and isinstance(evidence.get("quote"), str)
            and 8 <= len(evidence["quote"].strip()) <= 450)


def _link_valid(link, candidate, claim, pages):
    if not isinstance(link, dict):
        return False
    return (link.get("claim_id") == candidate["claim_id"] and link.get("doc_id") == candidate["doc_id"]
            and link.get("row") in ROW_IDS and link.get("column") in COLUMN_IDS
            and link.get("relation") in RELATIONS
            and all(isinstance(link.get(k), str) and link[k].strip() for k in ("limit", "scope"))
            and all(_citation(link.get(k), pages) for k in ("process_evidence", "result_evidence"))
            and link["result_evidence"]["pdf_page"] in {e.get("pdf_page") for e in claim.get("evidence", [])})


def _reader_valid(reader):
    return (isinstance(reader, dict) and bool(reader.get("model")) and bool(reader.get("reason"))
            and type(reader.get("candidate_id")) is int and reader["candidate_id"] >= 0
            and isinstance(reader.get("packet_sha256"), str) and len(reader["packet_sha256"]) == 64)


def _valid(decision, candidate, claim, pages):
    status = decision.get("status")
    if (decision.get("input_digest") != candidate["input_digest"] or decision.get("previous") != candidate["previous"]
            or decision.get("doc_id") != candidate["doc_id"] or status not in STATUSES or not decision.get("reason")
            or not _reader_valid(decision.get("assessment"))):
        return False
    if status == "uncertain":
        return not decision.get("link")
    citations = decision.get("evidence")
    if not (isinstance(citations, dict) and all(_citation(citations.get(k), pages) for k in ("factor", "endpoint"))
            and citations["endpoint"]["pdf_page"] in {e.get("pdf_page") for e in claim.get("evidence", [])}):
        return False
    if status == "withheld":
        return not decision.get("link")
    link = decision.get("link")
    if not _link_valid(link, candidate, claim, pages):
        return False
    if link["process_evidence"] != citations["factor"] or link["result_evidence"] != citations["endpoint"]:
        return False
    # A prior independent review exists for a literally unchanged, signed edge.
    if status == "kept":
        return link == candidate["previous"]
    confirmation = decision.get("confirmation")
    return (_reader_valid(confirmation) and confirmation.get("accepted") is True and confirmation.get("link_digest") == digest(link)
            and confirmation["packet_sha256"] != decision["assessment"]["packet_sha256"])


def _enact(candidates, decisions, links, withheld):
    indexed = {pair(x): x for x in links}
    hidden = set(withheld)
    modified = []
    stats = Counter(d["status"] for d in decisions)
    for d in decisions:
        old = pair(d)
        if d["status"] in {"withheld", "uncertain"}:
            hidden.add(old)
            indexed.pop(old, None)
        elif d["status"] == "replaced":
            if pair(d["link"]) != old:
                hidden.add(old)
                indexed.pop(old, None)
            modified.append(d)
    targets = defaultdict(list)
    for d in modified:
        target = pair(d["link"])
        if target in hidden or (target != pair(d) and target in candidates):
            # An existing target owns its own assessment; a blocked original or
            # a competing upstream decision must never be revived by a migration.
            stats["target_already_mapped_or_withheld"] += 1
            continue
        targets[target].append(d)
    for target, rows in sorted(targets.items()):
        if len({digest(d["link"]) for d in rows}) > 1:
            stats["conflicting_replacements"] += len(rows)
            continue
        indexed[target] = rows[0]["link"]
        stats["replacement_links_applied"] += 1
    return list(indexed.values()), hidden, stats


def apply_semantic_reviews(pilot: Path, papers: list[dict], claims: list[dict], links: list[dict],
                           withheld: set) -> tuple[list, set, dict]:
    """Apply the local optional overlay. Missing, stale or ambiguous rows are skipped."""
    path = Path(pilot) / "research_map/semantic_reviews.json"
    audit = dict(version=VERSION, current_pairs_reviewed=0, scope="current_document_selection")
    try:
        store = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return list(links), set(withheld), audit
    if not isinstance(store, dict) or store.get("schema") != SCHEMA or store.get("version") != VERSION:
        return list(links), set(withheld), audit
    groups = mapped_candidates(papers, claims, links, withheld)
    candidates = {pair(c): c for values in groups.values() for c in values}
    grouped_claims = defaultdict(list)
    for c in claims:
        grouped_claims[c["doc_id"]].append(c)
    papers_by_id = {p["doc_id"]: p for p in papers}
    by_claim = {c["claim_id"]: c for c in claims}
    accepted = []
    docs = store.get("documents")
    if not isinstance(docs, list):
        return list(links), set(withheld), audit
    doc_counts = Counter(d.get("doc_id") for d in docs if isinstance(d, dict))
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        doc_id = doc.get("doc_id")
        paper = papers_by_id.get(doc_id)
        if (doc_counts[doc_id] != 1 or not paper or not doc.get("source_sha256")
                or doc["source_sha256"] != paper.get("sha256")
                or doc.get("claims_digest") != claims_digest(grouped_claims[doc_id])):
            continue
        entries = doc.get("decisions")
        if not isinstance(entries, list):
            continue
        entries = [d for d in entries if isinstance(d, dict)
                   and all(isinstance(d.get(k), str) for k in ("claim_id", "row", "column"))]
        counts = Counter(map(pair, entries))
        for d in entries:
            key = pair(d)
            c = candidates.get(key)
            if (counts[key] == 1 and c and c["doc_id"] == doc_id and
                    _valid(d, c, by_claim[c["claim_id"]], paper.get("page_count", 0))):
                accepted.append(d)
    final_links, final_withheld, stats = _enact(candidates, accepted, links, withheld)
    audit.update(stats)
    audit.update(current_pairs_reviewed=len(accepted), current_pairs=len(candidates),
                 pending_pairs=len(candidates) - len(accepted))
    return final_links, final_withheld, audit
