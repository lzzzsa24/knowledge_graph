"""MCP domain checks use isolated corpora and synthetic PDFs only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pymupdf
import pytest

from kg_pilot import ingestion as ing
from kg_pilot import ingestion_publish as publication
from kg_pilot import mcp_ingestion as mcp


@pytest.fixture
def tmp_path():
    # pytest 9's 0700 Windows temp ACLs are inaccessible in this workspace;
    # ordinary inherited permissions match the existing runner test fixture.
    parent = (Path(__file__).resolve().parents[1] / ".cache").resolve()
    parent.mkdir(exist_ok=True)
    directory = parent / ("mcp-ingestion-test-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        assert directory.resolve().parent == parent
        shutil.rmtree(directory)


def make_pdf(path, *, pages=1, encrypted=False):
    with pymupdf.open() as pdf:
        for _ in range(pages):
            page = pdf.new_page()
            page.insert_text((40, 70), "Synthetic local MCP intake fixture.")
        options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "user"} if encrypted else {}
        path.write_bytes(pdf.tobytes(**options))
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    pilot = tmp_path / "isolated"
    (pilot / "catalog").mkdir(parents=True)
    (pilot / "catalog/papers.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(mcp, "codex_status", lambda: {"available": True, "model": "gpt-6.1-sol", "reasoning_effort": "ultra"})
    return mcp.IngestionTools(pilot), make_pdf(tmp_path / "fixture.pdf")


def test_specs_have_closed_arguments_and_no_provider_or_credentials():
    specs = mcp.tool_specs()
    assert len(specs) == 8
    for item in specs:
        assert item["inputSchema"]["additionalProperties"] is False
        assert not {"pilot", "model", "mode", "key", "api_key", "command", "url"} & set(item["inputSchema"]["properties"])
    assert "gpt-6.1-sol" in next(item["description"] for item in specs if item["name"] == "ingestion_start")


def test_validate_reads_bounded_pdf_without_creating_batch(setup):
    service, pdf = setup
    result = service.dispatch_tool("ingestion_validate", {"paths": [str(pdf)]})
    assert result["valid"] is True
    assert result["files"][0]["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert result["files"][0]["pages"] == 1
    assert not (service.pilot / "ingestion").exists()


@pytest.mark.parametrize("value", ["relative.pdf", "https://example.com/a.pdf", r"\\server\a.pdf", r"\\?\C:\a.pdf", r"C:\a.pdf:secret", r"C:\*.pdf", r"\\.\pipe\a.pdf", r"C:\NUL.pdf", r"C:\COM1.pdf", "/tmp/a.pdf\x00"])
def test_rejects_nonlocal_or_ambiguous_paths_before_open(setup, value):
    service, _ = setup
    with pytest.raises(mcp.MCPToolError) as exc:
        service.dispatch_tool("ingestion_validate", {"paths": [value]})
    assert exc.value.code == "invalid_pdf_path"


def test_size_header_password_and_page_limits(setup, tmp_path, monkeypatch):
    service, pdf = setup
    bad = tmp_path / "not-a-pdf.pdf"
    bad.write_bytes(b"private value MUST NOT BE ECHOED")
    with pytest.raises(mcp.MCPToolError, match="PDF") as exc:
        service.dispatch_tool("ingestion_validate", {"paths": [str(bad)]})
    assert "private" not in exc.value.message
    encrypted = make_pdf(tmp_path / "encrypted.pdf", encrypted=True)
    with pytest.raises(mcp.MCPToolError) as exc:
        service.dispatch_tool("ingestion_validate", {"paths": [str(encrypted)]})
    assert exc.value.code == "encrypted_pdf"
    monkeypatch.setitem(ing.LIMITS, "max_pages", 1)
    long = make_pdf(tmp_path / "long.pdf", pages=2)
    with pytest.raises(mcp.MCPToolError) as exc:
        service.dispatch_tool("ingestion_validate", {"paths": [str(long)]})
    assert exc.value.code == "pdf_page_limit"
    monkeypatch.setitem(ing.LIMITS, "max_file_mb", 0)
    with pytest.raises(mcp.MCPToolError) as exc:
        service.dispatch_tool("ingestion_validate", {"paths": [str(pdf)]})
    assert exc.value.code == "pdf_size_limit"


@pytest.mark.parametrize("name,args", [
    ("ingestion_start", {"paths": [], "mode": "api"}),
    ("ingestion_start", {"paths": [], "max_claims": True}),
    ("ingestion_start", {"paths": ["x"], "workers": True}),
    ("ingestion_start", {"paths": ["x"], "workers": 11}),
    ("ingestion_start", {"paths": [], "auto_publish": "true"}),
    ("ingestion_start", {"paths": [], "provenance": [{"title": "x"}]}),
    ("ingestion_start", {"paths": ["x"], "provenance": [{"api_key": "sentinel"}]}),
    ("ingestion_status", {"limit": False}),
    ("ingestion_resume", {"batch_id": "../secrets"}),
    ("ingestion_cancel", {}),
    ("unknown", {}),
])
def test_arguments_checked_without_transport(setup, name, args):
    service, _ = setup
    with pytest.raises(mcp.MCPToolError):
        service.dispatch_tool(name, args)


def test_api_batches_hidden_and_all_actions_rejected_before_api_access(setup, monkeypatch):
    service, pdf = setup
    api = ing.create_batch([(pdf.name, pdf.read_bytes())], mode="paratera", pilot=service.pilot)
    codex = ing.create_batch([(pdf.name, pdf.read_bytes())], mode="codex", pilot=service.pilot)
    api_dir = ing.batch_path(api["id"], service.pilot)
    (api_dir / "worker.lock").unlink()  # create_batch status has already made this lock.
    original = ing.get_batch
    def guarded(batch_id, pilot):
        assert batch_id != api["id"], "API batch must not reach ingestion functions"
        return original(batch_id, pilot)
    monkeypatch.setattr(ing, "get_batch", guarded)
    listing = service.dispatch_tool("ingestion_status", {})
    assert [item["id"] for item in listing["batches"]] == [codex["id"]]
    for name in ("ingestion_status", "ingestion_wait", "ingestion_resume", "ingestion_publish", "ingestion_cancel", "ingestion_skip"):
        args = {"batch_id": api["id"]}
        if name == "ingestion_skip":
            args["item_id"] = "item_000"
        with pytest.raises(mcp.MCPToolError) as exc:
            service.dispatch_tool(name, args)
        assert exc.value.code == "batch_not_found"
    assert not (api_dir / "worker.lock").exists()
    assert not (api_dir / "cancel.request").exists()


def test_launch_is_nonblocking_detached_and_cannot_select_executable(setup, monkeypatch):
    service, pdf = setup
    calls = []
    def launch(command, **options):
        calls.append((command, options))
        return SimpleNamespace(pid=mcp.os.getpid())
    monkeypatch.setattr(mcp.subprocess, "Popen", launch)
    result = service.dispatch_tool("ingestion_start", {"paths": [str(pdf)], "auto_publish": False, "workers": 10,
                                 "provenance": [{"title": "Source title", "year": 2026}]})
    batch = result["batch"]
    assert batch["accepted"] and batch["running"] and batch["stage"] == "starting"
    assert batch["mode"] == "codex" and batch["auto_publish"] is False
    assert batch["workers"] == 10
    assert len(calls) == 1
    command, options = calls[0]
    assert command[0] == str(Path(sys.executable).resolve())
    assert command[1:4] == ["-m", "kg_pilot.mcp_ingestion", "--worker"]
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["env"]["PYTHONPATH"] == str(Path(mcp.__file__).resolve().parents[1])
    assert not options.get("start_new_session")
    if mcp.os.name == "nt":
        assert options["creationflags"] == subprocess.CREATE_NO_WINDOW
    # A newly constructed service can observe launch state, request cancellation,
    # and cannot launch the same pending worker a second time.
    fresh = mcp.IngestionTools(service.pilot)
    assert fresh.dispatch_tool("ingestion_status", {"batch_id": batch["id"]})["batch"]["running"]
    fresh.dispatch_tool("ingestion_resume", {"batch_id": batch["id"]})
    assert len(calls) == 1
    assert fresh.dispatch_tool("ingestion_cancel", {"batch_id": batch["id"]})["batch"]["cancel_requested"]
    with pytest.raises(mcp.MCPToolError) as exc:
        fresh.dispatch_tool("ingestion_skip", {"batch_id": batch["id"], "item_id": "item_000"})
    assert exc.value.code == "batch_running"


def test_failed_launch_returns_durable_id_and_expired_start_is_resumable(setup, monkeypatch):
    service, pdf = setup
    def fail(*args, **kwargs):
        raise OSError("credentials MUST NOT BE ECHOED")
    monkeypatch.setattr(mcp.subprocess, "Popen", fail)
    result = service.dispatch_tool("ingestion_start", {"paths": [str(pdf)]})["batch"]
    assert not result["accepted"] and result["id"].startswith("b_")
    assert "credentials" not in json.dumps(result)
    path = ing.batch_path(result["id"], service.pilot)
    ing.write_atomic(path / "mcp_worker.json", {"state": "starting", "launched_at_epoch": time.time() - 31})
    assert not service.dispatch_tool("ingestion_status", {"batch_id": result["id"]})["batch"]["running"]


def test_killed_startup_is_immediately_interrupted_and_wait_returns(setup):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot)
    path = ing.batch_path(batch["id"], service.pilot)
    ing.write_atomic(path / "mcp_worker.json", {"token": "dead", "state": "starting", "launched_at_epoch": time.time(),
                                              "owner": {"pid": 999999999, "process_identity": "dead"}})
    ing.write_atomic(path / "mcp_worker_process.json", {"token": "dead", "pid": 999999999, "process_identity": "dead"})
    started = time.monotonic()
    current = mcp.IngestionTools(service.pilot).dispatch_tool("ingestion_wait", {"batch_id": batch["id"], "timeout_seconds": 1})
    assert time.monotonic() - started < 0.5
    assert current["batch"]["stage"] == "interrupted" and current["batch"]["next_action"] == "ingestion_resume"
    assert current["worker_lifetime"] == "host_session" and not current["wait_timed_out"]


def test_cancel_during_startup_is_kept_by_worker_and_stale_token_cannot_finish(setup, monkeypatch):
    service, pdf = setup
    monkeypatch.setattr(mcp.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=mcp.os.getpid()))
    batch = service.dispatch_tool("ingestion_start", {"paths": [str(pdf)]})["batch"]
    path = ing.batch_path(batch["id"], service.pilot)
    marker = mcp._marker(path)
    service.dispatch_tool("ingestion_cancel", {"batch_id": batch["id"]})
    assert mcp._worker(batch["id"], service.pilot, marker["token"]) == 0
    assert service.dispatch_tool("ingestion_status", {"batch_id": batch["id"]})["batch"]["status"] == "cancelled"
    assert (path / "cancel.request").exists()
    # A previous launch may not overwrite a newer launch's durable status.
    marker.update(state="starting", launched_at_epoch=time.time())
    ing.write_atomic(path / "mcp_worker.json", marker)
    def replace_launch(*args, **kwargs):
        ing.write_atomic(path / "mcp_worker.json", {"token": "newer", "state": "starting"})
    monkeypatch.setattr(ing, "run_batch", replace_launch)
    assert mcp._worker(batch["id"], service.pilot, marker["token"]) == 0
    assert mcp._marker(path)["token"] == "newer"


def test_wait_stays_bounded_for_a_live_startup(setup, monkeypatch):
    service, pdf = setup
    monkeypatch.setattr(mcp.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=mcp.os.getpid()))
    batch = service.dispatch_tool("ingestion_start", {"paths": [str(pdf)]})["batch"]
    started = time.monotonic()
    result = service.dispatch_tool("ingestion_wait", {"batch_id": batch["id"], "timeout_seconds": 1})
    assert 0.9 <= time.monotonic() - started < 2
    assert result["wait_timed_out"] is True and result["batch"]["running"]


def test_persisted_live_worker_outlasts_handshake_but_orphan_owner_expires(setup, monkeypatch):
    service, pdf = setup
    monkeypatch.setattr(mcp.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=mcp.os.getpid()))
    batch = service.dispatch_tool("ingestion_start", {"paths": [str(pdf)]})["batch"]
    path = ing.batch_path(batch["id"], service.pilot)
    marker = mcp._marker(path)
    marker["launched_at_epoch"] = time.time() - 31
    ing.write_atomic(path / "mcp_worker.json", marker)
    current = service.dispatch_tool("ingestion_status", {"batch_id": batch["id"]})["batch"]
    assert current["running"] and current["stage"] == "starting"
    # A live tracked worker can proceed after slow import; no fresh handshake
    # is needed, and a startup cancellation still takes precedence over work.
    service.dispatch_tool("ingestion_cancel", {"batch_id": batch["id"]})
    assert mcp._worker(batch["id"], service.pilot, marker["token"]) == 0
    assert service.dispatch_tool("ingestion_status", {"batch_id": batch["id"]})["batch"]["status"] == "cancelled"
    raw = read_batch(path)
    raw.update(status="queued", stage="queued")
    ing.write_atomic(path / "batch.json", raw)
    ing.write_atomic(path / "mcp_worker.json", marker)
    (path / "mcp_worker_process.json").unlink()
    expired = service.dispatch_tool("ingestion_status", {"batch_id": batch["id"]})["batch"]
    assert not expired["running"] and expired["stage"] == "interrupted"


@pytest.mark.parametrize("status", ["completed", "ready", "cancelled", "failed", "needs_attention"])
def test_dead_startup_marker_never_overwrites_terminal_state(setup, status):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot)
    path = ing.batch_path(batch["id"], service.pilot)
    ing.write_atomic(path / "mcp_worker.json", {"token": "dead", "state": "starting", "launched_at_epoch": time.time() - 1})
    raw = read_batch(path)
    raw.update(status=status, stage=status, updated_at=ing.now())
    ing.write_atomic(path / "batch.json", raw)
    current = service.dispatch_tool("ingestion_status", {"batch_id": batch["id"]})["batch"]
    assert current["status"] == current["stage"] == status
    assert not current["running"]


def test_publication_requires_review_and_only_schedules_publish(setup, monkeypatch):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot, auto_publish=False)
    with pytest.raises(mcp.MCPToolError) as exc:
        service.dispatch_tool("ingestion_publish", {"batch_id": batch["id"]})
    assert exc.value.code == "batch_not_ready"
    path = ing.batch_path(batch["id"], service.pilot)
    raw = read_batch(path)
    raw.update(status="ready", stage="ready")
    ing.write_atomic(path / "batch.json", raw)
    calls = []
    monkeypatch.setattr(service, "_launch", lambda bid, **kwargs: calls.append((bid, kwargs)) or batch)
    service.dispatch_tool("ingestion_publish", {"batch_id": batch["id"]})
    assert calls == [(batch["id"], {"publish_only": True})]
    service.dispatch_tool("ingestion_resume", {"batch_id": batch["id"]})
    assert len(calls) == 1  # ready staging remains an explicit publication step.


def test_resume_preserves_interrupted_manual_publication_intent(setup, monkeypatch):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot, auto_publish=False)
    path = ing.batch_path(batch["id"], service.pilot)
    raw = read_batch(path)
    raw.update(status="ready", stage="ready")
    ing.write_atomic(path / "batch.json", raw)
    calls = []
    monkeypatch.setattr(service, "_launch", lambda bid, **kw: calls.append((bid, kw)) or batch)
    service.dispatch_tool("ingestion_resume", {"batch_id": batch["id"]})
    assert calls == []
    ing.write_atomic(path / "mcp_worker.json", {"state": "starting", "publish_only": True, "launched_at_epoch": time.time() - 1})
    service.dispatch_tool("ingestion_resume", {"batch_id": batch["id"]})
    assert calls == [(batch["id"], {"publish_only": True})]


@pytest.mark.parametrize("mismatch", ["journal", "maintenance"])
def test_scoped_recovery_checks_owner_under_dataset_lock_before_changes(setup, mismatch):
    service, _ = setup
    own, other = "b_" + "a" * 20, "b_" + "b" * 20
    ing.write_atomic(service.pilot / publication.JOURNAL, {"status": "committed", "batch_id": other if mismatch == "journal" else own})
    ing.write_atomic(service.pilot / publication.MAINTENANCE, {"active": True, "batch_id": other})
    protected = [service.pilot / publication.JOURNAL, service.pilot / publication.MAINTENANCE, service.pilot / "catalog/papers.json"]
    before = {path: path.read_bytes() for path in protected}
    with pytest.raises(publication.PublicationError, match="其他批次"):
        publication.recover_publication(service.pilot, expected_batch_id=own)
    assert all(path.read_bytes() == data for path, data in before.items())


def test_api_owned_maintenance_blocks_codex_resume_without_cross_mode_writes(setup):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot)
    api = ing.create_batch([(pdf.name, pdf.read_bytes())], mode="paratera", pilot=service.pilot)
    ing.write_atomic(service.pilot / publication.JOURNAL, {"status": "publishing", "batch_id": api["id"]})
    ing.write_atomic(service.pilot / publication.MAINTENANCE, {"active": True, "batch_id": api["id"]})
    paths = [service.pilot / publication.JOURNAL, service.pilot / publication.MAINTENANCE,
             ing.batch_path(api["id"], service.pilot) / "batch.json"]
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(mcp.MCPToolError) as exc:
        service.dispatch_tool("ingestion_resume", {"batch_id": batch["id"]})
    assert exc.value.code == "publication_owned_elsewhere"
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not (ing.batch_path(batch["id"], service.pilot) / "mcp_worker.json").exists()


def test_worker_recovers_owned_journal_before_resuming_publish_only(setup, monkeypatch):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot, auto_publish=False)
    path = ing.batch_path(batch["id"], service.pilot)
    ing.write_atomic(path / "mcp_worker.json", {"token": "own", "state": "starting", "publish_only": True,
                                              "launched_at_epoch": time.time()})
    ing.write_atomic(service.pilot / publication.JOURNAL, {"status": "publishing", "batch_id": batch["id"],
                                                         "backup_relative": "ingestion/backups/own", "targets": []})
    ing.write_atomic(service.pilot / publication.MAINTENANCE, {"active": True, "batch_id": batch["id"]})
    calls = []
    def run(batch_id, pilot, **kwargs):
        assert not (pilot / publication.MAINTENANCE).exists()
        assert json.loads((pilot / publication.JOURNAL).read_text())["status"] == "rolled_back"
        calls.append((batch_id, kwargs))
    monkeypatch.setattr(ing, "run_batch", run)
    assert mcp._worker(batch["id"], service.pilot, "own", publish_only=True) == 0
    assert calls == [(batch["id"], {"publish_only": True, "clear_cancel": False})]


def read_batch(path):
    return json.loads((path / "batch.json").read_text(encoding="utf-8"))


def test_worker_waits_for_short_status_reader_lock_without_failing(setup):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot)
    path = ing.batch_path(batch["id"], service.pilot)
    raw = read_batch(path)
    raw["documents"][0]["status"] = "duplicate"
    ing.write_atomic(path / "batch.json", raw)
    # Simulate a slow but valid coherent status read exactly when run_batch
    # takes its per-batch lock. The previous nonblocking acquisition failed.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with ing.file_lock(path / "worker.lock"):
            future = pool.submit(ing.run_batch, batch["id"], service.pilot)
            time.sleep(0.1)
            assert not future.done()
        assert future.result(timeout=3)["status"] == "completed"


def test_worker_lock_retry_is_bounded_and_real_competing_worker_fails_immediately(setup):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot)
    path = ing.batch_path(batch["id"], service.pilot)
    with ing.file_lock(path / "worker.lock"):
        started = time.monotonic()
        with pytest.raises(ing.IntakeError):
            with ing.file_lock(path / "worker.lock", timeout=0.03):
                pass
        assert 0.02 <= time.monotonic() - started < 0.5
    with ing.file_lock(service.pilot / "ingestion/global_worker.lock"):
        started = time.monotonic()
        with pytest.raises(ing.IntakeError):
            ing.run_batch(batch["id"], service.pilot)
        assert time.monotonic() - started < 0.5


def test_completed_checkpoint_is_terminal_while_worker_releases_lock(setup):
    service, pdf = setup
    batch = ing.create_batch([(pdf.name, pdf.read_bytes())], pilot=service.pilot)
    path = ing.batch_path(batch["id"], service.pilot)
    raw = read_batch(path)
    raw.update(status="completed", stage="completed")
    ing.write_atomic(path / "batch.json", raw)
    with ing.file_lock(path / "worker.lock"):
        current = service.dispatch_tool("ingestion_status", {"batch_id": batch["id"]})["batch"]
    assert current["status"] == "completed" and current["next_action"] == "none" and not current["running"]


@pytest.mark.parametrize("iteration", range(5))
def test_actual_session_worker_completes_with_fast_fresh_service_polling(setup, iteration):
    service, pdf = setup
    # A duplicate file exercises the real private worker and durable checkpoints
    # without calling a model, loading embeddings, or writing a production corpus.
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (service.pilot / "catalog/papers.json").write_text(json.dumps([{"sha256": digest, "doc_id": "existing_fixture"}]), encoding="utf-8")
    result = service.dispatch_tool("ingestion_start", {"paths": [str(pdf)]})
    batch_id = result["batch"]["id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        fresh = mcp.IngestionTools(service.pilot)
        current = fresh.dispatch_tool("ingestion_status", {"batch_id": batch_id})["batch"]
        if current["status"] == "completed":
            break
        time.sleep(0.001)
    assert current["status"] == "completed", current
    assert not current["running"] and current["result"]["added_documents"] == 0
    assert current["auto_publish"] is True
    before = deepcopy(read_batch(ing.batch_path(batch_id, service.pilot)))
    fresh.dispatch_tool("ingestion_cancel", {"batch_id": batch_id})
    fresh.dispatch_tool("ingestion_resume", {"batch_id": batch_id})
    assert read_batch(ing.batch_path(batch_id, service.pilot)) == before
