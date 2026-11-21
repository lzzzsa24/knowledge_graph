"""Adapter-specific contracts not covered by storage or stdio integration."""
from pathlib import Path

import pytest

from kg_pilot.collaboration_store import CollaborationError
from kg_pilot.mcp_collaboration import CollaborationTools


def test_relative_database_uses_project_root_not_launcher_cwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    unrelated = tmp_path / "unrelated"
    project.mkdir()
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    tools = CollaborationTools(project, "CodeX", Path("state/inbox.sqlite3"))
    assert tools.database == project / "state/inbox.sqlite3"
    status = tools.dispatch_tool("collaboration_status", {})
    assert status["agent_id"] == "codex"
    assert status["database"] == "state/inbox.sqlite3"
    assert status["automatic_wake"] is False


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 31, True])
def test_wait_rejects_unbounded_or_invalid_timeouts(tmp_path, timeout):
    tools = CollaborationTools(tmp_path, "codex")
    with pytest.raises(CollaborationError) as error:
        tools.dispatch_tool("collaboration_wait_messages", {"timeout_seconds": timeout})
    assert error.value.code == "invalid_arguments"


def test_wait_timeout_is_a_normal_result(tmp_path):
    tools = CollaborationTools(tmp_path, "codex")
    result = tools.dispatch_tool("collaboration_wait_messages", {"timeout_seconds": 0})
    assert result == {"messages": [], "next_cursor": 0, "has_more": False, "timed_out": True}
