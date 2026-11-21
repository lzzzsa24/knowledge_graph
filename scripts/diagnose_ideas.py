"""Programmatic local diagnostics for research ideas; no browser or key access.

The generate command calls the configured model and consumes its API allowance.
All other commands inspect the local service without generating model content.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8600"
ENDPOINTS = {
    "status": ("GET", "/api/debug/health", 10),
    "runs": ("GET", "/api/debug/ideas/runs", 10),
    "preview": ("POST", "/api/debug/ideas/preview", 420),
    "generate": ("POST", "/api/ideas", 420),
}


class DiagnosticError(Exception):
    """A short, safe user-facing diagnostic failure."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DiagnosticError(f"命令参数不正确：{message}。可使用 --help 查看示例。")


def _count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("候选数量须为 1 至 5 的整数") from None
    if not 1 <= count <= 5:
        raise argparse.ArgumentTypeError("候选数量须为 1 至 5 的整数")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        description="通过固定本地服务 127.0.0.1:8600 调试创新点；详细结果保存为 JSON，无需浏览器。",
        epilog="preview 不调用生成模型，也不需要密钥；generate 会真实调用现有平台并消耗已配置模型额度。",
    )
    parser.add_argument("--output", type=Path, help="详细诊断 JSON 路径；默认保存在项目 .cache/idea_diagnostics 下")
    commands = parser.add_subparsers(dest="operation", required=True)
    descriptions = {
        "status": "检查本地服务、文献库与索引状态",
        "runs": "读取当前服务进程的生成记录",
        "preview": "仅预览检索与上下文，不调用生成模型、不需要密钥",
        "generate": "真实调用现有平台生成候选，会消耗已配置模型额度",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--output", type=Path, default=argparse.SUPPRESS,
                             help="详细诊断 JSON 路径；默认生成唯一时间文件")
        if name in {"preview", "generate"}:
            inputs = command.add_mutually_exclusive_group()
            inputs.add_argument("--prompt", default="", help="研究方向或讨论文本，最多 6000 字符；留空为随机模式")
            inputs.add_argument("--input-file", type=Path, help="读取 UTF-8 文本作为 prompt，最多 6000 字符")
            command.add_argument("--history-file", type=Path, help="可选 UTF-8 JSON 对话列表，最多 12 条 role/content；不截断内容")
            command.add_argument("--count", type=_count, default=3, help="候选数量 1 至 5，默认 3")
    return parser


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise DiagnosticError(f"{label}不是有效的 UTF-8 文本，请转换编码后重试。") from None
    except (OSError, ValueError):
        raise DiagnosticError(f"无法读取{label}，请检查文件路径和访问权限。") from None


def _payload(args: argparse.Namespace) -> dict | None:
    if args.operation not in {"preview", "generate"}:
        return None
    prompt = _read_text(args.input_file, "输入文件") if args.input_file else args.prompt
    if len(prompt) > 6000:
        raise DiagnosticError("研究输入超过 6000 字符，请自行精简后重试；程序不会截断原文。")
    history = []
    if args.history_file:
        try:
            history = json.loads(_read_text(args.history_file, "对话文件"))
        except ValueError:
            raise DiagnosticError("对话文件不是有效 JSON，请提供 role/content 对话列表。") from None
        if not isinstance(history, list) or len(history) > 12:
            raise DiagnosticError("对话文件须为最多 12 条消息的 JSON 列表；程序不会截断原对话。")
        for entry in history:
            if (not isinstance(entry, dict) or set(entry) != {"role", "content"}
                    or entry["role"] not in ("user", "assistant")
                    or not isinstance(entry["content"], str) or len(entry["content"]) > 16000):
                raise DiagnosticError("每条对话须只含 role 和 content；role 为 user/assistant，content 为不超过 16000 字符的文本。")
    return {"prompt": prompt, "history": history, "count": args.count}


