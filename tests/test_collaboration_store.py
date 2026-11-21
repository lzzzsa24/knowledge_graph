from __future__ import annotations

import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from kg_pilot.collaboration_store import CollaborationError, CollaborationStore


@pytest.fixture
def workspace():
    # Use the same normal ACLs as the project on Windows.
    cache = (Path(__file__).resolve().parents[1] / ".cache").resolve()
    directory = cache / f"collaboration_store_{uuid4().hex}"
    root = directory / "project"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        assert directory.resolve() == directory and directory.parent == cache
        shutil.rmtree(directory)


def ids(page):
    return [message["id"] for message in page["messages"]]


def assert_code(error, code):
    assert error.value.code == code


def test_ack_targets_snapshot_ids_and_broadcast_is_per_recipient(workspace):
    codex = CollaborationStore(workspace, "Codex")
    claude = CollaborationStore(workspace, "Claude")
    other = CollaborationStore(workspace, "other")
    first = codex.send_message(to="claude", body="先读这条", request_id="1")
    announcement = codex.send_message(to="broadcast", body="共同更新", request_id="2")
    page = claude.list_messages(unread_only=True, limit=1)
    assert ids(page) == [first["id"]]
    assert page["has_more"]
    assert ids(claude.list_messages(unread_only=True, limit=1)) == [first["id"]]

    new = codex.send_message(to="claude", body="刚到的新消息", request_id="3")
    assert claude.ack_messages(message_ids=[first["id"]], request_id="1") == {"acked_ids": [first["id"]]}
    assert ids(claude.list_messages(after_id=page["next_cursor"], unread_only=True)) == [announcement["id"], new["id"]]
    with pytest.raises(CollaborationError) as error:
        other.ack_messages(message_ids=[announcement["id"], new["id"]], request_id="rollback")
    assert_code(error, "message_missing")
    assert ids(other.list_messages(unread_only=True)) == [announcement["id"]]
    claude.ack_messages(message_ids=[announcement["id"]], request_id="2")
    assert ids(claude.list_messages(unread_only=True)) == [new["id"]]
    assert ids(other.list_messages(unread_only=True)) == [announcement["id"]]
    assert ids(codex.list_messages()) == []
    assert ids(codex.list_messages(box="sent")) == [first["id"], announcement["id"], new["id"]]
    assert claude.list_messages()["messages"][0]["acked"] is True


def test_reply_visibility_persistence_and_request_deduplication(workspace):
    codex = CollaborationStore(workspace, "codex")
    claude = CollaborationStore(workspace, "claude")
    args = dict(to="claude", body="请检查附件路径", attachments=["docs\\plan.md"], request_id="send")
    first = codex.send_message(**args)
    assert first["attachments"] == ["docs/plan.md"]
    assert first["thread_id"] == first["id"]
    reopened = CollaborationStore(workspace, "codex")
    assert reopened.send_message(**args) == first
    with pytest.raises(CollaborationError) as error:
        reopened.send_message(**(args | {"body": "另一条消息"}))
    assert_code(error, "request_conflict")
    with pytest.raises(CollaborationError) as error:
        codex.create_task(title="another operation", description="", request_id="send")
    assert_code(error, "request_conflict")

    reply = claude.send_message(to="codex", body="收到", reply_to=first["id"], request_id="send")
    assert reply["reply_to"] == first["id"]
    assert ids(codex.list_messages(thread_id=first["id"], box="all")) == [first["id"], reply["id"]]
    with pytest.raises(CollaborationError) as error:
        claude.send_message(to="*", body="意外公开回复", reply_to=first["id"], request_id="private")
    assert_code(error, "invalid_recipient")
    other = CollaborationStore(workspace, "other")
    assert ids(other.list_messages(thread_id=first["id"], box="all")) == []
    with pytest.raises(CollaborationError) as error:
        other.send_message(to="codex", body="猜测父消息 ID", reply_to=first["id"], request_id="guess")
    assert_code(error, "message_missing")


