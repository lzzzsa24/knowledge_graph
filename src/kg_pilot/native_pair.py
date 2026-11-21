"""Prepare native documents in parallel and publish through a single canonical writer."""
import asyncio
import hashlib
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
import os
from pathlib import Path
import sys
import tempfile
import time
import psutil

from kg_pilot import native_library as lib


def select_rows(pilot, revisions):
    root = lib.directory(pilot)
    plan = {r["id"]: r for r in lib.read_json(root / "queue.json")["documents"]}
    marked = root / "marked_revisions.json"
    approved = lib.read_json(marked) if marked.exists() else {}
    if not 2 <= len(revisions) <= lib.MAX_DOCUMENT_CONCURRENCY or len({item["id"] for item in revisions}) != len(revisions):
        raise ValueError("A group of distinct catalogued revisions is required")
    rows = []
    for item in revisions:
        row = plan[item["id"]]
        if row["prebuilt"] or row["sha256"] != item["sha256"] or not lib.eligible(row, approved):
            raise ValueError("Trial revision differs from the authorized queue")
        lib.verify_source(pilot, row)
        rows.append(row)
    return rows


def arm_pair(pilot, documents):
    if len(documents) != 2:
        raise ValueError("Two revisions are required for a paired trial")
    root = lib.directory(pilot)
    lib.check_pause(root)
    plan = lib.read_json(root / "queue.json")["documents"]
    manifest = lib.read_json(root / "manifest.json")
    selected = [r for r in plan if r["id"] in documents]
    revisions = [{"id": r["id"], "sha256": r["sha256"]} for r in selected]
    select_rows(pilot, revisions)
    if [r["id"] for r in selected] != list(documents):
        raise ValueError("Supply the two PDFs in queue order")
    if any(doc in manifest["documents"] for doc in documents):
        raise ValueError("A selected revision has already been published")
    path = root / "two_lane_trial.json"
    with lib.file_lock(root / "pair.lock"):
        if path.exists():
            previous = lib.read_json(path)["documents"]
            if previous == revisions:
                return
            if any(r["id"] not in manifest["documents"] for r in previous):
                raise ValueError("An unfinished pair is already selected")
        lib.save(path, {"documents": revisions, "armed": lib.now(), "per_lane_requests": 8,
                        "per_lane_modal_items": 4})


def _worker_init():
    # A nested process must leave the parent MCP's protocol stream alone.
    os.dup2(sys.stderr.fileno(), 1)
    sys.stdout = sys.stderr


def _run_lane(pilot, row, phase, selection=None):
    root = lib.directory(pilot)
    job = root / "documents" / row["id"]
    status_path = job / ("lane-" + phase + ".json")
    status = {"doc_id": row["id"], "sha256": row["sha256"], "phase": phase, "started": lib.now(),
              "pid": os.getpid(), "created": psutil.Process().create_time(), "status": "running"}
    if status_path.is_file():
        previous=lib.read_json(status_path)
        if previous.get("sha256")==row["sha256"]:
            prior=lib.recorded_seconds(previous)
            if prior is not None:
                status["prior_seconds"]=prior
    lib.save(status_path, status)
    try:
        lib.check_pause(root,selection=selection,row=row)
        if phase == "crops":
            from kg_pilot.native_pages import crop_regions
            pdf = lib.verify_source(pilot, row)
            with lib.file_lock(job / "work.lock"):
                hooks = lib.SubscriptionHooks(job, selection=selection, row=row)
                try:
                    asyncio.run(crop_regions(pilot, row, pdf, job, hooks))
                except ValueError as exc:
                    if not str(exc).startswith("裁图待复核："):
                        raise
                review = lib.read_json(job / "crop-review.json")
                if review["revision"] != row["sha256"] or review["status"] not in {"accepted", "needs_review"}:
                    raise ValueError("No crop review for the requested PDF revision")
                result = {"status":review["status"], "reviewed_pages":review["selected_pages"], "held":review["held"]}
        elif phase == "prepare":
            # Use the same processors, prompts and encoder in a private index.
            # Durable page, image and response checkpoints live outside this scratch area.
            pdf = lib.verify_source(pilot, row)
            with lib.file_lock(job / "work.lock"), tempfile.TemporaryDirectory(dir=job, prefix="private-index-") as scratch:
                record = asyncio.run(lib.ingest(pilot, row, pdf, Path(scratch),selection=selection))
            result = {"status": "prepared", "chunks": record["chunks"], "embedding": record["embedding"]}
        else:
            result = lib.publish_document(pilot, row["id"],selection=selection)
        status.update(status=result["status"], result=result)
        return result
    except Exception as exc:
        status.update(status="interrupted", error=str(exc)[:1400])
        raise
    finally:
        status["finished"] = lib.now()
        lib.save(status_path, status)