def _validate_result(operation: str, result) -> None:
    if not isinstance(result, dict):
        raise DiagnosticError("服务返回的 JSON 格式不符合预期，请检查服务版本。")
    valid = False
    if operation == "status":
        valid = (result.get("status") in ("ok", "degraded")
                 and result.get("service") == "knowledge_graph"
                 and isinstance(result.get("corpus"), dict)
                 and isinstance(result.get("indexes"), dict)
                 and type(result.get("active_generations")) is int)
    elif operation == "runs":
        valid = (isinstance(result.get("runs"), list)
                 and all(isinstance(run, dict) and isinstance(run.get("run_id"), str)
                         for run in result["runs"]))
    elif operation == "preview":
        valid = (result.get("mode") in ("random", "guided")
                 and result.get("provider_called") is False
                 and isinstance(result.get("sources"), list)
                 and isinstance(result.get("contexts"), list)
                 and isinstance(result.get("focus"), str)
                 and type(result.get("count")) is int and 1 <= result["count"] <= 5
                 and type(result.get("context_chars")) is int)
    elif operation == "generate":
        valid = (type(result.get("generated")) is bool
                 and result.get("mode") in ("random", "guided")
                 and isinstance(result.get("ideas"), list)
                 and all(isinstance(idea, dict) and isinstance(idea.get("title"), str)
                         for idea in result["ideas"])
                 and (not result["generated"] or bool(result["ideas"]))
                 and isinstance(result.get("sources"), list)
                 and (result.get("run_id") is None or isinstance(result["run_id"], str)))
    if not valid:
        raise DiagnosticError("服务返回的 JSON 结构不完整或与本命令不匹配，请检查服务版本。")


def _summary(operation: str, response: dict | None, report_path: Path, failed: bool) -> dict:
    count = None
    run_id = response.get("run_id") if isinstance(response, dict) else None
    status = "error" if failed else "ok"
    if response and not failed:
        if operation == "status":
            status = response["status"]
        elif operation == "runs":
            count = len(response["runs"])
            run_id = response["runs"][0].get("run_id") if count else None
        elif operation == "preview":
            status, count = "preview", response["count"]
        elif operation == "generate":
            status = "generated" if response["generated"] else "not_generated"
            count, run_id = len(response["ideas"]), response.get("run_id")
    return {"status": status, "count": count, "run_id": run_id, "report": str(report_path)}


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None,
         output_dir: Path | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    """Return an exit code; injectable client keeps tests entirely offline."""
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    try:
        args = build_parser().parse_args(argv)
        payload = _payload(args)
    except DiagnosticError as error:
        print(f"错误：{error}", file=stderr)
        return 1
    method, endpoint, timeout = ENDPOINTS[args.operation]
    at = datetime.now(timezone.utc)
    report_path = args.output or (output_dir or ROOT / ".cache/idea_diagnostics") / (
        f"{at.strftime('%Y%m%dT%H%M%S_%fZ')}_{args.operation}_{uuid.uuid4().hex[:8]}.json")
    try:
        report_path = report_path.expanduser().resolve()
    except (OSError, ValueError):
        print("错误：无法解析 --output 路径，请检查路径格式。", file=stderr)
        return 1
    report = {"operation": args.operation, "at": at.isoformat(), "url": BASE_URL + endpoint,
              "request": payload, "http_status": None, "elapsed_seconds": 0.0,
              "response": None, "error": None}
    failed = False
    started = time.monotonic()
    owned_client = client is None
    try:
        if client is None:
            client = httpx.Client(trust_env=False, follow_redirects=False)
        options = {"timeout": timeout}
        if payload is not None:
            options["json"] = payload
        response = client.request(method, BASE_URL + endpoint, **options)
        report["http_status"] = response.status_code
        try:
            report["response"] = response.json()
        except ValueError:
            if response.is_success:
                raise DiagnosticError("服务返回了非 JSON 内容，请检查本地服务版本。") from None
        if not response.is_success:
            raise DiagnosticError(f"服务拒绝请求（HTTP {response.status_code}），请查看诊断 JSON。")
        _validate_result(args.operation, report["response"])
    except httpx.TimeoutException:
        failed = True
        report["error"] = {"kind": "timeout", "message": "等待本地服务超时，请稍后重试。"}
    except httpx.ConnectError:
        failed = True
        report["error"] = {"kind": "connection", "message": "无法连接 127.0.0.1:8600，请先启动本地服务。"}
    except httpx.RequestError:
        failed = True
        report["error"] = {"kind": "request", "message": "与本地服务通信失败，请检查服务状态后重试。"}
    except DiagnosticError as error:
        failed = True
        report["error"] = {"kind": "response", "message": str(error)}
    finally:
        if owned_client and client is not None:
            client.close()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print("错误：无法保存诊断 JSON，请检查 --output 路径和写入权限。", file=stderr)
        return 1
    print(json.dumps(_summary(args.operation, report["response"], report_path, failed), ensure_ascii=False), file=stdout)
    if failed:
        print(f"错误：{report['error']['message']}", file=stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
