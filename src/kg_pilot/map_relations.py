"""Load research-map evidence bound to the exact source PDF and its current claims."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def claims_digest(records):
    content = json.dumps(sorted(records, key=lambda c: c["claim_id"]), ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(content).hexdigest()


def _current_documents(path, papers, claims):
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("schema") != 1:
        return
    by_doc = {p["doc_id"]: p for p in papers}
    grouped = {}
    for c in claims:
        grouped.setdefault(c["doc_id"], []).append(c)
    for doc in data.get("documents", []):
        doc_id = doc.get("doc_id")
        if doc_id not in by_doc or not doc.get("source_sha256") or doc["source_sha256"] != by_doc[doc_id].get("sha256"):
            continue
        records = grouped.get(doc_id, [])
        if doc.get("claims_digest") == claims_digest(records):
            yield doc, {c["claim_id"] for c in records}, by_doc[doc_id].get("page_count", 0)


def _citation(e, pages):
    return isinstance(e,dict) and type(e.get("pdf_page")) is int and 1 <= e["pdf_page"] <= pages and bool(e.get("quote"))


def _valid_link(link, doc, ids, pages):
    return (isinstance(link,dict) and link.get("claim_id") in ids and link.get("doc_id") == doc["doc_id"]
            and link.get("relation") in {"contrast","condition","method","interpretation"} and link.get("scope") and link.get("limit")
            and all(_citation(link.get(key),pages) for key in ("process_evidence","result_evidence")))


def _pair(link):
    return link["claim_id"], link["row"], link["column"]


def load_mapping_reviews(pilot: Path, papers: list[dict], claims: list[dict]) -> tuple[list, set, dict]:
    root = pilot / "research_map"
    links = {}
    withheld = set()
    examined = set()
    conflicts = set()
    discovery_docs = set()
    rule_docs = set()
    for doc, ids, pages in _current_documents(root / "reviewed_relations.json",papers,claims):
        discovery_docs.add(doc["doc_id"])
        for link in doc.get("links", []):
            if _valid_link(link,doc,ids,pages):
                links[_pair(link)] = link
    recovered = set()
    for doc, ids, pages in _current_documents(root / "recovered_relations.json",papers,claims):
        for link in doc.get("links", []):
            if _valid_link(link,doc,ids,pages) and _pair(link) not in links:
                links[_pair(link)] = link
                recovered.add(_pair(link))
    for doc, ids, pages in _current_documents(root / "adjudicated_relations.json",papers,claims):
        rule_docs.add(doc["doc_id"])
        for row in doc.get("decisions",[]):
            if (row.get("claim_id") not in ids or row.get("doc_id") != doc["doc_id"] or not row.get("reason")
                    or not row.get("row") or not row.get("column")):
                continue
            pair = _pair(row)
            examined.add(pair)
            state = row.get("status")
            alternative = row.get("link")
            if state == "supported" and _valid_link(alternative,doc,ids,pages) and _pair(alternative)==pair:
                if pair not in links:
                    links[pair] = alternative
            elif state == "unsupported" and _citation(row.get("evidence"),pages):
                if pair in links:
                    conflicts.add(pair)
                    del links[pair]
                withheld.add(pair)
            elif pair not in links:
                # A second search may already have found independent signed
                # evidence. An inconclusive search alone cannot overturn that.
                withheld.add(pair)
    audit = {"discovery_documents": len(discovery_docs), "rule_review_documents":len(rule_docs),
             "rule_pairs_examined":len(examined), "withheld_rule_pairs":len(withheld),
             "conflicting_pairs":len(conflicts), "recovered_pairs_present":len(recovered.intersection(links)), "scope":"current_document_selection"}
    return list(links.values()), withheld, audit


def load_reviewed(pilot: Path, papers: list[dict], claims: list[dict]) -> list[dict]:
    return load_mapping_reviews(pilot,papers,claims)[0]


def source_readings(pilot: Path, papers: list[dict], claims: list[dict]):
    """Keep passage notes available even when a document has no counted matrix link."""
    path = pilot / "research_map/unmapped_source_readings.json"
    readings = {}
    for item, _, pages in _current_documents(path, papers, claims):
        evidence = item.get("evidence")
        if (item.get("status") != "verified" or not all(item.get(k) for k in ("finding", "scope", "review_reason"))
                or not isinstance(evidence, list) or not evidence or not all(_citation(e, pages) for e in evidence)):
            continue
        readings[item["doc_id"]] = {k:item[k] for k in ("kind","finding","scope","evidence")}
    return readings

