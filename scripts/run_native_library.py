"""Drive the project's native-library ingestion tools over official MCP stdio."""
import argparse
import asyncio
from functools import lru_cache
import json
import os
from pathlib import Path
import sys
import time
import uuid
import psutil

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"src")]
from kg_pilot.native_library import directory, eligible, now, read_json, save, check_pause, pause_at_group_boundary, PauseRequested
from kg_pilot.native_library import DEFAULT_DOCUMENT_CONCURRENCY, PREPARE_WORKER_EXITED, document_concurrency, file_hash, live_selection, validate_concurrency, verify_source
from kg_pilot.ingestion import file_lock, IntakeError
from kg_pilot.codex_subscription import subscription_windows

QUOTA_WORDS=("quota","usage limit","usage_limit","credit balance","rate_limit","rate limit")


def current_windows(root):
    try:
        windows=subscription_windows(root)
    except (OSError,ValueError):
        windows={}
    save(root/"quota.json",{"observed":now(),"windows":windows})
    return windows


def exhausted_until(windows):
    limits=windows.get("codex",{})
    if not limits and len(windows)==1:
        limits=next(iter(windows.values()))
    return max((value["resetsAt"] for value in limits.values()
                if value.get("usedPercent",0)>=100 and value.get("resetsAt",0)>time.time()),default=0)


async def wait_for_window(root,until,doc):
    save(root/"scheduler.json",{"status":"waiting_for_subscription_reset","next_doc":doc,"reset_at_unix":until,"at":now()})
    print(now(),"WAIT FOR SUBSCRIPTION RESET",until,"NEXT",doc,flush=True)
    while time.time()<until+30:
        check_pause(root)
        await asyncio.sleep(min(60,max(0,until+30-time.time())))


def pending_rows(root, rows):
    published=read_json(root/"manifest.json")["documents"]
    for row in rows:
        record=published.get(row["id"])
        if record is not None and record["sha256"]!=row["sha256"]:
            raise ValueError("Published PDF revision differs: "+row["id"])
    return [r for r in rows if r["id"] not in published]


def groups_for_run(rows, trial=(), width=lambda: DEFAULT_DOCUMENT_CONCURRENCY):
    """Honour an armed pair only within this invocation's already filtered scope."""
    by_id={r["id"]:r for r in rows}
    selected=[by_id[item["id"]] for item in trial
              if item["id"] in by_id and by_id[item["id"]]["sha256"]==item["sha256"]]
    if len(selected)!=2:
        selected=[]
    buffer=[]
    for row in rows:
        if row in selected:
            if row is selected[0]:
                if buffer:
                    yield buffer
                    buffer=[]
                if width() == 1:
                    for member in selected:
                        yield [member]
                else:
                    yield selected
            continue
        if not buffer:
            limit=width()
        buffer.append(row)
        if len(buffer)==limit:
            yield buffer
            buffer=[]
    if buffer:
        yield buffer


async def ingest_group(root, rows, call, wait_for_reset):
    while pending := pending_rows(root,rows):
        check_pause(root)
        ids=[r["id"] for r in pending]
        until=exhausted_until(await asyncio.to_thread(current_windows,root))
        if until:
            if not wait_for_reset:
                return 2,[]
            await wait_for_window(root,until,ids[0])
            continue
        save(root/"scheduler.json",{"status":"running","doc_id":ids[0],"doc_ids":ids,
                                    "document_concurrency":len(ids),"at":now()})
        print(now(),"START",", ".join(ids),flush=True)
        if len(ids)>2:
            response=await call("native_library_ingest_group",{"doc_ids":ids})
        elif len(ids)==2:
            response=await call("native_library_ingest_pair",{"doc_ids":ids})
        else:
            response=await call("native_library_ingest",{"doc_id":ids[0],"single_only":True})
        check_pause(root)
        missing=pending_rows(root,pending)
        if not missing:
            print(now(),"PUBLISHED",", ".join(ids),flush=True)
            return 0,[]
        data=response.get("data") or {}
        errors=data.get("errors",{})
        current=read_json(root/"current.json") if (root/"current.json").exists() else {}
        failures=[]
        for row in missing:
            lane=chr(ord("A")+ids.index(row["id"]))
            lane_keys=(lane,lane+"_prepare")
            message=next((errors[key] for key in lane_keys if errors.get(key)),None)
            if not message and current.get("doc_id")==row["id"]:
                message=current.get("error")
            message=message or response.get("error",{}).get("code") or "No published checkpoint"
            failures.append({"doc_id":row["id"],"message":message,"at":now()})
        if wait_for_reset and any(any(w in f["message"].lower() for w in QUOTA_WORDS) for f in failures):
            until=exhausted_until(await asyncio.to_thread(current_windows,root))
            if until:
                await wait_for_window(root,until,missing[0]["id"])
                continue
        return 1,failures
    return 0,[]


