"""Resumable local PDF intake. Model work stays separate from verified publication."""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import re
import threading
import unicodedata
import uuid
import math
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from kg_pilot.pipeline import PILOT, read_json, normalize
from kg_pilot.classification import CATEGORY_LABELS, FACET_DEFINITIONS, CATEGORIES
from kg_pilot.validation import Claim

LIMITS = dict(max_files=20, max_file_mb=30, max_total_mb=60, max_pages=120,
              max_chars=450000, max_claims=30, chunk_chars=30000, max_workers=10)
PROMPT_VERSION = "ingestion-5-source-text-quality-quarantine"
PARSER_VERSION = "native-and-raster-tables-quality-2"
_THREADS: dict[str, threading.Thread] = {}
_THREAD_LOCK = threading.Lock()
_SAVE_LOCK = threading.RLock()
_PDF_PARSE_LOCK = threading.Lock()
_REPLACE_RETRY_SECONDS = 30.0
_REPLACE_SHORT_RETRY_SECONDS = 1.55


class IntakeError(ValueError):
    """A safe local message, without provider responses or credentials."""


class ReviewRejected(IntakeError):
    """A semantic review can request one fresh extraction with its feedback."""


def now():
    return datetime.now(timezone.utc).isoformat()


def _delete_access_state(path: Path):
    """Probe DELETE access without deleting; do not confuse available with denied."""
    if os.name != "nt":
        return "unknown"
    try:
        if path.stat().st_file_attributes & 0x1:  # FILE_ATTRIBUTE_READONLY
            return "readonly"
    except FileNotFoundError:
        return "missing"
    except PermissionError:
        return "denied"
    except OSError:
        return "unknown"
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # DELETE, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING. No write/delete call.
    handle = create(str(path), 0x10000, 0x7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {32, 33}:
            return "sharing"
        if error in {2, 3}:
            return "missing"
        return "denied" if error == 5 else "unknown"
    try:
        return "available"
    finally:
        close(handle)


def write_atomic(path: Path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        error.checkpoint_details = dict(operation="write_temporary", target=path.name,
                                       attempts=0, elapsed_seconds=0, retryable=False)
        raise
    # Windows replace can report access denied (5) for an ordinary reader which
    # omitted delete sharing. A probe may run after that reader already closed:
    # available DELETE access gets the original short retry window, while a
    # confirmed sharing conflict gets 30s. ACL/read-only failures fail at once.
    # Never delete the old checkpoint or rewrite the complete temporary file.
    started = time.monotonic()
    deadline = started + _REPLACE_RETRY_SECONDS
    short_deadline = None
    sharing_observed = False
    delay = 0.05
    attempts = 0
    while True:
        attempts += 1
        try:
            temporary.replace(path)
            return
        except OSError as error:
            winerror = getattr(error, "winerror", None)
            probes = {}
            retryable = False
            reason = "unsupported_error"
            if error.errno in {errno.EACCES, errno.EBUSY, errno.EAGAIN} and winerror in {5, 32, 33}:
                # Probe both paths: a scanner can hold the temporary source,
                # and a successful probe can mean a brief reader just closed.
                source = _delete_access_state(temporary)
                target = _delete_access_state(path)
                probes = dict(source_delete_access=source, target_delete_access=target)
                reason = "delete_access_unavailable"
                usable = source in {"available", "sharing"} and target in {"available", "sharing", "missing"}
                blocked = bool({source, target} & {"denied", "readonly"}) or source == "missing"
                if not blocked and (winerror in {32, 33} or usable):
                    retryable = True
                    if winerror in {32, 33} or "sharing" in {source, target}:
                        sharing_observed, reason = True, "windows_sharing"
                    else:
                        reason = "delete_access_available"
                        if short_deadline is None:
                            short_deadline = time.monotonic() + _REPLACE_SHORT_RETRY_SECONDS
            retry_window = _REPLACE_RETRY_SECONDS if sharing_observed else _REPLACE_SHORT_RETRY_SECONDS
            active_deadline = deadline if sharing_observed else (short_deadline if short_deadline is not None else started)
            remaining = active_deadline - time.monotonic()
            if not retryable or remaining <= 0:
                error.checkpoint_details = dict(operation="replace", target=path.name,
                    attempts=attempts, elapsed_seconds=round(time.monotonic() - started, 3),
                    retryable=retryable, retry_reason=reason,
                    retry_window_seconds=retry_window if retryable else 0, **probes)
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.5)


def _checkpoint_diagnostic(error):
    # Do not expose exception text (it can contain paths/provider content).
    return dict(error_type=type(error).__name__, errno=error.errno,
                winerror=getattr(error, "winerror", None),
                **getattr(error, "checkpoint_details", {}))


@contextmanager
def file_lock(path: Path, *, timeout: float = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IntakeError("已有入库任务正在执行，请在当前任务结束后继续。") from None
                time.sleep(min(0.01, remaining))
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def batch_path(batch_id, pilot=PILOT):
    if not re.fullmatch(r"b_[a-f0-9]{20}", batch_id):
        raise IntakeError("批次编号无效。")
    path = pilot / "ingestion/batches" / batch_id
    if not (path / "batch.json").is_file():
        raise IntakeError("未找到该批次。")
    return path


def _save(path, batch, message=None):
    with _SAVE_LOCK:
        batch["updated_at"] = now()
        if message:
            batch["events"].append({"time": now(), "message": message})
            batch["events"] = batch["events"][-200:]
        batch["progress"] = {"done": sum(d["status"] in {"prepared", "duplicate", "skipped"} for d in batch["documents"]),
                             "total": len(batch["documents"])}
        try:
            write_atomic(path / "batch.json", batch)
        except OSError as error:
            if not hasattr(error, "checkpoint_details"):
                error.checkpoint_details = dict(operation="write_checkpoint", target="batch.json")
            diagnostic = _checkpoint_diagnostic(error)
            batch["checkpoint_error"] = diagnostic
            # A fresh filename remains writable when the old checkpoint is held
            # without delete sharing. Keep a safe diagnostic even if its replace
            # never becomes possible; do not overwrite another failure record.
            try:
                (path / ("checkpoint_error." + uuid.uuid4().hex + ".json")).write_text(
                    json.dumps(dict(time=now(), **diagnostic), ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass  # Preserve the original checkpoint error.
            raise


def get_batch(batch_id, pilot=PILOT):
    path = batch_path(batch_id, pilot)
    try:
        with file_lock(path / "worker.lock"):
            # Read after acquiring the lock so a just-finished worker cannot
            # leave us reporting its earlier running snapshot as interrupted.
            batch = read_json(path / "batch.json")
            running = False
    except IntakeError:
        batch = read_json(path / "batch.json")
        running = True
    batch["running"] = running
    batch["cancel_requested"] = (path / "cancel.request").exists()
    if batch["status"] == "running" and not running:
        batch["status"] = "needs_attention"
        batch["error"] = "上次执行已中断，可继续处理；已完成的模型结果会复用。"
    # Internal paths are not necessary for the browser and never include credentials.
    for document in batch["documents"]:
        document.pop("source_path", None)
    return batch


def list_batches(pilot=PILOT):
    root = pilot / "ingestion/batches"
    result = []
    for path in sorted(root.glob("b_*/batch.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            result.append(get_batch(path.parent.name, pilot))
        except (ValueError, OSError):
            continue
    return result


def create_batch(files: list[tuple[str, bytes]], mode="codex", max_claims=8, auto_publish=True, pilot=PILOT, *, provenance=None, workers=1):
    if mode not in {"codex", "api", "paratera"}:
        raise IntakeError("请选择 Codex 或模型 API 模式。")
    _validate_workers(workers)
    if not 1 <= len(files) <= LIMITS["max_files"]:
        raise IntakeError("每批需要 1–20 份 PDF。")
    if not isinstance(max_claims, int) or not 1 <= max_claims <= LIMITS["max_claims"]:
        raise IntakeError("每篇抽取上限应在 1–30 条之间。")
    if provenance is not None and (not isinstance(provenance, list) or len(provenance) != len(files)
                                   or not all(isinstance(row, dict) for row in provenance)):
        raise IntakeError("来源元数据应与上传文件逐项对应。")
    if sum(len(data) for _, data in files) > LIMITS["max_total_mb"] * 1024 ** 2:
        raise IntakeError("单批文件总大小不能超过 60 MB。")
    for name, data in files:
        if Path(name.replace("\\", "/")).suffix.lower() != ".pdf" or not data.startswith(b"%PDF-"):
            raise IntakeError("当前批量入口接收 PDF 文件，请先将其他格式另行转换并保留原件。")
        if len(data) > LIMITS["max_file_mb"] * 1024 ** 2:
            raise IntakeError("单个 PDF 不能超过 30 MB。")
    batch_id = "b_" + uuid.uuid4().hex[:20]
    path = pilot / "ingestion/batches" / batch_id
    (path / "sources").mkdir(parents=True)
    catalog = read_json(pilot / "catalog/papers.json") if (pilot / "catalog/papers.json").is_file() else []
    hashes = {p["sha256"]: p["doc_id"] for p in catalog}
    batch = dict(id=batch_id, mode=mode, status="queued", stage="queued", created_at=now(), updated_at=now(),
                 documents=[], events=[], max_claims=max_claims, workers=workers, auto_publish=bool(auto_publish), result=None, error=None)
    for i, (name, data) in enumerate(files):
        sha = hashlib.sha256(data).hexdigest()
        doc_id = "doc_" + sha[:20]
        duplicate = hashes.get(sha)
        source = path / "sources" / f"{i:03d}_{doc_id}.pdf"
        source.write_bytes(data)
        batch["documents"].append(dict(id=f"item_{i:03d}", doc_id=doc_id, filename=Path(name.replace("\\", "/")).name,
            source_path=str(source), sha256=sha, status="duplicate" if duplicate else "queued", stage="received",
            duplicate_of=duplicate, warnings=[], error=None, claim_count=0))
        if provenance is not None:
            allowed = {"title", "publisher", "document_number", "version", "status", "source_url", "pdf_url",
                       "year", "authors", "doi", "accessed_at", "access_route", "document_type", "document_status", "acquisition_status", "relevance_tier", "relevance_reason", "notes"}
            batch["documents"][-1]["provenance"] = {k: v for k, v in provenance[i].items() if k in allowed}
        hashes[sha] = doc_id
    _save(path, batch, f"已接收 {len(files)} 份 PDF，重复文件自动跳过。")
    return get_batch(batch_id, pilot)


def quote_key(value):
    # No number, unit, punctuation, or minus-sign repair is permitted.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _local_ocr(page):
    import rapidocr
    from rapidocr import RapidOCR
    directory = Path(rapidocr.__file__).parent / "models"
    models = {"Det.model_path": directory / "PP-OCRv6_det_small.onnx",
              "Rec.model_path": directory / "PP-OCRv6_rec_small.onnx",
              "Cls.model_path": directory / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"}
    if not all(path.is_file() for path in models.values()):
        raise IntakeError("本地 OCR 模型不齐全，请由 Codex 检查该资料后继续。")
    import numpy as np
    engine = RapidOCR(params={**{key: str(value) for key, value in models.items()}, "Global.log_level": "error"})
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    result = engine(np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n))
    return "\n".join(result.txts or [])


def parse_document(document, ocr=None):
    from kg_pilot.table_evidence import extract_table_evidence
    from kg_pilot.reading_glyphs import repair_reading_pages
    from kg_pilot.text_quality import repair_document_text
    source = Path(document["source_path"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != document["sha256"]:
        raise IntakeError("暂存 PDF 已发生变化，需要重新建立批次。")
    pages, elements, warnings = [], [], []
    try:
        pdf = pymupdf.open(source)
    except Exception:
        raise IntakeError("PDF 无法打开，请检查文件是否完整。") from None
    with pdf:
        if pdf.is_encrypted or pdf.needs_pass:
            raise IntakeError("PDF 需要密码，请提供可读取的副本。")
        if not 1 <= len(pdf) <= LIMITS["max_pages"]:
            raise IntakeError("当前每份 PDF 支持 1–120 页，请按章节拆分较长资料。")
        reviewed_nontext = document.get("verified_nontext_pages", [])
        if (not isinstance(reviewed_nontext, list) or not all(type(p) is int and 1 <= p <= len(pdf) for p in reviewed_nontext)
                or (reviewed_nontext and document.get("nontext_review_source_sha256") != document["sha256"])):
            raise IntakeError("非技术页复核记录必须绑定当前PDF哈希和有效原页编号。")
        for i, page in enumerate(pdf):
            native = page.get_text()
            # Font/glyph authority precedes OCR: a PUA unit can have a perfectly
            # recoverable source glyph even when the native string looks bad.
            decoded = repair_reading_pages(source, [dict(pdf_page=i+1, text=native)], [])[0][0]["text"]
            text, parser = decoded, "pymupdf"
            if i + 1 in reviewed_nontext:
                if len(native.strip()) >= 80:
                    raise IntakeError(f"第 {i+1} 页包含较多文字，不适用少字非技术页复核通道。")
                pages.append(dict(pdf_page=i+1, text=native, native_text=native, width=page.rect.width,
                                  height=page.rect.height, parser=parser, nontechnical_page_reviewed=True))
                warnings.append(f"第 {i+1} 页已由agent查看原页并核对为非技术正文页，保留原文及原页编号。")
                continue
            body = pymupdf.Rect(0, page.rect.height * 0.12, page.rect.width, page.rect.height * 0.88)
            margin_only = (len(native.strip()) < 80 and not page.get_images()
                           and not any(pymupdf.Rect(span["bbox"]).intersects(body)
                                       for block in page.get_text("dict")["blocks"] if "lines" in block
                                       for line in block["lines"] for span in line["spans"] if span["text"].strip())
                           and not any(drawing["rect"].y1 >= body.y0 and drawing["rect"].y0 <= body.y1 for drawing in page.get_drawings()))
            if margin_only and not list(page.annots() or []):
                pages.append(dict(pdf_page=i+1, text=native, native_text=native, width=page.rect.width,
                                  height=page.rect.height, parser=parser))
                warnings.append(f"第 {i+1} 页为空白页或仅有页眉页脚，保留原文和原页编号。")
                continue
            # Empty/scanned pages and obvious replacement glyphs have a generic
            # OCR path. Non-obvious semantic font corruption remains reviewable.
            broken_glyphs = sum(unicodedata.category(ch) in {"Cc", "Co"} and ch not in "\n\r\t" for ch in decoded)
            if (len(decoded.strip()) < 45 or decoded.count("\ufffd") > max(3, len(decoded) // 100)
                    or broken_glyphs > max(3, len(decoded) // 100)):
                text = (ocr or _local_ocr)(page)
                parser = "rapidocr"
                warnings.append(f"第 {i+1} 页使用本地 OCR，引用需结合原页检查。")
            if len(text.strip()) < 20:
                raise IntakeError(f"第 {i+1} 页未得到可核对文字，请检查扫描质量或交由 Codex 处理。")
            try:
                tables = extract_table_evidence(page, doc_id=document["doc_id"], document_sha256=document["sha256"])
            except (RuntimeError, ImportError) as error:
                # Text intake remains usable when optional local table OCR is
                # unavailable, but absence of its rows is explicitly recorded.
                tables = []
                warnings.append(f"第 {i+1} 页表格抽取未完成（{type(error).__name__}），图片表格仍须查看原页。")
            augmented = text + ("\n\n" + "\n\n".join(table["text"] for table in tables) if tables else "")
            pages.append(dict(pdf_page=i+1, text=augmented, native_text=native, width=page.rect.width,
                              height=page.rect.height, parser=parser,
                              table_evidence_ids=[table["evidence_id"] for table in tables]))
            if any(table["parser"] == "rapidocr_table" for table in tables):
                warnings.append(f"第 {i+1} 页图片表格使用本地 OCR，保留原表行标签，数据引用需结合原页复核。")
            elif not tables and re.search(r"(?im)^\s*TABLE\s+(?:[IVXLCDM]+|\d+)\s*[.:]", native):
                warnings.append(f"第 {i+1} 页有表题但未得到完整可靠的单元格绑定，保留原页；未自动补齐表格数值。")
            # Bounded paragraph blocks retain exact source strings and page refs.
            chunks = [part for part in re.split(r"\n\s*\n", text) if part.strip()]
            for j, part in enumerate(chunks):
                for k, offset in enumerate(range(0, len(part), 4000)):
                    value = part[offset:offset + 4000]
                    elements.append(dict(evidence_id=f"{document['doc_id']}:p{i+1}:b{j}_{k}",
                        doc_id=document["doc_id"], pdf_page=i+1, text=value,
                        normalized_text=normalize(value),
                        bbox=[0, 0, page.rect.width, page.rect.height], kind="text",
                        document_sha256=document["sha256"], parser=parser))
            elements.extend(tables)
        if sum(len(page["text"]) for page in pages) > LIMITS["max_chars"]:
            raise IntakeError("本篇文字超过当前批量处理上限，请按章节拆分。")
        if not any(len(page["text"].strip()) >= 20 for page in pages):
            raise IntakeError("该 PDF 没有可处理的文字内容。")
        metadata = dict(title=(pdf.metadata or {}).get("title") or source.stem, doi=None,
                        year=None, authors=[], filename=document["filename"], access_route="other",
                        acquisition_note="用户提供的本地PDF，未提供下载来源；other表示获取途径未指定。")
    pages, elements, _, quality = repair_document_text(source, pages, elements,
                                                       document_sha256=document["sha256"])
    if quality["repaired_characters"]:
        warnings.append("已依据来源 PDF 的实际字形修复编码，原生提取文本与逐页修复记录已保留。")
    if quality["unresolved_critical"]:
        numbers = "、".join(str(page["pdf_page"]) for page in quality["pages"] if page["unresolved_critical"])
        warnings.append(f"第 {numbers} 页仍有未确认乱码；含这些符号的定量记录禁止入库，须查看原页。")
    return dict(pages=pages, elements=elements, metadata=metadata, warnings=warnings,
                parser_version=PARSER_VERSION, text_quality=quality)


def chunks_for(pages):
    chunks, current, size = [], [], 0
    for page in pages:
        # Long single pages are split with page number retained; all pieces still
        # use complete original page text for deterministic quote validation.
        for offset in range(0, len(page["text"]), LIMITS["chunk_chars"]):
            quality = page.get("text_quality", {})
            issues = [dict(issue, offset=issue["offset"]-offset) for issue in quality.get("issues", [])
                      if issue.get("field") == "text" and offset <= issue["offset"] < offset+LIMITS["chunk_chars"]]
            compact_quality = {key: quality[key] for key in ("version", "status", "unresolved_critical") if key in quality}
            compact_quality["issues"] = [{key: issue[key] for key in ("kind", "offset", "critical", "codepoint", "token") if key in issue}
                                         for issue in issues]
            part = dict(pdf_page=page["pdf_page"], text=page["text"][offset:offset + LIMITS["chunk_chars"]],
                        text_quality=compact_quality)
            if current and size + len(part["text"]) > LIMITS["chunk_chars"]:
                chunks.append(current)
                current, size = [], 0
            current.append(part)
            size += len(part["text"])
    if current:
        chunks.append(current)
    return chunks


def taxonomy_packet(pilot):
    vocab_path = pilot / "catalog/topic_vocabulary.json"
    if not vocab_path.exists():
        vocab_path = PILOT / "catalog/topic_vocabulary.json"
    return dict(categories=[dict(id=key, label=label, description=description) for key, label, _, description in CATEGORIES],
                facets={key: list(names) for key, _, _, names in FACET_DEFINITIONS if names},
                topic_groups=list(read_json(vocab_path)))


def validate_evidence(items, pages, doc_id, elements=None):
    if not isinstance(items, list) or not items or len(items) > 20:
        raise IntakeError("分类或知识记录缺少有效原文依据。")
    by_page = {p["pdf_page"]: p for p in pages}
    result = []
    for item in items:
        page, quote = item.get("pdf_page"), item.get("quote")
        if type(page) is not int or page not in by_page or not isinstance(quote, str) or len(quote.strip()) < 8:
            raise IntakeError("原文引句或物理页码格式无效。")
        source = by_page[page]
        if quote_key(quote) not in quote_key(source["text"]):
            raise IntakeError(f"第 {page} 页的引文无法匹配原文，已阻止入库。")
        linked_element = next((e for e in sorted(elements or [], key=lambda e: e.get("kind") != "table")
                               if e["pdf_page"] == page and quote_key(quote) in quote_key(e["text"])), None)
        linked = linked_element["evidence_id"] if linked_element else None
        if elements is not None and linked is None:
            linked = f"{doc_id}:p{page}:full"
            if not any(e["evidence_id"] == linked for e in elements):
                elements.append(dict(evidence_id=linked, doc_id=doc_id, pdf_page=page, text=source["text"],
                    normalized_text=normalize(source["text"]),
                    bbox=[0, 0, source["width"], source["height"]], kind="text",
                    document_sha256=elements[0]["document_sha256"], parser=source.get("parser", "pymupdf")))
        value = dict(pdf_page=page, quote=quote, evidence_id=linked or f"{doc_id}:p{page}")
        if source.get("parser") == "rapidocr":
            value["parser"] = "rapidocr"
        if linked_element and linked_element.get("parser") == "rapidocr_table":
            value["parser"] = "rapidocr_table"
        result.append(value)
    return result


def normalize_proposal(raw, document, parsed, taxonomy, offset, remaining, *, chunk_index=0):
    if not isinstance(raw, dict) or not isinstance(raw.get("metadata"), dict) or not isinstance(raw.get("classification"), dict):
        raise IntakeError("模型没有返回完整的资料信息和分类。")
    c = dict(raw["classification"])
    if c.get("primary_category") not in CATEGORY_LABELS or not isinstance(c.get("related_stages"), list):
        raise IntakeError("主分类或关联阶段不符合分类标准。")
    if not all(stage in CATEGORY_LABELS for stage in c["related_stages"]):
        raise IntakeError("关联阶段不在已确认的分类标准中。")
    c["related_stages"] = list(dict.fromkeys(s for s in c["related_stages"] if s != c["primary_category"]))
    for field, _, multiple, names in FACET_DEFINITIONS:
        value = c.get(field)
        if multiple:
            if not isinstance(value, list) or len(value) > 30 or not all(isinstance(v, str) and 0 < len(v) <= 100 for v in value):
                raise IntakeError(f"分类字段 {field} 格式无效。")
            values = value
            c[field] = list(dict.fromkeys(value))
        else:
            values = [value]
        if names and any(v not in names for v in values):
            raise IntakeError(f"分类字段 {field} 超出标准词表。")
    if any(topic not in taxonomy["topic_groups"] for topic in c["topics"]):
        raise IntakeError("关注问题超出当前词表，请核对后映射到已有类别。")
    if not isinstance(c.get("classification_reason"), str) or len(c["classification_reason"]) < 15:
        raise IntakeError("缺少具体分类理由。")
    c["evidence"] = validate_evidence(c.get("evidence"), parsed["pages"], document["doc_id"], parsed["elements"])
    c["doc_id"] = document["doc_id"]
    c["source_sha256"] = document["sha256"]
    c["classified_date"] = now()[:10]
    c["standard_version"] = "1.0"
    keywords = c.get("keywords", [])
    c["keywords"] = [v[:100] for v in keywords[:40] if isinstance(v, str)] if isinstance(keywords, list) else []
    raw_claims = raw.get("claims")
    if not isinstance(raw_claims, list) or len(raw_claims) > remaining:
        raise IntakeError("知识记录数量超过本次抽取上限或格式无效。")
    claims, quarantined = [], []
    for i, candidate in enumerate(raw_claims):
        claim = dict(candidate, doc_id=document["doc_id"], claim_id=f"{document['doc_id']}_c{offset+i+1:03d}",
                     review_status="pending_review")
        claim["evidence"] = validate_evidence(claim.get("evidence"), parsed["pages"], document["doc_id"], parsed["elements"])
        try:
            Claim.model_validate(claim)
        except ValueError:
            raise IntakeError("知识记录缺少样品、条件、数值或证据等必要字段。") from None
        from kg_pilot.text_quality import guard_claim_text_quality
        try:
            guard_claim_text_quality(claim, pages=parsed["pages"], elements=parsed["elements"])
        except ValueError as error:
            # Preserve the exact supported quotation and proposed values for
            # review, while allowing this paper's other clean claims through.
            claim["claim_id"] = f"{document['doc_id']}_quarantine_k{chunk_index+1:03d}_c{i+1:03d}"
            quarantined.append(dict(claim=claim, reason=str(error),
                status="pending_source_review", source_sha256=document["sha256"],
                chunk_number=chunk_index+1, candidate_index=i+1,
                model_claim_id=candidate.get("claim_id")))
            continue
        # Quarantine IDs occupy a separate namespace; accepted IDs remain
        # contiguous regardless of skipped candidates or subsequent chunks.
        claim["claim_id"] = f"{document['doc_id']}_c{offset+len(claims)+1:03d}"
        claims.append(claim)
    metadata = raw["metadata"]
    title = metadata.get("title")
    if not isinstance(title, str) or not 5 <= len(title.strip()) <= 1000:
        raise IntakeError("未得到可用的资料标题。")
    year = metadata.get("year")
    if year is not None and (type(year) is not int or not 1800 <= year <= datetime.now(timezone.utc).year + 2):
        raise IntakeError("发表年份格式无效。")
    authors = metadata.get("authors", [])
    if not isinstance(authors, list) or not all(isinstance(v, str) for v in authors):
        raise IntakeError("作者信息格式无效。")
    doi = metadata.get("doi")
    if doi:
        doi = str(doi).strip().lower().removeprefix("https://doi.org/").removeprefix("doi:").strip()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi) or doi not in quote_key(" ".join(p["text"] for p in parsed["pages"])).lower():
            raise IntakeError("DOI 未在原文中核实，请修正后继续。")
    return dict(metadata=dict(title=title.strip(), doi=doi or None, year=year, authors=authors),
                classification=c, claims=claims, quarantined_claims=quarantined,
                coverage_note=str(raw.get("coverage_note") or "按条数上限进行重点抽取，非穷尽抽取。")[:2000])


def validate_review(review, proposal):
    if review.get("classification_approved") is not True:
        raise ReviewRejected("独立复核未认可分类，请检查来源或调整后重试。")
    decisions = review.get("claims")
    if not isinstance(decisions, list):
        raise IntakeError("复核结果缺少逐条知识判断。")
    expected = {claim["claim_id"] for claim in proposal["claims"]}
    observed = [entry.get("claim_id") for entry in decisions if isinstance(entry, dict)]
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise IntakeError("复核结果未完整覆盖本次知识记录。")
    if any(entry.get("status") != "accepted" for entry in decisions):
        raise ReviewRejected("部分知识记录未通过复核，当前资料尚未入库，可重试或跳过。")


def _model_task(mode, task, packet, workdir, runner):
    workdir.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(json.dumps([PROMPT_VERSION, mode, task, packet], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cache = workdir / f"{task}.json"
    if cache.exists():
        previous = read_json(cache)
        if previous.get("signature") == signature:
            return previous["output"]
    output = runner(mode, task, packet, workdir)
    write_atomic(cache, {"signature": signature, "output": output})
    return output


def _invalidate(workdir, task):
    path = workdir / f"{task}.json"
    if path.exists():
        path.replace(workdir / f"{task}.rejected.{uuid.uuid4().hex[:8]}.json")


def prepare_document(path, batch, document, pilot, runner, ocr=None):
    docdir = path / "documents" / document["id"]
    docdir.mkdir(parents=True, exist_ok=True)
    parsed_path = docdir / "parsed.json"
    if parsed_path.exists():
        parsed = read_json(parsed_path)
    else:
        parsed = None
    if parsed is None or parsed.get("parser_version") != PARSER_VERSION:
        document["stage"] = "parsing"
        _save(path, batch)
        # MuPDF uses process-wide native state; model work can overlap safely.
        with _PDF_PARSE_LOCK:
            parsed = parse_document(document, ocr)
        write_atomic(parsed_path, parsed)
    write_atomic(docdir / "text_quality.json", parsed["text_quality"])
    document["warnings"] = list(parsed["warnings"])
    parsed["metadata"].setdefault("access_route", "other")
    parsed["metadata"].setdefault("acquisition_note", "用户提供的本地PDF，未提供下载来源；other表示获取途径未指定。")
    if document.get("provenance"):
        parsed["metadata"]["provenance"] = document["provenance"]
        parsed["metadata"]["access_route"] = document["provenance"].get("access_route", "other")
        parsed["metadata"]["acquisition_note"] = "来源元数据为本轮采集记录；正文依据仍须逐页核对。"
    schema = taxonomy_packet(pilot)
    chunks = chunks_for(parsed["pages"])
    accumulated, quarantined, first, processed = [], [], None, 0
    for chunk_index, pages in enumerate(chunks):
        if (path / "cancel.request").exists():
            raise IntakeError("已停止本批次；已完成的步骤可在继续时复用。")
        remaining = batch["max_claims"] - len(accumulated)
        if remaining <= 0:
            break
        workdir = docdir / f"chunk_{chunk_index:03d}"
        document["stage"] = "extracting"
        _save(path, batch)
        # Include title/abstract and conclusion context without silently dropping
        # middle pages: they are handled by subsequent bounded extraction chunks.
        context = {p["pdf_page"]: dict(pdf_page=p["pdf_page"], text=p["text"][:7000])
                   for p in [parsed["pages"][0], parsed["pages"][-1]]}
        extra_pages = document.get("classification_context_pages", [])
        if extra_pages:
            if (document.get("classification_context_source_sha256") != document["sha256"]
                    or not isinstance(extra_pages, list) or len(extra_pages) > 5
                    or any(type(n) is not int or not 1 <= n <= len(parsed["pages"]) for n in extra_pages)):
                raise IntakeError("补充分类上下文必须绑定当前PDF哈希，且最多包含5个有效原页。")
            for p in parsed["pages"]:
                if p["pdf_page"] in extra_pages:
                    context[p["pdf_page"]] = dict(pdf_page=p["pdf_page"], text=p["text"][:7000])
        for part in pages:
            context[part["pdf_page"]] = part
        packet = dict(doc_id=document["doc_id"], metadata=parsed["metadata"],
                      pages=list(context.values()), taxonomy=schema,
                      text_quality=parsed["text_quality"],
                      text_quality_policy="字体修复已绑定原PDF字形。未解决乱码不得猜成单位或数值；跳过含未确认符号的定量记录。",
                      limits=dict(max_claims=max(1, math.ceil(remaining / (len(chunks)-chunk_index))),
                                  chunk_number=chunk_index+1, total_chunks=len(chunks)))
        feedback_path = workdir / "review_feedback.json"
        if feedback_path.exists():
            packet["revision_feedback"] = read_json(feedback_path)
        raw = _model_task(batch["mode"], "extract", packet, workdir, runner)
        if (path / "cancel.request").exists():
            raise IntakeError("已停止本批次；已完成的步骤可在继续时复用。")
        try:
            proposal = normalize_proposal(raw, document, parsed, schema, len(accumulated), remaining,
                                          chunk_index=chunk_index)
        except IntakeError:
            _invalidate(workdir, "extract")
            raise
        quarantined.extend(proposal["quarantined_claims"])
        write_atomic(docdir / "text_quality_quarantine.json", dict(doc_id=document["doc_id"],
            source_sha256=document["sha256"], quarantined_claims=len(quarantined),
            records=quarantined, status="pending_source_review" if quarantined else "clear"))
        document["quarantined_claim_count"] = len(quarantined)
        document["warnings"] = list(parsed["warnings"]) + ([f"已隔离 {len(quarantined)} 条含未确认乱码的定量记录，待原页复核；其余合格记录继续入库。"] if quarantined else [])
        document["stage"] = "reviewing"
        _save(path, batch)
        # The reviewer starts a separate model context and sees the actual pages
        # underlying every proposed citation, including classification evidence.
        referenced = {e["pdf_page"] for item in [proposal["classification"], *proposal["claims"]] for e in item["evidence"]}
        review_context = dict(context)
        review_context.update({p["pdf_page"]: dict(pdf_page=p["pdf_page"], text=p["text"]) for p in parsed["pages"] if p["pdf_page"] in referenced})
        review_pages = list(review_context.values())
        review_packet = dict(packet, pages=review_pages,
            proposal={key: value for key, value in proposal.items() if key != "quarantined_claims"})
        review_packet.pop("revision_feedback", None)
        review = _model_task(batch["mode"], "review", review_packet, workdir, runner)
        try:
            validate_review(review, proposal)
        except ReviewRejected:
            write_atomic(feedback_path, {"review": review, "previous_proposal": proposal,
                "instruction": "依据原文重新抽取，修正明确问题。不要为获得认可补造条件；无法支持的论断应移除。"})
            _invalidate(workdir, "review")
            _invalidate(workdir, "extract")
            raise
        except IntakeError:
            _invalidate(workdir, "review")
            _invalidate(workdir, "extract")
            raise
        first = first or proposal
        accumulated.extend(proposal["claims"])
        processed += 1
        document["claim_count"] = len(accumulated)
        document["stage"] = "validated"
        _save(path, batch, f"{document['filename']}：已复核第 {processed}/{len(chunks)} 个片段组。")
    if not first:
        raise IntakeError("没有可用于入库的解析或分类结果。")
    metadata = first["metadata"]
    catalog_path = pilot / "catalog/papers.json"
    catalog = read_json(catalog_path) if catalog_path.exists() else []
    def title_key(title):
        return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", title).casefold())
    for old in catalog:
        if (metadata.get("doi") and metadata["doi"] == str(old.get("doi") or "").lower()) or title_key(metadata["title"]) == title_key(old["title"]):
            raise IntakeError("发现同 DOI 或同标题的已有资料，但文件不同。请核对版本；系统不会覆盖旧版。")
    c = first["classification"]
    c["classification_review"] = "independent_agent_reviewed" if batch["mode"] == "codex" else "api_reviewed"
    c["ingestion_batch_id"] = batch["id"]
    for claim in accumulated:
        claim["review_status"] = "independent_agent_reviewed" if batch["mode"] == "codex" else "api_model_reviewed"
        claim["verification_label"] = "automated_review_not_human_gold"
        claim["ingestion_batch_id"] = batch["id"]
    paper = dict(metadata, doc_id=document["doc_id"], id=document["doc_id"], sha256=document["sha256"],
                 local_path=document["source_path"], page_count=len(parsed["pages"]),
                 source_type=c["document_form"], is_review=c["content_nature"] == "review",
                 scope=c["relevance"], topic="; ".join(c["topics"]), relevance_reason=c["classification_reason"],
                 publisher_url="https://doi.org/"+metadata["doi"] if metadata.get("doi") else None,
                 pdf_url=None, retrieved_date=now()[:10], ingestion_batch_id=batch["id"],
                 extraction_coverage=dict(processed_chunks=processed, total_chunks=len(chunks),
                     max_claims=batch["max_claims"], quarantined_claims=len(quarantined),
                     note="重点抽取，非全文穷尽；全文均参与检索索引；含未确认乱码的定量记录另行隔离复核。"))
    provenance = document.get("provenance", {})
    if provenance:
        paper["provenance"] = provenance
        for key in ("publisher", "document_number", "version", "status", "source_url", "accessed_at"):
            paper[key] = provenance.get(key)
        paper["status"] = provenance.get("document_status") or (provenance.get("status") if provenance.get("status") != "downloaded_full_text_verified" else "版本信息见原文；现行性未独立确认")
        if str(provenance.get("source_url", "")).startswith(("https://", "http://")):
            paper["publisher_url"] = provenance["source_url"]
        if str(provenance.get("pdf_url", "")).startswith(("https://", "http://")):
            paper["pdf_url"] = provenance["pdf_url"]
    prepared = dict(paper=paper, pages=parsed["pages"], elements=parsed["elements"], classification=c,
                    claims=accumulated, quarantined_claims=quarantined,
                    text_quality=parsed["text_quality"], parser_version=PARSER_VERSION)
    write_atomic(docdir / "prepared.json", prepared)
    return prepared


def _validate_workers(workers):
    if type(workers) is not int or not 1 <= workers <= LIMITS["max_workers"]:
        raise IntakeError(f"并发处理数应为 1–{LIMITS['max_workers']}。")


def run_batch(batch_id, pilot=PILOT, *, runner=None, publisher=None, ocr=None, publish_only=False, clear_cancel=True, workers=None):
    from kg_pilot.ingestion_runners import run_task, RunnerError
    from kg_pilot.ingestion_publish import publish_batch
    path = batch_path(batch_id, pilot)
    # Status readers briefly take worker.lock to obtain a coherent snapshot.
    # They must not make a new worker fail after it acquired the global worker
    # lock. Real competing workers still fail immediately at the global lock.
    with file_lock(pilot / "ingestion/global_worker.lock"), file_lock(path / "worker.lock", timeout=1):
        batch = read_json(path / "batch.json")
        workers = batch.get("workers", 1) if workers is None else workers
        _validate_workers(workers)
        if batch["status"] == "completed":
            return batch
        if clear_cancel and (path / "cancel.request").exists():
            (path / "cancel.request").unlink()
        batch.update(status="running", stage="preparing", error=None)
        _save(path, batch, "开始或继续批量处理。")
        prepared = []
        checkpoint_failed = threading.Event()
        try:
            def prepare_one(document):
                if document["status"] in {"duplicate", "skipped"}:
                    return None
                existing = path / "documents" / document["id"] / "prepared.json"
                if document["status"] == "prepared" and existing.exists():
                    previous = read_json(existing)
                    if previous.get("parser_version") == PARSER_VERSION or publish_only:
                        return previous
                if publish_only:
                    return None
                if (path / "cancel.request").exists() or checkpoint_failed.is_set():
                    return None
                try:
                    # No other worker may persist a speculative processing state
                    # between the update and its failed initial checkpoint.
                    with _SAVE_LOCK:
                        if checkpoint_failed.is_set():
                            return None
                        document.update(status="processing", error=None)
                        try:
                            _save(path, batch)
                        except OSError:
                            document.update(status="needs_attention", stage="checkpoint_failed")
                            checkpoint_failed.set()
                            raise
                    try:
                        result = prepare_document(path, batch, document, pilot, runner or run_task, ocr)
                    except ReviewRejected:
                        if (path / "cancel.request").exists():
                            raise
                        _save(path, batch, f"{document['filename']}：根据复核意见重新抽取一次。")
                        result = prepare_document(path, batch, document, pilot, runner or run_task, ocr)
                    with _SAVE_LOCK:
                        document.update(status="prepared", stage="ready", title=result["paper"]["title"],
                                        claim_count=len(result["claims"]))
                        _save(path, batch)
                    return result
                except (IntakeError, RunnerError) as error:
                    document.update(status="needs_attention", error=str(error))
                except OSError as error:
                    with _SAVE_LOCK:
                        document.update(status="needs_attention", error_details=_checkpoint_diagnostic(error))
                        if hasattr(error, "checkpoint_details"):
                            checkpoint_failed.set()
                            document.update(stage="checkpoint_failed",
                                error="检查点保存失败，已保留上一次完整记录；请检查文件占用或写权限后重试。")
                            return None
                        document["error"] = "资料文件读写失败，请检查文件或访问权限后重试。"
                except Exception:
                    document.update(status="needs_attention", error="本资料处理未完成，可重试或交由 Codex 检查。")
                _save(path, batch)
                return None
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(prepare_one, batch["documents"]))
            # Resolve cross-document conflicts deterministically in upload order.
            for document, result in zip(batch["documents"], results):
                if result is None:
                    continue
                title = quote_key(result["paper"]["title"]).casefold()
                doi = result["paper"].get("doi")
                if any((doi and doi == prior["paper"].get("doi")) or
                       title == quote_key(prior["paper"]["title"]).casefold() for prior in prepared):
                    document.update(status="needs_attention", error="同一批次已存在相同标题或 DOI 的另一文件，请核对版本并跳过重复项。")
                else:
                    prepared.append(result)
            write_atomic(path / "prepared.json", dict(batch_id=batch_id, documents=prepared))
            if (path / "cancel.request").exists():
                batch.update(status="cancelled", stage="cancelled")
            elif checkpoint_failed.is_set():
                batch.update(status="needs_attention", stage="checkpoint_failed",
                    error="检查点保存失败，已停止启动后续资料；请检查文件占用或写权限后重试。")
            elif any(d["status"] not in {"prepared", "duplicate", "skipped"} for d in batch["documents"]):
                batch.update(status="needs_attention", stage="review_required", error="部分资料待处理；可重试，或跳过有问题的资料后继续。")
            elif not prepared:
                batch.update(status="completed", stage="completed", result={"added_documents": 0, "added_claims": 0, "added_evidence": 0})
            elif batch["auto_publish"] or publish_only:
                batch["stage"] = "publishing"
                _save(path, batch, "分类与知识复核完成，正在构建并发布检索索引。")
                result = (publisher or publish_batch)(path, pilot=pilot)
                batch.update(status="completed", stage="completed", result=result)
            else:
                batch.update(status="ready", stage="ready")
        except Exception as error:
            publishing = batch.get("stage") == "publishing"
            batch.update(status="failed",
                error="发布未完成，已保留准备结果。请查看恢复状态后继续。" if publishing else "批次准备未完成，已保留检查点，可继续处理。",
                stage="publication_failed" if publishing else "preparation_failed",
                error_type=type(error).__name__)
            if isinstance(error, OSError):
                batch["error_details"] = _checkpoint_diagnostic(error)
        _save(path, batch, "本批处理结束：" + batch["status"])
    return get_batch(batch_id, pilot)


def start_batch(batch_id, pilot=PILOT, publish_only=False):
    current = get_batch(batch_id, pilot)
    if current["running"]:
        return current
    if current["status"] == "completed":
        return current
    with _THREAD_LOCK:
        if any(t.is_alive() for t in _THREADS.values()):
            raise IntakeError("已有批次正在执行，请稍后继续。")
        cancellation = batch_path(batch_id, pilot) / "cancel.request"
        if cancellation.exists():
            cancellation.unlink()
        def execute():
            try:
                run_batch(batch_id, pilot, publish_only=publish_only, clear_cancel=False)
            except IntakeError:
                pass  # Another CLI/server process owns the durable worker lock.
        thread = threading.Thread(target=execute, name="ingestion-"+batch_id, daemon=True)
        _THREADS[batch_id] = thread
        thread.start()
    current["running"] = True
    current["status"] = "running"
    return current


def cancel_batch(batch_id, pilot=PILOT):
    path = batch_path(batch_id, pilot)
    (path / "cancel.request").touch()
    return get_batch(batch_id, pilot)


def skip_document(batch_id, item_id, pilot=PILOT):
    path = batch_path(batch_id, pilot)
    with file_lock(path / "worker.lock"):
        batch = read_json(path / "batch.json")
        if batch["status"] == "completed":
            raise IntakeError("已入库的批次不能通过跳过操作删除资料。")
        document = next((d for d in batch["documents"] if d["id"] == item_id), None)
        if document is None:
            raise IntakeError("未找到该批次资料。")
        document.update(status="skipped", stage="skipped")
        _save(path, batch, f"已跳过 {document['filename']}，原文件仍保留在批次中。")
    return get_batch(batch_id, pilot)


def main():
    ap = argparse.ArgumentParser(description="Codex / 模型 API 双模式批量 PDF 入库")
    ap.add_argument("--pilot", type=Path, default=PILOT, help="默认正式库；验证时可使用隔离目录")
    commands = ap.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("inputs", type=Path, nargs="+")
    create.add_argument("--mode", choices=["codex", "api", "paratera"], default="codex")
    create.add_argument("--max-claims", type=int, default=8)
    create.add_argument("--no-auto-publish", action="store_true")
    create.add_argument("--run", action="store_true")
    for name in ("run", "status", "cancel", "publish"):
        commands.add_parser(name).add_argument("batch_id")
    skip = commands.add_parser("skip")
    skip.add_argument("batch_id")
    skip.add_argument("item_id")
    commands.add_parser("list")
    commands.add_parser("recover")
    args = ap.parse_args()
    try:
        if args.command == "create":
            paths = [p for supplied in args.inputs for p in (sorted(supplied.glob("*.pdf")) if supplied.is_dir() else [supplied])]
            result = create_batch([(p.name, p.read_bytes()) for p in paths], args.mode, args.max_claims, not args.no_auto_publish, args.pilot)
            if args.run:
                result = run_batch(result["id"], args.pilot)
        elif args.command in {"run", "publish"}:
            result = run_batch(args.batch_id, args.pilot, publish_only=args.command == "publish")
        elif args.command == "status":
            result = get_batch(args.batch_id, args.pilot)
        elif args.command == "cancel":
            result = cancel_batch(args.batch_id, args.pilot)
        elif args.command == "skip":
            result = skip_document(args.batch_id, args.item_id, args.pilot)
        elif args.command == "list":
            result = list_batches(args.pilot)
        else:
            from kg_pilot.ingestion_publish import recover_publication
            result = recover_publication(args.pilot)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (IntakeError, OSError) as error:
        print(json.dumps({"error": str(error) if isinstance(error, IntakeError) else "本地文件无法读取或写入。"}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
