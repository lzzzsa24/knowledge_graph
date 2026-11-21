from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
import time
import uuid
from functools import lru_cache
from pathlib import Path
from kg_pilot.pipeline import PILOT, jsonl, read_json, write_json
from kg_pilot.bilingual_retrieval import concept_coverage, english_terms, english_tokens, is_crosslingual_query, plan_query
from kg_pilot.effective_evidence import load_effective_evidence

COLLECTION = "pilot_evidence_bge_m3"
BILINGUAL_CANDIDATE_DEPTH = 100
STOP = set("what which how in the for and with of a to is was were at from did under are does this that after before reported paper study using used compare between than into".split())


def expression(query):
    terms = [x for x in re.findall(r"[\w]+", query.lower()) if x not in STOP and len(x) > 1]
    return " OR ".join('"' + x + '"' for x in dict.fromkeys(terms))


def lexical(query, limit=20):
    expr = expression(query)
    if not expr:
        return []
    with closing(sqlite3.connect(PILOT / "indexes" / "pilot.sqlite")) as db:
        rows = db.execute("SELECT f.evidence_id,f.doc_id,e.pdf_page,e.text,bm25(evidence_fts) FROM evidence_fts f JOIN evidence e ON e.evidence_id=f.evidence_id WHERE evidence_fts MATCH ? ORDER BY bm25(evidence_fts) LIMIT ?", (expr, limit)).fetchall()
    view = load_effective_evidence(PILOT)
    return [view.source(dict(zip(["evidence_id", "doc_id", "pdf_page", "text", "score"], row), method="bm25"))
            for row in rows]


def build_claim_index():
    with closing(sqlite3.connect(PILOT / "indexes" / "pilot.sqlite")) as db:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS claim_fts USING fts5(claim_id UNINDEXED,text,tokenize='porter unicode61')")
        db.execute("DELETE FROM claim_fts")
        for c in jsonl(PILOT / "annotations" / "claims.jsonl"):
            text = " ".join([c["statement"], c["material_system"], c["stage"].replace("_", " "),
                             c["quantity"].replace("_", " "), json.dumps(c["conditions"], ensure_ascii=False),
                             " ".join(e["quote"] for e in c["evidence"])])
            db.execute("INSERT INTO claim_fts VALUES (?,?)", (c["claim_id"], text))
        db.commit()


def graph_evidence(query, limit=20, stage=None, material=None):
    expr = expression(query)
    if not expr:
        return []
    view = load_effective_evidence(PILOT)
    with closing(sqlite3.connect(PILOT / "indexes" / "pilot.sqlite")) as db:
        rows = db.execute("SELECT c.payload,bm25(claim_fts) FROM claim_fts f JOIN claims c ON c.claim_id=f.claim_id WHERE claim_fts MATCH ? ORDER BY bm25(claim_fts) LIMIT ?", (expr, limit)).fetchall()
        results = []
        for payload, score in rows:
            claim = view.claim(json.loads(payload))
            if stage and claim["stage"] != stage:
                continue
            if material and material.casefold() not in claim["material_system"].casefold():
                continue
            # Traverse persisted graph edges to source evidence. Claims without supported_by are excluded.
            linked = {r[0] for r in db.execute("SELECT target FROM graph_edges WHERE source=? AND kind='supported_by'", (claim["claim_id"],))}
            for i, ev in enumerate(claim["evidence"]):
                eid = ev.get("evidence_id") or f"{claim['doc_id']}:p{ev['pdf_page']}"
                if eid not in linked:
                    continue
                results.append(view.source({"evidence_id": eid, "doc_id": claim["doc_id"], "pdf_page": ev["pdf_page"],
                                "text": ev["quote"], "score": score, "method": "claim_graph",
                                "claim_id": claim["claim_id"], "stage": claim["stage"],
                                "material_system": claim["material_system"], "conditions": claim["conditions"]}))
    return results


