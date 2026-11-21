"""Exercise the collaboration MCP wire using isolated local agent processes."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from kg_pilot.mcp_server import RESULT_SCHEMA


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collaboration_mcp.py"
TOOLS = {
    "status", "send_message", "read_messages", "wait_messages", "ack_messages", "create_task", "list_tasks",
    "claim_task", "renew_task", "finish_task", "release_task",
}


@pytest.fixture
def project_root():
    # Windows workspace permissions can reject pytest's mode-0700 temp folders.
    cache = (ROOT / ".cache").resolve()
    directory = cache / ("collaboration_mcp_test_" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        assert directory.resolve() == directory and directory.parent == cache
        shutil.rmtree(directory)


@contextmanager
def stdio_agent(project_root, agent_id, database=None):
    command = [sys.executable, str(SCRIPT), "--project-root", str(project_root), "--agent-id", agent_id]
    if database is not None:
        command.extend(["--database", str(database)])
    # Ordinary subprocess pipes work in managed Windows workspaces where
    # asyncio's named-pipe creation can fail. Each role gets its own process.
    with subprocess.Popen(
        command, cwd=project_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    ) as process:
        stdout = queue.Queue()
        next_id = 0

        def collect_lines():
            for line in process.stdout:
                stdout.put(line)
            stdout.put(None)

        reader = threading.Thread(target=collect_lines, daemon=True)
        reader.start()

        def send(message):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            process.stdin.flush()

        def request(method, params):
            nonlocal next_id
            next_id += 1
            send({"id": next_id, "method": method, "params": params})
            try:
                line = stdout.get(timeout=15)
            except queue.Empty:
                pytest.fail(f"{agent_id} timed out during {method}")
            assert line is not None, f"{agent_id} closed stdout during {method}"
            response = json.loads(line)
            assert response["jsonrpc"] == "2.0"
            assert response.get("id") == next_id, response
            return response

        try:
            initialized = request("initialize", {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "collaboration-wire-test", "version": "1"},
            })["result"]
            assert initialized["protocolVersion"] == "2025-11-25"
            assert initialized["serverInfo"]["name"] == "agent-collaboration"
            assert "tools" in initialized["capabilities"]
            send({"method": "notifications/initialized"})
            yield request
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail(f"{agent_id} did not exit after its stdin closed")
            reader.join(timeout=5)
        assert process.returncode == 0
        assert stdout.get_nowait() is None, f"Unexpected output from {agent_id}"


def call(request, tool, error=None, **arguments):
    response = request("tools/call", {"name": f"collaboration_{tool}", "arguments": arguments})
    result = response["result"]
    payload = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == payload
    if error is not None:
        assert result["isError"] is True
        assert payload["ok"] is False
        assert payload["error"]["code"] == error, payload
        return
    assert not result.get("isError", False), payload
    assert payload["ok"] is True, payload
    return payload["data"]


def check_discovery(request):
    tools = request("tools/list", {})["result"]["tools"]
    assert {tool["name"] for tool in tools} == {f"collaboration_{name}" for name in TOOLS}
    for tool in tools:
        Draft202012Validator.check_schema(tool["inputSchema"])
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["outputSchema"] == RESULT_SCHEMA


def test_stdio_messages_and_restart(project_root):
    with stdio_agent(project_root, "codex") as codex, stdio_agent(project_root, "claude") as claude:
        check_discovery(codex)
        check_discovery(claude)
        assert call(codex, "status")["agent_id"] == "codex"
        assert call(claude, "status")["agent_id"] == "claude"
        unknown = codex("tools/call", {"name": "unknown_tool", "arguments": {}})
        assert unknown["error"]["code"] == -32602
        call(codex, "send_message", error="invalid_arguments", to="claude", body="伪造身份",
             request_id="spoof", sender="claude")
        empty = call(claude, "wait_messages", timeout_seconds=0)
        assert empty["timed_out"] is True and empty["messages"] == []

        arguments = {"to": "claude", "body": "请复核这条证据。", "request_id": "message-1"}
        sent = call(codex, "send_message", **arguments)
        assert (sent["from"], sent["to"], sent["body"]) == ("codex", "claude", arguments["body"])
        assert call(codex, "send_message", **arguments)["id"] == sent["id"]
        call(codex, "send_message", error="request_conflict", **{**arguments, "body": "变更内容"})
        assert call(codex, "read_messages")["messages"] == []
        outgoing = call(codex, "read_messages", box="sent")["messages"]
        assert [item["id"] for item in outgoing] == [sent["id"]]
        ready = call(claude, "wait_messages", timeout_seconds=0)
        assert ready["timed_out"] is False and ready["messages"][0]["id"] == sent["id"]
        for _ in range(2):
            inbox = call(claude, "read_messages")["messages"]
            assert [(item["id"], item["acked"]) for item in inbox] == [(sent["id"], False)]

        call(codex, "ack_messages", error="message_missing", request_id="not-recipient", message_ids=[sent["id"]])
        ack = call(claude, "ack_messages", request_id="ack-1", message_ids=[sent["id"]])
        assert ack["acked_ids"] == [sent["id"]]
        assert call(claude, "read_messages", unread_only=True)["messages"] == []
        reply = call(claude, "send_message", to="codex", body="已复核。", reply_to=sent["id"], request_id="reply-1")
        assert reply["reply_to"] == sent["id"]
        assert reply["thread_id"] == sent["thread_id"]

    with stdio_agent(project_root, "codex") as codex, stdio_agent(project_root, "claude") as claude:
        received = call(codex, "read_messages")["messages"]
        assert [(item["id"], item["from"], item["body"]) for item in received] == [
            (reply["id"], "claude", "已复核。"),
        ]
        assert received[0]["thread_id"] == sent["thread_id"]
        assert call(claude, "read_messages", unread_only=True)["messages"] == []
        history = call(claude, "read_messages")["messages"]
        assert [(item["id"], item["acked"]) for item in history] == [(sent["id"], True)]


def test_stdio_tasks_conflict_and_expired_lease(project_root):
    database = project_root / "mailbox.sqlite3"
    with (
        stdio_agent(project_root, "codex", database) as codex,
        stdio_agent(project_root, "claude", database) as claude,
    ):
        first = call(codex, "create_task", request_id="create-first", title="审阅报告",
                     description="检查报告目录", files=[{"path": "reports", "kind": "directory"}])
        second = call(claude, "create_task", request_id="create-second", title="修订报告",
                      description="修改报告摘要", files=[{"path": "reports/summary.md", "kind": "file"}])
        first_id, second_id = first["id"], second["id"]
        claimed = call(codex, "claim_task", request_id="claim-first", task_id=first_id, lease_seconds=60)
        assert (claimed["owner"], claimed["status"]) == ("codex", "claimed")
        call(claude, "claim_task", error="task_claimed", request_id="already-claimed", task_id=first_id)
        call(claude, "claim_task", error="scope_conflict", request_id="scope-conflict", task_id=second_id)
        call(claude, "release_task", error="lease_invalid", request_id="wrong-agent",
             task_id=first_id, lease_token=claimed["lease_token"])
        released = call(codex, "release_task", request_id="release-first",
                        task_id=first_id, lease_token=claimed["lease_token"])
        assert released["status"] == "open"

        expiring = call(codex, "claim_task", request_id="short-lease", task_id=first_id, lease_seconds=1)
        time.sleep(1.1)
        call(codex, "renew_task", error="lease_invalid", request_id="expired-token",
             task_id=first_id, lease_token=expiring["lease_token"])
        visible = {task["id"]: task for task in call(claude, "list_tasks")["tasks"]}
        assert visible[first_id]["status"] == "open"
        assert "lease_token" not in visible[first_id]
        acquired = call(claude, "claim_task", request_id="claim-second", task_id=second_id, lease_seconds=60)
        assert (acquired["owner"], acquired["status"]) == ("claude", "claimed")
        finished = call(claude, "finish_task", request_id="finish-second", task_id=second_id,
                        lease_token=acquired["lease_token"], result="修订完成")
        assert (finished["status"], finished["result"]) == ("completed", "修订完成")
        assert call(codex, "claim_task", request_id="reclaim-first", task_id=first_id)["status"] == "claimed"

    assert database.is_file()
