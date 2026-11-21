"""Fail-closed addition of independently checked specimen-group map evidence."""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from .map_relations import claims_digest
from .map_semantics import AXES_DIGEST, COLUMN_IDS, ROW_IDS, digest, mapped_candidates, pair
from .map_source_kinds import resolve_map_source_kind
from .research_map import _source_kind


SCHEMA = 1
VERSION = "sample-bindings-three-anchors-v1"
SIDECAR = "research_map/sample_bindings.json"
BASE_FILES = ("catalog/papers.json", "annotations/claims.jsonl", "catalog/classifications.json",
              "catalog/taxonomy.json", "research_map/source_kind_reviews.json",
              "research_map/reviewed_relations.json", "research_map/adjudicated_relations.json",
              "research_map/recovered_relations.json", "research_map/semantic_reviews.json")
HEX = re.compile(r"[0-9a-f]{64}")
ROLES = ("factor","endpoint","sample")
VERDICTS = {"accept","reject","uncertain","ineligible"}


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def base_files(pilot):
    def file_hash(name):
        path = Path(pilot)/name
        return sha(path.read_bytes()) if path.is_file() else None
    return {name:file_hash(name) for name in BASE_FILES}


def anchors(link):
    extra = link.get("supporting_evidence")
    joins = [e for e in extra if isinstance(e,dict) and e.get("role")=="sample"] if isinstance(extra,list) else []
    return {"factor":link.get("process_evidence"),"endpoint":link.get("result_evidence"),
            "sample":joins[0] if len(joins)==1 and len(extra)==1 else None}


def eligible(link, paper, claim, kind_reviews):
    if not (isinstance(claim,dict) and claim["doc_id"] == paper["doc_id"] == link.get("doc_id")
            and claim["claim_id"] == link.get("claim_id") and claim.get("claim_type") == "reported_experiment"
            and claim.get("review_status") in {"independent_agent_reviewed","api_model_reviewed"}
            and (resolve_map_source_kind(paper,claim,kind_reviews) or _source_kind(paper,claim))=="experiment"):
        return False
    if not (link.get("row") in ROW_IDS and link.get("column") in COLUMN_IDS
            and link.get("relation") in {"condition","contrast"} and link.get("origin")=="sample_methods"
            and all(isinstance(link.get(k),str) and link[k].strip() for k in ("scope","limit","sample_group","sample_link"))):
        return False
    evidence = anchors(link)
    for entry in evidence.values():
        if not (isinstance(entry,dict) and type(entry.get("pdf_page")) is int
                and 0 < entry["pdf_page"] <= paper["page_count"]
                and isinstance(entry.get("quote"),str) and 8 <= len(entry["quote"].strip()) <= 450):
            return False
    return evidence["endpoint"]["pdf_page"] in {e.get("pdf_page") for e in claim.get("evidence",[]) if isinstance(e,dict)}


