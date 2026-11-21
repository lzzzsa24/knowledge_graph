"""Exercise shared indexing and interrupted generation recovery."""
import asyncio
from collections import Counter
import hashlib

import numpy as np
import pymupdf
import pytest

from kg_pilot import native_embeddings, native_library as lib, native_pages, native_readiness, native_workspace as workspace


def ready_fixture(job, sha):
    lib.save(job / "preflight.json", {
        "policy":native_readiness.POLICY, "status":"ready", "sha256":sha,
        "inputs":{name:lib.file_hash(job / name) for name in ("crop-review.json", "mixed-parse.json")},
    })


def test_workspace_inserts_each_document_once_and_defers_unreviewed_crops(tmp_path, monkeypatch):
    from lightrag import LightRAG

    documents = [({"id": doc, "sha256": doc, "title": doc, "pages": 1}, tmp_path / f"{doc}.pdf")
                 for doc in ("alpha", "beta", "held")]

    async def pages(pilot, row, pdf, job, hooks, **_):
        if row["id"] == "held":
            raise ValueError("裁图待复核：1 项")
        return {"items":[{"type":"text", "page_idx":0, "text":f"{row['id']} wafer bonding was inspected independently."}],
                "identity":{"policy":"fixture"}, "pages":[{"source":"pdf"}], "skipped_decorations":[], "transcription":{}}

    class Encoder:
        identity = {"device":"cpu"}

        def __init__(self, *_, **kwargs):
            pass

        def encode(self, texts):
            return np.ones((len(texts), 1024), dtype=np.float32)

        def metrics(self):
            return self.identity

        def close(self):
            pass

    async def complete(self, *_, **kwargs):
        await asyncio.sleep(.02)
        return "entity<|#|>Bonding<|#|>Method<|#|>Attaches terminals.\n<|COMPLETE|>"

    original = lib.native_app
    extract = LightRAG._process_extract_entities
    enqueue = LightRAG.apipeline_enqueue_documents
    apps = []
    visits = Counter()
    extracting = set()
    overlap = asyncio.Event()

    async def stagger_enqueue(self, content, ids, *args, **kwargs):
        if ids == "beta":
            await asyncio.sleep(.05)
        return await enqueue(self, content, ids, *args, **kwargs)

    async def track_extraction(self, chunks, *args, **kwargs):
        extracting.update(chunk["full_doc_id"] for chunk in chunks.values())
        if extracting == {"alpha", "beta"}:
            overlap.set()
        await asyncio.wait_for(overlap.wait(), 5)
        return await extract(self, chunks, *args, **kwargs)

    def app_factory(*args, **kwargs):
        app = original(*args, **kwargs)
        apps.append(app)
        insert = app.insert_content_list

        async def tracked(items, *, file_path, doc_id):
            visits[doc_id] += 1
            return await insert(items, file_path=file_path, doc_id=doc_id)

        monkeypatch.setattr(app, "insert_content_list", tracked)
        return app

    monkeypatch.setattr(native_pages, "mixed_pages", pages)
    monkeypatch.setattr(native_embeddings, "NativeEncoder", Encoder)
    monkeypatch.setattr(lib.SubscriptionHooks, "complete", complete)
    monkeypatch.setattr(lib, "native_app", app_factory)
    monkeypatch.setattr(LightRAG, "_process_extract_entities", track_extraction)
    monkeypatch.setattr(LightRAG, "apipeline_enqueue_documents", stagger_enqueue)
    for row, _ in documents:
        job = lib.directory(tmp_path) / "documents" / row["id"]
        lib.save(job / "crop-review.json", {})
        lib.save(job / "mixed-parse.json", {})
        if row["id"] != "held":
            ready_fixture(job, row["sha256"])
    records, errors = asyncio.run(workspace.ingest_workspace(tmp_path, tmp_path / "working", documents,
                                                             cached_only=True))
    assert records == {} and errors == {"held":native_pages.STALE_INPUTS}
    assert not apps and not visits

    records, errors = asyncio.run(workspace.ingest_workspace(tmp_path, tmp_path / "working", documents[:2],
                                                             cached_only=True))
    assert len(apps) == 1
    assert visits == {"alpha":1, "beta":1}
    assert not errors
    assert {doc:record["sample_page"] for doc, record in records.items()} == {"alpha":1, "beta":1}


