"""Verify NumPy replay against frozen real Qdrant and/or served HTTP API.

Production files are read only. --baseline-engine backs up both SQLite stores
into a disposable independent snapshot before instantiating real QdrantClient.
--live-url calls only GET /api/search and writes evidence/ordering observations.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "pilot/reports/crosslingual_20261106"


def load(path):
    return json.loads(path.read_text("utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sqlite_backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)


def compare(expected, actual):
    expected_ids = [r["evidence_id"] for r in expected]
    actual_ids = [r["evidence_id"] for r in actual]
    return {"expected_count": len(expected), "actual_count": len(actual),
            "ordered_evidence_ids_match": expected_ids == actual_ids,
            "expected_evidence_ids": expected_ids, "actual_evidence_ids": actual_ids}


def baseline_engine():
    from evaluate_crosslingual_retrieval import assess
    baseline = load(OUT / "baseline.json")
    snapshot = ROOT / "tmp/crosslingual_qdrant_readonly_snapshot"
    origin = ROOT / "pilot"
    for rel in ["indexes/pilot.sqlite", "indexes/qdrant/collection/pilot_evidence_bge_m3/storage.sqlite"]:
        sqlite_backup(origin / rel, snapshot / rel)
    meta = snapshot / "indexes/qdrant/meta.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin / "indexes/qdrant/meta.json", meta)
    spec = importlib.util.spec_from_file_location("frozen_baseline", OUT / "baseline_retrieval_frozen.py")
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    frozen.PILOT = snapshot
    cache = dict(np.load(OUT / "query_vector_cache.npz"))
    class CachedRealEncoder:
        def encode(self, texts, **kwargs):
            return np.stack([cache[hashlib.sha256(q.encode("utf-8")).hexdigest()] for q in texts])
    rows = []
    for item in baseline["results"]:
        q = item["question"]
        actual = frozen.semantic(q["question_zh"], CachedRealEncoder(), baseline["method"]["retrieval_limit"])
        result = {"id": q["id"], **compare(item["returned_ranking"], actual)}
        expected_scores = [r["score"] for r in item["returned_ranking"]]
        result["scores_allclose"] = bool(np.allclose(expected_scores, [r["score"] for r in actual], atol=1e-6, rtol=1e-6))
        result["top5_labeled_metrics_match"] = all(
            assess(q, actual).get(level, {}).get(field) == item["metrics"].get(level, {}).get(field)
            for level in ("document", "page", "evidence") for field in ("hit_at_5", "recall_at_5"))
        rows.append(result)
        print(json.dumps({"baseline_engine": q["id"], "match": result["ordered_evidence_ids_match"]}), flush=True)
    report = {"created_utc": datetime.now(timezone.utc).isoformat(),
              "method": "Frozen old semantic with genuine QdrantClient on independently backed-up persisted SQLite stores; real cached BGE query vectors.",
              "all_ordered_evidence_ids_match": all(r["ordered_evidence_ids_match"] for r in rows),
              "all_scores_close": all(r["scores_allclose"] for r in rows), "results": rows}
    report["all_top5_labeled_metrics_match"] = all(r["top5_labeled_metrics_match"] for r in rows)
    report["tie_note"] = "Exact vector-score ties can swap equivalent order between NumPy stable argsort and Qdrant local argsort. Report exact-order mismatches separately; never claim all rows had byte-identical order."
    (OUT / "baseline_engine_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def live_api(url):
    import httpx
    from evaluate_crosslingual_retrieval import assess
    candidate = load(OUT / "candidate.json")
    for rel, expected_sha in candidate.get("candidate_source_sha256", {}).items():
        if sha(ROOT / rel) != expected_sha:
            raise ValueError("Candidate code changed since offline assessment; rerun candidate evaluation before API verification")
    with sqlite3.connect(f"file:{(ROOT / 'pilot/indexes/pilot.sqlite').as_posix()}?mode=ro", uri=True) as db:
        sources = {r[0]: {"doc_id": r[1], "pdf_page": r[2], "text": r[3]} for r in db.execute(
            "SELECT evidence_id,doc_id,pdf_page,text FROM evidence")}
    rows = []
    with httpx.Client(base_url=url, trust_env=False, timeout=240) as client:
        summary = client.get("/api/summary")
        summary.raise_for_status()
        if summary.json().get("papers") != 100:
            raise ValueError("Live API is not serving 100 documents")
        for item in candidate["results"]:
            q = item["question"]
            started = time.perf_counter()
            response = client.get("/api/search", params={"q": q["question_zh"], "mode": "semantic",
                "limit": candidate["method"]["retrieval_limit"]})
            response.raise_for_status()
            actual = response.json()
            actual_metrics = assess(q, actual)
            metrics_match = all(actual_metrics.get(level, {}).get(field) == item["metrics"].get(level, {}).get(field)
                for level in ("document", "page", "evidence") for field in ("hit_at_5", "recall_at_5"))
            source_match = all(row["evidence_id"] in sources and all(row.get(k) == sources[row["evidence_id"]][k]
                for k in ("doc_id", "pdf_page", "text")) for row in actual)
            rows.append({"id": q["id"], "seconds": round(time.perf_counter()-started, 3),
                         "actual_metrics": actual_metrics, "top5_labeled_metrics_match": metrics_match,
                         "source_identity_and_text_match": source_match,
                         **compare(item["returned_ranking"], actual)})
            print(json.dumps({"live_api": q["id"], "match": rows[-1]["ordered_evidence_ids_match"]}), flush=True)
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "base_url": url,
              "method": "Actual read-only GET /api/search(mode=semantic), matching limit30; no QA generation or chat write.",
              "candidate_report_sha256": sha(OUT / "candidate.json"),
              "all_ordered_evidence_ids_match": all(r["ordered_evidence_ids_match"] for r in rows),
              "all_top5_labeled_metrics_match": all(r["top5_labeled_metrics_match"] for r in rows),
              "source_identity_and_text_all_match": all(r["source_identity_and_text_match"] for r in rows),
              "results": rows}
    (OUT / "live_api_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-engine", action="store_true")
    parser.add_argument("--live-url")
    args = parser.parse_args()
    results = []
    if args.baseline_engine:
        results.append(baseline_engine())
    if args.live_url:
        results.append(live_api(args.live_url))
    if not results:
        parser.error("Provide --baseline-engine and/or --live-url")
    if any(not r.get("all_top5_labeled_metrics_match", r["all_ordered_evidence_ids_match"])
        or not r.get("source_identity_and_text_all_match", True) for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
