"""Active-Codex file handoff for this explicitly agent-operated collection run.

This is not an unattended model service. The normal ingestion runner remains
available for CLI/API operation. Requests and agent-authored replies preserve
source hashes and the same strict normalization, review and publication gates.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot import ingestion as intake
from kg_pilot import ingestion_runners as runners


def bridge(mode, task, packet, workdir):
    signature = hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    request = workdir / (task + ".agent_request.json")
    reply = workdir / (task + ".agent_response.json")
    system, user = runners._prompt(task, packet)
    visual_pages = []
    visual_source = None
    if task == "review":
        proposal = packet.get("proposal", {})
        visual_pages = sorted({ev["pdf_page"] for item in [proposal.get("classification", {}), *proposal.get("claims", [])]
                               for ev in item.get("evidence", []) if ev.get("parser") in {"rapidocr", "rapidocr_table"}})
        if visual_pages:
            batch = json.loads((workdir.parents[2] / "batch.json").read_text(encoding="utf-8-sig"))
            document = next(d for d in batch["documents"] if d["doc_id"] == packet["doc_id"])
            visual_source = {"pdf_path": document["source_path"], "sha256": document["sha256"]}
    # Do not duplicate the full source text in an extra serialized user prompt.
    intake.write_atomic(request, {"signature": signature, "task": task,
        "required_model": "gpt-6.1-sol", "required_reasoning_effort": "ultra",
        "instructions": system, "contract": runners._EXTRACT_CONTRACT if task == "extract" else runners._REVIEW_CONTRACT,
        "packet": packet, "visual_review_required_pages": visual_pages, "visual_source": visual_source,
        "visual_review_instruction": "有OCR引用时，必须渲染并实际查看这些PDF原页，核对数字/符号/样品归属；在响应envelope写visual_pages_checked。" if visual_pages else None})
    if not reply.exists():
        raise intake.IntakeError("等待本次 Codex agent 处理已保存的任务包。")
    saved = json.loads(reply.read_text(encoding="utf-8-sig"))
    if saved.get("signature") != signature:
        raise intake.IntakeError("任务包已更新，等待 Codex agent 重新处理。")
    actor = saved.get("actor", "")
    if not isinstance(actor, str) or not actor.startswith("/root/"):
        raise intake.IntakeError("缺少本轮实际 Codex agent 标识。")
    if task == "review":
        extraction = workdir / "extract.agent_response.json"
        if extraction.exists() and json.loads(extraction.read_text(encoding="utf-8-sig")).get("actor") == actor:
            raise intake.IntakeError("抽取与独立复核必须由不同 Codex agent 完成。")
        if visual_pages and not set(visual_pages).issubset(set(saved.get("visual_pages_checked", []))):
            raise intake.IntakeError("OCR引文尚未完成PDF原页图像复核。")
    try:
        result = runners._parse_result(json.dumps(saved["response"], ensure_ascii=False), task, packet)
    except (KeyError, runners.RunnerError) as error:
        intake.write_atomic(workdir / (task + ".agent_error.json"), {"signature": signature,
            "reason": getattr(error, "reason", "回复格式无效"), "type": type(error).__name__})
        raise intake.IntakeError("Codex agent 回复未通过格式或原文校验，已保存具体问题。") from None
    result["_result_meta"] = {"mode": "codex", "task": task, "model": "gpt-6.1-sol",
        "reasoning_effort": "ultra", "execution": "active_codex_agent", "actor": actor,
        "request_signature": signature, "response_sha256": hashlib.sha256(reply.read_bytes()).hexdigest(),
        "visual_pages_checked": visual_pages}
    return result


def advance(batch_id, clear_cancel=True):
    path = intake.batch_path(batch_id)
    batch = json.loads((path / "batch.json").read_text(encoding="utf-8-sig"))
    # Active agents may correct an earlier reply after reading later pages.
    # The regular runner cache key describes the input packet, so an edited
    # signed reply must explicitly invalidate its accepted output checkpoint.
    if batch["status"] != "completed":
        with intake.file_lock(path / "worker.lock"):
            changed = []
            for document in batch["documents"]:
                if document["status"] in {"duplicate", "skipped"}:
                    continue
                docdir = path / "documents" / document["id"]
                revised = False
                for workdir in docdir.glob("chunk_*"):
                    for task in ("extract", "review"):
                        cache = workdir / (task + ".json")
                        reply = workdir / (task + ".agent_response.json")
                        if not cache.exists() or not reply.exists():
                            continue
                        meta = json.loads(cache.read_text(encoding="utf-8-sig")).get("output", {}).get("_result_meta", {})
                        if meta.get("execution") != "active_codex_agent":
                            continue
                        if meta.get("response_sha256") != hashlib.sha256(reply.read_bytes()).hexdigest():
                            intake._invalidate(workdir, task)
                            if task == "extract":
                                intake._invalidate(workdir, "review")
                            revised = True
                if revised:
                    document.update(status="needs_attention", stage="reply_revised", error=None)
                    changed.append(document["id"])
            if changed:
                intake._save(path, batch, "Codex回复已修订，重新校验及独立复核：" + ", ".join(changed))
    # Reuse only independently validated, source-hash-bound parse caches.
    # Cache misses still use the strict parser, with source-bound visual reviews
    # for the campaign's verified nontechnical separator pages.
    for document in batch["documents"]:
        parsed_path = path / "documents" / document["id"] / "parsed.json"
        if not parsed_path.exists() and document["status"] not in {"duplicate", "skipped"}:
            from scripts.expansion_embeddings import cached_parse, DEFAULT_CACHE
            parsed, *_ = cached_parse(document, DEFAULT_CACHE)
            intake.write_atomic(parsed_path, parsed)
    result = intake.run_batch(batch_id, runner=bridge, workers=1, clear_cancel=clear_cancel)
    pending = []
    for request in path.glob("documents/*/chunk_*/*.agent_request.json"):
        value = json.loads(request.read_text(encoding="utf-8"))
        reply = request.with_name(request.name.replace(".agent_request", ".agent_response"))
        if not reply.exists() or json.loads(reply.read_text(encoding="utf-8-sig")).get("signature") != value["signature"]:
            pending.append({"task": value["task"], "request": str(request.resolve()), "signature": value["signature"]})
    return {"batch_id": batch_id, "status": result["status"], "progress": result["progress"], "pending": pending,
        "errors": [{"id": d["id"], "error": d.get("error")} for d in result["documents"] if d.get("error")]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_id")
    parser.add_argument("--watch", action="store_true", help="Advance when active agents write replies; never calls a model itself.")
    args = parser.parse_args()
    if not args.watch:
        print(json.dumps(advance(args.batch_id), ensure_ascii=False, indent=2))
        return
    path = intake.batch_path(args.batch_id)
    previous, first = None, True
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        if not first and (path / "cancel.request").exists():
            break
        stamps = sorted((str(p), p.stat().st_mtime_ns) for p in path.glob("documents/*/chunk_*/*.agent_response.json"))
        force = path / "advance.request"
        signature = (stamps, force.stat().st_mtime_ns if force.exists() else 0)
        if signature != previous:
            try:
                report = advance(args.batch_id, clear_cancel=first)
            except intake.IntakeError as error:
                if str(error) != "已有入库任务正在执行，请在当前任务结束后继续。":
                    raise
                # Other campaign watchers share the normal single-worker lock.
                # Retry later without consuming the newly arrived response.
                time.sleep(2)
                continue
            intake.write_atomic(path / "agent_queue.json", report)
            print(json.dumps({k: report[k] for k in ("batch_id", "status", "progress")}, ensure_ascii=False), flush=True)
            previous, first = signature, False
            if report["status"] in {"ready", "completed", "cancelled", "failed"}:
                break
        time.sleep(2)


if __name__ == "__main__":
    main()