def test_pause_keeps_the_published_generation_and_resumes_both_revisions(tmp_path, monkeypatch):
    root = lib.directory(tmp_path)
    active = root / "index-0"
    for name in ("graph_chunk_entity_relation.graphml", "kv_store_doc_status.json", "vdb_chunks.json"):
        lib.save(active / name, {})
    seed_chunks = {"old-chunk":{"full_doc_id":"old", "page_idx":0}}
    lib.save(active / "kv_store_text_chunks.json", seed_chunks)
    seed = {"generation":2, "storage":str(active.relative_to(tmp_path)), "storage_sha256":lib.storage_signature(active),
            "documents":{"old":{"index":"library", "sha256":"original", "chunks":1}}}
    lib.save(root / "manifest.json", seed)
    rows = [{"id":doc, "sha256":doc, "title":doc, "group":1, "prebuilt":False} for doc in ("a", "b")]
    lib.save(root / "queue.json", {"documents":rows})
    monkeypatch.setattr(lib, "verify_source", lambda *_: tmp_path / "input.pdf")

    async def build(pilot, slot, documents, selection, *, cached_only):
        assert cached_only
        assert lib.read_json(slot / "kv_store_text_chunks.json") == seed_chunks
        chunks = {row["id"]:{"full_doc_id":row["id"], "page_idx":0} for row, _ in documents}
        lib.save(slot / "kv_store_text_chunks.json", {**seed_chunks, **chunks})
        if not (root / "documents" / "_workspace" / "checkpoint.json").exists():
            lib.save(root / "documents" / "_workspace" / "checkpoint.json", {"saved":True})
            lib.save(root / "pause.json", {"requested":lib.now()})
        return {row["id"]:{"index":"library", "sha256":row["sha256"], "chunks":1} for row, _ in documents}, {}

    monkeypatch.setattr(workspace, "ingest_workspace", build)
    with pytest.raises(lib.PauseRequested):
        workspace.publish_workspace(tmp_path, rows)
    assert lib.read_json(root / "manifest.json") == seed
    assert lib.storage_signature(active) == seed["storage_sha256"]
    assert (root / "documents" / "_workspace" / "checkpoint.json").is_file()

    (root / "pause.json").unlink()
    assert workspace.publish_workspace(tmp_path, rows)["published"] == ["a", "b"]
    result = lib.checked_generation(tmp_path, root / "manifest.json")
    assert result["generation"] == 3
    assert result["documents"]["old"] == seed["documents"]["old"]
    assert set(result["documents"]) == {"old", "a", "b"}
    assert lib.read_json(root / "manifest.previous.json") == seed
    assert workspace.publish_workspace(tmp_path, rows)["published"] == []


def test_queue_pairs_similar_remaining_work_without_losing_skipped_rows(tmp_path, monkeypatch):
    from scripts import run_native_library as runner

    root = lib.directory(tmp_path)
    manifest = root / "manifest.json"
    lib.save(manifest, {"documents": {}})
    lib.save(root / "queue_settings.json", {"documents": 2})
    counts = {"held": 4, "anchor": 4, "large": 32, "matching": 5, "fresh": 30}
    rows = [{"id": doc, "sha256": doc, "pages": 8} for doc in counts]
    for doc, count in counts.items():
        if doc == "fresh":
            continue
        job = root / "documents" / doc
        lib.save(job / "mixed-parse.json",
                 {"identity": {"sha256": doc}, "items": [{"type": "image"}] * count})
        lib.save(job / "crop-review.json", {"revision": doc, "status": "accepted"})
        ready_fixture(job, doc)
    lib.save(root / "documents" / "held" / "crop-review.json",
             {"revision": "held", "status": "needs_review"})
    monkeypatch.setattr(runner, "current_windows", lambda _: {})
    batches = []

    async def call(name, arguments):
        assert name == "native_library_ingest_workspace"
        status = lib.read_json(root / "scheduler.json")
        assert status["pairing"] == "estimated_remaining_work"
        batch = [row["id"] for row in arguments["documents"]]
        assert set(status["estimated_work"]) == set(batch)
        if arguments["mode"] == "preflight":
            assert batch == ["fresh"]
            job = root / "documents" / "fresh"
            lib.save(job / "mixed-parse.json",
                     {"identity": {"sha256": "fresh"}, "items": [{"type": "image"}] * counts["fresh"]})
            lib.save(job / "crop-review.json", {"revision": "fresh", "status": "accepted"})
            ready_fixture(job, "fresh")
            return {"data": {"ready": batch}}
        assert arguments["mode"] == "ready" and "held" not in batch
        batches.append(batch)
        published = lib.read_json(manifest)["documents"]
        lib.save(manifest, {"documents": {**published, **{doc: {"sha256": doc} for doc in batch}}})
        return {"data": {}}

    result, deferred = asyncio.run(runner.ingest_workspace_batches(root, rows, call))
    assert result == 1 and [item["doc_id"] for item in deferred] == ["held"]
    assert batches == [["anchor", "matching"], ["large", "fresh"]]


