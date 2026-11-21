"""MCP protocol tests use isolated adapters and never touch the formal corpus."""
from __future__ import annotations

import json
import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.exceptions import MCPError

from kg_pilot.mcp_server import RESULT_SCHEMA, SERVER_NAME, create_server

ROOT = Path(__file__).resolve().parents[1]


class SafeError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Backend:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, name, arguments):
        self.calls.append((name, arguments))
        action = arguments.get("action", "success")
        if action == "safe_error":
            raise SafeError("batch_missing", "Batch does not exist")
        if action == "unsafe_error":
            raise RuntimeError("secret-credential-must-not-leak")
        if action == "wrong_result":
            return []
        return {"status": "ready", "label": "中文 PDF"}


def specs():
    return [{
        "name": "ingestion_status",
        "description": "Inspect ingestion status",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    }]


def server(backend):
    return create_server(backend, specs(), safe_error=SafeError)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_sdk_client_discovery_and_success(mode):
    async def scenario():
        backend = Backend()
        async with Client(server(backend), mode=mode, read_timeout_seconds=10) as client:
            found = await client.list_tools()
            assert [t.name for t in found.tools] == ["ingestion_status"]
            descriptor = found.tools[0]
            assert descriptor.annotations.read_only_hint is True
            assert descriptor.input_schema == specs()[0]["inputSchema"]
            assert descriptor.output_schema == RESULT_SCHEMA
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resource_templates == []
            assert (await client.list_prompts()).prompts == []
            result = await client.call_tool("ingestion_status")
            assert not result.is_error
            assert result.structured_content == {"ok": True, "data": {"status": "ready", "label": "中文 PDF"}}
            assert json.loads(result.content[0].text) == result.structured_content
            Draft202012Validator(RESULT_SCHEMA).validate(result.structured_content)
            assert backend.calls == [("ingestion_status", {})]

    anyio.run(scenario)


def test_validation_and_protocol_errors_do_not_dispatch():
    async def scenario():
        backend = Backend()
        async with Client(server(backend), read_timeout_seconds=10) as client:
            for arguments in [{"action": 1}, {"unexpected": "secret-input"}]:
                result = await client.call_tool("ingestion_status", arguments)
                assert result.is_error
                assert result.structured_content["error"]["code"] == "invalid_arguments"
                assert "secret-input" not in result.content[0].text
                Draft202012Validator(RESULT_SCHEMA).validate(result.structured_content)
            with pytest.raises(MCPError) as unknown:
                await client.call_tool("unknown_tool", {})
            assert unknown.value.code == -32602
            with pytest.raises(MCPError) as cursor:
                await client.list_tools(cursor="not-a-real-cursor")
            assert cursor.value.code == -32602
            assert not backend.calls

    anyio.run(scenario)


@pytest.mark.parametrize("action,code", [
    ("safe_error", "batch_missing"),
    ("unsafe_error", "internal_error"),
    ("wrong_result", "internal_error"),
])
def test_tool_error_envelope_is_safe(action, code):
    async def scenario():
        async with Client(server(Backend()), read_timeout_seconds=10) as client:
            result = await client.call_tool("ingestion_status", {"action": action})
            assert result.is_error
            assert result.structured_content["ok"] is False
            assert result.structured_content["error"]["code"] == code
            assert "secret-credential" not in result.content[0].text
            assert json.loads(result.content[0].text) == result.structured_content
            Draft202012Validator(RESULT_SCHEMA).validate(result.structured_content)
            # A failed operation must leave the connection usable.
            assert not (await client.call_tool("ingestion_status")).is_error

    anyio.run(scenario)