def fuse(*rankings, limit=20):
    scores, hits = {}, {}
    for ranking in rankings:
        seen = set()
        for rank, hit in enumerate(ranking, start=1):
            key = hit["evidence_id"]
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
            hits.setdefault(key, hit)
    return [{**hits[k], "rrf_score": scores[k]} for k in sorted(scores, key=lambda k: (-scores[k], k))[:limit]]


def build_vector_index():
    import numpy as np
    from qdrant_client import QdrantClient, models
    data_dir = PILOT / "benchmarks" / "embedding"
    vectors = np.load(data_dir / "all_vectors.npy")
    records = read_json(data_dir / "all_records.json")
    client = QdrantClient(path=str(PILOT / "indexes" / "qdrant"))
    started = time.perf_counter()
    if not client.collection_exists(COLLECTION):
        client.create_collection(COLLECTION, vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE))
    points, seen = [], set()
    for record, vector in zip(records, vectors, strict=True):
        vid = str(uuid.uuid5(uuid.NAMESPACE_URL, record["vector_id"]))
        if vid in seen:
            continue
        seen.add(vid)
        points.append(models.PointStruct(id=vid, vector=vector.tolist(), payload=record))
        if len(points) == 128:
            client.upsert(COLLECTION, points=points)
            points = []
    if points:
        client.upsert(COLLECTION, points=points)
    existing_ids, offset = set(), None
    while True:
        stored, offset = client.scroll(COLLECTION, limit=256, offset=offset, with_payload=False, with_vectors=False)
        existing_ids.update(str(p.id) for p in stored)
        if offset is None:
            break
    stale = sorted(existing_ids - seen)
    if stale:
        client.delete(COLLECTION, points_selector=models.PointIdsList(points=stale))
    count = client.count(COLLECTION, exact=True).count
    client.close()
    report = {"backend": "qdrant-client local persistent mode", "points": count,
              "dimensions": 1024, "build_seconds": round(time.perf_counter()-started, 3),
              "duplicate_segments_deduplicated": len(records)-len(seen),
              "stale_points_removed": len(stale),
              "integration_note": "direct project adapter; not LightRAG's server-only Qdrant adapter"}
    write_json(PILOT / "reports" / "qdrant_build.json", report)
    print(json.dumps(report))


