"""Bounded, process-local generation diagnostics without research/private text.

Only explicit metadata fields cross this boundary. Nothing is written to disk;
questions, conversations, evidence text, model output and exception messages are
not accepted by the public API. Running requests can be inspected independently
of the generation lock.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from time import monotonic
from uuid import uuid4

_MAX_RUNS = 30
_MAX_STAGES = 64
_LOCK = threading.Lock()
_RUNS: OrderedDict[str, dict] = OrderedDict()
_STAGES = frozenset({"starting", "retrieving", "generating", "validating", "retrying", "completed", "failed"})
_ERROR_CODES = frozenset({
    "invalid_input", "not_configured", "busy", "retrieval_error", "provider_error", "missing_key",
    "truncated_output", "unusable_output", "invalid_json", "invalid_idea_field", "invalid_idea_count",
    "invalid_idea_schema", "duplicate_idea", "invalid_source_ids", "unsupported_absence_claim",
    "unknown_source", "missing_basis_citation", "unsupported_novelty", "format_retry_exhausted", "internal_error",
})
_SOURCE_ID = re.compile(r"S[1-9][0-9]{0,5}\Z")
_DOCUMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


def _error_code(value):
    if value is None:
        return None
    return value if isinstance(value, str) and value in _ERROR_CODES else "internal_error"


def _idea_count(value) -> int:
    return value if type(value) is int and 0 <= value <= 5 else 0


def _source_refs(sources) -> list[dict]:
    result, seen = [], set()
    if not isinstance(sources, (list, tuple)):
        return result
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id, doc_id, page = (source.get(k) for k in ("id", "doc_id", "pdf_page"))
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id) or source_id in seen:
            continue
        if not isinstance(doc_id, str) or not _DOCUMENT_ID.fullmatch(doc_id):
            continue
        if type(page) is not int or not 1 <= page <= 100000:
            continue
        seen.add(source_id)
        result.append({"id": source_id, "doc_id": doc_id, "pdf_page": page})
        if len(result) == 20:
            break
    return result


def _elapsed(run: dict, now: float) -> int:
    end = run["_finished"] if run["_finished"] is not None else now
    return max(0, int((end - run["_started"]) * 1000))


def _record_stage(run: dict, stage: str, now: float, error_code=None):
    if run["stages"] and run["stages"][-1]["stage"] == stage:
        # Retrieval records its phase before work and attaches source identities
        # afterward. Keep that as one phase with its original start time.
        if error_code is None:
            run["stages"][-1].pop("error_code", None)
        else:
            run["stages"][-1]["error_code"] = error_code
        return
    event = {"stage": stage, "elapsed_ms": _elapsed(run, now)}
    if error_code is not None:
        event["error_code"] = error_code
    run["stage"] = stage
    run["stages"].append(event)
    if len(run["stages"]) > _MAX_STAGES:
        # Retain the start event and a bounded tail even if callers emit many
        # duplicate phases. Actual generation has only one bounded retry.
        del run["stages"][1]


def start_run() -> str:
    """Create a new running trace and return an opaque identifier."""
    run_id = uuid4().hex
    now = monotonic()
    with _LOCK:
        _RUNS[run_id] = {
            "run_id": run_id, "stage": "starting", "status": "running", "source_refs": [],
            "idea_count": 0, "error_code": None,
            "stages": [{"stage": "starting", "elapsed_ms": 0}],
            "_started": now, "_finished": None,
        }
        while len(_RUNS) > _MAX_RUNS:
            _RUNS.popitem(last=False)
    return run_id


def update_run(run_id: str, stage: str, *, sources=None, idea_count=None, error_code=None) -> None:
    """Record an allowlisted phase; arbitrary strings never become trace data."""
    if not isinstance(stage, str) or stage not in _STAGES:
        raise ValueError("Unsupported diagnostic stage.")
    safe_sources = _source_refs(sources) if sources is not None else None
    safe_error = _error_code(error_code)
    now = monotonic()
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None or run["status"] != "running":
            return
        if safe_sources is not None:
            run["source_refs"] = safe_sources
        if idea_count is not None:
            run["idea_count"] = _idea_count(idea_count)
        run["error_code"] = safe_error
        if stage in ("completed", "failed"):
            run["status"] = stage
            run["_finished"] = now
            run["error_code"] = None if stage == "completed" else safe_error or "internal_error"
        _record_stage(run, stage, now, run["error_code"])


def finish_run(run_id: str, *, completed: bool, sources=None, idea_count=0, error_code=None) -> None:
    """End a trace once. Failure details are restricted to safe error codes."""
    update_run(run_id, "completed" if completed else "failed", sources=sources,
               idea_count=idea_count, error_code=None if completed else error_code or "internal_error")


def list_runs() -> list[dict]:
    """Return newest-first snapshots; elapsed time advances for active runs."""
    now = monotonic()
    with _LOCK:
        return [{
            "run_id": run["run_id"], "stage": run["stage"], "status": run["status"],
            "elapsed_ms": _elapsed(run, now), "source_refs": [dict(source) for source in run["source_refs"]],
            "idea_count": run["idea_count"], "error_code": run["error_code"],
            "stages": [dict(event) for event in run["stages"]],
        } for run in reversed(_RUNS.values())]