def test_simultaneous_claim_from_two_connections(workspace):
    barrier = Barrier(2, timeout=5)

    def connect(agent):
        barrier.wait()
        return CollaborationStore(workspace, agent)

    def claim(store):
        barrier.wait()
        try:
            return store.claim_task(task_id=task["id"], request_id="claim")
        except CollaborationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        codex, claude = executor.map(connect, ["codex", "claude"])
        task = codex.create_task(title="重建索引", description="等候认领", request_id="new")
        results = list(executor.map(claim, [codex, claude]))
    assert results.count("task_claimed") == 1
    winner = next(value for value in results if isinstance(value, dict))
    winner_store = codex if winner["owner"] == "codex" else claude
    other_store = claude if winner_store is codex else codex
    assert winner_store.claim_task(task_id=task["id"], request_id="claim") == winner
    assert "lease_token" not in other_store.list_tasks(status="claimed")["tasks"][0]
    with pytest.raises(CollaborationError) as error:
        other_store.release_task(task_id=task["id"], lease_token=winner["lease_token"], request_id="steal")
    assert_code(error, "lease_invalid")
    with closing(sqlite3.connect(codex.database)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_scope_conflicts_and_expired_tokens(workspace):
    now = 1800000000
    codex = CollaborationStore(workspace, "codex", clock=lambda: now)
    claude = CollaborationStore(workspace, "claude", clock=lambda: now)
    group = codex.create_task(
        title="导出", description="", request_id="group", files=[{"path": "docs/plans", "kind": "directory"}],
    )
    child = claude.create_task(
        title="编辑", description="", request_id="child", files=[{"path": "docs/plans/a.md", "kind": "file"}],
    )
    sibling = claude.create_task(
        title="审阅", description="", request_id="sibling", files=[{"path": "docs/plans_old/a.md", "kind": "file"}],
    )
    original = codex.claim_task(task_id=group["id"], request_id="claim", lease_seconds=2)
    claude.claim_task(task_id=sibling["id"], request_id="other-claim")
    with pytest.raises(CollaborationError) as error:
        claude.claim_task(task_id=child["id"], request_id="claim", files=[])
    assert_code(error, "invalid_arguments")
    with pytest.raises(CollaborationError) as error:
        claude.claim_task(task_id=child["id"], request_id="claim")
    assert_code(error, "scope_conflict")
    now += 1
    renewed = codex.renew_task(task_id=group["id"], lease_token=original["lease_token"], request_id="renew", lease_seconds=3)
    assert renewed["lease_token"] == original["lease_token"]
    assert renewed["lease_expires_at"] > original["lease_expires_at"]
    now += 3
    assert [task["id"] for task in codex.list_tasks(status="open")["tasks"]] == [group["id"], child["id"]]
    with pytest.raises(CollaborationError) as error:
        codex.finish_task(task_id=group["id"], lease_token=original["lease_token"], request_id="expired")
    assert_code(error, "lease_invalid")
    claimed = claude.claim_task(task_id=child["id"], request_id="claim")
    with pytest.raises(CollaborationError) as error:
        codex.claim_task(task_id=group["id"], request_id="reclaim")
    assert_code(error, "scope_conflict")
    finished = claude.finish_task(task_id=child["id"], lease_token=claimed["lease_token"], request_id="finish", result="已完成")
    assert finished["status"] == "completed"
    assert finished["completed_by"] == "claude"
    assert claude.finish_task(task_id=child["id"], lease_token=claimed["lease_token"], request_id="finish", result="已完成") == finished
    reclaimed = codex.claim_task(task_id=group["id"], request_id="reclaim")
    assert reclaimed["lease_token"] != original["lease_token"]
    with pytest.raises(CollaborationError) as error:
        codex.release_task(task_id=group["id"], lease_token=original["lease_token"], request_id="stale")
    assert_code(error, "lease_invalid")
    released = codex.release_task(task_id=group["id"], lease_token=reclaimed["lease_token"], request_id="release")
    assert released["status"] == "open"


@pytest.mark.parametrize("path", [
    "../other.txt", "docs/../notes.txt", "/notes.txt", "F:notes.txt", "F:/notes.txt", "a/CON.txt",
    "//example.com/share/a.md", "pilot/secrets/api-key.txt", "pilot/llm_config.json", ".env",
    "subfolder/.env.local", "pilot/ingestion_llm_config.json", "pilot/collaboration/agent_collaboration.sqlite3-wal",
])
def test_invalid_or_sensitive_attachment_paths_are_rejected(workspace, path):
    store = CollaborationStore(workspace, "codex")
    with pytest.raises(CollaborationError) as error:
        store.send_message(to="claude", body="check", attachments=[path], request_id="bad-path")
    assert_code(error, "invalid_arguments")
    assert ids(store.list_messages(box="sent")) == []


def test_symlink_or_junction_cannot_point_outside_project(workspace):
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("outside", encoding="utf-8")
    alias = workspace / "alias"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(alias))
    else:
        alias.symlink_to(outside, target_is_directory=True)
    store = CollaborationStore(workspace, "codex")
    for path in ("alias/notes.md", "alias/planned.md"):
        with pytest.raises(CollaborationError) as error:
            store.send_message(to="claude", body="check", attachments=[path], request_id="escape")
        assert_code(error, "invalid_arguments")


def test_refuses_foreign_database_without_altering_it(workspace):
    database = workspace / "other.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE other (id INTEGER)")
        connection.execute("INSERT INTO other VALUES (42)")
    with pytest.raises(CollaborationError) as error:
        CollaborationStore(workspace, "codex", database)
    assert_code(error, "database_mismatch")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT id FROM other").fetchone()[0] == 42
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
