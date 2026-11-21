"""Ingest PDFs with the selected Paratera model and build a shared-page graph index."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
from kg_pilot import ingestion, shared_ingestion
from kg_pilot.pipeline import PILOT, read_json


def sizes():
    return {name: sum(p.stat().st_size for p in (PILOT / name).rglob("*") if p.is_file())
            for name in ("indexes", "parsed", "collection/batches", "benchmarks/embedding")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="*", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--token-limit", required=True, type=int)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--find", metavar="QUESTION", help="List physical PDF pages from this completed shared index")
    parser.add_argument("--provenance-json", action="store_true", help="Read the matching .json next to each input PDF")
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "run.json"
    with ingestion.file_lock(directory / "worker.lock"):
        run = shared_ingestion.ApiRun(directory / "calls.jsonl", args.model, args.token_limit)
        if state_path.exists():
            state = read_json(state_path)
            if args.pdf and [hashlib.sha256(p.read_bytes()).hexdigest() for p in args.pdf] != state["revisions"]:
                parser.error("Use the original PDFs or omit them to resume.")
            if state["model"] != args.model:
                parser.error("Cannot resume with a different model.")
        else:
            if args.find:
                parser.error("Supply a previously completed --run-dir to search.")
            if not args.pdf:
                parser.error("Supply PDFs on the first run.")
            meta = [read_json(p.with_suffix(".json")) for p in args.pdf] if args.provenance_json else None
            files = [(p.name, p.read_bytes()) for p in args.pdf]
            before = sizes()
            began = time.monotonic()
            batch = ingestion.create_batch(files, mode="api", max_claims=8, workers=min(3,len(files)),
                                           provenance=meta)
            state = dict(batch_id=batch["id"], model=args.model, endpoint=run.endpoint, sizes_before=before,
                         revisions=[d["sha256"] for d in batch["documents"]],
                         batch_creation_seconds=time.monotonic()-began, baseline_runs=[])
            ingestion.write_atomic(state_path, state)
        if args.find:
            if "framework_result" not in state:
                parser.error("Finish building the shared index before searching.")
            papers = shared_ingestion.shared_sources(PILOT, state["batch_id"])
            async def find():
                async with shared_ingestion.framework_search(run, state["framework_result"]) as pages:
                    return await pages(args.find)
            for doc, page in asyncio.run(find()):
                print(json.dumps({"doc_id":doc, "title":papers[doc][0]["title"], "pdf_page":page,
                                  "pdf":papers[doc][0]["local_path"]}, ensure_ascii=False))
            return
        if "baseline_result" not in state:
            began = time.monotonic()
            batch = ingestion.run_batch(state["batch_id"], runner=run.intake_task)
            state["baseline_runs"].append(dict(status=batch["status"], seconds=time.monotonic()-began))
            if batch["status"] == "completed":
                state["baseline_result"] = batch["result"]
                state["sizes_after"] = sizes()
            ingestion.write_atomic(state_path, state)
            if batch["status"] != "completed":
                print([(d["filename"], d["stage"], d.get("error")) for d in batch["documents"]], flush=True)
                raise SystemExit("Baseline batch requires attention: " + batch["status"])
        print("BASELINE", state["batch_id"], state["baseline_result"], flush=True)
        if not args.baseline_only:
            began = time.monotonic()
            result = asyncio.run(shared_ingestion.build_index(run, state["batch_id"]))
            if not result["reused"] or "framework_result" not in state:
                state["framework_result"] = result
            state["last_index_action"] = dict(reused=result["reused"], seconds=time.monotonic()-began)
            ingestion.write_atomic(state_path, state)
            print("FRAMEWORK", result, flush=True)


if __name__ == "__main__":
    main()
