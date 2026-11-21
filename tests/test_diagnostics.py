"""Pure-memory diagnostics tests: no provider, files, secrets or tmp_path."""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import pytest

from kg_pilot import diagnostics


@pytest.fixture(autouse=True)
def isolated_runs(monkeypatch):
    monkeypatch.setattr(diagnostics, "_RUNS", OrderedDict())


def test_active_stages_are_readable_and_elapsed_advances_then_stops(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(diagnostics, "monotonic", lambda: clock[0])
    run_id = diagnostics.start_run()
    clock[0] = 100.25
    diagnostics.update_run(run_id, "retrieving")
    clock[0] = 100.5
    diagnostics.update_run(run_id, "generating", sources=[{"id": "S1", "doc_id": "real_doc", "pdf_page": 3}])
    active = diagnostics.list_runs()[0]
    assert active["run_id"] == run_id and active["status"] == "running" and active["stage"] == "generating"
    assert active["elapsed_ms"] == 500
    assert [event["stage"] for event in active["stages"]] == ["starting", "retrieving", "generating"]
    clock[0] = 101.0
    assert diagnostics.list_runs()[0]["elapsed_ms"] == 1000
    diagnostics.update_run(run_id, "validating")
    diagnostics.finish_run(run_id, completed=True, idea_count=3)
    clock[0] = 200.0
    final = diagnostics.list_runs()[0]
    assert final["status"] == final["stage"] == "completed"
    assert final["idea_count"] == 3 and final["elapsed_ms"] == 1000
    assert final["error_code"] is None and final["stages"][-1]["stage"] == "completed"


def test_only_source_identity_and_safe_errors_are_saved():
    private = "PRIVATE-PROMPT-TEXT-ANSWER-KEY-MUST-NOT-SAVE"
    run_id = diagnostics.start_run()
    source = {"id": "S1", "doc_id": "doc_123", "pdf_page": 6,
              "text": private, "title": private, "reviewed_claims": [{"statement": private}],
              "question": private, "history": private, "api_key": private, "model_output": private}
    diagnostics.update_run(run_id, "retrieving", sources=[source], error_code=private)
    diagnostics.finish_run(run_id, completed=False, error_code=private)
    final = diagnostics.list_runs()[0]
    assert final["source_refs"] == [{"id": "S1", "doc_id": "doc_123", "pdf_page": 6}]
    assert final["error_code"] == "internal_error"
    serialized = json.dumps(final)
    assert private not in serialized
    assert all(key not in final for key in ("question", "history", "text", "model_output", "api_key", "title"))
    assert set(final) == {"run_id", "stage", "status", "elapsed_ms", "source_refs", "idea_count", "error_code", "stages"}


def test_retry_reason_and_failure_codes_are_visible_without_error_messages():
    run_id = diagnostics.start_run()
    for stage in ("retrieving", "generating", "validating"):
        diagnostics.update_run(run_id, stage)
    diagnostics.update_run(run_id, "retrying", error_code="unsupported_absence_claim")
    diagnostics.update_run(run_id, "generating")
    diagnostics.finish_run(run_id, completed=False, error_code="unknown_source")
    final = diagnostics.list_runs()[0]
    assert final["status"] == final["stage"] == "failed"
    assert final["error_code"] == "unknown_source"
    assert next(e for e in final["stages"] if e["stage"] == "retrying")["error_code"] == "unsupported_absence_claim"
    assert [e["stage"] for e in final["stages"]] == [
        "starting", "retrieving", "generating", "validating", "retrying", "generating", "failed"]


def test_snapshots_are_deep_copies_and_terminal_trace_cannot_be_reopened():
    run_id = diagnostics.start_run()
    diagnostics.update_run(run_id, "retrieving", sources=[{"id": "S1", "doc_id": "doc", "pdf_page": 1}])
    snapshot = diagnostics.list_runs()[0]
    snapshot["source_refs"][0]["doc_id"] = "altered"
    snapshot["stages"][0]["stage"] = "altered"
    assert diagnostics.list_runs()[0]["source_refs"][0]["doc_id"] == "doc"
    assert diagnostics.list_runs()[0]["stages"][0]["stage"] == "starting"
    diagnostics.finish_run(run_id, completed=True, idea_count=1)
    diagnostics.update_run(run_id, "generating", idea_count=5)
    diagnostics.finish_run(run_id, completed=False, error_code="provider_error")
    assert diagnostics.list_runs()[0]["stage"] == "completed"
    assert diagnostics.list_runs()[0]["idea_count"] == 1


def test_thirty_newest_runs_are_kept_in_creation_order():
    ids = [diagnostics.start_run() for _ in range(35)]
    runs = diagnostics.list_runs()
    assert len(runs) == 30 and [run["run_id"] for run in runs] == list(reversed(ids[-30:]))
    diagnostics.update_run(ids[0], "generating")
    diagnostics.finish_run(ids[0], completed=False, error_code="provider_error")
    assert len(diagnostics.list_runs()) == 30


def test_sources_are_bounded_filtered_and_not_shared_with_caller():
    valid = [{"id": f"S{i}", "doc_id": "doc", "pdf_page": i} for i in range(1, 26)]
    invalid = [{"id": "bad", "doc_id": "doc", "pdf_page": 1},
               {"id": "S90", "doc_id": "text with private spaces", "pdf_page": 1},
               {"id": "S91", "doc_id": "doc", "pdf_page": True},
               {"id": "S92", "doc_id": "doc", "pdf_page": 0}]
    run_id = diagnostics.start_run()
    diagnostics.update_run(run_id, "retrieving", sources=[*invalid, valid[0], valid[0], *valid[1:]])
    valid[0]["doc_id"] = "changed"
    refs = diagnostics.list_runs()[0]["source_refs"]
    assert len(refs) == 20 and [ref["id"] for ref in refs] == [f"S{i}" for i in range(1, 21)]
    assert refs[0]["doc_id"] == "doc"


def test_untrusted_phase_and_error_values_do_not_enter_trace():
    run_id = diagnostics.start_run()
    with pytest.raises(ValueError, match="Unsupported diagnostic stage"):
        diagnostics.update_run(run_id, "PRIVATE-UNTRUSTED-PHASE")
    diagnostics.update_run(run_id, "retrying", error_code={"private": "do not save"}, idea_count=True)
    run = diagnostics.list_runs()[0]
    assert run["stage"] == "retrying" and run["error_code"] == "internal_error" and run["idea_count"] == 0
    assert "PRIVATE" not in json.dumps(run) and "do not save" not in json.dumps(run)


def test_list_runs_remains_available_while_worker_is_generating():
    run_id = diagnostics.start_run()
    started, release = threading.Event(), threading.Event()

    def worker():
        diagnostics.update_run(run_id, "generating")
        started.set()
        assert release.wait(2)
        diagnostics.finish_run(run_id, completed=True, idea_count=1)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(2)
        assert diagnostics.list_runs()[0]["stage"] == "generating"
        assert diagnostics.list_runs()[0]["status"] == "running"
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and diagnostics.list_runs()[0]["stage"] == "completed"


def test_concurrent_updates_and_reads_keep_complete_bounded_snapshots():
    def worker(index):
        run_id = diagnostics.start_run()
        diagnostics.update_run(run_id, "retrieving", sources=[{"id": "S1", "doc_id": f"doc_{index}", "pdf_page": 1}])
        for stage in ("generating", "validating"):
            diagnostics.update_run(run_id, stage)
            assert len(diagnostics.list_runs()) <= 30
        diagnostics.finish_run(run_id, completed=True, idea_count=1)
        return run_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(worker, range(80)))
    runs = diagnostics.list_runs()
    assert len(ids) == len(set(ids)) == 80
    assert len(runs) == 30 and len({run["run_id"] for run in runs}) == 30
    assert all(run["stage"] == "completed" and run["status"] == "completed" for run in runs)
    assert all(run["stages"][0]["stage"] == "starting" and run["stages"][-1]["stage"] == "completed" for run in runs)


def test_stage_history_is_itself_bounded():
    run_id = diagnostics.start_run()
    for _ in range(50):
        diagnostics.update_run(run_id, "generating")
        diagnostics.update_run(run_id, "validating")
    diagnostics.finish_run(run_id, completed=True)
    stages = diagnostics.list_runs()[0]["stages"]
    assert len(stages) <= 64 and stages[0]["stage"] == "starting" and stages[-1]["stage"] == "completed"


def test_repeated_phase_can_attach_sources_without_duplicate_stage():
    run_id = diagnostics.start_run()
    diagnostics.update_run(run_id, "retrieving")
    diagnostics.update_run(run_id, "retrieving", sources=[{"id": "S1", "doc_id": "doc", "pdf_page": 1}])
    run = diagnostics.list_runs()[0]
    assert [event["stage"] for event in run["stages"]] == ["starting", "retrieving"]
    assert run["source_refs"] == [{"id": "S1", "doc_id": "doc", "pdf_page": 1}]
