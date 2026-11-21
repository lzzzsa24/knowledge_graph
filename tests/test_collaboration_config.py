"""Project config changes use isolated folders and synthetic settings."""

from pathlib import Path
import json
import shutil
import tomllib
import uuid

import pytest

from scripts import configure_collaboration_mcp as config


CODEX_PATH = Path(".codex/config.toml")
CLAUDE_MCP_PATH = Path(".mcp.json")
CLAUDE_SETTINGS_PATH = Path(".claude/settings.local.json")


@pytest.fixture
def project():
    # Ordinary inherited permissions avoid pytest's private Windows temp ACLs.
    parent = (Path(__file__).resolve().parents[1] / ".cache").resolve()
    parent.mkdir(exist_ok=True)
    root = parent / f"collaboration config 测试 {uuid.uuid4().hex}"
    for path in (root / ".venv/Scripts/python.exe", root / "scripts/collaboration_mcp.py"):
        path.parent.mkdir(parents=True)
        path.touch()
    try:
        yield root
    finally:
        assert root.resolve().parent == parent
        shutil.rmtree(root)


def run(project, *args):
    return config.main(["--root", str(project), *args])


def seed_config(project, relative, content):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_preview_is_read_only_and_fresh_install_is_idempotent(project, capsys):
    assert run(project) == 0
    output = capsys.readouterr().out
    for relative in (CODEX_PATH, CLAUDE_MCP_PATH, CLAUDE_SETTINGS_PATH):
        assert f"{project / relative} [create]" in output
        assert not (project / relative).exists()
    assert not (project / ".codex").exists()
    assert not (project / ".claude").exists()

    assert run(project, "--apply") == 0
    assert capsys.readouterr().out.count("Applied: ") == 3
    codex = tomllib.loads((project / CODEX_PATH).read_text(encoding="utf-8"))["mcp_servers"][config.SERVICE]
    claude = read_json(project / CLAUDE_MCP_PATH)["mcpServers"][config.SERVICE]
    args = ["-X", "utf8", str(project / "scripts/collaboration_mcp.py"), "--project-root", str(project), "--agent-id"]
    executable = str(project / ".venv/Scripts/python.exe")
    assert codex == {
        "command": executable, "args": [*args, "codex"], "cwd": str(project),
        "startup_timeout_sec": 20, "tool_timeout_sec": 45,
    }
    assert claude == {"type": "stdio", "command": executable, "args": [*args, "claude"]}
    assert read_json(project / CLAUDE_SETTINGS_PATH) == {"enabledMcpjsonServers": [config.SERVICE]}

    original_times = {change.path: change.path.stat().st_mtime_ns for change in config.plan(project)}
    assert run(project, "--apply") == 0
    output = capsys.readouterr().out
    assert output.count("[unchanged]") == 3
    assert "Applied: " not in output
    assert {path: path.stat().st_mtime_ns for path in original_times} == original_times
    assert not list(project.rglob("*.bak"))


def test_updates_preserve_settings_and_backup_exact_bytes(project, capsys):
    codex_raw = (
        b'# Retain comments and CRLF\r\n[mcp_servers]\r\n'
        b'[mcp_servers.other]\r\ncommand = "other.exe"\r\n'
        b'[mcp_servers.other.env]\r\nTOKEN = "PRIVATE-CODEX-TOKEN"'
    )
    claude_raw = b'{\r\n"mcpServers":{"other":{"type":"stdio","command":"PRIVATE-MCP-TOKEN"}}}\r\n'
    settings_raw = (
        b'{"enableAllProjectMcpServers":false,"enabledMcpjsonServers":["other"],'
        b'"permissions":{"deny":["PRIVATE-PERMISSION"]}}'
    )
    originals = {
        seed_config(project, CODEX_PATH, codex_raw): codex_raw,
        seed_config(project, CLAUDE_MCP_PATH, claude_raw): claude_raw,
        seed_config(project, CLAUDE_SETTINGS_PATH, settings_raw): settings_raw,
    }

    assert run(project) == 0
    preview = capsys.readouterr()
    assert preview.out.count("[update]") == 3
    assert "PRIVATE" not in preview.out + preview.err
    assert not list(project.rglob("*.bak"))

    assert run(project, "--apply") == 0
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    for path, before in originals.items():
        backups = list(path.parent.glob(f"{path.name}.*.bak"))
        assert len(backups) == 1 and backups[0].read_bytes() == before
        assert f"Backup: {backups[0]}" in output
    assert (project / CODEX_PATH).read_bytes().startswith(codex_raw + b"\r\n\r\n")
    assert read_json(project / CLAUDE_MCP_PATH)["mcpServers"]["other"] == json.loads(claude_raw)["mcpServers"]["other"]
    assert read_json(project / CLAUDE_SETTINGS_PATH) == {
        "enableAllProjectMcpServers": False,
        "enabledMcpjsonServers": ["other", config.SERVICE],
        "permissions": {"deny": ["PRIVATE-PERMISSION"]},
    }


