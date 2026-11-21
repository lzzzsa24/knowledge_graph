"""Serve the shared project mailbox over MCP stdio; no network or model key needed."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def serve(root: Path, agent_id: str, database: Path | None):
    with redirect_stdout(sys.stderr):
        from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        from kg_pilot.collaboration_store import CollaborationError
        from kg_pilot.mcp_collaboration import CollaborationTools, tool_specs, SERVER_NAME, SERVER_VERSION, INSTRUCTIONS
        from kg_pilot.mcp_server import create_server

        server = create_server(CollaborationTools(root, agent_id, database), tool_specs(), safe_error=CollaborationError,
                               name=SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS)
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--agent-id", required=True, help="Fixed sender identity, for example codex or claude")
    parser.add_argument("--database", type=Path, help="Optional database inside project root (tests/isolated mailboxes)")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        import anyio
        anyio.run(serve, args.project_root.resolve(), args.agent_id, args.database)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Collaboration MCP stopped ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
