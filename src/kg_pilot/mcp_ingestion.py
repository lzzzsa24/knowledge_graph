"""Codex-only local ingestion tools, independent of the MCP transport and web UI.

Long operations use a hidden worker inside host process containment and the
ingestion pipeline's durable locks and checkpoints. Tool arguments cannot select a provider, credential, model,
corpus root, executable, or command.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import subprocess
import sys
import time
import uuid

import pymupdf
import psutil

from kg_pilot import ingestion as ing
from kg_pilot.ingestion_runners import codex_status
from kg_pilot.pipeline import PILOT, ROOT, read_json


class MCPToolError(ValueError):
    """A controlled tool error; never contains a file or provider exception."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_BATCH_ID = r"^b_[a-f0-9]{20}$"
_ITEM_ID = r"^item_[0-9]{3}$"
_STARTUP_SECONDS = 30
_PROVENANCE_STRINGS = {
    "title", "publisher", "document_number", "version", "status", "source_url",
    "pdf_url", "doi", "accessed_at", "access_route", "document_type",
    "document_status", "acquisition_status", "relevance_tier", "relevance_reason", "notes",
}
_PROVENANCE_PROPERTIES = {key: {"type": "string", "maxLength": 4096}
                          for key in sorted(_PROVENANCE_STRINGS)}
_PROVENANCE_PROPERTIES.update(year={"type": ["integer", "null"], "minimum": 1000, "maximum": 9999},
                              authors={"type": "array", "maxItems": 100,
                                       "items": {"type": "string", "maxLength": 256}})
_PATHS = {"type": "array", "minItems": 1, "maxItems": ing.LIMITS["max_files"],
          "items": {"type": "string", "minLength": 1, "maxLength": 4096}}
_ID_PROPERTY = {"type": "string", "pattern": _BATCH_ID}


def _spec(name, description, properties, required=(), *, read_only=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                            "openWorldHint": not read_only}}


def tool_specs() -> list[dict]:
    """Return transport-neutral MCP tool definitions."""
    return [
        _spec("ingestion_status", "查看 Codex CLI 状态及 Codex 入库批次；提供 batch_id 查看进度、错误、取消请求和下一步。不会列出或访问模型 API 批次。",
              {"batch_id": _ID_PROPERTY, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, read_only=True),
        _spec("ingestion_wait", "在当前 MCP 会话内等待指定 Codex 批次最多 25 秒；完成、取消、失败或待处理时立即返回。长期任务应反复调用此工具直到终态，保持会话连接。主机可能在断开 MCP 时终止整个工作进程树；重连后查看状态并 ingestion_resume 复用检查点。",
              {"batch_id": _ID_PROPERTY, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 25, "default": 25}}, ("batch_id",), read_only=True),
        _spec("ingestion_validate", "只读检查明确指定的本机绝对 PDF 路径、文件大小、可打开性、加密、页数和 SHA-256；不递归目录、不下载、不执行模型、不建立批次。",
              {"paths": _PATHS}, ("paths",), read_only=True),
        _spec("ingestion_start", "将明确指定的本机 PDF 复制到新 Codex 批次并立即返回。后台独立使用 gpt-6.1-sol / ultra 抽取和复核；默认 auto_publish=true，复核通过后构建索引并完成正式入库；false 时停在 ready，随后调用 ingestion_publish。保持当前 MCP 会话并反复调用 ingestion_wait 直到完成；主机可能在断开 MCP 时停止工作进程，重连后可继续。",
              {"paths": _PATHS, "max_claims": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8},
               "workers": {"type": "integer", "minimum": 1, "maximum": ing.LIMITS["max_workers"], "default": 1,
                           "description": "同时抽取和复核的资料数；同一份资料先抽取后复核，最终统一发布。"},
               "auto_publish": {"type": "boolean", "default": True},
               "provenance": {"type": "array", "minItems": 1, "maxItems": ing.LIMITS["max_files"],
                              "description": "可选来源元数据，按 paths 顺序逐项对应；只填写已核实信息。",
                              "items": {"type": "object", "properties": _PROVENANCE_PROPERTIES, "additionalProperties": False}}}, ("paths",)),
        _spec("ingestion_resume", "在后台继续指定 Codex 批次，复用已完成抽取与复核检查点并清除旧取消请求；必要时仅恢复本批次拥有的中断发布事务。保留此前明确的发布意图；普通 ready 草稿仍需 ingestion_publish。已完成或正在运行的批次不会重复执行。",
              {"batch_id": _ID_PROPERTY}, ("batch_id",)),
        _spec("ingestion_publish", "将已复核且处于 ready 的 Codex 批次在后台构建索引并发布；也可重试 publication_failed。不会执行新的抽取或复核。发布会修改配置的文献库，并由原发布管线保留备份和事务日志。",
              {"batch_id": _ID_PROPERTY}, ("batch_id",)),
        _spec("ingestion_cancel", "请求协作取消指定 Codex 批次，在当前模型任务结束后的检查点停止；不删除资料、检查点或已发布内容，也不会中止已经开始的原子发布。",
              {"batch_id": _ID_PROPERTY}, ("batch_id",)),
        _spec("ingestion_skip", "跳过指定 Codex 批次中待处理的单份资料，保留原 PDF 和检查点；仅限批次未运行且未发布时。随后可 ingestion_resume 继续其他资料。",
              {"batch_id": _ID_PROPERTY, "item_id": {"type": "string", "pattern": _ITEM_ID}}, ("batch_id", "item_id")),
    ]


