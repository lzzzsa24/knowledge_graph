"""Insert a batch into one working generation and publish it after verification."""
import asyncio
from collections import defaultdict
from contextlib import ExitStack
import os
import time

import psutil

from kg_pilot import native_library as lib


async def ingest_workspace(pilot, slot, documents, selection=None, *, cached_only=False):
    from kg_pilot.native_embeddings import NativeEncoder
    from kg_pilot.native_pages import STALE_INPUTS, mixed_pages
    from kg_pilot.native_readiness import preflight_receipt, verify_prepared
    from raganything.callbacks import ProcessingCallback
    from raganything.utils import separate_content_with_page_map

    root = lib.directory(pilot)
    shared = root / "documents" / "_workspace"
    shared.mkdir(parents=True, exist_ok=True)
    jobs = {row["id"]: root / "documents" / row["id"] for row, _ in documents}
    rows = {row["id"]: row for row, _ in documents}
    model_slots = asyncio.Semaphore(lib.WORKSPACE_MODEL_LIMIT)
    layout_slot = asyncio.Semaphore(1)
    hooks = lib.SubscriptionHooks(shared, selection=selection, cache_jobs=tuple(jobs.values()),
                                  semaphore=model_slots)
    def checkpoint(doc, stage, **extra):
        lib.save(jobs[doc] / "index-stage.json",
                 {"sha256": rows[doc]["sha256"], "stage": stage, "at": lib.now(), **extra})

    class Progress(ProcessingCallback):
        def on_multimodal_start(self, doc_id=None, **_):
            if doc_id in jobs:
                checkpoint(doc_id, "multimodal")

        def on_document_complete(self, doc_id=None, **_):
            if doc_id in jobs:
                checkpoint(doc_id, "checking")

    async def embed(texts, **_):
        return await asyncio.to_thread(encoder.encode, texts)

    async def prepare(row, pdf):
        doc = row["id"]
        job = jobs[doc]
        document_hooks = lib.SubscriptionHooks(job, selection=selection, row=row,
                                               cache_jobs=(shared,), semaphore=model_slots)
        if not cached_only:
            checkpoint(doc, "crop_review")
        try:
            if cached_only and preflight_receipt(job, row["sha256"]).get("status") != "ready":
                raise ValueError(STALE_INPUTS)
            parsed = await mixed_pages(pilot, row, pdf, job, document_hooks, review_crops=True,
                                       layout_slot=layout_slot, cached_only=cached_only)
            if slot is None:
                checkpoint(doc, "preflight_checking")
                verify_prepared(job, row, pdf, parsed)
        except Exception as error:
            # No part of this document has been inserted yet.
            return None, str(error) or type(error).__name__
        finally:
            if document_hooks.pending:
                await asyncio.gather(*list(document_hooks.pending.values()), return_exceptions=True)
        checkpoint(doc, "batch_ready")
        return parsed, None

    outcomes = await asyncio.gather(*(prepare(row, pdf) for row, pdf in documents))
    errors = {doc: error for doc, (_, error) in zip(rows, outcomes) if error}
    parsed_documents = {doc: parsed for doc, (parsed, error) in zip(rows, outcomes) if not error}
    if slot is None:
        return parsed_documents, errors
    if cached_only and errors:
        return {}, errors

    backend = root / "embedding_backend.json"
    device = lib.read_json(backend)["device"] if backend.is_file() else "cpu"
    encoder = NativeEncoder(pilot, device, cache_path=shared / "embedding-cache.sqlite3",
                            cache_sources=[job / "embedding-cache.sqlite3" for job in jobs.values()])
    app = None
    try:
        app = lib.native_app(slot, shared / "parser", hooks, embed, parallel=len(documents),
                             model_parallel=lib.WORKSPACE_MODEL_LIMIT)
        app.callback_manager.register(Progress())
        result = await app._ensure_lightrag_initialized()
        if not result.get("success"):
            raise RuntimeError("Could not initialize the shared RAG-Anything workspace")
        # LightRAG snapshots the text queue as soon as the first insert starts
        # its processor. Let all text inserts enqueue before it takes that snapshot.
        text_count = sum(bool(separate_content_with_page_map(parsed["items"])[0].strip())
                         for parsed in parsed_documents.values())
        barrier = asyncio.Barrier(text_count) if text_count > 1 else None
        queue_started = asyncio.Event()
        process_queue = app.lightrag.apipeline_process_enqueue_documents
        if barrier is not None:
            async def process_batch(*args, **kwargs):
                if not queue_started.is_set():
                    await barrier.wait()
                    queue_started.set()
                return await process_queue(*args, **kwargs)

            app.lightrag.apipeline_process_enqueue_documents = process_batch

        async def insert(doc, parsed):
            try:
                lib.check_pause(root, selection=selection, row=rows[doc])
                checkpoint(doc, "indexing")
                await app.insert_content_list(parsed["items"], file_path=doc + ".pdf", doc_id=doc)
            except BaseException:
                if barrier is not None and not queue_started.is_set():
                    await barrier.abort()
                raise

        results = await asyncio.gather(*(insert(doc, parsed) for doc, parsed in parsed_documents.items()),
                                       return_exceptions=True)
        failed_inserts = {doc: error for doc, error in zip(parsed_documents, results)
                          if isinstance(error, BaseException)}
        if hooks.error or failed_inserts:
            # Partial graph changes from a failed insert must stay in the inactive generation.
            errors.update({doc: str(error) or type(error).__name__ for doc, error in failed_inserts.items()})
            reason = hooks.error or errors[next(iter(failed_inserts))]
            return {}, {doc: errors.get(doc, reason) for doc in rows}

        chunks = defaultdict(dict)
        for key, value in lib.read_json(slot / "kv_store_text_chunks.json").items():
            if value.get("full_doc_id") in parsed_documents:
                chunks[value["full_doc_id"]][key] = value
        records = {}
        for doc, parsed in parsed_documents.items():
            records[doc] = await lib.verify_indexed_document(app, rows[doc], parsed, chunks[doc])
            checkpoint(doc, "verified", chunks=len(chunks[doc]))
        for record in records.values():
            record["embedding"] = dict(encoder.identity, scope="workspace")
        return records, errors
    finally:
        try:
            if app is not None:
                await lib.finalize_native_app(app, hooks)
        finally:
            encoder.close()
            lib.save(shared / "embedding_run.json", {**encoder.metrics(), "at": lib.now()})


