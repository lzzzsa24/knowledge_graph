"""Publish an already reviewed campaign batch with real cached BGE vectors."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.expansion_embeddings import CachedEmbedder
from kg_pilot import ingestion as intake
from kg_pilot.ingestion_publish import publish_batch
from kg_pilot.pipeline import PILOT, read_json


def ensure_latest_agent_replies(path):
    """Do not publish a ready checkpoint superseded by an agent correction."""
    for cache in path.glob("documents/*/chunk_*/*.json"):
        if cache.name not in {"extract.json", "review.json"}:
            continue
        meta = read_json(cache).get("output", {}).get("_result_meta", {})
        if meta.get("execution") != "active_codex_agent":
            continue
        reply = cache.with_name(cache.stem + ".agent_response.json")
        if not reply.exists() or hashlib.sha256(reply.read_bytes()).hexdigest() != meta.get("response_sha256"):
            raise ValueError("Agent reply revised after preparation; resume extraction/review before publishing.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_id")
    args = parser.parse_args()
    path = intake.batch_path(args.batch_id)
    with intake.file_lock(path / "worker.lock"):
        batch = read_json(path / "batch.json")
        if batch["status"] == "completed":
            print(json.dumps(batch["result"], ensure_ascii=False))
            return
        if batch["status"] != "ready":
            raise ValueError("Batch has not passed all extraction/review gates.")
        ensure_latest_agent_replies(path)
        batch.update(status="running", stage="publishing", error=None)
        intake._save(path, batch, "独立复核全部完成，使用真实 BGE 向量构建并发布索引。")
        started = time.monotonic()
        try:
            embedder = CachedEmbedder(PILOT)
            result = publish_batch(path, pilot=PILOT, embedder=embedder)
            batch.update(status="completed", stage="completed", result=result)
            report = {"batch_id": args.batch_id, "status": "published", "result": result,
                      "embedding": embedder.identity, "embedding_cache_stats": embedder.stats(),
                      "torch_threads": embedder.torch_threads, "elapsed_seconds": time.monotonic() - started}
            intake.write_atomic(PILOT / "reports/expansion_20261103" / (args.batch_id + "_publication.json"), report)
        except Exception:
            batch.update(status="failed", stage="publication_failed", error="发布未完成；准备结果保留，请核对恢复状态。")
            intake._save(path, batch)
            raise
        intake._save(path, batch, "批次已发布，文献、分类、图谱和检索索引已更新。")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
