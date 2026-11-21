"""Reproducible, read-only retrieval comparison on the real published corpus.

The baseline is the frozen pre-change semantic implementation's cosine ranking,
computed from the *real persisted* BGE-M3 vectors with NumPy. No production
Qdrant client is opened. Candidate retrieval accepts the same dense ranking,
which avoids query re-encoding and local-Qdrant lock contention. This measures
retrieval only, not answer generation or correct abstention.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pickle
import sqlite3
import sys
import time
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PILOT = ROOT / "pilot"
OUT = PILOT / "reports/crosslingual_20261106"
GAO_QUERY = "Gao 的《Low Temperature Hybrid Bonding for Die to Wafer Stacking Applications》中，测试芯片的厚度、TSV 直径和 TSV 间距分别是多少？请注明单位和原文页码。"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def questions():
    legacy = json.loads((PILOT / "evaluation/retrieval_questions.json").read_text("utf-8"))
    for q in legacy:
        q["suite"] = "legacy_16"
    gao = {"id": "gao_historical", "suite": "historical_case", "question_zh": GAO_QUERY,
           "answerable": True, "expected_doc_ids": ["reliability_gao_d2w_2021"],
           "expected_pdf_pages": [{"doc_id": "reliability_gao_d2w_2021", "pdf_page": 2}],
           "expected_evidence_ids": ["reliability_gao_d2w_2021:p2:b8"],
           "required_conditions": ["8 mm × 12 mm × 50 μm test die: thickness 50 μm; TSV diameter 5 μm; TSV pitch 35 μm; physical PDF page 2."]}
    added = OUT / "independent_questions.json"
    return legacy + [gao] + (json.loads(added.read_text("utf-8")) if added.is_file() else [])


def load_corpus(verify=False):
    records_path = PILOT / "benchmarks/embedding/all_records.json"
    vectors_path = PILOT / "benchmarks/embedding/all_vectors.npy"
    records = json.loads(records_path.read_text("utf-8"))
    vectors = np.asarray(np.load(vectors_path, mmap_mode="r"), dtype=np.float32)
    if len(records) != len(vectors):
        raise ValueError("Record/vector count mismatch")
    # build_vector_index upserts duplicate vector IDs, so only the final row of
    # each UUID survives. Distinct segments remain distinct exactly as Qdrant.
    last = {r["vector_id"]: i for i, r in enumerate(records)}
    indices = list(last.values())
    records = [records[i] for i in indices]
    vectors = vectors[indices]
    with sqlite3.connect(f"file:{(PILOT / 'indexes/pilot.sqlite').as_posix()}?mode=ro", uri=True) as db:
        evidence = {row[0]: {"evidence_id": row[0], "doc_id": row[1], "pdf_page": row[2], "text": row[3]}
                    for row in db.execute("SELECT evidence_id,doc_id,pdf_page,text FROM evidence")}
        docs = db.execute("SELECT count(*) FROM papers").fetchone()[0]
    missing = [r["evidence_id"] for r in records if r["evidence_id"] not in evidence]
    if missing:
        raise ValueError(f"Missing evidence: {missing[:3]}")
    provenance = {"published_documents": docs, "vector_documents": len({r["doc_id"] for r in records}),
                  "vector_points": len(records), "evidence_elements": len(evidence),
                  "records_sha256": digest(records_path), "vectors_sha256": digest(vectors_path),
                  "sqlite_sha256": digest(PILOT / "indexes/pilot.sqlite"),
                  "frozen_baseline_source_sha256": digest(OUT / "baseline_retrieval_frozen.py")}
    if docs != 100 or provenance["vector_documents"] != 100:
        raise ValueError("This evaluation requires all 100 real published documents")
    if verify:
        # Trusted project-owned pickle objects, read via sqlite mode=ro.
        storage = PILOT / "indexes/qdrant/collection/pilot_evidence_bge_m3/storage.sqlite"
        with sqlite3.connect(f"file:{storage.as_posix()}?mode=ro", uri=True) as db:
            stored = dict(db.execute("SELECT id,point FROM points"))
        checks, max_error, payload_mismatches = [], 0.0, []
        for r, vector in zip(records, vectors, strict=True):
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, r["vector_id"]))
            key = base64.b64encode(pickle.dumps(pid, protocol=4)).decode("utf-8")
            point = pickle.loads(stored[key])
            actual = np.asarray(point.vector, dtype=np.float32)
            max_error = max(max_error, float(np.max(np.abs(vector-actual))))
            checks.append(bool(np.allclose(vector, actual, rtol=1e-6, atol=1e-7)))
            if point.payload != r:
                payload_mismatches.append(r["vector_id"])
        provenance["persisted_qdrant_readonly_verification"] = {
            "points": len(stored), "all_payloads_match": not payload_mismatches,
            "all_vectors_match": all(checks), "max_vector_abs_error": max_error,
            "payload_mismatches": payload_mismatches,
            "method": "Read Qdrant trusted local points with SQLite mode=ro; no Qdrant client or lock."}
        if len(stored) != len(records) or not all(checks) or payload_mismatches:
            raise ValueError("Cached matrix and persisted Qdrant differ")
    # Qdrant's local cosine normalizes vectors, including originally normalized
    # BGE outputs. Apply the same normalization for numerical comparability.
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    return records, vectors, evidence, provenance


def encode_queries(items):
    from kg_pilot.qa import retrieval_model
    cache = OUT / "query_vector_cache.npz"
    meta_path = OUT / "query_vector_cache_meta.json"
    config = json.loads((PILOT / "model_config.json").read_text("utf-8"))
    if cache.is_file():
        if not meta_path.is_file() or json.loads(meta_path.read_text("utf-8")).get("model_config") != config:
            raise ValueError("Query vector cache lacks matching model provenance; preserve the old cache and regenerate for the configured model")
    old = dict(np.load(cache)) if cache.is_file() else {}
    keys = [hashlib.sha256(q["question_zh"].encode("utf-8")).hexdigest() for q in items]
    missing = [(key, q["question_zh"]) for key, q in zip(keys, items, strict=True) if key not in old]
    if missing:
        started = time.perf_counter()
        vectors = retrieval_model().encode([text for _, text in missing], batch_size=4,
                                           normalize_embeddings=True, show_progress_bar=False)
        for (key, _), vector in zip(missing, vectors, strict=True):
            old[key] = vector
        np.savez(cache, **old)
        write(meta_path, {"model_config": config, "queries": len(old),
                         "encoding": "Local BGE-M3, CPU, max_seq_length=512, normalize_embeddings=True, batch_size=4",
                         "cache_key": "SHA256 of original UTF-8 query, with exact matching model_config required"})
        print(json.dumps({"encoded_queries": len(missing), "seconds": round(time.perf_counter()-started, 2)}), flush=True)
    return [old[key] for key in keys]


def unique(items, key, budget=5):
    seen, out = set(), []
    for item in items:
        value = key(item)
        if value not in seen:
            seen.add(value)
            out.append(item)
        if len(out) == budget:
            break
    return out


def assess(q, rows):
    if not q["answerable"]:
        return {"answerable": False, "abstention_evaluated": False,
                "note": "No answerable gold evidence: excluded from all positive hit/recall denominators."}
    specs = {
        "document": (lambda r: r["doc_id"], set(q.get("expected_doc_ids", []))),
        "page": (lambda r: (r["doc_id"], r["pdf_page"]),
                 {(r["doc_id"], r["pdf_page"]) for r in q.get("expected_pdf_pages", [])}),
        "evidence": (lambda r: r["evidence_id"], set(q.get("expected_evidence_ids", []))),
    }
    result = {}
    for level, (key, gold) in specs.items():
        if not gold:
            result[level] = {"labeled": False}
            continue
        selected = unique(rows, key)
        got = {key(r) for r in selected}
        # Rank is on the level-deduplicated returned ranking; raw segment rank
        # remains available in raw_top50 for exact historical reproduction.
        all_unique = unique(rows, key, len(rows)+1)
        ranks = [i for i, row in enumerate(all_unique, 1) if key(row) in gold]
        result[level] = {"labeled": True, "hit_at_5": bool(gold & got),
                         "recall_at_5": len(gold & got)/len(gold),
                         "first_expected_rank": min(ranks) if ranks else None,
                         "selected_top5": [row["evidence_id"] for row in selected]}
    return result


def summarize(rows):
    summary = {}
    for suite in ["all", "legacy_16", "historical_case", "independent", "negative_controls"]:
        subset = rows if suite == "all" else [r for r in rows if r["question"]["suite"] == suite]
        if not subset:
            continue
        item = {"questions": len(subset), "answerable": sum(r["question"]["answerable"] for r in subset),
                "insufficient": sum(not r["question"]["answerable"] for r in subset), "levels": {}}
        for level in ["document", "page", "evidence"]:
            labeled = [r["metrics"][level] for r in subset if r["question"]["answerable"]
                       and r["metrics"].get(level, {}).get("labeled")]
            if labeled:
                item["levels"][level] = {"denominator": len(labeled),
                    "hit_at_5": sum(r["hit_at_5"] for r in labeled)/len(labeled),
                    "mean_recall_at_5": sum(r["recall_at_5"] for r in labeled)/len(labeled)}
        summary[suite] = item
    return summary


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["baseline", "candidate"], default="baseline")
    parser.add_argument("--verify-persisted", action="store_true")
    parser.add_argument("--limit", type=int, default=30, help="Match QA's 30-point original dense budget.")
    parser.add_argument("--dense-limit", type=int, default=100, help="Candidate dense shortlist; match current runtime budget.")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    items = questions()
    write(OUT / "questions_resolved.json", items)
    records, vectors, evidence, provenance = load_corpus(args.verify_persisted)
    queries = encode_queries(items)
    details = []
    if args.phase == "candidate":
        from kg_pilot import retrieval
    for q, vector in zip(items, queries, strict=True):
        vector = np.asarray(vector, dtype=np.float32)
        vector = vector / np.linalg.norm(vector)
        scores = vectors @ vector
        indices = np.argsort(-scores, kind="stable")[:200]
        dense = [{**records[i], **evidence[records[i]["evidence_id"]],
                  "score": float(scores[i]), "method": "dense"} for i in indices]
        rows = dense[:args.limit] if args.phase == "baseline" else retrieval.retrieve(
            q["question_zh"], limit=args.limit, dense_results=dense[:max(args.dense_limit, args.limit)])
        details.append({"question": q, "metrics": assess(q, rows),
                        "returned_ranking": [{k: r.get(k) for k in ["evidence_id", "doc_id", "pdf_page", "score", "method"]}
                                      for r in rows],
                        "exact_gold_dense_segment_ranks": {eid: next((rank for rank, i in enumerate(
                            np.argsort(-scores, kind="stable"), 1) if records[i]["evidence_id"] == eid), None)
                            for eid in q.get("expected_evidence_ids", [])}})
    report = {"schema_version": "crosslingual-real-corpus-retrieval-v1", "phase": args.phase,
              "created_utc": datetime.now(timezone.utc).isoformat(), "corpus": provenance,
              "questions_sha256": digest(OUT / "questions_resolved.json"),
              "query_vectors_sha256": digest(OUT / "query_vector_cache.npz"),
              "query_vector_metadata_sha256": digest(OUT / "query_vector_cache_meta.json"),
              "model_config": json.loads((PILOT / "model_config.json").read_text("utf-8")),
              "candidate_source_sha256": {str(path.relative_to(ROOT)): digest(path) for path in [
                  ROOT / "src/kg_pilot/retrieval.py", ROOT / "src/kg_pilot/bilingual_retrieval.py"] if path.is_file()},
              "method": {"baseline": "Frozen original semantic(): original Chinese query BGE-M3 cosine over real corpus points.",
                         "candidate": "Shared retrieval.retrieve with original-query dense scores injected; no candidate LLM/API.",
                         "top5_budget": "Deduplicate by scored level: document, physical PDF page, evidence element.",
                         "labels": "Legacy file has page/document labels only; exact evidence labels are selected, grounded anchors, not an exhaustive annotation of all relevant source passages.",
                         "retrieval_limit": args.limit,
                         "dense_shortlist_limit": args.limit if args.phase == "baseline" else max(args.dense_limit, args.limit),
                         "generation_accuracy_measured": False, "rejection_accuracy_measured": False,
                         "no_production_mutation": True},
              "summary": summarize(details), "results": details}
    write(OUT / f"{args.phase}.json", report)
    print(json.dumps({"phase": args.phase, "corpus": provenance, "summary": report["summary"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
