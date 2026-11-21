"""Isolated batch flow checks. Synthetic PDFs/model replies never enter the pilot."""
from copy import deepcopy
from functools import partial
import base64
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

import numpy as np
import pymupdf
import pytest
from fastapi.testclient import TestClient

from kg_pilot import ingestion as ing
from kg_pilot.ingestion_publish import publish_batch

TITLE = "Synthetic intake fixture for automated tests"
QUOTE = "The test sample was annealed at 200 C for 60 minutes."


@pytest.fixture
def tmp_path():
    # Match the existing runner/MCP fixtures: pytest 9's Windows 0700 temp
    # ACLs are inaccessible in this managed workspace. Use inherited ACLs.
    parent = (Path(__file__).resolve().parents[1] / ".cache").resolve()
    parent.mkdir(exist_ok=True)
    directory = parent / ("ingestion-test-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        assert directory.resolve().parent == parent
        shutil.rmtree(directory)


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        elapsed = 0.0
        sleeps = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.elapsed += seconds

    value = Clock()
    monkeypatch.setattr(ing.time, "monotonic", lambda: value.elapsed)
    monkeypatch.setattr(ing.time, "sleep", value.sleep)
    return value


def windows_error(winerror, code=errno.EACCES):
    error = PermissionError(code, "sensitive test path/provider text must stay private")
    error.winerror = winerror
    return error


def pdf_bytes(title=TITLE, scan=False):
    pdf = pymupdf.open()
    page = pdf.new_page()
    if scan:
        raster = pymupdf.open()
        rp = raster.new_page()
        rp.insert_text((50, 70), title)
        rp.insert_text((50, 100), QUOTE)
        page.insert_image(page.rect, stream=rp.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png"))
        raster.close()
    else:
        page.insert_text((50, 70), title)
        page.insert_text((50, 100), QUOTE)
        page.insert_text((50, 130), "This is synthetic test data, not a real scientific publication.")
    data = pdf.tobytes()
    pdf.close()
    return data


@pytest.fixture
def pilot(tmp_path):
    (tmp_path / "catalog").mkdir()
    (tmp_path / "catalog/papers.json").write_text("[]", encoding="utf-8")
    return tmp_path


def proposal():
    return dict(metadata=dict(title=TITLE, doi=None, year=2026, authors=[]),
        classification=dict(primary_category="post_bond_annealing", related_stages=[], relevance="core",
            document_form="technical_report", content_nature="original_research", publication_status="unknown",
            access_route="other", routes=[], materials=["Cu/SiO2"], topics=["退火与组织演化"], methods=["experimental"],
            keywords=["退火温度"], classification_reason="这是隔离测试用的合成工艺记录，用于检验退火阶段资料的入库流程。",
            evidence=[dict(pdf_page=1, quote=QUOTE)]),
        claims=[dict(claim_id="c1", statement="合成试样在200 C退火60分钟。", material_system="Cu/SiO2",
            sample_id="synthetic_sample", stage="post_bond_annealing", quantity="annealing_temperature",
            value_text="200", unit="C", conditions={"duration_minutes": 60},
            evidence=[dict(pdf_page=1, quote=QUOTE)], claim_type="reported_experiment")],
        coverage_note="合成测试，仅一条知识记录。")


def runner(mode, task, packet, workdir):
    if task == "extract":
        return proposal()
    return dict(classification_approved=True, classification_notes="Synthetic accepted review.",
                claims=[dict(claim_id=c["claim_id"], status="accepted", reason="Matches synthetic page.")
                        for c in packet["proposal"]["claims"]], summary="Synthetic review only.")


class TestEmbedder:
    __test__ = False
    def encode(self, texts, **kwargs):
        return np.full((len(texts), 1024), 1 / 32, dtype=np.float32)


@pytest.mark.parametrize("mode", ["codex", "paratera"])
def test_two_modes_publish_through_same_verified_pipeline(pilot, mode):
    batch = ing.create_batch([("test.pdf", pdf_bytes())], mode, pilot=pilot)
    result = ing.run_batch(batch["id"], pilot, runner=runner,
                           publisher=partial(publish_batch, embedder=TestEmbedder()))
    assert result["status"] == "completed", result
    assert result["result"]["added_documents"] == 1
    papers = json.loads((pilot / "catalog/papers.json").read_text(encoding="utf-8"))
    assert papers[0]["title"] == TITLE and Path(papers[0]["local_path"]).is_file()
    assert (pilot / "indexes/pilot.sqlite").is_file()
    claims = [json.loads(line) for line in (pilot / "annotations/claims.jsonl").read_text(encoding="utf-8").splitlines()]
    assert claims[0]["review_status"] == ("api_model_reviewed" if mode == "paratera" else "independent_agent_reviewed")
    assert ing.run_batch(batch["id"], pilot, runner=lambda *args: pytest.fail("completed tasks must not rerun"))["status"] == "completed"


def test_duplicates_do_not_call_models_and_bad_file_is_rejected(pilot):
    data = pdf_bytes()
    batch = ing.create_batch([("a.pdf", data), ("b.pdf", data)], pilot=pilot, auto_publish=False)
    assert batch["documents"][1]["status"] == "duplicate"
    result = ing.run_batch(batch["id"], pilot, runner=runner)
    assert result["status"] == "ready"
    with pytest.raises(ing.IntakeError):
        ing.create_batch([("bad.pdf", b"not pdf")], pilot=pilot)
    with pytest.raises(ing.IntakeError):
        ing.batch_path("../secrets", pilot)


def test_failed_quote_does_not_change_catalog_and_retry_repairs(pilot):
    batch = ing.create_batch([("a.pdf", pdf_bytes())], pilot=pilot, auto_publish=False)
    def bad(mode, task, packet, path):
        value = runner(mode, task, packet, path)
        if task == "extract":
            value["claims"][0]["evidence"][0]["quote"] = QUOTE.replace("200", "900")
        return value
    result = ing.run_batch(batch["id"], pilot, runner=bad)
    assert result["status"] == "needs_attention"
    assert json.loads((pilot / "catalog/papers.json").read_text()) == []
    assert ing.run_batch(batch["id"], pilot, runner=runner)["status"] == "ready"


def test_separate_review_rejects_and_cancel_resume_reuses_extract(pilot):
    batch = ing.create_batch([("a.pdf", pdf_bytes())], pilot=pilot, auto_publish=False)
    calls = []
    def cancel_after_extract(mode, task, packet, path):
        calls.append(task)
        value = runner(mode, task, packet, path)
        if task == "extract":
            ing.cancel_batch(batch["id"], pilot)
        return value
    result = ing.run_batch(batch["id"], pilot, runner=cancel_after_extract)
    assert result["status"] == "cancelled" and calls == ["extract"]
    def resume(mode, task, packet, path):
        calls.append(task)
        return runner(mode, task, packet, path)
    assert ing.run_batch(batch["id"], pilot, runner=resume)["status"] == "ready"
    assert calls == ["extract", "review"]


def test_negative_review_requires_attention_then_skip(pilot):
    batch = ing.create_batch([("a.pdf", pdf_bytes())], pilot=pilot)
    def reject(mode, task, packet, path):
        value = runner(mode, task, packet, path)
        if task == "review":
            value["claims"][0]["status"] = "rejected"
        return value
    result = ing.run_batch(batch["id"], pilot, runner=reject)
    assert result["status"] == "needs_attention"
    ing.skip_document(batch["id"], "item_000", pilot)
    result = ing.run_batch(batch["id"], pilot, runner=lambda *args: pytest.fail("skipped"))
    assert result["status"] == "completed" and result["result"]["added_documents"] == 0


def test_semantic_feedback_is_given_to_extractor_not_next_reviewer(pilot):
    batch = ing.create_batch([("a.pdf", pdf_bytes())], pilot=pilot, auto_publish=False)
    reviews, extracts = [], []
    def revise(mode, task, packet, path):
        value = runner(mode, task, packet, path)
        if task == "review":
            assert "revision_feedback" not in packet
            reviews.append(packet)
            if len(reviews) == 1:
                value["classification_approved"] = False
                value["classification_notes"] = "Correct the synthetic classification."
        else:
            extracts.append(packet)
        return value
    result = ing.run_batch(batch["id"], pilot, runner=revise)
    assert result["status"] == "ready"
    assert len(extracts) == 2 and "revision_feedback" in extracts[1]
    assert len(reviews) == 2


def test_generic_scan_path_preserves_native_and_records_ocr(pilot):
    batch = ing.create_batch([("scan.pdf", pdf_bytes(scan=True))], pilot=pilot, auto_publish=False)
    result = ing.run_batch(batch["id"], pilot, runner=runner, ocr=lambda page: TITLE + "\n" + QUOTE)
    assert result["status"] == "ready"
    document = json.loads((ing.batch_path(batch["id"], pilot) / "prepared.json").read_text(encoding="utf-8"))["documents"][0]
    assert document["pages"][0]["native_text"] == ""
    assert document["elements"][0]["parser"] == "rapidocr"
    assert document["claims"][0]["evidence"][0]["parser"] == "rapidocr"


def test_confirmed_blank_page_is_preserved_without_ocr(pilot):
    pdf = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
    pdf.new_page()
    data = pdf.tobytes()
    pdf.close()
    batch = ing.create_batch([("blank-end.pdf", data)], pilot=pilot, auto_publish=False)
    result = ing.run_batch(batch["id"], pilot, runner=runner, ocr=lambda p: pytest.fail("empty pages need no OCR"))
    assert result["status"] == "ready"
    assert any("第 2 页为空白页" in w for w in result["documents"][0]["warnings"])


def test_page_with_only_header_and_rule_keeps_number_without_ocr(pilot):
    pdf = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
    page = pdf.new_page()
    page.insert_text((50, 40), "10     Chapter 2")
    page.insert_text((50, 200), "   ")
    page.draw_line((50, 60), (500, 60))
    data = pdf.tobytes()
    pdf.close()
    batch = ing.create_batch([("header-only.pdf", data)], pilot=pilot, auto_publish=False)
    result = ing.run_batch(batch["id"], pilot, runner=runner, ocr=lambda p: pytest.fail("header-only blank needs no OCR"))
    assert result["status"] == "ready", result
    prepared = json.loads((ing.batch_path(batch["id"], pilot) / "prepared.json").read_text(encoding="utf-8"))
    assert "Chapter 2" in prepared["documents"][0]["pages"][1]["text"]


def test_short_nontechnical_page_requires_source_bound_visual_review(pilot):
    pdf = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
    page = pdf.new_page()
    page.insert_text((50, 400), "Questions?")
    data = pdf.tobytes()
    pdf.close()
    created = ing.create_batch([("closing.pdf", data)], pilot=pilot)
    batch = json.loads((ing.batch_path(created["id"], pilot) / "batch.json").read_text(encoding="utf-8"))
    document = batch["documents"][0]
    with pytest.raises(ing.IntakeError, match="第 2 页"):
        ing.parse_document(document, ocr=lambda page: "Questions?")
    document.update(verified_nontext_pages=[2], nontext_review_source_sha256=document["sha256"])
    parsed = ing.parse_document(document, ocr=lambda p: pytest.fail("source-bound visually verified page needs no OCR"))
    assert "Questions?" in parsed["pages"][1]["text"]
    assert parsed["pages"][1]["nontechnical_page_reviewed"] is True
    document["nontext_review_source_sha256"] = "0" * 64
    with pytest.raises(ing.IntakeError, match="绑定当前PDF哈希"):
        ing.parse_document(document)


def test_same_title_different_file_in_batch_can_be_skipped(pilot):
    batch = ing.create_batch([("a.pdf", pdf_bytes()), ("b.pdf", pdf_bytes(TITLE + " revision"))], pilot=pilot, auto_publish=False)
    result = ing.run_batch(batch["id"], pilot, runner=runner)
    assert result["status"] == "needs_attention"
    assert result["documents"][0]["status"] == "prepared"
    assert "同一批次" in result["documents"][1]["error"]
    ing.skip_document(batch["id"], "item_001", pilot)
    assert ing.run_batch(batch["id"], pilot, runner=runner)["status"] == "ready"


def test_cancel_before_worker_starts_is_not_lost(pilot):
    batch = ing.create_batch([("a.pdf", pdf_bytes())], pilot=pilot)
    ing.cancel_batch(batch["id"], pilot)
    result = ing.run_batch(batch["id"], pilot, clear_cancel=False, runner=lambda *a: pytest.fail("cancelled"))
    assert result["status"] == "cancelled"


def test_status_reads_after_worker_releases_lock(pilot, monkeypatch):
    from contextlib import contextmanager
    batch = ing.create_batch([("a.pdf", pdf_bytes())], pilot=pilot)
    path = ing.batch_path(batch["id"], pilot) / "batch.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = "running"
    ing.write_atomic(path, raw)
    @contextmanager
    def worker_just_finished(lock_path):
        raw["status"] = "completed"
        ing.write_atomic(path, raw)
        yield
    monkeypatch.setattr(ing, "file_lock", worker_just_finished)
    status = ing.get_batch(batch["id"], pilot)
    assert status["status"] == "completed" and not status["running"]


def test_api_creation_origin_and_key_validation_do_not_echo_secrets(pilot, monkeypatch):
    from web import server
    from kg_pilot import ingestion_runners
    monkeypatch.setattr(server, "PILOT", pilot)
    monkeypatch.setattr(ingestion_runners, "provider_status", lambda: {"configured": False, "models": []})
    with TestClient(server.app) as client:
        body = {"mode": "api", "files": [{"name": "a.pdf", "content_base64": base64.b64encode(pdf_bytes()).decode()}]}
        assert client.post("/api/ingestion/batches", json=body, headers={"Origin": "https://evil.invalid"}).status_code == 403
        created = client.post("/api/ingestion/batches", json=body)
        assert created.status_code == 200
        assert "source_path" not in created.text
        assert client.get("/api/ingestion/batches").json()["batches"][0]["id"] == created.json()["id"]
        response = client.post("/api/ingestion/configure", json={"api_key": "do-not-echo-"*400})
        assert response.status_code == 422 and "do-not-echo" not in response.text
        assert client.post(f"/api/ingestion/batches/{created.json()['id']}/publish").status_code == 409


def test_industry_provenance_and_claim_type_survive_publication(pilot):
    source = {"publisher": "Fixture manufacturer", "document_number": "GUIDE-1", "version": "A",
              "source_url": "https://example.org/guide", "pdf_url": "https://example.org/guide.pdf",
              "access_route": "company_shared", "status": "Fixture current revision"}
    batch = ing.create_batch([("guide.pdf", pdf_bytes())], pilot=pilot, provenance=[source])
    def industry_runner(mode, task, packet, workdir):
        assert packet["metadata"]["provenance"] == source
        value = runner(mode, task, packet, workdir)
        if task == "extract":
            value["classification"].update(document_form="technical_manual", content_nature="technical_guidance", access_route="company_shared")
            value["claims"][0]["claim_type"] = "recommended_process"
        return value
    result = ing.run_batch(batch["id"], pilot, runner=industry_runner, publisher=partial(publish_batch, embedder=TestEmbedder()))
    assert result["status"] == "completed", result
    paper = json.loads((pilot / "catalog/papers.json").read_text(encoding="utf-8"))[0]
    assert paper["publisher"] == source["publisher"] and paper["version"] == "A"
    assert paper["publisher_url"] == source["source_url"] and paper["pdf_url"] == source["pdf_url"]
    claim = json.loads((pilot / "annotations/claims.jsonl").read_text(encoding="utf-8").strip())
    assert claim["claim_type"] == "recommended_process"


@pytest.mark.parametrize("count", [2, 10])
def test_parallel_documents_preserve_checkpoints_and_order(pilot, count):
    import threading
    barrier = threading.Barrier(count)
    files = [(f"document-{i}.pdf", pdf_bytes(f"{TITLE} {i}")) for i in range(count)]
    batch = ing.create_batch(files, pilot=pilot, auto_publish=False, workers=count)
    def concurrent_runner(mode, task, packet, workdir):
        value = runner(mode, task, packet, workdir)
        if task == "extract":
            barrier.wait(timeout=30)
            value["metadata"]["title"] = packet["metadata"]["filename"]
        return value
    result = ing.run_batch(batch["id"], pilot, runner=concurrent_runner)
    assert result["status"] == "ready", result
    assert result["progress"] == {"done": count, "total": count}
    prepared = json.loads((ing.batch_path(batch["id"], pilot) / "prepared.json").read_text(encoding="utf-8"))
    assert [d["paper"]["title"] for d in prepared["documents"]] == [name for name, _ in files]


@pytest.mark.parametrize("winerror", [32, 33, 5])
def test_atomic_checkpoint_survives_22_second_windows_reader_lock(tmp_path, monkeypatch, clock, winerror):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    original = Path.replace
    attempts = []
    def busy_then_replace(source, destination):
        if destination == target:
            attempts.append(1)
            if clock.elapsed < 22:
                assert json.loads(target.read_text()) == {"old": True}
                raise windows_error(winerror)
        return original(source, destination)
    monkeypatch.setattr(Path, "replace", busy_then_replace)
    monkeypatch.setattr(ing, "_delete_access_state", lambda _: "sharing")
    ing.write_atomic(target, {"new": True})
    assert len(attempts) > 6
    assert 22 <= clock.elapsed < 23
    assert json.loads(target.read_text()) == {"new": True}
    assert not list(tmp_path.glob("checkpoint.json.*.tmp"))


def test_atomic_checkpoint_has_deadline_and_keeps_both_complete_files(tmp_path, monkeypatch, clock):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(windows_error(32)))
    with pytest.raises(PermissionError) as failure:
        ing.write_atomic(target, {"new": True})
    assert clock.elapsed == pytest.approx(30)
    assert max(clock.sleeps) <= 0.5
    assert failure.value.checkpoint_details["elapsed_seconds"] == 30
    assert failure.value.checkpoint_details["retryable"] is True
    assert json.loads(target.read_text()) == {"old": True}
    candidates = list(tmp_path.glob("checkpoint.json.*.tmp"))
    assert len(candidates) == 1 and json.loads(candidates[0].read_text()) == {"new": True}


@pytest.mark.parametrize("target_state", ["available", "missing"])
def test_access_denied_with_available_delete_access_keeps_short_retry_window(tmp_path, monkeypatch, clock, target_state):
    target = tmp_path / "checkpoint.json"
    if target_state == "available":
        target.write_text('{"old": true}', encoding="utf-8")
    calls, probes = [], []
    def fail(*args):
        calls.append(1)
        raise windows_error(5)
    def probe(path):
        probes.append(path)
        return target_state if path == target else "available"
    monkeypatch.setattr(Path, "replace", fail)
    monkeypatch.setattr(ing, "_delete_access_state", probe)
    with pytest.raises(PermissionError) as failure:
        ing.write_atomic(target, {"new": True})
    assert len(calls) > 2 and clock.elapsed == pytest.approx(1.55)
    assert len(probes) == 2 * len(calls)
    details = failure.value.checkpoint_details
    assert details["retryable"] is True and details["retry_window_seconds"] == 1.55
    assert details["source_delete_access"] == "available" and details["target_delete_access"] == target_state


@pytest.mark.parametrize(("source_state", "target_state"), [("denied", "sharing"), ("sharing", "denied"),
                                                            ("readonly", "available"), ("available", "readonly")])
@pytest.mark.parametrize("winerror", [5, 32])
def test_explicit_source_or_target_permission_failure_prevents_retry(tmp_path, monkeypatch, clock, source_state, target_state, winerror):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    probes = []
    def probe(path):
        probes.append(path)
        return target_state if path == target else source_state
    monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(windows_error(winerror)))
    monkeypatch.setattr(ing, "_delete_access_state", probe)
    with pytest.raises(PermissionError) as failure:
        ing.write_atomic(target, {"new": True})
    assert len(probes) == 2 and not clock.sleeps
    assert failure.value.checkpoint_details["retryable"] is False


