"""Preview or install the collaboration MCP in this project's client configs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import uuid


ROOT = Path(__file__).resolve().parents[1]
SERVICE = "agent_collaboration"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    path: Path
    original: bytes | None
    content: bytes
    service_fragment: dict

    @property
    def action(self) -> str:
        if self.original is None:
            return "create"
        return "unchanged" if self.original == self.content else "update"


def read_config(path: Path) -> bytes | None:
    if path.resolve() != path:
        raise ConfigError(f"Configuration path resolves elsewhere: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def newline_for(original: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in original else b"\n"


def load_toml(original: bytes, path: Path) -> dict:
    try:
        return tomllib.loads(original.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError(f"Invalid UTF-8 TOML: {path}") from None


def load_json(original: bytes | None, path: Path) -> dict:
    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate key")
            result[name] = value
        return result

    def reject_constant(_):
        raise ValueError("Invalid numeric constant")

    try:
        document = {} if original is None else json.loads(
            original.decode("utf-8-sig"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except ValueError:
        raise ConfigError(f"Invalid UTF-8 JSON or duplicate key: {path}") from None
    if not isinstance(document, dict):
        raise ConfigError(f"Expected a JSON object: {path}")
    return document


def dump_json(document: dict, original: bytes | None) -> bytes:
    text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
    newline = newline_for(original or b"")
    bom = b"\xef\xbb\xbf" if original and original.startswith(b"\xef\xbb\xbf") else b""
    return bom + text.encode("utf-8", errors="backslashreplace").replace(b"\n", newline) + newline


def require_object(parent: dict, key: str, path: Path) -> dict:
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Expected an object for {key}: {path}")
    return value


def check_service(current, expected: dict, path: Path, *, claude: bool = False) -> None:
    if claude and isinstance(current, dict):
        current = {"type": "stdio", **current}
        if current.get("env") == {}:
            current.pop("env")
    if current != expected:
        raise ConfigError(f"A different {SERVICE} service already exists: {path}")


def codex_change(path: Path, service: dict) -> Change:
    original = read_config(path)
    parsed = load_toml(original or b"", path)
    servers = require_object(parsed, "mcp_servers", path)
    fragment = {"mcp_servers": {SERVICE: service}}
    if SERVICE in servers:
        check_service(servers[SERVICE], service, path)
        return Change(path, original, original, fragment)

    newline = newline_for(original or b"")
    lines = [f"[mcp_servers.{SERVICE}]"]
    lines += [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in service.items()]
    table = newline.join(line.encode("utf-8") for line in lines) + newline
    separator = newline if original and original.endswith(b"\n") else newline * 2
    content = original + separator + table if original else table

    # Some legal TOML forms, such as inline tables, cannot be extended by appending.
    try:
        updated = tomllib.loads(content.decode("utf-8-sig"))
    except tomllib.TOMLDecodeError:
        raise ConfigError(f"Cannot append a service table to the existing TOML: {path}") from None
    servers[SERVICE] = service
    if updated != parsed:
        raise ConfigError(f"Appending the service would alter existing TOML settings: {path}")
    return Change(path, original, content, fragment)


def claude_mcp_change(path: Path, service: dict) -> Change:
    original = read_config(path)
    document = load_json(original, path)
    servers = require_object(document, "mcpServers", path)
    fragment = {"mcpServers": {SERVICE: service}}
    if SERVICE in servers:
        check_service(servers[SERVICE], service, path, claude=True)
        return Change(path, original, original, fragment)
    servers[SERVICE] = service
    return Change(path, original, dump_json(document, original), fragment)


def claude_settings_change(path: Path) -> Change:
    original = read_config(path)
    document = load_json(original, path)
    key = "enabledMcpjsonServers"
    fragment = {key: [SERVICE]}
    enabled = document.setdefault(key, [])
    if not isinstance(enabled, list) or not all(isinstance(name, str) for name in enabled):
        raise ConfigError(f"Expected a list of server names for {key}: {path}")
    disabled = document.get("disabledMcpjsonServers", [])
    if isinstance(disabled, list) and SERVICE in disabled:
        raise ConfigError(f"{SERVICE} is explicitly disabled: {path}")
    if SERVICE in enabled:
        return Change(path, original, original, fragment)
    enabled.append(SERVICE)
    return Change(path, original, dump_json(document, original), fragment)


def plan(root: Path) -> list[Change]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ConfigError(f"Expected a project directory: {root}")
    python = root / ".venv" / "Scripts" / "python.exe"
    script = root / "scripts" / "collaboration_mcp.py"
    for path in (python, script):
        if not path.is_file():
            raise ConfigError(f"Required file is missing: {path}")

    args = ["-X", "utf8", str(script), "--project-root", str(root), "--agent-id"]
    codex = {
        "command": str(python),
        "args": [*args, "codex"],
        "cwd": str(root),
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 45,
    }
    claude = {"type": "stdio", "command": str(python), "args": [*args, "claude"]}
    return [
        codex_change(root / ".codex" / "config.toml", codex),
        claude_mcp_change(root / ".mcp.json", claude),
        claude_settings_change(root / ".claude" / "settings.local.json"),
    ]


def digest(content: bytes | None) -> bytes | None:
    return None if content is None else hashlib.sha256(content).digest()


def require_unchanged(change: Change) -> None:
    if digest(read_config(change.path)) != digest(change.original):
        raise ConfigError(f"Configuration changed after inspection. Rerun the preview: {change.path}")


def write_synced(stream, content: bytes) -> None:
    stream.write(content)
    stream.flush()
    os.fsync(stream.fileno())


def backup(change: Change) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = change.path.with_name(f"{change.path.name}.{stamp}.{uuid.uuid4().hex}.bak")
    stream = path.open("xb")
    try:
        with stream:
            write_synced(stream, change.original)
    except BaseException:
        path.unlink()
        raise
    return path


def apply(changes: list[Change]) -> None:
    pending = [change for change in changes if change.action != "unchanged"]
    staged: dict[Path, Path] = {}
    try:
        for change in changes:
            require_unchanged(change)
        for change in pending:
            change.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{change.path.name}.", suffix=".tmp",
                dir=change.path.parent, delete=False,
            ) as stream:
                staged[change.path] = Path(stream.name)
                write_synced(stream, change.content)
        for change in pending:
            require_unchanged(change)
            if change.original is not None:
                print(f"Backup: {backup(change)}", flush=True)
        for change in pending:
            require_unchanged(change)
            os.replace(staged[change.path], change.path)
            print(f"Applied: {change.path}", flush=True)
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Project root (defaults to this checkout)")
    parser.add_argument("--apply", action="store_true", help="Write the displayed project configuration changes")
    args = parser.parse_args(argv)

    try:
        changes = plan(args.root)
        print("Mode: apply" if args.apply else "Mode: preview")
        for change in changes:
            print(f"{change.path} [{change.action}]")
            print(json.dumps(change.service_fragment, ensure_ascii=False, indent=2))
        if args.apply:
            apply(changes)
    except (ConfigError, OSError) as error:
        print(f"Configuration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
