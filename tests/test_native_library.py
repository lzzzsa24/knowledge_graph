"""Regression checks for revision-scoped migration and the alternating published indexes."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from kg_pilot import native_library as lib


def _probe_document_worker(pilot, doc_id):
    # Contend the lock so asyncio binds it to this invocation's loop, like LightRAG.
    lock = getattr(_probe_document_worker, "lock", None)
    if lock is None:
        lock = _probe_document_worker.lock = asyncio.Lock()
    async def contend():
        async with lock:
            waiting = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0)
        await waiting
        lock.release()
    asyncio.run(contend())
    return {"id": doc_id, "pid": os.getpid(), "pilot": str(pilot)}


def test_consecutive_mcp_documents_get_fresh_framework_state(tmp_path, monkeypatch):
    from scripts.native_library_mcp import NativeTools

    monkeypatch.setattr(lib, "run_document", _probe_document_worker)
    service = NativeTools(pilot=tmp_path)
    first, second = [service.dispatch_tool("native_library_ingest", {"doc_id": doc_id})
                     for doc_id in ("first", "second")]
    assert [first["id"], second["id"]] == ["first", "second"]
    assert first["pilot"] == second["pilot"] == str(tmp_path)
    assert os.getpid() not in (first["pid"], second["pid"])


@pytest.mark.parametrize("transport_error", [False, True])
def test_scheduler_recognizes_wrapped_pause(tmp_path, monkeypatch, transport_error):
    from argparse import Namespace
    from scripts import run_native_library as runner

    async def paused(args):
        errors = [ExceptionGroup("session", [lib.PauseRequested()])]
        if transport_error:
            errors.append(OSError("pipe failed"))
        raise ExceptionGroup("stdio", errors)

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "main", paused)
    args = Namespace(pause=False, resume=False, search=None, status=False)
    status_file = lib.directory(tmp_path / "pilot") / "scheduler.json"
    if transport_error:
        with pytest.raises(ExceptionGroup):
            runner.control(args)
        assert not status_file.exists()
    else:
        assert runner.control(args) == 0
        assert lib.read_json(status_file)["status"] == "paused"


def test_marked_permission_does_not_follow_changed_pdf():
    record={"id":"paper","group":9,"sha256":"original"}
    assert not lib.eligible(record,{})
    assert not lib.eligible(record,{"paper":"other"})
    assert lib.eligible(record,{"paper":"original"})


def test_working_copy_never_overwrites_active_index(tmp_path):
    pilot=tmp_path/"pilot"
    current=lib.directory(pilot)/"index-1"
    current.mkdir(parents=True)
    required=("graph_chunk_entity_relation.graphml","kv_store_doc_status.json","vdb_chunks.json","kv_store_text_chunks.json")
    for name in required:
        (current/name).write_text("good",encoding="utf8")
    manifest={"generation":1,"storage":str(current.relative_to(pilot))}
    slot=lib.prepare_slot(pilot,manifest)
    assert slot!=current
    (slot/required[0]).write_text("partial",encoding="utf8")
    (slot/"kv_store_multimodal_status.json").write_text("interrupted",encoding="utf8")
    assert (current/required[0]).read_text()=="good"
    assert lib.prepare_slot(pilot,manifest)==slot
    assert (slot/required[0]).read_text()=="good"
    assert not (slot/"kv_store_multimodal_status.json").exists()
    assert len(list((lib.directory(pilot)/"interrupted_files").rglob("kv_store_multimodal_status.json")))==1


def test_failed_document_keeps_published_generation(tmp_path,monkeypatch):
    root=lib.directory(tmp_path)
    row={"id":"a","sha256":"hash","group":1,"prebuilt":"","title":"A"}
    original={"generation":1,"storage":"framework/native_library/index-1","documents":{}}
    lib.save(root/"manifest.json",original)
    lib.save(root/"queue.json",{"documents":[row]})
    monkeypatch.setattr(lib,"initialize",lambda _:original)
    monkeypatch.setattr(lib,"verify_source",lambda *_: Path("a.pdf"))
    monkeypatch.setattr(lib,"prepare_slot",lambda *_: root/"index-0")
    async def incomplete(*args):
        raise RuntimeError("Incomplete source")
    monkeypatch.setattr(lib,"ingest",incomplete)
    with pytest.raises(RuntimeError,match="Incomplete source"):
        lib.run_document(tmp_path,"a")
    assert json.loads((root/"manifest.json").read_text())==original
    assert lib.state(tmp_path)["next_id"]=="a"


def test_committed_retry_skips_all_provider_calls(tmp_path,monkeypatch):
    root=lib.directory(tmp_path)
    row={"id":"a","sha256":"same","group":1,"prebuilt":"","title":"A"}
    lib.save(root/"manifest.json",{"documents":{"a":{"sha256":"same"}}})
    lib.save(root/"queue.json",{"documents":[row]})
    monkeypatch.setattr(lib,"initialize",lambda _:lib.read_json(root/"manifest.json"))
    assert lib.run_document(tmp_path,"a")["status"]=="already_published"


def test_inflight_call_is_saved_when_next_call_is_paused(tmp_path,monkeypatch):
    root=lib.directory(tmp_path)
    job=root/"documents/paper"
    started=threading.Event()
    finish=threading.Event()
    requests=[]
    def model(messages,area,image_root):
        requests.append(messages)
        started.set()
        assert finish.wait(8)
        return "answer",{"input_tokens":10},"gpt-6.2-sol"
    monkeypatch.setattr(lib,"run_subscription",model)
    async def run():
        hooks=lib.SubscriptionHooks(job)
        hooks.semaphore=asyncio.Semaphore(1)
        first=asyncio.create_task(hooks.complete("one"))
        assert await asyncio.to_thread(started.wait,5)
        lib.save(root/"pause.json",{"requested":True})
        second=asyncio.create_task(hooks.complete("two"))
        finish.set()
        assert await first=="answer"
        with pytest.raises(lib.PauseRequested):
            await second
        assert len(requests)==1
        (root/"pause.json").unlink()
        fresh=lib.SubscriptionHooks(job)
        assert await fresh.complete("one")=="answer"
        assert await fresh.complete("two")=="answer"
        assert len(requests)==2
    asyncio.run(run())


def test_abrupt_worker_exit_then_recover_current_pdf(tmp_path):
    root=lib.directory(tmp_path)
    active=root/"index-1"
    active.mkdir(parents=True)
    for name in ("graph_chunk_entity_relation.graphml","kv_store_doc_status.json","vdb_chunks.json","kv_store_text_chunks.json"):
        (active/name).write_text("{}",encoding="utf8")
    manifest={"generation":1,"storage":str(active.relative_to(tmp_path)),"storage_sha256":lib.storage_signature(active),"documents":{}}
    lib.save(root/"manifest.json",manifest)
    env={**os.environ,"PYTHONPATH":str(Path(lib.__file__).resolve().parents[1])}
    program="""import asyncio,os,sys