@pytest.mark.parametrize(("winerror", "code"), [(5, errno.EACCES), (None, errno.EACCES),
                                               (None, errno.EPERM), (32, errno.ENOSPC)])
def test_atomic_checkpoint_does_not_retry_permanent_permissions(tmp_path, monkeypatch, clock, winerror, code):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    attempts = []
    def fail(*args):
        attempts.append(1)
        raise windows_error(winerror, code)
    monkeypatch.setattr(Path, "replace", fail)
    monkeypatch.setattr(ing, "_delete_access_state", lambda _: "denied")
    with pytest.raises(PermissionError):
        ing.write_atomic(target, {"new": True})
    assert len(attempts) == 1 and not clock.sleeps
    assert json.loads(target.read_text()) == {"old": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing semantics")
def test_real_windows_reader_access_denied_is_probed_and_retried(tmp_path, monkeypatch, clock):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    with target.open("rb") as reader:
        assert ing._delete_access_state(target) == "sharing"
        def release(seconds):
            assert json.loads(target.read_text()) == {"old": True}
            reader.close()
            clock.sleep(seconds)
        monkeypatch.setattr(ing.time, "sleep", release)
        ing.write_atomic(target, {"new": True})
    assert clock.sleeps and json.loads(target.read_text()) == {"new": True}
    assert ing._delete_access_state(target) == "available"


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing semantics")
def test_real_windows_reader_closed_before_probe_still_retries(tmp_path, monkeypatch, clock):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    original = Path.replace
    failures, probes = [], []
    native_probe = ing._delete_access_state
    with target.open("rb") as reader:
        def close_after_denial(source, destination):
            try:
                return original(source, destination)
            except PermissionError as error:
                assert error.winerror == 5
                failures.append(error)
                reader.close()  # The real conflicting reader closes before either probe.
                raise
        def probe(path):
            state = native_probe(path)
            probes.append(state)
            return state
        monkeypatch.setattr(Path, "replace", close_after_denial)
        monkeypatch.setattr(ing, "_delete_access_state", probe)
        ing.write_atomic(target, {"new": True})
    assert len(failures) == 1 and probes == ["available", "available"]
    assert clock.sleeps == [0.05] and json.loads(target.read_text()) == {"new": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing semantics")
@pytest.mark.parametrize("reported_error", [None, 5])
def test_real_windows_temporary_source_reader_is_probed_and_retried(tmp_path, monkeypatch, clock, reported_error):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    original, native_probe = Path.replace, ing._delete_access_state
    readers, probes = [], []
    def hold_source(source, destination):
        if not readers:
            readers.append(source.open("rb"))
        try:
            return original(source, destination)
        except PermissionError:
            # Windows reports 32 for this source lock on this machine; also
            # cover the access-denied mapping while holding the real reader.
            if reported_error is not None:
                raise windows_error(reported_error) from None
            raise
    def probe(path):
        state = native_probe(path)
        probes.append((path == target, state))
        return state
    def release(seconds):
        readers[0].close()
        clock.sleep(seconds)
    monkeypatch.setattr(Path, "replace", hold_source)
    monkeypatch.setattr(ing, "_delete_access_state", probe)
    monkeypatch.setattr(ing.time, "sleep", release)
    try:
        ing.write_atomic(target, {"new": True})
    finally:
        for reader in readers:
            reader.close()
    assert probes == [(False, "sharing"), (True, "available")]
    assert clock.sleeps == [0.05] and json.loads(target.read_text()) == {"new": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attributes")
def test_real_windows_read_only_target_is_not_retried_even_with_reader(tmp_path, monkeypatch, clock):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"old": true}', encoding="utf-8")
    target.chmod(stat.S_IREAD)
    try:
        with target.open("rb"):
            assert ing._delete_access_state(target) == "readonly"
            with pytest.raises(PermissionError):
                ing.write_atomic(target, {"new": True})
        assert not clock.sleeps
        assert json.loads(target.read_text()) == {"old": True}
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize("workers", [1, 3])
def test_initial_document_checkpoint_failure_stops_new_work_and_records_safe_error(pilot, monkeypatch, clock, workers):
    files = [(f"fixture-{i}.pdf", pdf_bytes(f"{TITLE} {i}")) for i in range(3)]
    batch = ing.create_batch(files, pilot=pilot, auto_publish=False, workers=workers)
    path = ing.batch_path(batch["id"], pilot)
    original = Path.replace
    failed = False
    def fail_first_start(source, destination):
        nonlocal failed
        candidate = json.loads(source.read_text(encoding="utf-8"))
        if (destination == path / "batch.json" and not failed and
                any(d["status"] == "processing" for d in candidate["documents"])):
            previous = json.loads(destination.read_text(encoding="utf-8"))
            assert not any(d["status"] == "processing" for d in previous["documents"])
            failed = clock.elapsed >= 30
            raise windows_error(32)
        return original(source, destination)
    monkeypatch.setattr(Path, "replace", fail_first_start)
    result = ing.run_batch(batch["id"], pilot, runner=lambda *a: pytest.fail("no model work after failed start"))
    assert result["status"] == "needs_attention" and result["stage"] == "checkpoint_failed"
    first, = [d for d in result["documents"] if d["status"] == "needs_attention"]
    queued = [d for d in result["documents"] if d is not first]
    assert first["status"] == "needs_attention" and first["stage"] == "checkpoint_failed"
    assert all(d["status"] == "queued" for d in queued)
    assert not (path / "documents").exists()
    assert first["error_details"]["winerror"] == 32
    diagnostics = list(path.glob("checkpoint_error.*.json"))
    assert len(diagnostics) == 1
    diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert diagnostic["errno"] == errno.EACCES and diagnostic["elapsed_seconds"] == 30
    assert "sensitive test" not in json.dumps(result) + diagnostics[0].read_text(encoding="utf-8")
    assert json.loads((pilot / "catalog/papers.json").read_text()) == []


def test_batch_start_save_failure_preserves_last_checkpoint_and_safe_diagnostic(pilot, monkeypatch, clock):
    batch = ing.create_batch([("fixture.pdf", pdf_bytes())], pilot=pilot, auto_publish=False)
    path = ing.batch_path(batch["id"], pilot)
    original = (path / "batch.json").read_bytes()
    monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(windows_error(5)))
    monkeypatch.setattr(ing, "_delete_access_state", lambda _: "denied")
    with pytest.raises(PermissionError):
        ing.run_batch(batch["id"], pilot, runner=lambda *a: pytest.fail("must not start models"))
    assert (path / "batch.json").read_bytes() == original
    assert not clock.sleeps
    assert not (path / "documents").exists()
    diagnostic, = path.glob("checkpoint_error.*.json")
    value = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert value["winerror"] == 5 and value["retryable"] is False
    assert "sensitive test" not in diagnostic.read_text(encoding="utf-8")


def test_preparation_failure_is_not_reported_as_publication(pilot, monkeypatch):
    batch = ing.create_batch([("fixture.pdf", pdf_bytes())], pilot=pilot, auto_publish=False)
    path = ing.batch_path(batch["id"], pilot)
    original = ing.write_atomic
    def fail_aggregate(target, data):
        if target == path / "prepared.json":
            raise OSError("simulated checkpoint failure")
        return original(target, data)
    monkeypatch.setattr(ing, "write_atomic", fail_aggregate)
    result = ing.run_batch(batch["id"], pilot, runner=runner,
                           publisher=lambda *a, **k: pytest.fail("must not publish"))
    assert result["status"] == "failed"
    assert result["stage"] == "preparation_failed"
    assert (path / "documents/item_000/prepared.json").exists()
    assert json.loads((pilot / "catalog/papers.json").read_text()) == []
    monkeypatch.setattr(ing, "write_atomic", original)
    assert ing.run_batch(batch["id"], pilot, runner=lambda *a: pytest.fail("reuse checkpoint"))["status"] == "ready"


def test_extra_classification_context_uses_hash_bound_original_page(pilot, monkeypatch):
    pdf = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
    pdf.new_page().insert_text((50, 100), "Methods: actual samples are diced chips, not intact wafers.")
    pdf.new_page().insert_text((50, 100), "Synthetic final page with sufficient source text for parsing.")
    batch = ing.create_batch([("context.pdf", pdf.tobytes())], pilot=pilot, auto_publish=False)
    pdf.close()
    path = ing.batch_path(batch["id"], pilot)
    saved = json.loads((path / "batch.json").read_text(encoding="utf-8"))
    document = saved["documents"][0]
    document.update(classification_context_pages=[2], classification_context_source_sha256=document["sha256"])
    ing.write_atomic(path / "batch.json", saved)
    monkeypatch.setitem(ing.LIMITS, "chunk_chars", 220)
    observed = []
    def inspect_context(mode, task, packet, workdir):
        observed.append(packet)
        assert any(p["pdf_page"] == 2 and "actual samples are diced chips" in p["text"] for p in packet["pages"])
        raise ing.IntakeError("fixture: stop after observing input")
    result = ing.run_batch(batch["id"], pilot, runner=inspect_context)
    assert result["status"] == "needs_attention" and observed
    saved = json.loads((path / "batch.json").read_text(encoding="utf-8"))
    saved["documents"][0]["classification_context_source_sha256"] = "0" * 64
    ing.write_atomic(path / "batch.json", saved)
    result = ing.run_batch(batch["id"], pilot, runner=lambda *a: pytest.fail("wrong source hash"))
    assert "绑定当前PDF哈希" in result["documents"][0]["error"]


@pytest.mark.parametrize("all_unsafe", [False, True])
def test_unsafe_candidates_are_quarantined_while_clean_claims_publish(pilot, monkeypatch, all_unsafe):
    # Repeat a bounded page chunk to exercise accepted/quarantine ID allocation
    # across chunks without inventing another scientific source document.
    monkeypatch.setattr(ing, "chunks_for", lambda pages: [[pages[0]], [pages[0]]])
    batch = ing.create_batch([("mixed.pdf", pdf_bytes())], pilot=pilot, max_claims=8)
    reviewed = []

    def mixed_runner(mode, task, packet, workdir):
        if task == "extract":
            value = proposal()
            unsafe = deepcopy(value["claims"][0])
            unsafe.update(statement="The proposed width is 5 \ue001m.", value_text="5", unit="\ue001m")
            value["claims"] = [unsafe] + ([] if all_unsafe else value["claims"])
            return value
        assert "quarantined_claims" not in packet["proposal"]
        reviewed.extend(packet["proposal"]["claims"])
        return runner(mode, task, packet, workdir)

    result = ing.run_batch(batch["id"], pilot, runner=mixed_runner,
                           publisher=partial(publish_batch, embedder=TestEmbedder()))
    assert result["status"] == "completed", result
    doc_id = result["documents"][0]["doc_id"]
    active = [json.loads(line) for line in (pilot / "annotations/claims.jsonl").read_text(encoding="utf-8").splitlines()]
    expected = 0 if all_unsafe else 2
    assert len(active) == len(reviewed) == expected
    assert [claim["claim_id"] for claim in active] == [f"{doc_id}_c{i+1:03d}" for i in range(expected)]
    pending_path = pilot / "parsed" / doc_id / "text_quality_quarantine.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending["quarantined_claims"] == 2
    assert len({record["claim"]["claim_id"] for record in pending["records"]}) == 2
    assert not {record["claim"]["claim_id"] for record in pending["records"]} & {claim["claim_id"] for claim in active}
    assert all(record["claim"]["evidence"][0]["quote"] == QUOTE for record in pending["records"])
    assert all(record["claim"]["unit"] == "\ue001m" and record["source_sha256"] == pending["source_sha256"]
               for record in pending["records"])
    assert any("隔离 2 条" in warning for warning in result["documents"][0]["warnings"])
    before = pending_path.read_bytes()
    assert ing.run_batch(batch["id"], pilot, runner=lambda *args: pytest.fail("Completed batch must reuse result"))["status"] == "completed"
    assert pending_path.read_bytes() == before
