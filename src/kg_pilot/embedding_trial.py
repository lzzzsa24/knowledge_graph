from __future__ import annotations
import argparse
import importlib.metadata
import json
import time
from pathlib import Path
from kg_pilot.embedding_chunks import source_chunks
from kg_pilot.pipeline import PILOT, jsonl, read_json, write_json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    # Windows without symlink privilege: use the hub's copy fallback consistently.
    from huggingface_hub import file_download
    file_download.are_symlinks_supported = lambda cache_dir=None: False
    import torch
    torch.set_num_threads(4)
    from sentence_transformers import SentenceTransformer
    started = time.perf_counter()
    revision = read_json(PILOT / "model_config.json")["revision"]
    model = SentenceTransformer("BAAI/bge-m3", revision=revision, cache_folder=str(PILOT / "model_cache"), device="cpu")
    model.max_seq_length = 512
    model_ready = time.perf_counter()
    path = PILOT / "parsed" / "effective_evidence.jsonl"
    records = [x for x in jsonl(path if path.exists() else PILOT / "parsed" / "evidence.jsonl")
               if len(x["normalized_text"]) >= 80]
    if not args.all:
        # Spread the trial over documents rather than taking only the first paper.
        by_doc = {}
        for x in records:
            by_doc.setdefault(x["doc_id"], []).append(x)
        records = [x for doc in by_doc.values() for x in doc[:2]]
    if args.all:
        segmented = []
        for record in records:
            for part, passage in enumerate(source_chunks(model.tokenizer, record["normalized_text"])):
                item = dict(record)
                item["vector_id"] = record["evidence_id"] + f":seg{part}"
                item["embedding_text"] = passage
                segmented.append(item)
        records = segmented
    texts = [x.get("embedding_text", x["normalized_text"]) for x in records]
    vectors = model.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=True)
    completed = time.perf_counter()
    import numpy as np
    out = PILOT / "benchmarks" / "embedding"
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / ("all_vectors.npy" if args.all else "trial_vectors.npy"), vectors)
    write_json(out / ("all_ids.json" if args.all else "trial_ids.json"), [x["evidence_id"] for x in records])
    if args.all:
        write_json(out / "all_records.json", [{k:x.get(k) for k in ("vector_id","evidence_id","doc_id","pdf_page","embedding_text")} for x in records])
    report = {"model": "BAAI/bge-m3", "revision": revision, "sentence_transformers": importlib.metadata.version("sentence-transformers"),
              "device": "cpu", "torch_threads": 4, "max_seq_length": 512, "batch_size": 8,
              "records": len(texts), "dimensions": int(vectors.shape[1]),
              "load_and_download_seconds": round(model_ready-started, 3),
              "encode_seconds": round(completed-model_ready, 3),
              "records_per_second": round(len(texts)/(completed-model_ready), 3),
              "scope": "all_eligible_native_text_blocks" if args.all else "two_blocks_per_readable_document"}
    write_json(PILOT / "reports" / ("embedding_full.json" if args.all else "embedding_trial.json"), report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
