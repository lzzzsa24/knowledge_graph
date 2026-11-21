"""Admission checks for active Codex handoffs; no real models or credentials."""
import json
import hashlib
import pytest
from scripts.agent_intake import bridge
from kg_pilot.ingestion import IntakeError, write_atomic


def test_handoff_requires_matching_sources_and_distinct_reviewer(tmp_path):
    packet = {"pages": [{"pdf_page": 1, "text": "Synthetic source for a handoff test."}],
              "proposal": {"claims": []}, "taxonomy": {}, "metadata": {}}
    with pytest.raises(IntakeError, match="等待"):
        bridge("codex", "review", packet, tmp_path)
    request = json.loads((tmp_path / "review.agent_request.json").read_text(encoding="utf-8"))
    response = {"classification_approved": True, "classification_notes": "Synthetic fixture only.",
                "claims": [], "summary": "Synthetic fixture only."}
    saved = {"signature": request["signature"], "actor": "/root/reviewer_fixture", "response": response}
    write_atomic(tmp_path / "review.agent_response.json", saved)
    accepted = bridge("codex", "review", packet, tmp_path)
    assert accepted["_result_meta"]["actor"] == "/root/reviewer_fixture"
    changed = {**packet, "pages": [{"pdf_page": 1, "text": "A different synthetic source."}]}
    with pytest.raises(IntakeError, match="任务包已更新"):
        bridge("codex", "review", changed, tmp_path)
    write_atomic(tmp_path / "extract.agent_response.json", {"actor": "/root/reviewer_fixture"})
    with pytest.raises(IntakeError, match="不同 Codex agent"):
        bridge("codex", "review", packet, tmp_path)


@pytest.mark.parametrize("published,skipped", [(False, False), (True, False), (False, True)])
def test_revised_reply_reopens_unpublished_review_without_touching_published_batch(tmp_path, monkeypatch, published, skipped):
    from scripts import agent_intake
    from kg_pilot import ingestion
    docdir = tmp_path / "documents/item_000"
    workdir = docdir / "chunk_000"
    workdir.mkdir(parents=True)
    write_atomic(docdir / "parsed.json", {})
    response = workdir / "extract.agent_response.json"
    write_atomic(response, {"response": "old synthetic reply"})
    write_atomic(workdir / "extract.json", {"output": {"_result_meta": {
        "execution": "active_codex_agent", "response_sha256": hashlib.sha256(response.read_bytes()).hexdigest()}}})
    write_atomic(workdir / "review.json", {"output": {"old_review": True}})
    write_atomic(response, {"response": "revised synthetic reply"})
    batch = {"id": "fixture", "status": "completed" if published else "ready", "events": [],
             "progress": {"done": 1, "total": 1}, "documents": [{"id": "item_000", "status": "skipped" if skipped else "prepared"}]}
    write_atomic(tmp_path / "batch.json", batch)
    monkeypatch.setattr(ingestion, "batch_path", lambda _: tmp_path)
    def resume(*args, **kwargs):
        saved = json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))
        assert (workdir / "extract.json").exists() == (published or skipped)
        assert (workdir / "review.json").exists() == (published or skipped)
        if not published and not skipped:
            assert saved["documents"][0]["status"] == "needs_attention"
            assert len(list(workdir.glob("*.rejected.*.json"))) == 2
        return saved
    monkeypatch.setattr(ingestion, "run_batch", resume)
    agent_intake.advance("fixture")


def test_publication_requires_latest_reviewed_agent_reply(tmp_path):
    from scripts.publish_expansion_batch import ensure_latest_agent_replies
    workdir = tmp_path / "documents/item_000/chunk_000"
    workdir.mkdir(parents=True)
    reply = workdir / "extract.agent_response.json"
    write_atomic(reply, {"response": "reviewed fixture"})
    write_atomic(workdir / "extract.json", {"output": {"_result_meta": {
        "execution": "active_codex_agent", "response_sha256": hashlib.sha256(reply.read_bytes()).hexdigest()}}})
    ensure_latest_agent_replies(tmp_path)
    write_atomic(reply, {"response": "new revision awaiting review"})
    with pytest.raises(ValueError, match="resume extraction/review"):
        ensure_latest_agent_replies(tmp_path)