def _prepared(root, row):
    checkpoint=root/"documents"/row["id"]/"lane-prepare.json"
    if not checkpoint.is_file():
        return False
    data=read_json(checkpoint)
    return data.get("sha256")==row["sha256"] and data.get("status")=="prepared"


@lru_cache(maxsize=256)
def _estimate_checkpoint(path, modified):
    return read_json(path)


def _checkpoint_for(job, name, sha, revision):
    path = job / name
    try:
        data = _estimate_checkpoint(path, path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and revision(data) == sha else {}


def _pairing_profile(root, row):
    job = root / "documents" / row["id"]
    sha = row["sha256"]
    mixed = _checkpoint_for(job, "mixed-parse.json", sha, lambda data: data.get("identity", {}).get("sha256"))
    review = _checkpoint_for(job, "crop-review.json", sha, lambda data: data.get("revision"))
    priority = 2 if review.get("status") == "needs_review" else int(not (mixed or _prepared(root, row)))
    return priority, mixed, review


def _remaining_work(root, row, mixed, review):
    from kg_pilot.native_pages import page_plan

    if mixed:
        items = mixed.get("items", [])
        characters = sum(len(item["text"]) for item in items if item["type"] == "text")
        visuals = sum(item["type"] != "text" for item in items)
        return 2 + characters / 1600 + 2 * visuals

    pages = row["pages"]
    job = root / "documents" / row["id"]
    regions = _checkpoint_for(job, "pdf-regions.json", row["sha256"], lambda data: data.get("sha256"))
    items = [item for item in (review or regions).get("items", []) if not item.get("filter_reason")]
    visuals = len(items) if review or regions else 2 * pages
    pilot = root.parents[1]
    plan = page_plan(pilot / "parsed" / row["id"], row)
    missing = plan.count(None)
    characters = sum(len(text) for text in plan if text)
    needs_transcription = sum((item["type"] == "equation" and not item.get("latex"))
                              or (item["type"] == "table" and not item.get("table_body")) for item in items)
    needs_review = 0 if review.get("status") == "accepted" else min(pages, visuals) + pages / 4
    needs_layout = 0 if regions else pages / 2
    return 2 + characters / 1600 + 2 * visuals + 4 * missing + needs_transcription + needs_review + needs_layout


def _paired_batch(root, waiting):
    profiles = {row["id"]: _pairing_profile(root, row) for row in waiting}
    ordered = sorted(waiting, key=lambda row: profiles[row["id"]][0])
    first = ordered[0]
    peers = [row for row in ordered[1:16] if profiles[row["id"]][0] != 2 or profiles[first["id"]][0] == 2]
    if not peers:
        peers = ordered[1:2]
    candidates = [first, *peers]
    scores = {row["id"]: _remaining_work(root, row, *profiles[row["id"]][1:]) for row in candidates}
    if peers:
        peer = min(peers, key=lambda row: abs(scores[row["id"]] - scores[first["id"]]))
        return [first, peer], {doc:round(scores[doc], 1) for doc in (first["id"], peer["id"])}
    return [first], {first["id"]:round(scores[first["id"]], 1)}


async def _rolling_lane(root, row, phase, call, selection=None):
    doc=row["id"]
    started=now()
    print(started,"START",phase.upper(),doc,flush=True)
    code=None
    try:
        arguments={"doc_id":doc,"sha256":row["sha256"],"phase":phase}
        if selection:
            arguments["selection"]=selection
        response=await call("native_library_run_lane",arguments)
        result=response.get("data") or {}
        if phase=="prepare" and result.get("status")=="prepared":
            print(now(),"PREPARED",doc,flush=True)
            return None
        if phase=="publish" and not pending_rows(root,[row]):
            print(now(),"PUBLISHED",doc,flush=True)
            return None
        error=response.get("error") or {}
        code=error.get("code")
        message=error.get("message") if code==PREPARE_WORKER_EXITED else code
        message=message or "No durable checkpoint"
    except Exception as error:
        message=type(error).__name__
    checkpoint=root/"documents"/doc/f"lane-{phase}.json"
    if checkpoint.is_file():
        data=read_json(checkpoint)
        if data.get("sha256")==row["sha256"] and data.get("started","")>=started:
            message=data.get("error") or message
    return {"doc_id":doc,"message":message,"code":code,"at":now()}


async def _rolling_pass(root, rows, call, *, selection=None, concurrency=None):
    waiting={}
    ready={}
    for row in rows:
        (ready if _prepared(root,row) else waiting)[row["id"]]=row
    active={}
    failures=[]
    deferred=[]
    paused=False
    last_state=None
    drain=root/"pause_after_group.json"
    try:
        while waiting or ready or active:
            try:
                check_pause(root,selection=selection)
            except PauseRequested:
                paused=True
            limit=concurrency if selection else document_concurrency(root)
            allowed=read_json(drain)["documents"] if not selection and drain.is_file() else None
            if not paused and not failures:
                while len(active)<limit:
                    next_ready=next((r for r in ready.values()
                                     if allowed is None or allowed.get(r["id"])==r["sha256"]),None)
                    next_waiting=next((r for r in waiting.values()
                                       if allowed is None or allowed.get(r["id"])==r["sha256"]),None)
                    writer_busy=any(phase=="publish" for _,phase in active.values())
                    if not writer_busy and (next_ready or next_waiting):
                        # Publish a prepared PDF first, or start on a fresh PDF if nothing is ready.
                        row=ready.pop(next_ready["id"]) if next_ready else waiting.pop(next_waiting["id"])
                        phase="publish"
                    elif next_waiting:
                        row=waiting.pop(next_waiting["id"])
                        phase="prepare"
                    else:
                        break
                    options={"selection":selection} if selection else {}
                    task=asyncio.create_task(_rolling_lane(root,row,phase,call,**options))
                    active[task]=row,phase
            if not active:
                break
            ids=[row["id"] for row,_ in active.values()]
            state=(tuple(ids),limit)
            if state!=last_state:
                progress="selected-progress.json" if selection else "scheduler.json"
                save(root/progress,{"status":"running","scheduling":"rolling",
                    **({"run_id":selection} if selection else {}),
                    "doc_id":ids[0],"doc_ids":ids,"document_concurrency":limit,"at":now()})
                last_state=state
            completed,_=await asyncio.wait(active,timeout=1,return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                row,phase=active.pop(task)
                failure=task.result()
                if failure:
                    if phase=="prepare" and failure.get("code")==PREPARE_WORKER_EXITED:
                        print(failure["at"],"DEFERRED",row["id"],failure["message"],flush=True)
                        deferred.append(failure)
                    elif "裁图待复核" in failure["message"]:
                        deferred.append(failure)
                    else:
                        failures.append(failure)
                elif phase=="prepare":
                    ready[row["id"]]=row
    finally:
        if active:
            await asyncio.gather(*active,return_exceptions=True)
    check_pause(root,selection=selection)
    return failures + deferred


async def ingest_rolling(root, rows, call, wait_for_reset):
    while pending := pending_rows(root,rows):
        check_pause(root)
        until=exhausted_until(await asyncio.to_thread(current_windows,root))
        if until:
            if not wait_for_reset:
                return 2,[]
            await wait_for_window(root,until,pending[0]["id"])
            continue
        failures=await _rolling_pass(root,pending,call)
        if not failures:
            return 0,[]
        if wait_for_reset and any(any(word in f["message"].lower() for word in QUOTA_WORDS) for f in failures):
            until=exhausted_until(await asyncio.to_thread(current_windows,root))
            if until:
                await wait_for_window(root,until,failures[0]["doc_id"])
                continue
        return 1,failures
    return 0,[]


async def ingest_workspace_batches(root, rows, call, *, wait_for_reset=False, selection=None, concurrency=None,
                                   prefetch_call=None):
    from kg_pilot.native_crop_review import needs_followup
    from kg_pilot.native_pages import STALE_INPUTS
    from kg_pilot.native_readiness import preflight_receipt

    waiting = sorted(pending_rows(root, rows), key=lambda row: not _prepared(root, row))
    deferred = []
    stale = set()
    progress = "selected-progress.json" if selection else "scheduler.json"
    drain = root / "pause_after_group.json"

    async def fill_buffer(retry_rows, unprepared, target, started, pairing, limit, formal_finished):
        path = root / "prefetch-progress.json"
        while not formal_finished.is_set() and (retry_rows or (target > 0 and unprepared)):
            try:
                check_pause(root)
            except PauseRequested:
                return
            if drain.is_file():
                return
            available = retry_rows or unprepared
            size = limit if retry_rows else target
            batch = _paired_batch(root, available)[0] if pairing and size == 2 else available[:size]
            files = [{"id":row["id"], "sha256":row["sha256"]} for row in batch]
            ids = [row["id"] for row in batch]
            available[:] = [row for row in available if row["id"] not in ids]
            marker = {"status":"running", "doc_ids":ids, "at":now(), "group_started":started, "stage":"preflight"}
            save(path, marker)
            print(marker["at"], "START BUFFER PREFLIGHT", ", ".join(ids), flush=True)
            try:
                reply = await prefetch_call("native_library_ingest_workspace", {"documents":files, "mode":"preflight"})
                result = reply.get("data") or {}
                accepted = result.get("ready", [])
                target -= len(accepted)
                if accepted:
                    print(now(), "BUFFER READY", ", ".join(accepted), flush=True)
                errors = result.get("errors", {})
                if errors or not reply.get("ok", bool(result)):
                    print(now(), "BUFFER PREFLIGHT RESULT", json.dumps(errors or reply.get("error"), ensure_ascii=False), flush=True)
                if len(accepted) + len(errors) != len(batch) or any(
                        not message.startswith(("裁图待复核", "预处理待复核")) for message in errors.values()):
                    return
            except Exception as error:
                print(now(), "BUFFER PREFLIGHT FAILED", str(error), flush=True)
                return
            finally:
                save(path, {**marker, "status":"finished", "finished":now()})

    while waiting:
        check_pause(root, selection=selection)
        if not selection and drain.is_file():
            allowed = read_json(drain)["documents"]
            waiting = [row for row in waiting if allowed.get(row["id"]) == row["sha256"]]
            if not waiting:
                break
        limit = concurrency if concurrency is not None else document_concurrency(root)
        pairing = limit == 2 and not selection and concurrency is None and not drain.is_file()
        ready, pending, retry, held = [], [], [], []
        for row in waiting:
            doc = row["id"]
            priority, _, review = _pairing_profile(root, row)
            receipt = preflight_receipt(root/"documents"/doc, row["sha256"])
            if needs_followup(review, row["sha256"]):
                retry.append(row)
            elif priority == 2 or receipt.get("status") == "needs_review":
                held.append({"doc_id":doc, "message":receipt.get("error", "裁图待复核"), "at":now()})
            elif receipt.get("status") == "ready" and doc not in stale:
                ready.append(row)
            else:
                pending.append(row)
        preparing = len(ready) < limit and bool(retry or pending)
        candidates = (retry or pending) if preparing else ready
        if not candidates:
            deferred.extend(held)
            break
        batch, estimates = _paired_batch(root, candidates) if pairing else (candidates[:limit], {})
        ids = [row["id"] for row in batch]
        remainder = [row for row in waiting if row["id"] not in ids]
        if not selection:
            until = exhausted_until(await asyncio.to_thread(current_windows, root))
            if until:
                if not wait_for_reset:
                    return 2, deferred
                await wait_for_window(root, until, ids[0])
                continue
        started = now()
        state = {"status":"running", "scheduling":"shared_workspace", "doc_id":ids[0], "doc_ids":ids,
                 "document_concurrency":limit, "at":started}
        state["stage"] = "preflight" if preparing else "indexing"
        if selection:
            state["run_id"] = selection
        if pairing:
            state.update(pairing="estimated_remaining_work", estimated_work=estimates)
        save(root / progress, state)
        print(started, "START PREFLIGHT" if preparing else "START WORKSPACE", ", ".join(ids), flush=True)
        if pairing:
            print(started, "PAIR WORK ESTIMATES", json.dumps(estimates), flush=True)
        args = {"documents": [{"id":row["id"], "sha256":row["sha256"]} for row in batch]}
        if selection:
            args["selection"] = selection
        args["mode"] = "preflight" if preparing else "ready"
        # Keep a following batch ready and work through older flagged PDFs while
        # the formal worker owns the index. The preflight worker writes inputs only.
        buffer = None
        formal_finished = asyncio.Event()
        buffer_size = limit - (len(ready) - len(batch))
        if prefetch_call and not preparing and (retry or (buffer_size > 0 and pending)) and not drain.is_file():
            buffer = asyncio.create_task(fill_buffer(retry, pending, buffer_size, started, pairing, limit, formal_finished))
        try:
            response = await call("native_library_ingest_workspace", args)
        finally:
            if buffer:
                formal_finished.set()
                await buffer
        check_pause(root, selection=selection)
        data = response.get("data") or {}
        errors = data.get("errors", {})
        if preparing:
            prepared = set(data.get("ready", []))
            stale.difference_update(prepared)
            if prepared:
                print(now(), "PREFLIGHT READY", ", ".join(doc for doc in ids if doc in prepared), flush=True)
            failures = [{"doc_id":doc, "message":errors.get(doc) or "预处理未生成可用检查点", "at":now()}
                        for doc in ids if doc not in prepared]
            if wait_for_reset and any(any(word in f["message"].lower() for word in QUOTA_WORDS) for f in failures):
                until = exhausted_until(await asyncio.to_thread(current_windows, root))
                if until:
                    await wait_for_window(root, until, failures[0]["doc_id"])
                    continue
            failed = {f["doc_id"] for f in failures
                      if not f["message"].startswith(("裁图待复核", "预处理待复核"))}
            deferred.extend(f for f in failures if f["doc_id"] in failed)
            if failed and not prepared:
                return 1, deferred
            waiting = [row for row in waiting if row["id"] not in failed]
            continue
        if errors and all(message == STALE_INPUTS for message in errors.values()):
            stale.update(errors)
            continue
        missing = pending_rows(root, batch)
        missing_ids = {row["id"] for row in missing}
        published = [doc for doc in ids if doc not in missing_ids]
        if published:
            print(now(), "PUBLISHED WORKSPACE", ", ".join(published), flush=True)
        failures = []
        for row in missing:
            doc = row["id"]
            message = errors.get(doc)
            lane = root / "documents" / doc / "lane-publish.json"
            if not message and lane.is_file():
                status = read_json(lane)
                if status.get("sha256") == row["sha256"] and status.get("started", "") >= started:
                    message = status.get("error")
            message = message or (response.get("error") or {}).get("code") or "No durable checkpoint"
            failures.append({"doc_id":doc, "message":message, "at":now()})
        if wait_for_reset and any(any(word in f["message"].lower() for word in QUOTA_WORDS) for f in failures):
            until = exhausted_until(await asyncio.to_thread(current_windows, root))
            if until:
                waiting = missing + remainder
                await wait_for_window(root, until, missing[0]["id"])
                continue
        deferred.extend(failures)
        if failures and not published and any("裁图待复核" not in f["message"] for f in failures):
            return 1, deferred
        waiting = remainder
    check_pause(root, selection=selection)
    return int(bool(deferred)), deferred


def selected_rows(pilot, requested):
    root=directory(pilot)
    queue={r["id"]:r for r in read_json(root/"queue.json")["documents"]}
    marked=root/"marked_revisions.json"
    approved=read_json(marked) if marked.is_file() else {}
    rows=[]
    for spec in requested:
        doc,sep,sha=spec.partition("@")
        row=queue.get(doc)
        if (not row or row in rows or (sep and sha!=row["sha256"])
                or row["prebuilt"] or not eligible(row,approved)):
            raise ValueError("未知、重复、未授权或修订不匹配的文献："+spec)
        verify_source(pilot,row)
        rows.append(row)
    if not rows:
        raise ValueError("请选择至少一篇文献")
    return rows


async def run_crop_selection(root, rows, limit, token, call):
    failures=[]
    for offset in range(0,len(rows),limit):
        check_pause(root,selection=token)
        group=rows[offset:offset+limit]
        save(root/"crop-progress.json",{"run_id":token,"status":"running","at":now(),
                                           "doc_ids":[r["id"] for r in group],"document_concurrency":limit})
        async def review(row):
            response=await call("native_library_run_lane",{"doc_id":row["id"],"sha256":row["sha256"],
                                                           "phase":"crops","selection":token})
            result=response.get("data") or {}
            print(row["id"],json.dumps(response,ensure_ascii=False),flush=True)
            if result.get("status") not in {"accepted","needs_review"}:
                checkpoint=root/"documents"/row["id"]/"lane-crops.json"
                lane=read_json(checkpoint) if checkpoint.exists() else {}
                return {"doc_id":row["id"],"message":lane.get("error") or str(response.get("error")),"at":now()}
        failures.extend(x for x in await asyncio.gather(*(review(r) for r in group)) if x)
        if failures:
            break
    return failures


async def run_selection(pilot, requested, concurrency, call, *, crops_only=False):
    root=directory(pilot)
    marker=root/"pause.json"
    if not marker.is_file():
        raise ValueError("此入口用于暂停中的队列，请先暂停全队列")
    limit=validate_concurrency(concurrency)
    rows=selected_rows(pilot,requested)
    original=read_json(root/"manifest.json")["documents"]
    pending=pending_rows(root,rows)
    token=uuid.uuid4().hex
    proc=psutil.Process()
    scope={"run_id":token,"mode":"crops" if crops_only else "publish","documents":{r["id"]:r["sha256"] for r in rows},"started":now(),
           "concurrency":limit,"pause_sha256":file_hash(marker),"pid":proc.pid,"created":proc.create_time()}
    name="crop" if crops_only else "selected"
    lease=root/(name+"-run.json")
    with file_lock(root/"selection-start.lock"):
        other=live_selection(root,"selected-run.json" if crops_only else "crop-run.json")
        if other:
            if not crops_only or set(other["documents"]) & set(scope["documents"]):
                raise ValueError("仍有指定任务占用这些处理通道")
            limit=min(limit,validate_concurrency(concurrency)-other["concurrency"])
            if limit<1:
                raise ValueError("并行上限已满")
            scope["concurrency"]=limit
        save(lease,scope)
    report={**scope,"status":"interrupted"}
    try:
        if crops_only:
            failures=await run_crop_selection(root,pending,limit,token,call)
        else:
            _,failures=await ingest_workspace_batches(root,pending,call,selection=token,concurrency=limit)
        after=read_json(root/"manifest.json")["documents"]
        allowed_additions=other.get("documents",{}) if crops_only else scope["documents"]
        if any(after.get(k)!=v for k,v in original.items()) or set(after)-original.keys()-allowed_additions.keys():
            raise RuntimeError("指定范围以外的发布清单发生变化")
        missing=[] if crops_only else pending_rows(root,rows)
        report.update(status="incomplete" if missing else "complete",failures=failures,
                      published=[r["id"] for r in pending if r["id"] in after],total=len(after))
        print(json.dumps(report,ensure_ascii=False),flush=True)
        return int(bool(missing or failures))
    finally:
        lease.unlink(missing_ok=True)
        report["finished"]=now()
        save(root/"selected-runs"/(token+".json"),report)
        save(root/(name+"-progress.json"),report)


async def main(args):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    root=directory(ROOT/"pilot")
    params=StdioServerParameters(command=sys.executable,args=["-X","utf8","-u",str(ROOT/"scripts/ingestion_mcp.py"),"--native-library"],cwd=str(ROOT),
        env={**os.environ,"PYTHONUTF8":"1","HF_HUB_CACHE":str(ROOT/"pilot/model_cache/hub"),"HF_HUB_OFFLINE":"1","TRANSFORMERS_OFFLINE":"1","TOKENIZERS_PARALLELISM":"false","OMP_NUM_THREADS":"4","MKL_NUM_THREADS":"4","DOCLING_NUM_THREADS":"4","CUDA_VISIBLE_DEVICES":""})
    async with Client(params,mode="legacy",read_timeout_seconds=86400) as client:
        async def call(name,data):
            result=await client.call_tool(name,data)
            return result.structured_content
        status=await call("native_library_status",{})
        if not status["ok"]:
            raise RuntimeError(status)
        print(json.dumps(status["data"],ensure_ascii=False),flush=True)
        if args.status:
            return 0
        if args.search:
            reply=await call("native_library_search",{"query":args.search})
            print(json.dumps(reply,ensure_ascii=False,indent=2),flush=True)
            return 0 if reply["ok"] else 1
        if getattr(args,"only",None):
            return await run_selection(ROOT/"pilot",args.only,args.concurrency,call,crops_only=args.crops_only)
        check_pause(root)
        prepared=await call("native_library_prepare",{})
        if not prepared["ok"]:
            raise RuntimeError("Cannot verify the latest durable index checkpoint")
        queue=read_json(root/"queue.json")["documents"]
        approved=read_json(root/"marked_revisions.json") if (root/"marked_revisions.json").exists() else {}
        todo=pending_rows(root,[row for row in queue if eligible(row,approved) and not row["prebuilt"]])
        drain=root/"pause_after_group.json"
        if drain.is_file():
            allowed=read_json(drain)["documents"]
            todo=[r for r in todo if allowed.get(r["id"])==r["sha256"]]
        if args.group:
            todo=[row for row in todo if row["group"]==args.group]
        if args.document:
            todo=[row for row in todo if row["id"]==args.document]
            if not todo and args.document not in read_json(root/"manifest.json")["documents"]:
                raise ValueError("Requested document is outside the eligible queue")
        if args.limit:
            todo=todo[:args.limit]
        trial=root/"two_lane_trial.json"
        selection=read_json(trial)["documents"] if trial.is_file() else ()
        by_id={r["id"]:r for r in todo}
        armed=[by_id[r["id"]] for r in selection
               if r["id"] in by_id and by_id[r["id"]]["sha256"]==r["sha256"]]
        code,failures=0,[]
        if len(armed)==2 and document_concurrency(root)>=2:
            before_pair=todo[:todo.index(armed[0])]
            code,failures=await ingest_workspace_batches(root,before_pair,call,wait_for_reset=args.wait_for_reset)
            if not code:
                code,failures=await ingest_workspace_batches(root,armed,call,wait_for_reset=args.wait_for_reset,concurrency=2)
        if not code:
            async with Client(params,mode="legacy",read_timeout_seconds=86400) as preflight_client:
                async def prefetch_call(name, data):
                    result = await preflight_client.call_tool(name, data)
                    return result.structured_content

                code,failures=await ingest_workspace_batches(root,todo,call,wait_for_reset=args.wait_for_reset,
                                                               prefetch_call=prefetch_call)
        if code==2:
            return code
        if failures:
            save(root/"last_failures.json",failures)
            print(now(),"FAILED",json.dumps(failures,ensure_ascii=False),flush=True)
        final=(await call("native_library_status",{}))["data"]
        save(root/"scheduler.json",{"status":"needs_attention" if failures else "pass_complete","at":now(),"state":final})
        print(json.dumps(final,ensure_ascii=False),flush=True)
        return 1 if failures else 0


def control(args):
    root=directory(ROOT/"pilot")
    marker=root/"pause.json"
    if getattr(args,"pause_after_current",False):
        scheduler=read_json(root/"scheduler.json")
        ids=scheduler.get("doc_ids") or [scheduler.get("doc_id")]
        if scheduler.get("status")!="running" or not all(ids):
            raise RuntimeError("No running document group")
        revisions={r["id"]:r["sha256"] for r in read_json(root/"queue.json")["documents"] if r["id"] in ids}
        if set(revisions)!=set(ids):
            raise ValueError("Running revisions are missing from the queue")
        save(root/"pause_after_group.json",{"requested":now(),"documents":revisions})
        pause_at_group_boundary(root)
        print("本组提交后暂停：",", ".join(ids),flush=True)
        return 0
    if getattr(args,"arm_pair",None):
        from kg_pilot.native_pair import arm_pair
        arm_pair(ROOT/"pilot",args.arm_pair)
        print("双线试跑已登记，将由原生入库 MCP 在文献边界接手。",flush=True)
        return 0
    if args.pause:
        save(marker,{"requested":now()})
        print("暂停信号已保存。正在等待在途调用和索引写入结束……",flush=True)
        while True:
            try:
                with (file_lock(root/"scheduler.lock"), file_lock(root/"crop-scheduler.lock"), file_lock(root/"pair.lock"),
                      file_lock(root/"writer.lock"), file_lock(root/"preflight.lock")):
                    save(root/"scheduler.json",{"status":"paused","at":now()})
                    print("已暂停，可关机。已发布的索引和已落盘的模型响应均已保留。",flush=True)
                    return 0
            except IntakeError:
                time.sleep(2)
    if args.status or args.search:
        return asyncio.run(main(args))
    try:
        with file_lock(root/("crop-scheduler.lock" if getattr(args,"crops_only",False) else "scheduler.lock")):
            if getattr(args,"only",None):
                return asyncio.run(main(args))
            if live_selection(root,"crop-run.json"):
                raise ValueError("指定裁图任务正在运行")
            save(root/"scheduler_process.json",{"pid":os.getpid(),"created":psutil.Process().create_time(),"at":now(),
                                               "reads_group_settings":True,"scheduling":"shared_workspace"})
            if args.resume:
                if pause_at_group_boundary(root):
                    (root/"pause_after_group.json").unlink()
                if marker.exists():
                    marker.unlink()
            try:
                return asyncio.run(main(args))
            except (PauseRequested, ExceptionGroup) as error:
                # MCP's async transports can wrap a clean pause in nested task groups.
                if isinstance(error, ExceptionGroup) and error.split(PauseRequested)[1] is not None:
                    raise
                save(root/"scheduler.json",{"status":"paused","at":now()})
                print("已暂停，可关机。",flush=True)
                return 0
    except IntakeError:
        print("已有入库队列正在运行或收尾。",flush=True)
        return 3


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--status",action="store_true")
    parser.add_argument("--search",help="Search the published native indices locally and return original PDF pages")
    parser.add_argument("--wait-for-reset",action="store_true",help="Resume the same PDF after a confirmed subscription-window reset")
    parser.add_argument("--limit",type=int)
    parser.add_argument("--document",help="Process only this catalogue document, for a single-paper validation")
    parser.add_argument("--group",type=int,choices=[1,2,3,4,9])
    action=parser.add_mutually_exclusive_group()
    action.add_argument("--pause-after-current",action="store_true",help="Finish only the current group, then keep the queue paused")
    action.add_argument("--arm-pair",nargs=2,metavar=("DOC_A","DOC_B"),help="Run these two queued PDF revisions concurrently at the next matching MCP document boundary")
    action.add_argument("--pause",action="store_true")
    action.add_argument("--resume",action="store_true")
    action.add_argument("--only",nargs="+",metavar="ID[@SHA256]",help="Run only the listed PDF revisions while the general queue remains paused")
    parser.add_argument("--concurrency",type=int,default=2,help="Document limit for --only")
    parser.add_argument("--crops-only",action="store_true",help="Stop after the original-PDF crop and visual review checkpoints")
    args=parser.parse_args()
    if args.crops_only and not args.only:
        parser.error("--crops-only requires --only")
    if args.only and (args.status or args.search or args.group or args.limit or args.document or args.wait_for_reset):
        parser.error("--only cannot be combined with status, search or general queue selectors")
    sys.exit(control(args))