@pytest.fixture
def stdio_workdir():
    # Python 3.12's Windows mode-0700 temporary directories can deny this
    # managed workspace's token. Use a normal, uniquely owned workspace folder.
    directory = ROOT / ".cache" / ("mcp_stdio_test_" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def test_stdio_launcher_legacy_handshake_and_stdout_shielding(stdio_workdir):
    """Inspect the actual wire, including Python/native prints inside handlers."""
    tmp_path = stdio_workdir
    wrapper = tmp_path / "isolated_mcp_launcher.py"
    wrapper.write_text(
        "import os, runpy, sys, types\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "adapter = types.ModuleType('kg_pilot.mcp_ingestion')\n"
        "class Error(ValueError):\n"
        "    def __init__(self, code, message):\n"
        "        self.code = code\n"
        "        super().__init__(message)\n"
        "class Backend:\n"
        "    def __init__(self, pilot):\n"
        "        print('noisy constructor', flush=True)\n"
        "        os.write(1, b'native noisy constructor\\n')\n"
        "    def dispatch_tool(self, name, arguments):\n"
        "        print('noisy tool', flush=True)\n"
        "        os.write(1, b'native noisy tool\\n')\n"
        "        if arguments.get('fail'):\n"
        "            raise Error('fixture_failure', 'Safe fixture error')\n"
        "        return {'fixture': True, 'text': '中文'}\n"
        "adapter.IngestionTools = Backend\n"
        "adapter.MCPToolError = Error\n"
        "adapter.tool_specs = lambda: [{'name':'ingestion_status','description':'Fixture status',"
        "'inputSchema':{'type':'object','properties':{'fail':{'type':'boolean'}},'additionalProperties':False}}]\n"
        "sys.modules['kg_pilot.mcp_ingestion'] = adapter\n"
        f"sys.argv = [{str(ROOT / 'scripts' / 'ingestion_mcp.py')!r}, '--pilot', {str(tmp_path / 'pilot')!r}]\n"
        f"runpy.run_path({str(ROOT / 'scripts' / 'ingestion_mcp.py')!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(wrapper)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    lines = queue.Queue()

    def collect_lines():
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=collect_lines, daemon=True).start()

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def response(request_id):
        while True:
            # Every stdout line must be protocol JSON; native/Python noise fails here.
            message = json.loads(lines.get(timeout=15))
            assert message["jsonrpc"] == "2.0"
            if message.get("id") == request_id:
                return message

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "isolated-protocol-test", "version": "1"},
        }})
        initialized = response(1)["result"]
        assert initialized["serverInfo"]["name"] == SERVER_NAME
        assert initialized["protocolVersion"] == "2025-11-25"
        assert "tools" in initialized["capabilities"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert response(2)["result"]["tools"][0]["name"] == "ingestion_status"
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ingestion_status"}})
        succeeded = response(3)["result"]
        assert not succeeded.get("isError", False)
        assert succeeded["structuredContent"] == {"ok": True, "data": {"fixture": True, "text": "中文"}}
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "ingestion_status", "arguments": {"fail": True},
        }})
        failed = response(4)["result"]
        assert failed["isError"] is True
        assert failed["structuredContent"]["error"]["code"] == "fixture_failure"
        send({"jsonrpc": "2.0", "id": 5, "method": "unsupported/method", "params": {}})
        assert response(5)["error"]["code"] == -32601
    finally:
        process.stdin.close()
        process.stdin = None
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "noisy constructor" in stderr and "native noisy tool" in stderr
    while not lines.empty():
        assert json.loads(lines.get_nowait())["jsonrpc"] == "2.0"


def test_real_backend_subprocess_read_only_status_and_pdf_validation(stdio_workdir):
    import pymupdf

    pdf = stdio_workdir / "reference.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Isolated transport test")
    document.save(pdf)
    document.close()
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    isolated_pilot = stdio_workdir / "pilot"

    # Ordinary subprocess pipes also run inside managed Windows sandboxes that
    # prohibit asyncio's named-pipe creation. The in-process tests above cover
    # the official SDK client; here we inspect the actual server wire directly.
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "ingestion_mcp.py"), "--pilot", str(isolated_pilot)],
        cwd=stdio_workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    lines = queue.Queue()

    def collect_lines():
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=collect_lines, daemon=True).start()
    next_id = 0

    def request(method, params):
        nonlocal next_id
        next_id += 1
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        while True:
            message = json.loads(lines.get(timeout=15))
            if message.get("id") == next_id:
                assert "error" not in message, message
                return message["result"]

    def call(name, arguments=None):
        return request("tools/call", {"name": name, "arguments": arguments or {}})

    try:
        request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                               "clientInfo": {"name": "backend-stdio-test", "version": "1"}})
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.flush()
        found = request("tools/list", {})["tools"]
        assert {tool["name"] for tool in found} == {
            "ingestion_status", "ingestion_validate", "ingestion_start", "ingestion_resume",
            "ingestion_publish", "ingestion_cancel", "ingestion_skip", "ingestion_wait",
        }
        for tool in found:
            Draft202012Validator.check_schema(tool["inputSchema"])
            assert tool["outputSchema"] == RESULT_SCHEMA
        status = call("ingestion_status")
        assert not status.get("isError", False)
        assert status["structuredContent"]["data"]["batches"] == []
        validated = call("ingestion_validate", {"paths": [str(pdf)]})
        assert not validated.get("isError", False)
        summary = validated["structuredContent"]["data"]
        assert summary["valid"] is True
        assert summary["files"][0]["sha256"] == digest
        assert summary["files"][0]["pages"] == 1
        missing = call("ingestion_status", {"batch_id": "b_" + "0" * 20})
        assert missing["isError"]
        assert missing["structuredContent"]["error"]["code"] == "batch_not_found"
        bad_path = call("ingestion_validate", {"paths": ["https://example.invalid/paper.pdf"]})
        assert bad_path["isError"]
        assert bad_path["structuredContent"]["error"]["code"] == "invalid_pdf_path"
    finally:
        process.stdin.close()
        process.stdin = None
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == digest
    assert not isolated_pilot.exists()