@lru_cache(maxsize=1)
def retrieval_model():
    """Load one local BGE-M3 model per process for both search and QA."""
    os.environ["HF_HOME"] = str(PILOT / "model_cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from huggingface_hub import file_download
    file_download.are_symlinks_supported = lambda cache_dir=None: False
    import torch
    torch.set_num_threads(2)
    from sentence_transformers import SentenceTransformer
    config_path = PILOT / "model_config.json"
    revision = (read_json(config_path) if config_path.is_file() else {}).get(
        "revision", "5617a9f61b028005a4858fdac845db406aefb181")
    snapshot = PILOT / "model_cache/models--BAAI--bge-m3/snapshots" / revision
    model = SentenceTransformer(str(snapshot) if snapshot.is_dir() else "BAAI/bge-m3", revision=revision, cache_folder=str(PILOT / "model_cache"),
                                device="cpu", local_files_only=True)
    model.max_seq_length = 512
    return model


def semantic(query, model, limit=20):
    from qdrant_client import QdrantClient
    vector = model.encode([query], normalize_embeddings=True)[0]
    view = load_effective_evidence(PILOT)
    client = QdrantClient(path=str(PILOT / "indexes" / "qdrant"))
    try:
        points = client.query_points(COLLECTION, query=vector.tolist(), limit=limit).points
        results = []
        with closing(sqlite3.connect(PILOT / "indexes" / "pilot.sqlite")) as db:
            for point in points:
                payload = point.payload
                source = db.execute("SELECT doc_id,pdf_page,text FROM evidence WHERE evidence_id=?",
                                    (payload["evidence_id"],)).fetchone()
                if source is None:
                    raise ValueError(f"Vector references missing evidence: {payload['evidence_id']}")
                record = {k: v for k, v in payload.items() if k != "embedding_text"}
                results.append(view.source({**record, "doc_id": source[0], "pdf_page": source[1],
                                            "text": source[2], "score": point.score, "method": "dense"}))
        return results
    finally:
        client.close()


def _fts_expression(phrases):
    """Only quoted literals enter FTS syntax; queries never supply operators."""
    return " OR ".join('"' + " ".join(english_tokens(p)).replace('"', '""') + '"'
                       for p in dict.fromkeys(phrases) if english_terms(p))


def _fts_rows(db, expr, limit, doc_ids=()):
    if not expr:
        return []
    params = [expr]
    scope = ""
    if doc_ids:
        scope = " AND f.doc_id IN (" + ",".join("?" for _ in doc_ids) + ")"
        params.extend(sorted(doc_ids))
    params.append(limit)
    try:
        rows = db.execute(
            "SELECT f.evidence_id,f.doc_id,e.pdf_page,e.text,bm25(evidence_fts) "
            "FROM evidence_fts f JOIN evidence e ON e.evidence_id=f.evidence_id "
            "WHERE evidence_fts MATCH ?" + scope + " ORDER BY bm25(evidence_fts),f.evidence_id LIMIT ?", params).fetchall()
    except sqlite3.OperationalError as error:
        # Small read-only/offline corpora can have evidence without an FTS
        # table. Do not hide malformed SQL or corrupt indexes as empty results.
        if "no such table: evidence_fts" in str(error):
            return []
        raise
    return [dict(zip(("evidence_id", "doc_id", "pdf_page", "text", "score"), row), method="bm25") for row in rows]


def retrieve(query, model=None, limit=20, *, dense_results=None, pilot=None):
    """Shared local bilingual retrieval; at most one original dense query.

    Chinese/mixed queries fuse dense and translated lexical candidates, then
    apply bounded, explainable concept/identity boosts. English calls retain
    the original semantic order (or lexical mode when no model is supplied).
    Dense failures deliberately propagate so callers can disclose degradation
    before retrying this helper with ``model=None``. No provider is called and
    this function never writes corpus, annotation, or index files.
    """
    if limit <= 0:
        return []
    crosslingual = is_crosslingual_query(query)
    if dense_results is None and model is not None:
        depth = max(BILINGUAL_CANDIDATE_DEPTH if crosslingual else 30, limit)
        dense_results = semantic(query, model, depth)
    base = Path(PILOT if pilot is None else pilot)
    if not crosslingual:
        view = load_effective_evidence(base)
        hits = list(dense_results) if dense_results is not None else lexical(query, limit)
        return [view.source({**hit, "retrieval_reasons": list(hit.get("retrieval_reasons", []))})
                for hit in hits[:limit]]
    papers_path, claims_path = base / "catalog/papers.json", base / "annotations/claims.jsonl"
    papers = read_json(papers_path) if papers_path.is_file() else []
    paper_map = papers if isinstance(papers, dict) else {p["doc_id"]: p for p in papers}
    raw_claims = jsonl(claims_path)
    view = load_effective_evidence(base, papers=paper_map.values(), claims=raw_claims)
    claims = [view.claim(claim) for claim in raw_claims]
    plan = plan_query(query, papers, claims)
    uri = (base / "indexes/pilot.sqlite").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        expanded = _fts_expression(plan.phrases)
        raw = _fts_rows(db, expression(plan.intent), max(100, limit * 2))
        broad = _fts_rows(db, expanded, max(200, limit * 4))
        # A conjunction finds focused passages buried below broad OR matches;
        # all lexical variants still contribute just one ranking to RRF.
        strong = [c for c in plan.concepts if c.weight >= 1.3][:3]
        focused_expr = " AND ".join("(" + _fts_expression(c.phrases) + ")" for c in strong)
        focused = _fts_rows(db, focused_expr, max(100, limit * 2)) if len(strong) > 1 else []
        scoped_docs = set(plan.entity_docs)
        if plan.identity_phrases:
            # Older catalog records may omit authors. A name/identifier already
            # known from the catalog can discover their first pages without
            # pretending the whole corpus has complete author metadata.
            for hit in _fts_rows(db, _fts_expression(plan.identity_phrases), 100):
                source_tokens = " " + " ".join(english_tokens(hit["text"])) + " "
                name_match = not plan.identity_names or any(" " + name + " " in source_tokens for name in plan.identity_names)
                if hit["pdf_page"] == 1 and name_match:
                    scoped_docs.add(hit["doc_id"])
        scoped = _fts_rows(db, expanded, max(100, limit * 2), scoped_docs) if scoped_docs else []
        year_docs = {doc_id for doc_id in scoped_docs
                     if str(paper_map.get(doc_id, {}).get("year", "")) in {str(year) for year in plan.publication_years}}
        lexical_hits = {}
        for hit in [*focused, *scoped, *raw, *broad]:
            lexical_hits.setdefault(hit["evidence_id"], hit)
        coverages = {}
        for eid, hit in lexical_hits.items():
            coverages[eid] = concept_coverage(plan, hit["text"])
        # Score each concept once, regardless of synonym count or channels.
        lexical_rank = sorted(lexical_hits.values(), key=lambda hit: (
            -(coverages[hit["evidence_id"]][0] + .18 * (hit["doc_id"] in scoped_docs)
              + .15 * (hit["doc_id"] in year_docs)),
            hit["score"], hit["evidence_id"]))
        # Rehydrate IDs in one SQLite read, not from vector embedding snippets.
        dense = list(dense_results or [])
        ids = list(dict.fromkeys(h["evidence_id"] for h in dense))
        stored = {}
        if ids:
            rows = db.execute("SELECT evidence_id,doc_id,pdf_page,text FROM evidence WHERE evidence_id IN ("
                              + ",".join("?" for _ in ids) + ")", ids).fetchall()
            stored = {row[0]: dict(zip(("evidence_id", "doc_id", "pdf_page", "text"), row)) for row in rows}
        dense = [{**hit, **stored[hit["evidence_id"]]} for hit in dense if hit["evidence_id"] in stored]
    # A single lexical vote avoids rewarding repetitions of near-identical
    # expansion queries. Dense and lexical channels have equal RRF weight.
    merged = fuse(dense, lexical_rank, limit=len(dense) + len(lexical_rank))
    channel_scores = []
    for ranking in (dense, lexical_rank):
        scores = {}
        for rank, candidate in enumerate(ranking, 1):
            scores.setdefault(candidate["evidence_id"], 1 / (60 + rank))
        channel_scores.append(scores)
    for hit in merged:
        eid = hit["evidence_id"]
        coverage, terms = coverages.get(eid) or concept_coverage(plan, hit["text"])
        identity = hit["doc_id"] in scoped_docs
        # Agreement on a broad subject cannot double the rank contribution:
        # cap the secondary-channel bonus, keeping strong single-channel
        # technical matches competitive. Raw RRF remains separately visible.
        scores = [channel.get(eid, 0) for channel in channel_scores]
        consensus = max(scores) + .25 * min(scores)
        exact_title = hit["doc_id"] in plan.title_docs
        identifier = hit["doc_id"] in plan.identifier_docs
        year_match = hit["doc_id"] in year_docs
        hit["retrieval_score"] = (consensus + .022 * coverage + .006 * identity + .014 * exact_title
                                  + .004 * identifier + .008 * year_match)
        reasons = list(hit.get("retrieval_reasons", []))
        if eid in lexical_hits:
            reasons.append({"method": "bilingual_fts", "terms": terms})
        if identity:
            reasons.append({"method": "document_scoped_fts", "exact_title": hit["doc_id"] in plan.title_docs})
        if year_match:
            reasons.append({"method": "catalog_publication_year", "year": paper_map[hit["doc_id"]]["year"]})
        if terms:
            reasons.append({"method": "domain_query_rerank", "terms": terms, "coverage": round(coverage, 4)})
        hit["retrieval_reasons"] = reasons
    ordered = sorted(merged, key=lambda hit: (-hit["retrieval_score"], hit["evidence_id"]))
    return [view.source(hit) for hit in ordered[:limit]]


def evaluate(language="en"):
    import torch
    torch.set_num_threads(2)
    from huggingface_hub import file_download
    file_download.are_symlinks_supported = lambda cache_dir=None: False
    from sentence_transformers import SentenceTransformer
    from qdrant_client import QdrantClient
    question_path = PILOT / "evaluation" / "retrieval_questions.json"
    questions = read_json(question_path)
    if isinstance(questions, dict):
        questions = questions.get("questions", questions.get("items", []))
    build_claim_index()
    revision = read_json(PILOT / "model_config.json")["revision"]
    model = SentenceTransformer("BAAI/bge-m3", revision=revision, cache_folder=str(PILOT / "model_cache"), device="cpu")
    model.max_seq_length = 512
    query_field = "question_zh" if language == "zh" else "query_en"
    query_vectors = model.encode([q[query_field] for q in questions], normalize_embeddings=True, batch_size=4)
    client = QdrantClient(path=str(PILOT / "indexes" / "qdrant"))
    details = []
    for q, vector in zip(questions, query_vectors, strict=True):
        started = time.perf_counter()
        b = lexical(q[query_field], 30)
        raw = client.query_points(COLLECTION, query=vector.tolist(), limit=30).points
        d = [{**p.payload, "text": p.payload["embedding_text"], "score": p.score, "method": "dense"} for p in raw]
        g = graph_evidence(q[query_field], 15)
        rankings = {"bm25": b, "dense": d, "hybrid_text": fuse(b, d, limit=30),
                    "hybrid_with_claim_graph": fuse(b, d, g, limit=30)}
        expected = {(p["doc_id"], p["pdf_page"]) for p in q.get("expected_pdf_pages", [])}
        result = {"question": q, "latency_excluding_query_embedding_seconds": round(time.perf_counter()-started, 4), "methods": {}}
        for method, hits in rankings.items():
            # Page-level deduplication makes the retrieval budget comparable across segmentation schemes.
            pages, selected = set(), []
            for hit in hits:
                key = (hit["doc_id"], hit["pdf_page"])
                if key not in pages:
                    pages.add(key)
                    selected.append(hit)
                if len(selected) == 5:
                    break
            got = {(h["doc_id"], h["pdf_page"]) for h in selected}
            result["methods"][method] = {"top5": selected, "any_expected_page_hit": bool(expected & got) if q["answerable"] else None,
                                          "expected_page_recall": len(expected & got)/len(expected) if expected else None}
        details.append(result)
    client.close()
    answerable = [r for r in details if r["question"]["answerable"]]
    summary = {"questions_sha256": hashlib.sha256(question_path.read_bytes()).hexdigest(),
               "questions": len(details), "answerable": len(answerable), "insufficient_evidence": len(details)-len(answerable),
               "scope": "pilot relevance tests; not a human gold benchmark; no tuning on these retrieval results",
               "query_language": language, "keyword_cjk_segmentation": False,
               "generation_accuracy_measured": False, "rejection_accuracy_measured": False,
               "results": {m: {"page_hit_at_5": sum(r["methods"][m]["any_expected_page_hit"] for r in answerable)/len(answerable),
                                "mean_expected_page_recall_at_5": sum(r["methods"][m]["expected_page_recall"] for r in answerable)/len(answerable)}
                           for m in ("bm25", "dense", "hybrid_text", "hybrid_with_claim_graph")}}
    suffix = "_zh" if language == "zh" else ""
    write_json(PILOT / "evaluation" / f"retrieval_results{suffix}.json", details)
    write_json(PILOT / "reports" / f"retrieval_summary{suffix}.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    if sys.argv[1] == "evaluate":
        evaluate(sys.argv[2] if len(sys.argv) > 2 else "en")
    else:
        {"build": build_vector_index, "claims": build_claim_index}[sys.argv[1]]()