def publish_workspace(pilot, revisions, selection=None, mode="ready"):
    root = lib.directory(pilot)
    ids = [revision["id"] for revision in revisions]
    lib.validate_concurrency(len(ids))
    if len(set(ids)) != len(ids):
        raise ValueError("Repeated document in workspace batch")
    preflight = mode == "preflight"
    lease = "preflight.lock" if preflight else "writer.lock"
    with lib.file_lock(root / lease), ExitStack() as locks:
        lib.check_pause(root, selection=selection)
        current = lib.read_json(root / "manifest.json") if preflight else lib.initialize(pilot)
        catalogue = {row["id"]: row for row in lib.read_json(root / "queue.json")["documents"]}
        marked = root / "marked_revisions.json"
        approval = lib.read_json(marked) if marked.is_file() else {}
        documents = []
        for revision in revisions:
            doc = revision["id"]
            row = catalogue.get(doc)
            if not row or row["sha256"] != revision["sha256"] or row["prebuilt"] or not lib.eligible(row, approval):
                raise ValueError("Workspace batch contains an unauthorized PDF revision")
            lib.check_pause(root, selection=selection, row=row)
            if doc in current["documents"]:
                if current["documents"][doc]["sha256"] != row["sha256"]:
                    raise ValueError("Existing revision differs")
                continue
            documents.append((row, lib.verify_source(pilot, row)))
        if not documents:
            return {"published": [], "ready": [], "errors": {}}

        statuses = {}
        started = lib.now()
        clock = time.monotonic()
        proc = psutil.Process()
        for row, _ in documents:
            doc = row["id"]
            phase = "prepare" if preflight else "publish"
            flags = {"doc_id": doc, "sha256": row["sha256"], "phase": phase,
                     "mode": "workspace_preflight" if preflight else "workspace",
                     "status": "running", "started": started, "pid": os.getpid(), "created": proc.create_time()}
            job = root / "documents" / doc
            locks.enter_context(lib.file_lock(job / "work.lock"))
            statuses[doc] = (job / f"lane-{phase}.json", flags)
        writer = {"pid": os.getpid(), "created": proc.create_time()}
        first = documents[0][0]
        active = {"doc_id": first["id"], "title": first["title"], "writer": writer, "mode": "workspace",
                  "started": started, "status": "running"}
        records = {}
        errors = {}
        committed = False
        try:
            for path, flags in statuses.values():
                lib.save(path, flags)
            if preflight:
                records, errors = asyncio.run(ingest_workspace(pilot, None, documents, selection))
                lib.check_pause(root, selection=selection)
                return {"ready": list(records), "errors": errors}
            lib.save(root / "current.json", active)
            slot = lib.prepare_slot(pilot, current)
            records, errors = asyncio.run(ingest_workspace(pilot, slot, documents, selection, cached_only=True))
            lib.check_pause(root, selection=selection)
            if records:
                prospective = {**current, "documents": {**current["documents"], **records}}
                lib.validate_previous_documents(slot, prospective)
                old_chunks = lib.read_json(pilot / current["storage"] / "kv_store_text_chunks.json")
                new_chunks = lib.read_json(slot / "kv_store_text_chunks.json")
                provenance = ("full_doc_id", "content", "page_idx", "page_idx_end", "original_type")
                for key, old in old_chunks.items():
                    new = new_chunks.get(key)
                    if new is None or any(new.get(field) != old.get(field) for field in provenance):
                        raise ValueError("An earlier PDF chunk lost its source attribution")
                digest = lib.storage_signature(slot, sync=True)
                lib.check_pause(root, selection=selection)
                for record in records.values():
                    record["seconds"] = round(time.monotonic()-clock, 2)
                    record["timing_scope"] = "workspace_batch"
                lib.save(root / "manifest.previous.json", current)
                prospective.update(storage=str(slot.relative_to(pilot)), generation=current["generation"]+1,
                                   updated=lib.now(), storage_sha256=digest)
                lib.save(root / "manifest.json", prospective)
                committed = True
                lib.pause_at_group_boundary(root, prospective)
            return {"published": list(records), "errors": errors}
        except Exception as error:
            if not committed:
                errors.update({doc: errors.get(doc, str(error)) for doc in statuses})
            raise
        finally:
            finished = lib.now()
            for doc, (path, flags) in statuses.items():
                published = committed and doc in records
                prepared = preflight and doc in records
                status = "published" if published else ("prepared" if prepared else "interrupted")
                flags.update(status=status, finished=finished)
                if not (published or prepared):
                    flags["error"] = errors.get(doc, "Workspace generation was not published")
                lib.save(path, flags)
            if not preflight:
                active.update(status=statuses[first["id"]][1]["status"], finished=finished)
                if first["id"] in errors:
                    active["error"] = errors[first["id"]]
                lib.save(root / "current.json", active)
