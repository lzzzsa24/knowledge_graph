"""Durable local inbox and advisory task leases for a single project."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_DATABASE = Path("pilot/collaboration/agent_collaboration.sqlite3")
_APPLICATION_ID = 0x4B47434F  # KGCO
_ACTOR = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
        body TEXT NOT NULL, attachments TEXT NOT NULL, created_at REAL NOT NULL,
        reply_to INTEGER REFERENCES messages(id), thread_id INTEGER REFERENCES messages(id)
    )""",
    "CREATE INDEX IF NOT EXISTS message_thread ON messages(thread_id, id)",
    "CREATE INDEX IF NOT EXISTS message_recipient ON messages(recipient, id)",
    "CREATE INDEX IF NOT EXISTS message_sender ON messages(sender, id)",
    """CREATE TABLE IF NOT EXISTS message_acks (
        message_id INTEGER NOT NULL REFERENCES messages(id), agent_id TEXT NOT NULL,
        PRIMARY KEY (message_id, agent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
        files TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
        updated_at REAL NOT NULL, owner TEXT, lease_token TEXT, lease_expires_at REAL,
        completed_by TEXT, result TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS requests (
        agent_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        response TEXT NOT NULL, PRIMARY KEY (agent_id, request_id)
    )""",
)


class CollaborationError(ValueError):
    """A safe domain error suitable for returning to an MCP caller."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _string(name: str, value: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise CollaborationError("invalid_arguments", f"{name} must be a valid string of up to {maximum} characters.")
    return value


def _integer(name: str, value: int, minimum: int, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CollaborationError("invalid_arguments", f"{name} must be an integer from {minimum} through {maximum}.")
    return value


def _actor(value: str, *, broadcast: bool = False) -> str:
    value = _string("Agent ID", value, 64)
    if not value.isascii():
        raise CollaborationError("invalid_arguments", "Invalid agent ID.")
    value = value.lower()
    if broadcast and value in ("*", "broadcast"):
        return "*"
    if value == "broadcast" or not _ACTOR.fullmatch(value):
        raise CollaborationError("invalid_arguments", "Invalid agent ID.")
    return value


def _path_list(name: str, value: list | None) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 40:
        raise CollaborationError("invalid_arguments", f"{name} must be an array of up to 40 items.")
    return value


def _page(rows: list[dict], key: str, after_id: int, limit: int) -> dict:
    items = rows[:limit]
    return {key: items, "next_cursor": items[-1]["id"] if items else after_id, "has_more": len(rows) > limit}


def _scope_overlap(first: dict, second: dict) -> bool:
    left = os.path.normcase(first["path"]).replace("\\", "/")
    right = os.path.normcase(second["path"]).replace("\\", "/")
    # An ancestor declared as a file also excludes descendants. A planned file
    # and a directory at the same path cannot both be created.
    return left == right or right.startswith(left + "/") or left.startswith(right + "/")


class CollaborationStore:
    def __init__(
        self,
        root: Path,
        agent_id: str,
        database: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.root = Path(root).resolve(strict=True)
        self.agent_id = _actor(agent_id)
        self.clock = clock
        database = Path(database) if database is not None else DEFAULT_DATABASE
        self.database = (self.root / database).resolve()
        if not self.database.is_relative_to(self.root) or self.database == self.root:
            raise CollaborationError("invalid_arguments", "The collaboration database must be inside the project.")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            existing_tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
            if application_id != _APPLICATION_ID and (application_id or existing_tables):
                raise CollaborationError("database_mismatch", "The selected database does not belong to this service.")
            if application_id == _APPLICATION_ID:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != 1:
                    raise CollaborationError("database_mismatch", "Unsupported collaboration database version.")
            else:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                connection.execute("PRAGMA user_version = 1")
            connection.commit()
            deadline = time.monotonic() + 5
            while True:
                try:
                    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    if mode != "wal":
                        raise CollaborationError("database_mismatch", "The collaboration database requires WAL support.")
                    break
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)

    def _write(
        self, operation: str, request_id: str, payload: dict, action: Callable[[sqlite3.Connection, float], dict],
    ) -> dict:
        _string("request_id", request_id, 128)
        try:
            fingerprint = hashlib.sha256(_json([operation, payload]).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            raise CollaborationError("invalid_arguments", "Request values must be valid JSON.") from None
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            recorded = connection.execute(
                "SELECT fingerprint, response FROM requests WHERE agent_id = ? AND request_id = ?",
                (self.agent_id, request_id),
            ).fetchone()
            if recorded is not None:
                if recorded["fingerprint"] != fingerprint:
                    raise CollaborationError("request_conflict", "This request_id was used with different arguments.")
                return json.loads(recorded["response"])
            response = action(connection, self.clock())
            connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?)",
                (self.agent_id, request_id, fingerprint, _json(response)),
            )
            return response

    def _project_path(self, value: str, *, attachment: bool = False) -> str:
        value = _string("Path", value, 1024).replace("\\", "/")
        parts = value.split("/")
        if any(
            not part or part.endswith((".", " ")) or re.search(r'[:<>"|?*\x00-\x1f]', part)
            or re.fullmatch(r"(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?", part, re.IGNORECASE)
            for part in parts
        ):
            raise CollaborationError("invalid_arguments", "Use a project-relative path without traversal or device names.")
        try:
            resolved = (self.root / value).resolve()
            normalized = resolved.relative_to(self.root).as_posix()
        except (ValueError, OSError, RuntimeError):
            raise CollaborationError("invalid_arguments", "The path must resolve inside the project.") from None
        if normalized == ".":
            raise CollaborationError("invalid_arguments", "The path must name an item within the project.")
        if attachment:
            database_path = self.database.relative_to(self.root).as_posix().casefold()
            for candidate in (value, normalized):
                candidate = candidate.casefold()
                candidate_parts = candidate.split("/")
                if (
                    candidate == database_path or candidate.startswith(database_path + "-")
                    or candidate_parts[:2] == ["pilot", "secrets"]
                    or any(part == ".env" or part.startswith(".env.") for part in candidate_parts)
                    or candidate.startswith(("pilot/llm_config.json", "pilot/ingestion_llm_config.json"))
                ):
                    raise CollaborationError("invalid_arguments", "Credential and collaboration database paths cannot be attached.")
        return normalized

    def _file_scopes(self, files: list) -> list[dict]:
        scopes = []
        seen = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "kind"} or item["kind"] not in ("file", "directory"):
                raise CollaborationError("invalid_arguments", "Each scope requires path and kind (file or directory).")
            path = self._project_path(item["path"])
            kind = item["kind"]
            target = self.root / path
            if target.exists() and target.is_dir() != (kind == "directory"):
                raise CollaborationError("invalid_arguments", "The scope kind does not match the existing path.")
            key = os.path.normcase(path)
            if key in seen:
                raise CollaborationError("invalid_arguments", "Declare each file scope path once.")
            seen.add(key)
            scopes.append({"path": path, "kind": kind})
        return scopes

    def _can_read(self, row: sqlite3.Row) -> bool:
        return row["sender"] == self.agent_id or row["recipient"] in (self.agent_id, "*")

    def _is_recipient(self, row: sqlite3.Row) -> bool:
        return row["recipient"] == self.agent_id or (row["recipient"] == "*" and row["sender"] != self.agent_id)

    @staticmethod
    def _message(row: sqlite3.Row, *, acked: bool = False) -> dict:
        return {
            "id": row["id"], "from": row["sender"], "to": row["recipient"], "body": row["body"],
            "attachments": json.loads(row["attachments"]), "created_at": _timestamp(row["created_at"]),
            "reply_to": row["reply_to"], "thread_id": row["thread_id"], "acked": acked,
        }

    def send_message(
        self, *, to: str, body: str, request_id: str, reply_to: int | None = None,
        attachments: list[str] | None = None,
    ) -> dict:
        recipient = _actor(to, broadcast=True)
        _string("body", body, 12000)
        paths = _path_list("attachments", attachments)
        if reply_to is not None:
            _integer("reply_to", reply_to, 1)

        def send(connection: sqlite3.Connection, now: float) -> dict:
            thread_id = None
            if reply_to is not None:
                parent = connection.execute("SELECT * FROM messages WHERE id = ?", (reply_to,)).fetchone()
                if parent is None or not self._can_read(parent):
                    raise CollaborationError("message_missing", "The parent message is unavailable.")
                if parent["recipient"] != "*":
                    other = parent["recipient"] if parent["sender"] == self.agent_id else parent["sender"]
                    if recipient != other:
                        raise CollaborationError("invalid_recipient", "Reply to the other participant in the private message.")
                thread_id = parent["thread_id"]
            normalized = [self._project_path(path, attachment=True) for path in paths]
            message_id = connection.execute(
                "INSERT INTO messages (sender, recipient, body, attachments, created_at, reply_to, thread_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.agent_id, recipient, body, _json(normalized), now, reply_to, thread_id),
            ).lastrowid
            if thread_id is None:
                connection.execute("UPDATE messages SET thread_id = id WHERE id = ?", (message_id,))
            row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            return self._message(row)

        return self._write("send_message", request_id, dict(to=recipient, body=body, reply_to=reply_to, attachments=paths), send)

    def list_messages(
        self, *, after_id: int = 0, limit: int = 50, unread_only: bool = False, thread_id: int | None = None,
        box: str = "inbox",
    ) -> dict:
        _integer("after_id", after_id, 0)
        _integer("limit", limit, 1, 100)
        if type(unread_only) is not bool:
            raise CollaborationError("invalid_arguments", "unread_only must be a boolean.")
        inbox = "(m.recipient = ? OR (m.recipient = '*' AND m.sender <> ?))"
        boxes = {
            "inbox": (inbox, (self.agent_id, self.agent_id)),
            "sent": ("m.sender = ?", (self.agent_id,)),
            "all": ("(m.sender = ? OR m.recipient IN (?, '*'))", (self.agent_id, self.agent_id)),
        }
        if not isinstance(box, str) or box not in boxes:
            raise CollaborationError("invalid_arguments", "box must be inbox, sent or all.")
        visibility, actors = boxes[box]
        clauses = ["m.id > ?", visibility]
        parameters = [self.agent_id, after_id, *actors]
        if unread_only:
            clauses.append(f"{inbox} AND a.message_id IS NULL")
            parameters.extend((self.agent_id, self.agent_id))
        if thread_id is not None:
            clauses.append("m.thread_id = ?")
            parameters.append(_integer("thread_id", thread_id, 1))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT m.*, a.message_id AS ack_id FROM messages AS m "
                "LEFT JOIN message_acks AS a ON a.message_id = m.id AND a.agent_id = ? "
                f"WHERE {' AND '.join(clauses)} ORDER BY m.id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        return _page([self._message(row, acked=row["ack_id"] is not None) for row in rows], "messages", after_id, limit)

    def ack_messages(self, *, message_ids: list[int], request_id: str) -> dict:
        if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 100:
            raise CollaborationError("invalid_arguments", "message_ids must contain 1 through 100 message IDs.")
        ids = sorted({_integer("message_id", value, 1) for value in message_ids})

        def ack(connection: sqlite3.Connection, now: float) -> dict:
            for message_id in ids:
                row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
                if row is None or not self._is_recipient(row):
                    raise CollaborationError("message_missing", "A message is unavailable for acknowledgment.")
                connection.execute("INSERT OR IGNORE INTO message_acks VALUES (?, ?)", (message_id, self.agent_id))
            return {"acked_ids": ids}

        return self._write("ack_messages", request_id, dict(message_ids=ids), ack)

    @staticmethod
    def _task(row: sqlite3.Row, now: float, *, include_token: bool = False) -> dict:
        status = "open"
        if row["completed_by"] is not None:
            status = "completed"
        elif row["lease_expires_at"] is not None and row["lease_expires_at"] > now:
            status = "claimed"
        task = {
            "id": row["id"], "title": row["title"], "description": row["description"],
            "status": status, "files": json.loads(row["files"]), "created_by": row["created_by"],
            "created_at": _timestamp(row["created_at"]), "updated_at": _timestamp(row["updated_at"]),
            "owner": row["owner"] if status == "claimed" else None,
            "lease_expires_at": _timestamp(row["lease_expires_at"]) if status == "claimed" else None,
            "completed_by": row["completed_by"], "result": row["result"],
        }
        if include_token:
            task["lease_token"] = row["lease_token"]
        return task

    def create_task(self, *, title: str, description: str, request_id: str, files: list[dict] | None = None) -> dict:
        _string("title", title, 240)
        _string("description", description, 12000, allow_empty=True)
        scopes = _path_list("files", files)

        def create(connection: sqlite3.Connection, now: float) -> dict:
            task_id = connection.execute(
                "INSERT INTO tasks (title, description, files, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (title, description, _json(self._file_scopes(scopes)), self.agent_id, now, now),
            ).lastrowid
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._task(row, now)

        return self._write("create_task", request_id, dict(title=title, description=description, files=scopes), create)

    def list_tasks(self, *, after_id: int = 0, limit: int = 50, status: str | None = None) -> dict:
        _integer("after_id", after_id, 0)
        _integer("limit", limit, 1, 100)
        now = self.clock()
        filters = {
            None: ("", ()),
            "open": ("AND completed_by IS NULL AND (lease_expires_at IS NULL OR lease_expires_at <= ?)", (now,)),
            "claimed": ("AND completed_by IS NULL AND lease_expires_at > ?", (now,)),
            "completed": ("AND completed_by IS NOT NULL", ()),
        }
        if status is not None and (not isinstance(status, str) or status not in filters):
            raise CollaborationError("invalid_arguments", "status must be open, claimed or completed.")
        condition, parameters = filters[status]
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE id > ? {condition} ORDER BY id LIMIT ?", (after_id, *parameters, limit + 1),
            ).fetchall()
        return _page([self._task(row, now) for row in rows], "tasks", after_id, limit)

    def claim_task(
        self, *, task_id: int, request_id: str, lease_seconds: int = 900, files: list[dict] | None = None,
    ) -> dict:
        _integer("task_id", task_id, 1)
        _integer("lease_seconds", lease_seconds, 1, 86400)
        _path_list("files", files)

        def claim(connection: sqlite3.Connection, now: float) -> dict:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise CollaborationError("task_missing", "Task does not exist.")
            if row["completed_by"] is not None:
                raise CollaborationError("task_completed", "The task is already completed.")
            if row["lease_expires_at"] is not None and row["lease_expires_at"] > now:
                raise CollaborationError("task_claimed", "The task already has an active lease.")
            planned = json.loads(row["files"])
            if files == [] and planned:
                raise CollaborationError("invalid_arguments", "A task with declared files requires a nonempty scope.")
            scopes = self._file_scopes(planned if files is None else files)
            if scopes:
                active = connection.execute(
                    "SELECT id, files FROM tasks WHERE id <> ? AND completed_by IS NULL AND lease_expires_at > ?",
                    (task_id, now),
                )
                for other in active:
                    other_scopes = json.loads(other["files"])
                    if any(_scope_overlap(scope, claimed) for scope in scopes for claimed in other_scopes):
                        raise CollaborationError("scope_conflict", f"The file scope overlaps active task {other['id']}.")
            connection.execute(
                "UPDATE tasks SET files = ?, owner = ?, lease_token = ?, lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (_json(scopes), self.agent_id, secrets.token_urlsafe(24), now + lease_seconds, now, task_id),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._task(row, now, include_token=True)

        return self._write("claim_task", request_id, dict(task_id=task_id, lease_seconds=lease_seconds, files=files), claim)

    def _change_lease(
        self, operation: str, task_id: int, lease_token: str, request_id: str, *,
        lease_seconds: int | None = None, result: str | None = None,
    ) -> dict:
        _integer("task_id", task_id, 1)
        _string("lease_token", lease_token, 128)

        def change(connection: sqlite3.Connection, now: float) -> dict:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise CollaborationError("task_missing", "Task does not exist.")
            if (
                row["owner"] != self.agent_id or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= now or row["lease_token"] != lease_token
            ):
                raise CollaborationError("lease_invalid", "This operation requires the current agent's active lease token.")
            if operation == "renew_task":
                expires_at = max(row["lease_expires_at"], now + lease_seconds)
                connection.execute("UPDATE tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ?", (expires_at, now, task_id))
            else:
                completed_by = self.agent_id if operation == "finish_task" else None
                connection.execute(
                    "UPDATE tasks SET owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ?, "
                    "completed_by = ?, result = ? WHERE id = ?",
                    (now, completed_by, result, task_id),
                )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._task(row, now, include_token=operation == "renew_task")

        payload = dict(task_id=task_id, lease_token=lease_token, lease_seconds=lease_seconds, result=result)
        return self._write(operation, request_id, payload, change)

    def renew_task(self, *, task_id: int, lease_token: str, request_id: str, lease_seconds: int = 900) -> dict:
        _integer("lease_seconds", lease_seconds, 1, 86400)
        return self._change_lease("renew_task", task_id, lease_token, request_id, lease_seconds=lease_seconds)

    def finish_task(self, *, task_id: int, lease_token: str, request_id: str, result: str = "") -> dict:
        _string("result", result, 12000, allow_empty=True)
        return self._change_lease("finish_task", task_id, lease_token, request_id, result=result)

    def release_task(self, *, task_id: int, lease_token: str, request_id: str) -> dict:
        return self._change_lease("release_task", task_id, lease_token, request_id)