@lru_cache(maxsize=512)
def _physical_hash(path, size, mtime_ns, ctime_ns):
    with open(path,"rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def physical_matches(paper):
    try:
        path = Path(paper["local_path"])
        stat = path.stat()
        return _physical_hash(str(path.resolve()),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns) == paper["sha256"]
    except (OSError,KeyError,TypeError):
        return False


@lru_cache(maxsize=12)
def _selected_paths(release_path, release_stamp, catalog_path, catalog_stamp):
    release = json.loads(Path(release_path).read_bytes())
    owners = {d["doc_id"] for d in release["decisions"] if d.get("verdict")=="accept"}
    return tuple(sorted((p["doc_id"],p["local_path"]) for p in json.loads(Path(catalog_path).read_bytes())
                        if p.get("doc_id") in owners))


def physical_revision(pilot, release_path):
    """Invalidate a cached HTTP projection when a bound PDF changes ahead of its catalog."""
    def stamp(path):
        stat = Path(path).stat()
        return stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns
    try:
        catalog = Path(pilot)/"catalog/papers.json"
        selected = _selected_paths(str(release_path), stamp(release_path), str(catalog), stamp(catalog))
        states = []
        for ident,path in selected:
            try:
                actual = _physical_hash(str(Path(path).resolve()),*stamp(path))
            except OSError:
                actual = "missing"
            states.append((ident,actual))
        return digest(states)
    except (OSError,KeyError,ValueError,TypeError,AttributeError):
        return "unavailable"


def checked_accept(decision, link, envelope, candidate_sha):
    if decision.get("verdict")!="accept":
        return False
    review = decision.get("review")
    notes = decision.get("anchor_checks")
    evidence = anchors(link)
    if not (isinstance(review,dict) and isinstance(notes,dict) and review.get("model")
            and HEX.fullmatch(str(review.get("packet_sha256"))) and HEX.fullmatch(str(review.get("reply_sha256")))
            and decision.get("reason") and decision.get("input_digest")==digest(link)
            and decision.get("candidate_sha256")==candidate_sha
            and decision.get("source_sha256")==envelope.get("source_sha256")
            and decision.get("claims_digest")==envelope.get("claims_digest")):
        return False
    for key in ROLES:
        note = notes.get(key)
        if not (isinstance(note,dict) and note.get("pdf_page")==evidence[key]["pdf_page"]
                and type(note.get("pdf_page")) is int and isinstance(note.get("basis"),str) and note["basis"].strip()):
            return False
    return True


def candidate_index(candidate):
    """Document and coordinate uniqueness are part of the frozen comparison."""
    documents, entries = {}, {}
    if candidate.get("schema")!=1 or not HEX.fullmatch(str(candidate.get("comparison_snapshot"))):
        raise ValueError("Unrecognized specimen candidate signature")
    for doc in candidate["documents"]:
        ident = doc["doc_id"]
        if ident in documents or not isinstance(doc["links"],list):
            raise ValueError("Duplicate or malformed specimen document")
        documents[ident] = doc
        for link in doc["links"]:
            key = pair(link)
            if key in entries or link.get("doc_id")!=ident:
                raise ValueError("Ambiguous specimen coordinate")
            entries[key] = link
    return documents, entries


def apply_sample_bindings(pilot, papers, claims, links, withheld, *, kind_reviews=None, release_path=None):
    """Read one atomic release; the embedded exact candidate and all decisions must match."""
    path = Path(release_path) if release_path else Path(pilot)/SIDECAR
    audit = dict(version=VERSION, status="absent", accepted=0, scope="current_document_selection")
    if not path.is_file():
        return list(links),audit
    try:
        release = json.loads(path.read_bytes())
        if (release.get("schema")!=SCHEMA or release.get("version")!=VERSION or release.get("axes_digest")!=AXES_DIGEST
                or release.get("base_files")!=base_files(pilot)):
            raise ValueError("Stale base")
        control = release.pop("release_digest")
        if digest(release)!=control:
            raise ValueError("Changed signed decision content")
        raw = release["candidate_utf8"].encode("utf-8")
        candidate_sha = sha(raw)
        candidate = json.loads(raw)
        if (candidate_sha!=release["candidate_sha256"] or release["comparison_snapshot"]!=candidate["comparison_snapshot"]):
            raise ValueError("Changed candidate")
        docs, entries = candidate_index(candidate)
        decisions = {pair(d):d for d in release["decisions"]}
        if (len(decisions)!=len(release["decisions"]) or set(decisions)!=set(entries)
                or any(d.get("verdict") not in VERDICTS for d in decisions.values())):
            raise ValueError("Incomplete adjudication")
        for key,d in decisions.items():
            link = entries[key]
            owner = docs[link["doc_id"]]
            if (d.get("doc_id")!=link["doc_id"] or d.get("candidate_sha256")!=candidate_sha
                    or d.get("input_digest")!=digest(link) or d.get("source_sha256")!=owner["source_sha256"]
                    or d.get("claims_digest")!=owner["claims_digest"]):
                raise ValueError("Review ownership mismatch")
        scope = {p["doc_id"]:p for p in papers}
        grouped = defaultdict(list)
        for c in claims:
            grouped[c["doc_id"]].append(c)
        by_claim = {c["claim_id"]:c for c in claims}
        locked = set(withheld)
        for group in mapped_candidates(papers,claims,links,withheld).values():
            locked.update(map(pair,group))
        valid_docs = {}
        additions = []
        counts = Counter()
        for key, link in entries.items():
            ident = link["doc_id"]
            if ident not in scope:
                continue
            d = decisions[key]
            counts[d["verdict"]] += 1
            if d["verdict"]!="accept":
                continue
            doc, paper = docs[ident],scope[ident]
            if ident not in valid_docs:
                valid_docs[ident] = (paper.get("sha256")==doc.get("source_sha256")
                                     and claims_digest(grouped[ident])==doc.get("claims_digest") and physical_matches(paper))
            if (not valid_docs[ident] or key in locked or not eligible(link,paper,by_claim.get(link["claim_id"]),kind_reviews)
                    or not checked_accept(d,link,doc,candidate_sha)):
                counts["suppressed"] += 1
                continue
            locked.add(key)
            additions.append(dict(link, sample_review={"reason":d["reason"],"anchor_checks":d["anchor_checks"],
                                        "model":d["review"]["model"],"packet_sha256":d["review"]["packet_sha256"]}))
        audit.update(status="current", accepted=len(additions), decisions=dict(counts),
                     candidate_sha256=candidate_sha, release_digest=control)
        return [*links,*additions],audit
    except (ValueError,TypeError,KeyError,OSError,AttributeError):
        return list(links),dict(audit,status="invalid_or_stale")
