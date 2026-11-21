"""Official-SDK MCP transport for the local ingestion workflow.

The domain adapter owns corpus operations. This module only advertises schemas,
validates calls and turns safe domain failures into consistent MCP tool results.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import anyio
import mcp_types as types
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

LOGGER = logging.getLogger(__name__)
SERVER_NAME = "knowledge-graph-ingestion"
SERVER_VERSION = "0.1.0"

RESULT_SCHEMA = {
    "type": "object",
    "oneOf": [
        {
            "properties": {"ok": {"const": True}, "data": {"type": "object"}},
            "required": ["ok", "data"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "ok": {"const": False},
                "error": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                    "required": ["code", "message"],
                    "additionalProperties": False,
                },
            },
            "required": ["ok", "error"],
            "additionalProperties": False,
        },
    ],
}


class ToolBackend(Protocol):
    def dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def _result(payload: dict[str, Any], *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=error,
    )


def _failure(code: str, message: str) -> types.CallToolResult:
    return _result({"ok": False, "error": {"code": code, "message": message}}, error=True)


def create_server(
    backend: ToolBackend,
    specs: list[dict[str, Any]],
    *,
    safe_error: type[Exception],
    name: str = SERVER_NAME,
    version: str = SERVER_VERSION,
    instructions: str | None = None,
) -> Server:
    """Bind a domain backend without importing it or accessing a corpus at import."""
    tools: dict[str, types.Tool] = {}
    validators: dict[str, Draft202012Validator] = {}
    for spec in specs:
        descriptor = dict(spec, outputSchema=RESULT_SCHEMA)
        tool = types.Tool.model_validate(descriptor)
        if tool.name in tools:
            raise ValueError(f"Duplicate MCP tool name: {tool.name}")
        Draft202012Validator.check_schema(tool.input_schema)
        tools[tool.name] = tool
        validators[tool.name] = Draft202012Validator(tool.input_schema)

    def check_cursor(params: types.PaginatedRequestParams | None) -> None:
        if params is not None and params.cursor is not None:
            raise MCPError(-32602, "Invalid cursor: this server returns the complete list in one page")

    async def list_tools(ctx, params):
        check_cursor(params)
        return types.ListToolsResult(tools=list(tools.values()))

    async def list_resources(ctx, params):
        check_cursor(params)
        return types.ListResourcesResult(resources=[])

    async def list_resource_templates(ctx, params):
        check_cursor(params)
        return types.ListResourceTemplatesResult(resource_templates=[])

    async def list_prompts(ctx, params):
        check_cursor(params)
        return types.ListPromptsResult(prompts=[])

    async def call_tool(ctx, params):
        if params.name not in tools:
            raise MCPError(-32602, f"Unknown tool: {params.name}")
        arguments = params.arguments if params.arguments is not None else {}
        errors = sorted(validators[params.name].iter_errors(arguments), key=lambda e: str(e.path))
        if errors:
            # The validator's normal message embeds supplied values. Report only
            # the argument location and violated rule, avoiding credential echoes.
            error = errors[0]
            location = ".".join(str(p) for p in error.absolute_path) or "arguments"
            return _failure("invalid_arguments", f"Invalid {location}: violates {error.validator} constraint")
        try:
            data = await anyio.to_thread.run_sync(backend.dispatch_tool, params.name, arguments)
        except safe_error as exc:
            return _failure(str(exc.code), str(exc))
        except Exception as exc:
            LOGGER.error("MCP tool %s failed (%s)", params.name, type(exc).__name__)
            return _failure("internal_error", "The operation failed; inspect local server diagnostics")
        if not isinstance(data, dict):
            LOGGER.error("MCP tool %s returned an invalid result type", params.name)
            return _failure("internal_error", "The operation returned an invalid result")
        return _result({"ok": True, "data": data})

    return Server(
        name,
        version=version,
        instructions=instructions if instructions is not None else (
            "Local knowledge-graph PDF ingestion. Inspect ingestion_status and validate local paths before "
            "starting. Mutations use the existing Codex-only workflow and return immediately. Keep this "
            "connection open and use ingestion_wait or ingestion_status until the operation reaches a "
            "terminal state. Windows hosts may stop workers when a connection closes; reconnect, inspect "
            "the batch and use ingestion_resume after interruption. Publication uses the existing "
            "independent-review and data-integrity gates. "
            "Each tool returns {ok:true,data:{...}} or {ok:false,error:{code,message}}."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_resource_templates,
        on_list_prompts=list_prompts,
    )
