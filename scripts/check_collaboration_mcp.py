"""Read-only protocol check of the two installed project MCP configurations."""
from __future__ import annotations

import argparse
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.configure_collaboration_mcp import plan, SERVICE


async def check(root: Path, mode: str):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    changes = plan(root)
    if any(change.action != "unchanged" for change in changes):
        raise ValueError("Install the project configurations before running this check.")
    entries = [changes[0].service_fragment["mcp_servers"][SERVICE], changes[1].service_fragment["mcpServers"][SERVICE]]
    clients, results = [], []
    async with AsyncExitStack() as stack:
        for entry in entries:
            params = StdioServerParameters(command=entry["command"], args=entry["args"], cwd=str(root))
            client = await stack.enter_async_context(Client(params, mode=mode, read_timeout_seconds=15))
            clients.append(client)
        for identity, client in zip(("codex", "claude"), clients, strict=True):
            listing = await client.list_tools()
            tool_names = sorted(tool.name for tool in listing.tools)
            response = await client.call_tool("collaboration_status", {})
            payload = response.structured_content
            if response.is_error or not payload or payload.get("ok") is not True:
                raise RuntimeError("MCP status failed.")
            status = payload["data"]
            assert status["agent_id"] == identity and status["status"] == "ready"
            results.append({"configuration": identity, "status": status, "tools": tool_names})
    assert results[0]["tools"] == results[1]["tools"]
    assert results[0]["status"]["database"] == results[1]["status"]["database"]
    return {"status": "passed", "protocol_mode": mode, "clients": results,
            "check_scope": "SDK connections launched from both installed configurations; status/discovery only, no messages sent and no model invoked."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=("auto", "legacy"), default="auto")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    import anyio
    report = anyio.run(check, args.root.resolve(), args.mode)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