def run_queue_pair(pilot, documents):
    if len(documents) != 2:
        raise ValueError("Two revisions are required")
    return run_queue_group(pilot, documents)


def run_queue_group(pilot, documents):
    """The scheduler supplies its filtered, ordered IDs. Never pick a neighbour here."""
    root = lib.directory(pilot)
    queue = lib.read_json(root / "queue.json")["documents"]
    revisions = [{"id": r["id"], "sha256": r["sha256"]} for r in queue if r["id"] in documents]
    if not 2 <= len(documents) <= lib.MAX_DOCUMENT_CONCURRENCY or [r["id"] for r in revisions] != documents:
        raise ValueError("Distinct queued papers in catalog order are required")
    report = run_selected_pair(pilot, documents[0], {"documents": revisions}, production=True)
    if report is not None:
        return report
    # An interrupted attempt or another writer may already have committed one member.
    # publish_document rechecks the revision and skips committed work under the writer lock.
    for doc in documents:
        lib.publish_document(pilot, doc)
    return {"published": documents, "errors": {}}


def run_selected_pair(pilot, doc_id, trial, *, production=False):
    revisions = trial["documents"]
    if doc_id != revisions[0]["id"]:
        return None
    root = lib.directory(pilot)
    with lib.file_lock(root / "pair.lock"):
        manifest = lib.read_json(root / "manifest.json")
        if any(r["id"] in manifest["documents"] for r in revisions):
            # Recovery of a partially published pair goes through the ordinary writer.
            return None
        rows = select_rows(pilot, revisions)
        lib.check_pause(root)
        started = time.monotonic()
        pair_id = "-".join(r["id"] for r in rows)
        if len(rows) > 2 or len(pair_id) > 155:
            pair_id = hashlib.sha256(pair_id.encode()).hexdigest()[:24]
        report_path = (root / "pair_runs" / pair_id / f"{time.time_ns()}.json" if production
                       else root / "validation/two_lanes" / pair_id / "run.json")
        report = {"started": lib.now(), "generation_before": manifest["generation"], "documents": revisions,
                  "per_lane_requests": 8, "per_lane_modal_items": 4, "results": {}, "errors": {}}
        lib.save(report_path, report)
        # Only the first lane writes the canonical index during parallel preparation.
        def collect(future, key):
            try:
                report["results"][key] = future.result()
            except Exception as exc:
                report["errors"][key] = str(exc)[:1400]
            lib.save(report_path, report)

        pool_options = {"mp_context": get_context("spawn"), "initializer": _worker_init}
        lanes = [(chr(ord("A") + i), row) for i, row in enumerate(rows)]
        with ProcessPoolExecutor(max_workers=len(rows), **pool_options) as pool:
            futures = [(pool.submit(_run_lane, pilot, row, "publish" if key == "A" else "prepare"),
                        key if key == "A" else key + "_prepare") for key, row in lanes]
            for future, key in futures:
                collect(future, key)
        for key, row in lanes[1:]:
            if (root / "pause.json").exists():
                break
            if key + "_prepare" not in report["results"]:
                continue
            # A fresh process keeps each LightRAG event loop isolated.
            with ProcessPoolExecutor(max_workers=1, **pool_options) as pool:
                collect(pool.submit(_run_lane, pilot, row, "publish"), key)
        final = lib.read_json(root / "manifest.json")
        report.update(finished=lib.now(), seconds=round(time.monotonic()-started, 2),
                      generation_after=final["generation"],
                      published=[r["id"] for r in revisions if r["id"] in final["documents"]])
        lib.save(report_path, report)
        lib.check_pause(root)
        if production:
            return {"published": report["published"], "errors": report["errors"],
                    "seconds": report["seconds"], "report": str(report_path.relative_to(root))}
        if "A" in report["errors"]:
            # The outer queue uses the current document's provider error for quota recovery.
            lib.save(root / "current.json", {"doc_id": doc_id, "status": "interrupted", "finished": lib.now(),
                                             "error": report["errors"]["A"]})
            raise RuntimeError(report["errors"]["A"])
        # If B failed, the scheduler reaches it next and checks its quota window and checkpoints.
        return report["results"]["A"]