def _arguments(name, arguments):
    specs = {item["name"]: item["inputSchema"] for item in tool_specs()}
    if name not in specs:
        raise MCPToolError("unknown_tool", "未知的入库工具。")
    schema = specs[name]
    if not isinstance(arguments, dict) or set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
        raise MCPToolError("invalid_arguments", "工具参数缺失或包含不支持的字段。")
    for key in ("batch_id", "item_id"):
        if key in arguments and (not isinstance(arguments[key], str) or not re.fullmatch(_BATCH_ID if key == "batch_id" else _ITEM_ID, arguments[key])):
            raise MCPToolError("invalid_arguments", "批次或资料编号无效。")
    for key, low, high in (("limit", 1, 100), ("max_claims", 1, ing.LIMITS["max_claims"]), ("workers", 1, ing.LIMITS["max_workers"]), ("timeout_seconds", 1, 25)):
        if key in arguments and (type(arguments[key]) is not int or not low <= arguments[key] <= high):
            raise MCPToolError("invalid_arguments", f"{key} 超出支持范围。")
    if "auto_publish" in arguments and type(arguments["auto_publish"]) is not bool:
        raise MCPToolError("invalid_arguments", "auto_publish 必须为布尔值。")
    if "provenance" in arguments:
        rows = arguments["provenance"]
        if not isinstance(rows, list) or not isinstance(arguments.get("paths"), list) or len(rows) != len(arguments["paths"]):
            raise MCPToolError("invalid_arguments", "来源元数据应与 PDF 路径逐项对应。")
        for row in rows:
            if not isinstance(row, dict) or set(row) - set(_PROVENANCE_PROPERTIES):
                raise MCPToolError("invalid_arguments", "来源元数据包含不支持的字段。")
            for key, value in row.items():
                if key in _PROVENANCE_STRINGS:
                    valid = isinstance(value, str) and len(value) <= 4096
                elif key == "year":
                    valid = value is None or type(value) is int and 1000 <= value <= 9999
                else:
                    valid = isinstance(value, list) and len(value) <= 100 and all(isinstance(v, str) and len(v) <= 256 for v in value)
                if not valid:
                    raise MCPToolError("invalid_arguments", "来源元数据的字段类型或长度无效。")


