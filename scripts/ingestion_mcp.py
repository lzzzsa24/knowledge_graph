"""Serve local ingestion tools over MCP stdio using the project Python environment."""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


async def serve(pilot: Path, native_library: bool = False) -> None:
    # Import the official SDK quietly, then let its stdio transport own and
    # shield the protocol file descriptors before loading corpus dependencies.
    with redirect_stdout(sys.stderr):
        from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        from kg_pilot.mcp_ingestion import IngestionTools, MCPToolError, tool_specs
        from kg_pilot.mcp_server import create_server

        backend, specs = IngestionTools(pilot=pilot), tool_specs()
        if native_library:
            from scripts.native_library_mcp import NativeTools, native_specs
            backend, specs = NativeTools(pilot=pilot), specs + native_specs()
        server = create_server(backend, specs, safe_error=MCPToolError)
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=ROOT / "pilot", help="Dataset root (default: project pilot)")
    parser.add_argument("--native-library", action="store_true", help="Enable original-PDF RAG-Anything migration tools")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        import anyio

        anyio.run(serve, args.pilot.resolve(), args.native_library)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Never print a dependency/provider exception body onto the protocol or
        # into model-visible results, where it could disclose credentials.
        print(f"Ingestion MCP server stopped ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
