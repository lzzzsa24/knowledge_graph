"""Read the published native index and ground subscription answers on physical PDF pages."""
from __future__ import annotations

import base64
import hashlib
import json
import random
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from pathlib import Path

import psutil
import pymupdf

from kg_pilot import native_library
from kg_pilot import decoration_filter
from kg_pilot.codex_subscription import SOL_MODEL
from kg_pilot import qa
from kg_pilot.effective_evidence import FIELD, load_effective_evidence
from kg_pilot.framework_qa import bind_quotes
from kg_pilot.native_pages import page_plan
from kg_pilot.native_readiness import preflight_receipt
from kg_pilot.pipeline import read_json
from kg_pilot.qa import QAError, SYSTEM_PROMPT, _pack_answer_evidence, _requested_papers, normalize_history, validate_answer
from kg_pilot.retrieval import retrieval_model


_control_lock = threading.Lock()
_commands = {}


def queue_command(pilot, action):
    """Delegate an explicit local click to the same resumable scheduler as the .cmd files."""
    flags = {"resume": ["--resume", "--wait-for-reset"], "pause": ["--pause"]}
    root = native_library.directory(pilot)
    with _control_lock:
        previous = _commands.get(action)
        if previous and previous.poll() is None:
            return
        other = _commands.get("pause")
        if action == "resume" and other and other.poll() is None:
            raise ValueError("队列正在保存检查点，请等暂停完成后再继续。")
        runner = Path(__file__).resolve().parents[1] / "scripts/run_native_library.py"
        with (root / "web-queue.log").open("ab") as log:
            _commands[action] = subprocess.Popen([sys.executable, "-X", "utf8", "-u", str(runner), *flags[action]],
                cwd=runner.parents[1], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@lru_cache(maxsize=96)
def _parsed_counts(path, sha, modified):
    parsed = read_json(path)
    if parsed.get("identity", {}).get("sha256") != sha:
        return None
    return len(parsed["pages"]), sum(i["type"] in {"image", "table", "equation"} for i in parsed["items"])


def _gallery_path(root, doc_id, sha):
    mixed = root / "documents" / doc_id / "mixed-parse.json"
    review = mixed.with_name("crop-review.json")
    if review.is_file() and (not mixed.is_file() or review.stat().st_mtime_ns > mixed.stat().st_mtime_ns):
        if _review_status(review,review.stat().st_mtime_ns).get("revision")==sha:
            return review
    if mixed.is_file():
        return mixed
    return root / "legacy_gallery" / doc_id / sha / "regions.json"


@lru_cache(maxsize=96)
def _review_status(path, modified):
    from kg_pilot.native_crop_review import needs_followup

    data=read_json(path)
    return {**{k:data.get(k) for k in ("status","selected_pages","held","revision")},
            "awaiting_followup":needs_followup(data, data.get("revision")),
            "visual_items":sum(not i.get("filter_reason") for i in data.get("items",[]))}


def _document_regions(pilot, doc_id):
    root = native_library.directory(pilot)
    row = next((r for r in read_json(root / "queue.json")["documents"] if r["id"] == doc_id), None)
    if row is None:
        raise FileNotFoundError
    path = _gallery_path(root, doc_id, row["sha256"])
    return row, path, _indexed_regions(path, row["sha256"], path.stat().st_mtime_ns)


def region_list(pilot, doc_id):
    row, path, regions = _document_regions(pilot, doc_id)
    result = {"doc_id": doc_id, "title": row["title"], "pages": row["pages"], "revision": row["sha256"],
              "items": [], "skipped":[]}
    report=path.with_name("crop-review.json")
    if report.is_file():
        audit=_review_status(report,report.stat().st_mtime_ns)
        if audit["revision"]==row["sha256"]:
            result["crop_review"]=audit
    reviewed = read_json(Path(decoration_filter.__file__).with_name("publication_decorations.json"))
    templates = decoration_filter.visual_templates(reviewed)
    for item in regions:
        public = {k:v for k,v in item.items() if k not in {"path","footnote"}}
        if item.get("footnote"):
            note=item["footnote"]
            public["footnote"]=" ".join(note) if isinstance(note,list) else note
            label="表注" if item["kind"]=="table" else "附注"
            public["caption"]=(public["caption"]+"　"+label+"："+public["footnote"]).strip()
        if not item.get("reason"):
            crop = item["path"].resolve()
            if crop.parent == (path.parent / "original-regions").resolve():
                public["current_decoration"] = decoration_filter.decoration_reason(
                    {"type":item["kind"], "page_idx":item["page"]-1, "image_sha256":item["sha256"], "img_path":crop,
                     "image_caption":item["caption"], "image_footnote":item["footnote"]}, reviewed, templates,
                    pdf_sha=row["sha256"])
        result["skipped" if item.get("reason") else "items"].append(public)
    return result


@lru_cache(maxsize=16)
def _indexed_regions(path, sha, modified):
    data = read_json(path)
    revision=data["revision"] if path.name=="crop-review.json" else data["identity"]["sha256"]
    if revision != sha:
        raise ValueError("PDF revision mismatch")
    regions = []
    for item in [*data["items"], *data.get("skipped_decorations", [])]:
        kind = item["type"]
        if kind not in {"image", "table", "equation"}:
            continue
        captions = item.get(kind + "_caption") or []
        regions.append({"kind":kind, "page":item["page_idx"] + 1, "sha256":item["image_sha256"],
                        "caption":" ".join(captions) if isinstance(captions, list) else str(captions),
                        "path":Path(item["img_path"]), "reason":item.get("reason") or item.get("filter_reason"),
                        "footnote":item.get(kind + "_footnote"),
                        "preview_source":item.get("preview_source")})
    return regions


def region_bytes(pilot, doc_id, digest):
    _, manifest_path, regions = _document_regions(pilot, doc_id)
    item = next((r for r in regions if r["sha256"] == digest), None)
    if item is None:
        raise FileNotFoundError
    image = item["path"].resolve(strict=True)
    if image.suffix.lower() != ".png" or image.parent != (manifest_path.parent / "original-regions").resolve():
        raise ValueError("Invalid crop path")
    content = image.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Crop hash mismatch")
    return content


def _paper_elapsed(job, sha, lane, published, observed):
    durations = []
    for phase in ("crops", "prepare", "publish"):
        path = job / f"lane-{phase}.json"
        record = read_json(path) if path.is_file() else {}
        if record.get("sha256") != sha:
            record = {}
        active = (lane.get("phase")==phase and lane.get("status")=="running"
                  and lane.get("sha256",sha)==sha)
        if active and (not record or (lane.get("pid") and lane["pid"]!=record.get("pid"))):
            record = lane
        seconds = native_library.recorded_seconds(record, until=observed if active else None)
        if phase=="publish" and published and not record.get("finished") and published.get("seconds") is not None:
            seconds = (record.get("prior_seconds") or 0) + published["seconds"]
        if seconds is not None:
            durations.append(seconds)
    return int(sum(durations)) if durations else None


def _paper_progress(root, row, lane, published=None, *, observed=None):
    doc, sha = row["id"], row["sha256"]
    result = {"doc_id": doc, "title": row["title"], "pages": row["pages"], "status": lane.get("status", "queued"),
              "stage": "queued", "milestones": 0, "parsed_pages": None, "visual_items": None, "responses": 0}
    job = root / "documents" / doc
    result["elapsed_seconds"] = _paper_elapsed(job,sha,lane,published,observed or native_library.now())
    if published:
        return {**result, "stage":"published", "milestones":3, "parsed_pages":published["pages"], "chunks":published["chunks"]}
    if not job.is_dir():
        return result
    parsed_path = job / "mixed-parse.json"
    try:
        counts = _parsed_counts(parsed_path, sha, parsed_path.stat().st_mtime_ns)
    except FileNotFoundError:
        counts = None
    if counts:
        result.update(parsed_pages=counts[0], visual_items=counts[1], milestones=1)
    prepare = job / "lane-prepare.json"
    ready = read_json(prepare) if prepare.is_file() else {}
    preflight = preflight_receipt(job, sha)
    prepared = (ready.get("sha256") == sha and ready.get("status") == "prepared"
                and ready.get("mode") != "workspace_preflight")
    current = job / "index-stage.json"
    marker = read_json(current) if current.is_file() else {}
    if marker.get("sha256") != sha or marker.get("at", "") < lane.get("started", ""):
        marker = {}
    if prepared or marker.get("stage") == "verified":
        result["milestones"] = 2
    if counts or marker or ready.get("sha256") == sha or lane.get("sha256") == sha:
        result["responses"] = sum(1 for _ in (job / "responses").glob("*.json"))
    if lane.get("status") == "running":
        result["stage"] = marker.get("stage") or ("indexing" if counts else "parsing")
        if lane.get("phase") == "publish" and prepared and lane.get("mode") != "workspace":
            result["stage"] = "merging"
        if marker.get("stage") == "verified" and lane.get("phase") == "publish":
            result["stage"] = "batch_verified" if lane.get("mode") == "workspace" else "publishing"
    elif lane.get("status") == "interrupted":
        result["stage"] = "interrupted"
    elif preflight.get("status") == "needs_review":
        result["stage"] = "preflight_review_held"
        result["review_issue"] = preflight.get("error", "").removeprefix("预处理待复核：")
    elif preflight.get("status") == "ready":
        result["stage"] = "batch_ready"
    elif prepared:
        result["stage"] = "prepared"
    elif result["milestones"] or result["responses"]:
        result["stage"] = "saved"
    review=job/"crop-review.json"
    if review.is_file():
        audit=_review_status(review,review.stat().st_mtime_ns)
        if audit["revision"]==sha:
            result["has_regions"]=True
            if not counts:
                result.update(visual_items=audit["visual_items"], parsed_pages=row["pages"])
                if result["stage"]=="queued":
                    result["stage"]="saved"
            if audit["status"]=="needs_review":
                if lane.get("status") != "running":
                    result["stage"]="crop_review_held"
                if audit.get("awaiting_followup"):
                    result["review_issue"]="等候自动复核" if lane.get("status") != "running" else "正在复核已标记问题"
                elif audit.get("held"):
                    result["review_issue"] = f"复核后仍有 {len(audit['held'])} 项疑点"
    if lane.get("phase")=="crops" and lane.get("status")=="running":
        result["stage"]="crop_review"
    return result


def status(pilot, *, progress=False):
    root = native_library.directory(pilot)
    state = native_library.state(pilot)
    manifest = read_json(root / "manifest.json")
    scheduler = read_json(root / "scheduler.json") if (root / "scheduler.json").is_file() else {}
    phase = scheduler.get("status", "idle")
    identity_path = root / "scheduler_process.json"
    identity = read_json(identity_path) if identity_path.is_file() else {}
    if phase in {"running", "waiting_for_subscription_reset"}:
        try:
            if not identity:
                raise FileNotFoundError
            if abs(psutil.Process(identity["pid"]).create_time() - identity["created"]) > 0.1:
                phase = "interrupted"
        except (FileNotFoundError, psutil.NoSuchProcess):
            phase = "interrupted"
        except psutil.AccessDenied:
            pass
    if state["pause_requested"]:
        phase = "pausing" if phase == "running" else "paused"
    selection=state.get("selection")
    if selection:
        phase="pausing" if selection["pausing"] else "running"
    with _control_lock:
        if _commands.get("pause") and _commands["pause"].poll() is None:
            phase = "pausing"
        elif phase in {"paused", "idle", "pass_complete", "interrupted"} and _commands.get("resume") and _commands["resume"].poll() is None:
            phase = "starting"
    docs = manifest["documents"]
    active = [lane for lane in state.get("queue_lanes", []) if lane.get("doc_id") not in docs
              and not (lane.get("prefetch") and lane["status"] in {"prepared", "interrupted"})
              ] if phase in {"running", "pausing"} else []
    result = {
        "published": len(docs), "catalog": state["catalog"], "pending": state["pending"],
        "held": state["held"], "phase": phase, "generation": manifest["generation"],
        "updated": manifest.get("updated"), "model": SOL_MODEL, "effort": state["effort"],
        "cli_available": bool(shutil.which("codex")), "concurrency": state["queue_document_concurrency"],
        "max_concurrency": native_library.MAX_DOCUMENT_CONCURRENCY,
        "active_limit": scheduler.get("document_concurrency") if phase in {"running", "pausing"} else None,
        "batch_stage": scheduler.get("stage") if active else None,
        "shared_workspace": identity.get("scheduling") == "shared_workspace" or any(lane.get("mode") == "workspace" for lane in active),
        "legacy_runner": phase in {"running","waiting_for_subscription_reset","pausing"} and not identity.get("reads_group_settings"),
        "active": [{"doc_id": lane["doc_id"], "status": lane["status"]} for lane in active if lane.get("doc_id")],
        "documents": [{"doc_id": doc, "title": entry["title"], "pages": entry["pages"],
                       "pdf_sha256": entry["sha256"], "chunks": entry["chunks"]} for doc, entry in docs.items()],
    }
    if selection:
        result.update(scope="selected_documents",global_queue_paused=state["pause_requested"],
                      concurrency=selection["concurrency"],active_limit=selection["concurrency"],
                      max_concurrency=None,legacy_runner=False)
    if progress:
        observed = native_library.now()
        started = selection.get("started") if selection else identity.get("at")
        finish = None
        if phase in {"running","pausing","waiting_for_subscription_reset"}:
            finish = observed
        elif scheduler.get("status") in {"pass_complete","paused","needs_attention"}:
            finish = scheduler.get("at")
        if not selection and phase not in {"running","pausing","waiting_for_subscription_reset"}:
            for name in ("selected-progress.json","crop-progress.json"):
                path = root / name
                last = read_json(path) if path.is_file() else {}
                if last.get("finished") and last.get("started","") > (started or ""):
                    started, finish = last["started"], last["finished"]
        result["run_elapsed_seconds"] = native_library.recorded_seconds({"started":started,"finished":finish})
        for doc in result["documents"]:
            doc["has_regions"] = _gallery_path(root, doc["doc_id"], doc["pdf_sha256"]).is_file()
            doc["elapsed_seconds"] = _paper_elapsed(root/"documents"/doc["doc_id"],doc["pdf_sha256"],{},
                                                   docs[doc["doc_id"]],observed)
        plan = read_json(root / "queue.json")["documents"]
        by_id = {r["id"]: r for r in plan}
        active_ids = {lane["doc_id"] for lane in active}
        result["active"] = []
        for lane in active:
            if lane["doc_id"] not in by_id:
                continue
            item = _paper_progress(root, by_id[lane["doc_id"]], lane, docs.get(lane["doc_id"]), observed=observed)
            item["preflight"] = bool(lane.get("prefetch") or lane.get("phase")=="crops"
                                     or scheduler.get("stage")=="preflight"
                                     or (selection and lane.get("phase")=="prepare"))
            result["active"].append(item)
        permitted = root / "marked_revisions.json"
        approvals = read_json(permitted) if permitted.is_file() else {}
        result["waiting"] = []
        for row in plan:
            if row["id"] in docs or row["id"] in active_ids:
                continue
            item = _paper_progress(root, row, {},observed=observed)
            if not native_library.eligible(row, approvals):
                item["stage"] = "held"
            result["waiting"].append(item)
    return result


def document_graph(pilot, doc):
    manifest_path = native_library.directory(pilot) / "manifest.json"
    for _ in range(2):
        manifest = read_json(manifest_path)
        item = manifest["documents"].get(doc)
        if not item:
            return None
        slot = {"library": "storage", "gao_adeia": "retained_storage"}[item["index"]]
        storage = pilot / manifest[slot]
        entities = read_json(storage / "kv_store_full_entities.json")[doc]["entity_names"]
        relations = read_json(storage / "kv_store_full_relations.json")[doc]["relation_pairs"]
        if read_json(manifest_path)["generation"] == manifest["generation"]:
            return {"entities": entities, "relations": relations}
    raise ValueError("Index changed")


_SEARCH_LOCK = threading.Lock()
_ANSWER_LOCK = threading.Lock()


def search(pilot, query, limit, *, doc_ids=None):
    with _SEARCH_LOCK:
        return native_library.search(pilot, query, limit, encoder=retrieval_model(), doc_ids=doc_ids)


def source_pages(pilot, hits, *, with_images=True):
    pages, images, seen, pdf_bytes = [], {}, set(), {}
    reading_copies = {}
    view = load_effective_evidence(pilot)
    for hit in hits:
        doc = hit["doc_id"]
        if doc not in pdf_bytes:
            path = Path(hit["pdf"]).resolve()
            if not path.is_relative_to((pilot / "collection").resolve()) or not path.is_file():
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != hit["pdf_sha256"]:
                raise QAError("原 PDF 版本已变更，请更新索引再提问。")
            pdf_bytes[doc] = raw
        with pymupdf.open(stream=pdf_bytes[doc], filetype="pdf") as pdf:
            for n in range(hit["page"], min(hit["page_end"], len(pdf)) + 1):
                if (doc, n) in seen:
                    continue
                seen.add((doc, n))
                page = pdf[n - 1]
                body = page.get_text(sort=True).strip()
                if not body:
                    if doc not in reading_copies:
                        reading_copies[doc] = page_plan(pilot / "parsed" / doc, {"sha256": hit["pdf_sha256"], "pages": len(pdf)})
                    body = reading_copies[doc][n - 1]
                    if not body:
                        continue
                source = view.source({"doc_id": doc, "title": hit["title"], "pdf_page": n,
                                      "pdf_sha256": hit["pdf_sha256"], "text": body,
                                      "evidence_id": f"{doc}:page:{n}", "framework_page": True})
                if any(q.get("status") == "stale" for q in source.get(FIELD, [])):
                    continue
                pages.append(source)
                if with_images:
                    scale = 1400 / max(page.rect.width, page.rect.height)
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                    images[doc, n] = base64.b64encode(pix.tobytes("jpeg", jpg_quality=85)).decode("ascii")
                if len(pages) == 6:
                    return pages, images
    return pages, images


def idea_sources(pilot, focus, count):
    scope = None
    if not focus:
        docs = list(read_json(native_library.directory(pilot) / "manifest.json")["documents"].items())
        if not docs:
            return [], ""
        selected = random.sample(docs, min(count, len(docs)))
        scope = {doc for doc, _ in selected}
        focus = "；".join(row["title"] for _, row in selected)
    hits = search(pilot, focus, 10, doc_ids=scope)["hits"]
    pages, _ = source_pages(pilot, hits, with_images=False)
    return pages, focus


def answer(pilot, question, history, *, channel="subscription"):
    if not _ANSWER_LOCK.acquire(blocking=False):
        raise QAError("已有一条原生索引问答正在生成，请稍后重试。")
    try:
        status, connection, key = qa.generation_settings(channel)
        history = normalize_history(history)
        allowed = _requested_papers(question)
        if allowed is not None:
            history = []
        previous = [m["content"] for m in history if m["role"] == "user"][-1:]
        result = search(pilot, "\n".join([*previous, question]), 10, doc_ids=allowed)
        pages, images = source_pages(pilot, result["hits"], with_images=channel == "subscription")
        sources, contexts, attachments, count = _pack_answer_evidence([], pages, images)
        response = {"provider": status["provider"], "model": status["model"], "warnings": [], "sources": [],
                    "retrieval": {"mode": "native", "pages": count, "generation": result["generation"]}}
        if not sources:
            return {**response, "answerability": "insufficient", "answer": "已发布索引中未找到可以核对原文短引的 PDF 页。"}
        instruction = SYSTEM_PROMPT + ("\n正文用 [S编号] 引用，页码由界面显示。JSON 另含 quotes 对象："
            "每个被引用的编号对应从同一编号的 text 中逐字复制的 1 至 4 个连续短引，每段 4 至 350 字符。"
            "有附图时结合原页图核对数据、单位和图表。")
        payload = json.dumps({"question": question, "evidence": contexts}, ensure_ascii=False)
        messages = [{"role": "system", "content": instruction}, *history,
                    {"role": "user", "content": [{"type": "text", "text": payload}, *attachments] if attachments else payload}]
        raw = qa.generate_completion(connection, key, {"model": status["model"], "messages": messages, "stream": False})
        choice = raw["choices"][0]
        if choice.get("finish_reason") == "length":
            raise QAError("模型回答已被截断，请缩小问题范围后重试。")
        content = choice["message"]["content"]
        validated = validate_answer(content, sources)
        bind_quotes(content, validated, sources)
        cited = [{k: source[k] for k in ("id", "title", "doc_id", "pdf_page", "pdf_sha256", "verified_quote")}
                 for source in sources if "verified_quote" in source]
        return {**response, **validated, "sources": cited}
    finally:
        _ANSWER_LOCK.release()