def _local_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise MCPToolError("invalid_pdf_path", "请提供明确的本机绝对 PDF 文件路径。")
    # Reject URLs, UNC, extended/device namespaces, streams, wildcards and pipes
    # before resolving or opening anything. A drive prefix is the only allowed colon.
    normalized = value.replace("\\", "/")
    if (normalized.startswith("//") or normalized.startswith(("/??/", "/Device/"))
            or "://" in normalized or any(c in value for c in "*?|")
            or ":" in normalized[2:] or PureWindowsPath(value).is_reserved()):
        raise MCPToolError("invalid_pdf_path", "只支持本机普通 PDF 文件；不支持网络、设备、通配符或数据流路径。")
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != ".pdf":
        raise MCPToolError("invalid_pdf_path", "请提供明确的本机绝对 PDF 文件路径。")
    try:
        path = path.resolve(strict=True)
        if str(path).replace("\\", "/").startswith("//") or not stat.S_ISREG(path.stat().st_mode):
            raise MCPToolError("invalid_pdf_path", "PDF 路径必须指向本机普通文件。")
    except (OSError, RuntimeError):
        raise MCPToolError("pdf_unreadable", "PDF 文件不存在或无法读取。") from None
    return path


def _read_pdfs(paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= ing.LIMITS["max_files"]:
        raise MCPToolError("invalid_arguments", "每批需要明确指定 1–20 个 PDF 文件路径。")
    per_file = ing.LIMITS["max_file_mb"] * 1024 ** 2
    total_limit = ing.LIMITS["max_total_mb"] * 1024 ** 2
    payloads, summaries, total = [], [], 0
    for index, supplied in enumerate(paths):
        path = _local_path(supplied)
        try:
            with path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                if size > per_file or size + total > total_limit:
                    raise MCPToolError("pdf_size_limit", "单份 PDF 最多 30 MB，每批合计最多 60 MB。")
                data = stream.read(min(per_file, total_limit - total) + 1)
        except OSError:
            raise MCPToolError("pdf_unreadable", "PDF 文件无法读取。") from None
        total += len(data)
        if len(data) > per_file or total > total_limit:
            raise MCPToolError("pdf_size_limit", "单份 PDF 最多 30 MB，每批合计最多 60 MB。")
        if not data.startswith(b"%PDF-"):
            raise MCPToolError("invalid_pdf", "文件缺少有效 PDF 标识。")
        try:
            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                if pdf.is_encrypted or pdf.needs_pass:
                    raise MCPToolError("encrypted_pdf", "PDF 需要密码，请提供可读取的副本。")
                pages = len(pdf)
                if not 1 <= pages <= ing.LIMITS["max_pages"]:
                    raise MCPToolError("pdf_page_limit", "每份 PDF 支持 1–120 页，请按章节拆分较长资料。")
        except MCPToolError:
            raise
        except Exception:
            raise MCPToolError("invalid_pdf", "PDF 无法打开，请检查文件是否完整。") from None
        digest = hashlib.sha256(data).hexdigest()
        payloads.append((path.name, data))
        summaries.append({"index": index, "filename": path.name, "bytes": len(data), "pages": pages, "sha256": digest})
    return payloads, summaries


def _marker_path(batch_dir):
    return batch_dir / "mcp_worker.json"


def _marker(batch_dir):
    try:
        record = read_json(_marker_path(batch_dir))
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _process_identity(pid):
    """Query process birth without sending signals or leaving host containment."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        process = psutil.Process(pid)
        if process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return None
        return repr(process.create_time())
    except (psutil.Error, OSError, ValueError):
        return None


def _alive(record):
    expected = record.get("process_identity")
    return expected is not None and _process_identity(record.get("pid")) == expected


def _process_record(batch_dir, marker):
    try:
        record = read_json(batch_dir / "mcp_worker_process.json")
        return record if isinstance(record, dict) and record.get("token") == marker.get("token") else {}
    except (OSError, ValueError, TypeError):
        return {}


def _starting(record):
    launched = record.get("launched_at_epoch")
    return record.get("state") == "starting" and type(launched) in (int, float) and 0 <= time.time() - launched < _STARTUP_SECONDS


class IngestionTools:
    """A server configured for one trusted corpus; callers cannot change it."""

    def __init__(self, pilot: Path = PILOT):
        self.pilot = Path(pilot).resolve()

    def _codex_batch(self, batch_id):
        try:
            path = ing.batch_path(batch_id, self.pilot)
            path.resolve().relative_to((self.pilot / "ingestion/batches").resolve())
            raw = read_json(path / "batch.json")
            if raw.get("mode") != "codex":
                raise MCPToolError("batch_not_found", "未找到可由此工具操作的 Codex 批次。")
        except (OSError, ValueError, TypeError):
            raise MCPToolError("batch_not_found", "未找到可由此工具操作的 Codex 批次。") from None
        batch = ing.get_batch(batch_id, self.pilot)
        marker = _marker(path)
        try:
            finished_since_launch = (raw.get("status") in {"completed", "ready", "cancelled", "failed", "needs_attention"}
                                     and datetime.fromisoformat(raw["updated_at"]).timestamp() >= marker["launched_at_epoch"])
        except (KeyError, TypeError, ValueError):
            finished_since_launch = False
        if not batch["running"] and marker.get("state") == "starting" and not finished_since_launch and batch["status"] != "completed":
            process = _process_record(path, marker)
            active = _alive(process) if process else _alive(marker.get("owner", {}))
            # The short handshake expires only until a concrete worker PID is
            # persisted. A matching live process may legitimately import slowly;
            # its identity (including birth time) then determines liveness.
            if active and (bool(process) or _starting(marker)):
                batch.update(running=True, status="running", stage="starting")
            elif raw.get("status") in {"queued", "running"}:
                batch.update(status="needs_attention", stage="interrupted",
                             error="MCP 会话或后台进程已中断；可继续处理，已完成的检查点会复用。")
        if marker.get("state") == "failed" and not batch["running"] and batch["status"] != "completed":
            batch["worker_error"] = ("发布恢复未完成，请检查维护日志和备份后重试；其他批次保持原状。"
                                     if marker.get("error_code") == "publication_recovery_failed" else
                                     "后台任务未能取得执行权或启动，请稍后继续。")
        if batch["status"] == "completed":
            # The final durable checkpoint is written just before run_batch
            # releases its OS locks. Completed means no work remains, including
            # during that short cleanup interval.
            batch["running"] = False
            batch["next_action"] = "none"
        elif batch["running"]:
            batch["next_action"] = "ingestion_status"
        elif batch["status"] == "ready" or batch.get("stage") == "publication_failed":
            batch["next_action"] = "ingestion_publish"
        else:
            batch["next_action"] = "ingestion_resume"
        batch["events"] = batch.get("events", [])[-20:]
        return batch

    def _publication_requested(self, batch_id):
        path = ing.batch_path(batch_id, self.pilot)
        raw, marker = read_json(path / "batch.json"), _marker(path)
        return (marker.get("publish_only") is True and (marker.get("state") in {"starting", "failed"} or raw.get("stage") == "publication_failed")
                or raw.get("auto_publish") is False and raw.get("stage") in {"publishing", "publication_failed"})

    def _recovery_needed(self, batch_id):
        """Read-only preflight; actual ownership is checked again under lock."""
        from kg_pilot.ingestion_publish import JOURNAL, MAINTENANCE
        journal_path, maintenance_path = self.pilot / JOURNAL, self.pilot / MAINTENANCE
        journal = read_json(journal_path) if journal_path.exists() else None
        maintenance = read_json(maintenance_path) if maintenance_path.exists() else None
        if journal is not None and not isinstance(journal, dict) or maintenance is not None and not isinstance(maintenance, dict):
            raise MCPToolError("publication_recovery_blocked", "发布维护日志格式无效，请检查备份并通过原发布入口恢复。")
        pending = maintenance is not None or (journal is not None and journal.get("status") not in {"committed", "rolled_back"})
        if not pending:
            return False
        if journal is None:
            raise MCPToolError("publication_recovery_blocked", "文献库处于维护状态但缺少恢复日志，请检查备份并通过原发布入口恢复。")
        if journal.get("batch_id") != batch_id or maintenance is not None and maintenance.get("batch_id") != batch_id:
            raise MCPToolError("publication_owned_elsewhere", "文献库有其他批次尚未完成的发布；请通过该批次原入口恢复，当前工具不会修改该维护状态。")
        return True

    def _launch(self, batch_id, *, publish_only=False):
        path = ing.batch_path(batch_id, self.pilot)
        with ing.file_lock(self.pilot / "ingestion/mcp_launch.lock"):
            current = self._codex_batch(batch_id)
            if current["running"] or current["status"] == "completed":
                return current
            self._recovery_needed(batch_id)
            # Avoid creating a new subprocess for a known occupied global worker.
            with ing.file_lock(self.pilot / "ingestion/global_worker.lock"):
                pass
            cancellation = path / "cancel.request"
            if cancellation.exists():
                cancellation.unlink()
            token = uuid.uuid4().hex
            record = {"version": 1, "token": token, "state": "starting", "launched_at_epoch": time.time(), "publish_only": publish_only,
                      "owner": {"pid": os.getpid(), "process_identity": _process_identity(os.getpid())}}
            ing.write_atomic(_marker_path(path), record)
            command = [str(Path(sys.executable).resolve()), "-m", "kg_pilot.mcp_ingestion", "--worker", batch_id,
                       "--pilot", str(self.pilot), "--token", token]
            if publish_only:
                command.append("--publish-only")
            environment = os.environ.copy()
            # A constant trusted module path is necessary when launched from an
            # installed entrypoint or from a fresh MCP session with another cwd.
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                       "cwd": str(ROOT), "env": environment, "close_fds": True}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                process = subprocess.Popen(command, **options)
                ing.write_atomic(path / "mcp_worker_process.json", {"token": token, "pid": process.pid,
                                 "process_identity": _process_identity(process.pid)})
            except (OSError, ValueError):
                record.update(state="failed")
                ing.write_atomic(_marker_path(path), record)
                raise MCPToolError("worker_start_failed", "后台进程未能启动，批次已保留，可稍后继续。") from None
        result = self._codex_batch(batch_id)
        result["accepted"] = True
        return result

    def dispatch_tool(self, name: str, arguments: dict) -> dict:
        _arguments(name, arguments)
        try:
            return self._dispatch(name, arguments)
        except MCPToolError:
            raise
        except ing.IntakeError as error:
            # IntakeError messages are deliberately controlled by ingestion.py.
            raise MCPToolError("ingestion_error", str(error)) from None
        except (OSError, ValueError, TypeError, KeyError):
            raise MCPToolError("local_state_error", "本地入库状态无法读取或写入，请检查配置的文献库。") from None

    def _dispatch(self, name, arguments):
        if name == "ingestion_wait":
            deadline = time.monotonic() + arguments.get("timeout_seconds", 25)
            while True:
                batch = self._codex_batch(arguments["batch_id"])
                if not batch["running"] or time.monotonic() >= deadline:
                    return {"batch": batch, "worker_lifetime": "host_session",
                            "wait_timed_out": batch["running"]}
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        if name == "ingestion_status":
            if "batch_id" in arguments:
                return {"backend": codex_status(), "batch": self._codex_batch(arguments["batch_id"]), "worker_lifetime": "host_session"}
            batches = []
            candidates = list((self.pilot / "ingestion/batches").glob("b_*/batch.json"))
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for candidate in candidates:
                try:
                    # Inspect only the mode before using any batch API: API
                    # batches get no locks, mutations or returned metadata.
                    if read_json(candidate).get("mode") == "codex":
                        batches.append(self._codex_batch(candidate.parent.name))
                except (MCPToolError, ing.IntakeError, OSError, ValueError, TypeError):
                    continue
                if len(batches) >= arguments.get("limit", 20):
                    break
            return {"backend": codex_status(), "limits": deepcopy(ing.LIMITS), "batches": batches, "worker_lifetime": "host_session"}
        if name in {"ingestion_validate", "ingestion_start"}:
            payloads, summaries = _read_pdfs(arguments["paths"])
            if name == "ingestion_validate":
                return {"valid": True, "files": summaries, "total_bytes": sum(item["bytes"] for item in summaries),
                        "limits": deepcopy(ing.LIMITS)}
            if not codex_status()["available"]:
                raise MCPToolError("codex_unavailable", "未找到 Codex CLI，请先安装并登录后使用此模式。")
            batch = ing.create_batch(payloads, mode="codex", max_claims=arguments.get("max_claims", 8),
                                     auto_publish=arguments.get("auto_publish", True), pilot=self.pilot,
                                     provenance=arguments.get("provenance"), workers=arguments.get("workers", 1))
            try:
                started = self._launch(batch["id"])
            except (MCPToolError, ing.IntakeError) as error:
                # Return the durable batch identifier even when another worker
                # won a race; callers can poll/resume without creating a duplicate.
                started = self._codex_batch(batch["id"])
                started["accepted"] = False
                started["start_message"] = "批次已建立，后台暂未启动；请稍后 ingestion_resume。"
                started["start_error"] = {"code": getattr(error, "code", "ingestion_error"), "message": str(error)}
            return {"batch": started, "files": summaries}
        batch_id = arguments["batch_id"]
        current = self._codex_batch(batch_id)
        if name == "ingestion_resume":
            publish_requested = self._publication_requested(batch_id)
            if current["status"] == "ready" and not current["running"] and not publish_requested:
                return {"batch": current}
            return {"batch": self._launch(batch_id, publish_only=publish_requested)}
        if name == "ingestion_publish":
            if current["running"] or current["status"] == "completed":
                return {"batch": current}
            if current["status"] != "ready" and current.get("stage") != "publication_failed" and not self._publication_requested(batch_id):
                raise MCPToolError("batch_not_ready", "批次尚未全部复核完成，请先查看状态并继续处理。")
            return {"batch": self._launch(batch_id, publish_only=True)}
        if name == "ingestion_cancel":
            if current["status"] != "completed":
                ing.cancel_batch(batch_id, self.pilot)
            return {"batch": self._codex_batch(batch_id)}
        if name == "ingestion_skip":
            if current["running"]:
                raise MCPToolError("batch_running", "请先请求取消并等待当前任务停止，再跳过资料。")
            ing.skip_document(batch_id, arguments["item_id"], self.pilot)
            return {"batch": self._codex_batch(batch_id)}
        raise MCPToolError("unknown_tool", "未知的入库工具。")


def dispatch_tool(name: str, arguments: dict, *, pilot: Path | None = None) -> dict:
    return IngestionTools(PILOT if pilot is None else pilot).dispatch_tool(name, arguments)


def _worker(batch_id, pilot, token, publish_only=False):
    """Private process entrypoint; its root is fixed by the configured server."""
    service = IngestionTools(pilot)
    path = ing.batch_path(batch_id, service.pilot)
    record = _marker(path)
    if (record.get("token") != token or record.get("state") != "starting"
            or not (_starting(record) or _alive(_process_record(path, record)))):
        return 1
    try:
        service._codex_batch(batch_id)  # Reject non-Codex batches in the worker too.
        if service._recovery_needed(batch_id):
            from kg_pilot.ingestion_publish import recover_publication, PublicationError
            try:
                recover_publication(service.pilot, expected_batch_id=batch_id)
            except PublicationError:
                record.update(state="failed", error_code="publication_recovery_failed")
                if _marker(path).get("token") == token:
                    ing.write_atomic(_marker_path(path), record)
                return 1
        ing.run_batch(batch_id, service.pilot, publish_only=publish_only, clear_cancel=False)
    except Exception:
        record.update(state="failed")
        if _marker(path).get("token") == token:
            ing.write_atomic(_marker_path(path), record)
        return 1
    record.update(state="finished")
    if _marker(path).get("token") == token:
        ing.write_atomic(_marker_path(path), record)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private session-contained Codex ingestion worker")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--publish-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        return _worker(args.worker, args.pilot, args.token, args.publish_only)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