def test_preflight_receipt_binds_reviewed_assets_and_full_page_inputs(tmp_path):
    pdf_file = tmp_path / "original.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(pdf_file)
    row = {"sha256":lib.file_hash(pdf_file), "pages":1}
    text = "Hybrid bonding"
    cropped = tmp_path / "figure.png"
    cropped.write_bytes(b"crop fixture")
    region = {"type":"equation", "page_idx":0, "source_bbox":[1, 1, 20, 20],
              "img_path":str(cropped), "image_sha256":lib.file_hash(cropped)}
    lib.save(tmp_path / "crop-review.json", {"revision":row["sha256"], "status":"accepted", "items":[region]})
    payload = {"identity":{"sha256":row["sha256"]},
               "pages":[{"pdf_page":1, "source":"legacy", "text_sha256":hashlib.sha256(text.encode()).hexdigest()}],
               "items":[{"type":"text", "page_idx":0, "text":text}, region]}
    lib.save(tmp_path / "mixed-parse.json", payload)
    with pytest.raises(ValueError, match="未完整转写"):
        native_readiness.verify_prepared(tmp_path, row, pdf_file, payload)
    assert native_readiness.preflight_receipt(tmp_path, row["sha256"])["status"] == "needs_review"
    payload["items"][1] = {**region, "latex":"x = y"}
    lib.save(tmp_path / "mixed-parse.json", payload)
    assert native_readiness.preflight_receipt(tmp_path, row["sha256"]) == {}
    native_readiness.verify_prepared(tmp_path, row, pdf_file, payload)
    assert native_readiness.preflight_receipt(tmp_path, row["sha256"])["visual_items"] == 1
    lib.save(tmp_path / "crop-review.json", {"revision":row["sha256"], "status":"needs_review", "items":[region]})
    assert native_readiness.preflight_receipt(tmp_path, row["sha256"]) == {}


def test_formal_batch_overlaps_automatic_review_for_next_batch(tmp_path, monkeypatch):
    from scripts import run_native_library as runner

    root = lib.directory(tmp_path)
    manifest = root / "manifest.json"
    lib.save(manifest, {"documents": {}})
    rows = [{"id": doc, "sha256": doc, "pages": 1} for doc in ("current", "held")]
    lib.save(root / "queue_settings.json", {"documents": 1})
    current = root / "documents" / "current"
    held = root / "documents" / "held"
    lib.save(current / "mixed-parse.json", {})
    lib.save(current / "crop-review.json", {"revision":"current", "status":"accepted"})
    ready_fixture(current, "current")
    lib.save(held / "crop-review.json", {"revision":"held", "status":"needs_review", "held":[{"page":1}]})
    monkeypatch.setattr(runner, "current_windows", lambda _: {})

    async def run():
        formal = asyncio.Event()
        buffer_done = asyncio.Event()

        async def call(name, args):
            assert args["mode"] == "ready"
            doc = args["documents"][0]["id"]
            if doc == "current":
                formal.set()
                await asyncio.wait_for(buffer_done.wait(), 2)
            published = lib.read_json(manifest)["documents"]
            lib.save(manifest, {"documents": {**published, doc: {"sha256":doc}}})
            return {"data": {"published":[doc]}}

        async def prefetch(name, args):
            await asyncio.wait_for(formal.wait(), 2)
            assert args == {"documents":[{"id":"held", "sha256":"held"}], "mode":"preflight"}
            assert not lib.read_json(manifest)["documents"]
            assert lib.read_json(root / "prefetch-progress.json")["doc_ids"] == ["held"]
            lib.save(held / "mixed-parse.json", {})
            lib.save(held / "crop-review.json", {"revision":"held", "status":"accepted"})
            ready_fixture(held, "held")
            buffer_done.set()
            return {"data": {"ready":["held"]}}

        assert await runner.ingest_workspace_batches(root, rows, call, prefetch_call=prefetch) == (0, [])

    asyncio.run(run())
    assert list(lib.read_json(manifest)["documents"]) == ["current", "held"]