from pathlib import Path
from kg_pilot import native_library as lib
p=Path(sys.argv[1]); root=lib.directory(p)
with lib.file_lock(root/'writer.lock'):
    slot=lib.prepare_slot(p,lib.initialize(p))
    lib.run_subscription=lambda *args: ('saved answer',{'input_tokens':12},'gpt-6.2-sol')
    asyncio.run(lib.SubscriptionHooks(root/'documents/p').complete('same prompt'))
    (slot/'vdb_chunks.json').write_bytes(b'{partial')
    os._exit(91)
"""
    result=subprocess.run([sys.executable,"-c",program,str(tmp_path)],env=env,timeout=40)
    assert result.returncode==91
    with lib.file_lock(root/"writer.lock"):
        assert lib.initialize(tmp_path)==manifest
        staged=lib.prepare_slot(tmp_path,manifest)
        assert (staged/"vdb_chunks.json").read_text()=="{}"
        assert asyncio.run(lib.SubscriptionHooks(root/"documents/p").complete("same prompt"))=="saved answer"


def test_cancelled_framework_caller_still_checkpoints_running_cli(tmp_path,monkeypatch):
    job=lib.directory(tmp_path)/"documents/d"
    started=threading.Event()
    finish=threading.Event()
    calls=[]
    def model(*args):
        calls.append(1)
        started.set()
        assert finish.wait(8)
        return "completed",{"output_tokens":7},"gpt-6.2-sol"
    monkeypatch.setattr(lib,"run_subscription",model)
    async def run():
        hook=lib.SubscriptionHooks(job)
        caller=asyncio.create_task(hook.complete("chunk"))
        assert await asyncio.to_thread(started.wait,5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        finish.set()
        await asyncio.gather(*list(hook.pending.values()))
        assert await lib.SubscriptionHooks(job).complete("chunk")=="completed"
        assert len(calls)==1
    asyncio.run(run())


def test_corrupt_published_generation_recovers_verified_previous(tmp_path):
    root=lib.directory(tmp_path)
    for gen in (0,1):
        slot=root/("index-"+str(gen))
        slot.mkdir(parents=True)
        for f in ("graph_chunk_entity_relation.graphml","kv_store_doc_status.json","vdb_chunks.json","kv_store_text_chunks.json"):
            (slot/f).write_text("{}",encoding="utf8")
        manifest={"generation":gen,"storage":str(slot.relative_to(tmp_path)),"documents":{},"storage_sha256":lib.storage_signature(slot)}
        lib.save(root/("manifest.json" if gen else "manifest.previous.json"),manifest)
    (root/"index-1/vdb_chunks.json").write_text("damaged",encoding="utf8")
    assert lib.initialize(tmp_path)["generation"]==0
    assert lib.read_json(root/"manifest.json")["generation"]==0