def test_equivalent_configs_keep_existing_serialization(project):
    codex, claude, _ = config.plan(project)
    codex_service = codex.service_fragment["mcp_servers"][config.SERVICE]
    inline = ", ".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in reversed(codex_service.items()))
    codex_raw = f"mcp_servers = {{ {config.SERVICE} = {{ {inline} }} }}\n".encode("utf-8")
    seed_config(project, CODEX_PATH, codex_raw)
    claude_service = claude.service_fragment["mcpServers"][config.SERVICE]
    del claude_service["type"]
    claude_service["env"] = {}
    claude_raw = json.dumps(claude.service_fragment).encode("utf-8")
    seed_config(project, CLAUDE_MCP_PATH, claude_raw)

    assert [change.action for change in config.plan(project)] == ["unchanged", "unchanged", "create"]
    assert run(project, "--apply") == 0
    assert (project / CODEX_PATH).read_bytes() == codex_raw
    assert (project / CLAUDE_MCP_PATH).read_bytes() == claude_raw
    assert not list(project.rglob("*.bak"))


@pytest.mark.parametrize(
    "path, content",
    [
        (CODEX_PATH, b'[mcp_servers.agent_collaboration]\ncommand = "PRIVATE"\n'),
        (CODEX_PATH, b'mcp_servers = { other = { command = "PRIVATE" } }'),
        (CLAUDE_MCP_PATH, b'{"mcpServers":{"agent_collaboration":{"command":"PRIVATE"}}}'),
        (CLAUDE_MCP_PATH, b'{"PRIVATE":{},"PRIVATE":{}}'),
        (CLAUDE_SETTINGS_PATH, b'{"enabledMcpjsonServers":"PRIVATE"}'),
        (CLAUDE_SETTINGS_PATH, b'{"disabledMcpjsonServers":["agent_collaboration"]}'),
    ],
)
def test_conflicts_leave_files_untouched_and_do_not_print_existing_values(project, capsys, path, content):
    seed_config(project, path, content)
    assert run(project, "--apply") == 1
    assert (project / path).read_bytes() == content
    for other in {CODEX_PATH, CLAUDE_MCP_PATH, CLAUDE_SETTINGS_PATH} - {path}:
        assert not (project / other).exists()
    output = capsys.readouterr()
    assert str(project / path) in output.err
    assert "PRIVATE" not in output.out + output.err
    assert not list(project.rglob("*.bak"))


def test_apply_detects_file_changed_after_backup(project, monkeypatch):
    codex_path = seed_config(project, CODEX_PATH, b"# initial contents\n")
    changes = config.plan(project)
    backup = config.backup

    def backup_and_edit(change):
        result = backup(change)
        codex_path.write_bytes(b"# concurrent edit\n")
        return result

    monkeypatch.setattr(config, "backup", backup_and_edit)
    with pytest.raises(config.ConfigError, match="changed after inspection"):
        config.apply(changes)
    assert codex_path.read_bytes() == b"# concurrent edit\n"
    assert not (project / CLAUDE_MCP_PATH).exists()
    assert not (project / CLAUDE_SETTINGS_PATH).exists()
    assert not list(project.rglob("*.tmp"))


def test_missing_runtime_fails_before_config_changes(project, capsys):
    missing = project / ".venv/Scripts/python.exe"
    missing.unlink()
    assert run(project, "--apply") == 1
    assert str(missing) in capsys.readouterr().err
    assert not (project / CODEX_PATH).exists()
    assert not (project / CLAUDE_MCP_PATH).exists()
    assert not (project / CLAUDE_SETTINGS_PATH).exists()
