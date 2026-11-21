"""Incremental original-PDF ingestion into a recoverable RAG-Anything library."""
from __future__ import annotations

import asyncio
import atexit
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
import uuid
import psutil

from kg_pilot.codex_subscription import SOL_MODEL, run_subscription
from kg_pilot.ingestion import file_lock
from kg_pilot.pipeline import read_json

SOL_RUN = Path("benchmarks/three_raw_sol_1017_20261119/sol")
GAO_RUN = Path("benchmarks/gao_adeia_raw_20261118/native_vision")
DEFAULT_DOCUMENT_CONCURRENCY = 2
MAX_DOCUMENT_CONCURRENCY = 4
WORKSPACE_MODEL_LIMIT = 12
PREPARE_WORKER_EXITED = "native_prepare_worker_exited"


def document_concurrency(root):
    path = root / "queue_settings.json"
    return validate_concurrency(read_json(path)["documents"]) if path.is_file() else DEFAULT_DOCUMENT_CONCURRENCY


def validate_concurrency(value):
    if type(value) is not int or not 1 <= value <= MAX_DOCUMENT_CONCURRENCY:
        raise ValueError(f"同时处理的文献数须为 1 至 {MAX_DOCUMENT_CONCURRENCY} 的整数。")
    return value


class PauseRequested(RuntimeError):
    pass


def pause_at_group_boundary(root, manifest=None):
    request=root/"pause_after_group.json"
    if not request.is_file():
        return False
    wanted=read_json(request)["documents"]
    published=(manifest if manifest is not None else read_json(root/"manifest.json"))["documents"]
    if not wanted or not all(published.get(doc,{}).get("sha256")==sha for doc,sha in wanted.items()):
        return False
    if not (root/"pause.json").is_file():
        save(root/"pause.json",{"requested":now(),"reason":"current_group_published","documents":list(wanted)})
    return True


def live_selection(root, filename):
    try:
        scope=read_json(root/filename)
        if abs(psutil.Process(scope["pid"]).create_time()-scope["created"])<0.1:
            return scope
    except (FileNotFoundError, psutil.NoSuchProcess):
        pass
    return {}


def check_pause(root, *, selection=None, row=None):
    if selection is not None:
        marker=root/"pause.json"
        if marker.is_file():
            for filename in ("selected-run.json","crop-run.json"):
                scope=live_selection(root,filename)
                if (scope.get("run_id")==selection and file_hash(marker)==scope["pause_sha256"]
                        and (row is None or scope["documents"].get(row["id"])==row["sha256"])):
                    return
        raise PauseRequested("指定文献任务已停止或修订不在本轮范围内")
    if (root/"pause.json").exists() or pause_at_group_boundary(root):
        raise PauseRequested("文献入库已请求暂停")


def now():
    return datetime.now(timezone.utc).isoformat()


def recorded_seconds(record, *, until=None):
    """Sum completed attempts and this interval, excluding gaps between attempts."""
    prior = record.get("prior_seconds")
    end = record.get("finished") or until
    if end and record.get("started"):
        try:
            duration = (datetime.fromisoformat(end) - datetime.fromisoformat(record["started"])).total_seconds()
        except (TypeError, ValueError):
            return prior
        if duration >= 0:
            return (prior or 0) + duration
    return prior


def save(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    with temp.open("w",encoding="utf8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp,path)


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def directory(pilot):
    return pilot/"framework/native_library"


def eligible(row, permitted):
    return row["group"]!=9 or permitted.get(row["id"])==row["sha256"]


def state(pilot):
    root=directory(pilot)
    plan=read_json(root/"queue.json")["documents"]
    try:
        latest=root/"manifest.json"
        if not latest.exists():
            latest=root/"manifest.previous.json"
        manifest=read_json(latest) if latest.exists() else {"documents":{}}
    except (OSError,ValueError):
        manifest=read_json(root/"manifest.previous.json")
    try:
        current=read_json(root/"current.json") if (root/"current.json").exists() else None
    except (OSError,ValueError):
        current={"status":"interrupted"}
    if current and current.get("status")=="running":
        identity=current.get("writer")
        if not identity and (root/"scheduler_process.json").exists():
            identity=read_json(root/"scheduler_process.json")
        if identity:
            try:
                alive=abs(psutil.Process(identity["pid"]).create_time()-identity["created"])<0.1
            except psutil.NoSuchProcess:
                alive=False
            except psutil.AccessDenied:
                alive=True
            if not alive:
                current={**current,"status":"interrupted"}
    permission=read_json(root/"marked_revisions.json") if (root/"marked_revisions.json").exists() else {}
    pending=[r for r in plan if not r["prebuilt"] and manifest["documents"].get(r["id"],{}).get("sha256")!=r["sha256"]]
    ready=[r for r in pending if eligible(r,permission)]
    from kg_pilot.native_pages import TRANSCRIPTION_LIMIT
    result={"model":SOL_MODEL,"effort":"high","text_concurrency":WORKSPACE_MODEL_LIMIT,"modal_concurrency":TRANSCRIPTION_LIMIT,
            "catalog":len(plan),"prebuilt":sum(bool(r["prebuilt"]) for r in plan),"published":len(manifest["documents"]),"pending":len(ready),"held":len(pending)-len(ready),
            "next_id":ready[0]["id"] if ready else None,"pause_requested":(root/"pause.json").exists(),
            "current":current}
    result["queue_document_concurrency"]=document_concurrency(root)
    scheduler_path=root/"scheduler.json"
    scheduler=read_json(scheduler_path) if scheduler_path.is_file() else {}
    try:
        for name in ("selected","crop"):
            selection=live_selection(root,name+"-run.json")
            if not selection:
                continue
            progress_path=root/(name+"-progress.json")
            progress=read_json(progress_path) if progress_path.is_file() else {}
            ids=[doc for doc in progress.get("doc_ids",[]) if doc in selection["documents"]]
            if progress.get("at","") < selection["started"] or progress.get("run_id",selection["run_id"])!=selection["run_id"]:
                ids=[]
            marker=root/"pause.json"
            if not result.get("selection"):
                result["selection"]={"concurrency":0,"documents":[],"pausing":False}
                scheduler={"status":"running","doc_ids":[]}
            result["selection"]["started"]=min(selection["started"],result["selection"].get("started",selection["started"]))
            result["selection"]["concurrency"]+=selection["concurrency"]
            result["selection"]["documents"].extend(selection["documents"])
            result["selection"]["pausing"]|=not marker.is_file() or file_hash(marker)!=selection["pause_sha256"]
            scheduler["doc_ids"].extend(ids)
    except (FileNotFoundError,psutil.NoSuchProcess,psutil.AccessDenied):
        pass
    trial=root/"two_lane_trial.json"
    buffered = {}
    if scheduler.get("status")=="running" and (scheduler.get("doc_ids") or result.get("selection")):
        selected_ids=list(scheduler.get("doc_ids",[]))
        marker = root / "prefetch-progress.json"
        if not result.get("selection") and marker.is_file():
            prefetch = read_json(marker)
            if prefetch.get("status")=="running" and prefetch.get("group_started")==scheduler.get("at"):
                buffered = {doc:prefetch["at"] for doc in prefetch["doc_ids"] if doc not in selected_ids}
                selected_ids.extend(buffered)
        lanes_key="queue_lanes"
    elif trial.is_file():
        selected_ids=[r["id"] for r in read_json(trial)["documents"]]
        lanes_key="trial_lanes"
    else:
        selected_ids=[]
        lanes_key="queue_lanes"
    if selected_ids:
        lanes=[]
        for doc in selected_ids:
            lane={"doc_id":doc,"status":"queued"}
            for phase in ("crops","prepare","publish"):
                checkpoint=root/"documents"/doc/("lane-"+phase+".json")
                if checkpoint.is_file():
                    record=read_json(checkpoint)
                    if record["started"] >= lane.get("started",""):
                        lane=record
            if (current and current.get("doc_id")==doc and current.get("status")=="running"
                    and current.get("writer") and current["started"] >= lane.get("started","")):
                lane={**lane,**current,**current["writer"],"phase":"publish"}
            if lane.get("status")=="running":
                try:
                    if abs(psutil.Process(lane["pid"]).create_time()-lane["created"])>=0.1:
                        lane["status"]="interrupted"
                except psutil.NoSuchProcess:
                    lane["status"]="interrupted"
                except psutil.AccessDenied:
                    pass
            if doc in manifest["documents"]:
                lane["status"]="published"
            if doc in buffered:
                if lane.get("started", "") < buffered[doc]:
                    lane = {"doc_id":doc, "status":"queued"}
                lane = {**lane, "prefetch":True}
            lanes.append(lane)
        result[lanes_key]=lanes
    return result


def verify_source(pilot,row):
    papers={p["doc_id"]:p for p in read_json(pilot/"catalog/papers.json")}
    paper=papers[row["id"]]
    pdf=Path(paper["local_path"]).resolve(strict=True)
    if not pdf.is_relative_to((pilot/"collection").resolve()) or pdf.suffix.lower()!=".pdf":
        raise ValueError("PDF is outside the local collection")
    if paper["sha256"]!=row["sha256"] or file_hash(pdf)!=row["sha256"] or paper["page_count"]!=row["pages"]:
        raise ValueError("Catalogue/PDF revision differs from the authorized queue")
    return pdf


def storage_signature(path,*,sync=False):
    result={}
    for f in sorted(path.iterdir()):
        if f.is_file():
            # On Windows FlushFileBuffers needs a writable handle.
            with f.open("r+b" if sync else "rb") as stream:
                if sync:
                    os.fsync(stream.fileno())
                result[f.name]=hashlib.file_digest(stream,"sha256").hexdigest()
    required={"vdb_chunks.json","kv_store_doc_status.json","kv_store_text_chunks.json","graph_chunk_entity_relation.graphml"}
    if not required<=set(result):
        raise ValueError("Incomplete storage generation")
    return result


def checked_generation(pilot,path):
    data=read_json(path)
    active=(pilot/data["storage"]).resolve()
    root=directory(pilot).resolve()
    if active not in ((pilot/SOL_RUN/"storage").resolve(),root/"index-0",root/"index-1"):
        raise ValueError("Unrecognized storage generation")
    hashes=storage_signature(active)
    if data.get("storage_sha256",hashes)!=hashes:
        raise ValueError("Published index checksum mismatch")
    if not data.get("storage_sha256"):
        validate_previous_documents(active,data)
        data["storage_sha256"]=hashes
        save(path,data)
    return data


def initialize(pilot):
    root=directory(pilot)
    if not (root/"manifest.json").exists() and (root/"manifest.previous.json").exists():
        restored=checked_generation(pilot,root/"manifest.previous.json")
        save(root/"manifest.json",restored)
        return restored
    if (root/"manifest.json").exists():
        active=root/"manifest.json"
        try:
            return checked_generation(pilot,active)
        except (OSError,KeyError,ValueError):
            previous=checked_generation(pilot,root/"manifest.previous.json")
            shutil.copy2(active,root/("manifest.interrupted."+uuid.uuid4().hex+".json"))
            save(active,previous)
            return previous
    rows=read_json(root/"queue.json")["documents"]
    seeds={}
    for source,run_path in (("sol",SOL_RUN),("gao_adeia",GAO_RUN)):
        old=pilot/run_path
        contract=read_json(old/("contract.json" if source=="sol" else "run_contract.json"))
        versions=contract["documents"] if source=="sol" else {x["doc_id"]:x["sha256"] for x in contract["documents"]}
        statuses=read_json(old/"storage/kv_store_doc_status.json")
        chunks=read_json(old/"storage/kv_store_text_chunks.json")
        for row in (r for r in rows if r["prebuilt"]==source):
            verify_source(pilot,row)
            doc=row["id"]
            cs=[v for v in chunks.values() if v["full_doc_id"]==doc]
            if versions.get(doc)!=row["sha256"] or statuses[doc]["status"]!="processed" or not statuses[doc].get("multimodal_processed") or not cs:
                raise ValueError("Seed index not usable: "+doc)
            seeds[doc]={"title":row["title"],"pages":row["pages"],"sha256":row["sha256"],"chunks":len(cs),
                        "index":"library" if source=="sol" else "gao_adeia","origin":source}
    manifest={"generation":0,"storage":str(SOL_RUN/"storage"),"retained_storage":str(GAO_RUN/"storage"),"documents":seeds,
              "storage_sha256":storage_signature(pilot/SOL_RUN/"storage")}
    save(root/"manifest.previous.json",manifest)
    save(root/"manifest.json",manifest)
    return manifest


def validate_previous_documents(slot,manifest):
    # The framework's chunk keys are content hashes. If two publications share
    # a chunk, detect a lost attribution before activating the new generation.
    counts=Counter(c.get("full_doc_id") for c in read_json(slot/"kv_store_text_chunks.json").values())
    for doc,record in manifest["documents"].items():
        if record["index"]=="library" and counts[doc]!=record["chunks"]:
            raise ValueError("Earlier source coverage changed: "+doc)


def prepare_slot(pilot,manifest):
    root=directory(pilot)
    target=root/("index-"+str((manifest["generation"]+1)%2))
    source=(pilot/manifest["storage"]).resolve(strict=True)
    if source not in ((pilot/SOL_RUN/"storage").resolve(),(root/"index-0").resolve(),(root/"index-1").resolve()) or target.resolve()==source:
        raise ValueError("Invalid index checkpoint")
    target.mkdir(parents=True,exist_ok=True)
    files={p.name for p in source.iterdir() if p.is_file()}
    required={"graph_chunk_entity_relation.graphml","kv_store_doc_status.json","vdb_chunks.json","kv_store_text_chunks.json"}
    if not required<=files:
        raise ValueError("Incomplete active index")
    # These are the library's own two alternate working copies. Never write the active one.
    for p in target.iterdir():
        if p.name not in files:
            if not p.is_file() or not p.name.startswith(("kv_store_","vdb_")) or p.suffix!=".json":
                raise ValueError("Unexpected file in the inactive index: "+p.name)
            recovery=root/"interrupted_files"/uuid.uuid4().hex
            recovery.mkdir(parents=True)
            p.replace(recovery/p.name)
    for name in sorted(files):
        shutil.copy2(source/name,target/name)
    return target


class SubscriptionHooks:
    def __init__(self,job,*,selection=None,row=None,cache_jobs=(),semaphore=None):
        self.job=job
        self.selection=selection
        self.row=row
        self.cache_jobs=cache_jobs
        self.semaphore=semaphore if semaphore is not None else asyncio.Semaphore(8)
        self.error=None
        self.calls=0
        self.pending={}

    async def invoke(self,messages):
        digest=hashlib.sha256(json.dumps(messages,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        cached=self.job/"responses"/(digest+".json")
        if cached.exists():
            return read_json(cached)["answer"]
        for job in self.cache_jobs:
            try:
                return read_json(job/"responses"/cached.name)["answer"]
            except FileNotFoundError:
                continue
        if digest in self.pending:
            return await asyncio.shield(self.pending[digest])
        async def request():
            async with self.semaphore:
                if self.error:
                    raise RuntimeError(self.error)
                for attempt in range(3):
                    try:
                        check_pause(self.job.parent.parent,selection=self.selection,row=self.row)
                    except PauseRequested as exc:
                        self.error=str(exc)
                        raise
                    tag=uuid.uuid4().hex
                    record={"started":now(),"digest":digest,"images":sum(1 for m in messages if isinstance(m["content"],list) for p in m["content"] if p["type"]=="image_url")}
                    path=self.job/"calls"/(tag+".json")
                    save(path,record)
                    try:
                        answer,usage,model=await asyncio.to_thread(run_subscription,messages,self.job/"cli"/tag,self.job/"attachments")
                        save(cached,{"answer":answer,"usage":usage,"model":model})
                        record.update(status="success",usage=usage)
                        self.calls+=1
                        return answer
                    except Exception as exc:
                        error=str(exc)[:1100]
                        record.update(status="failed",error=error)
                        limited=any(s in error.lower() for s in ("usage limit","usage_limit","quota","insufficient","credit balance"))
                        transient=any(s in error.lower() for s in ("429","timed out","rate limit","server error","connection","stream disconnected"))
                        if limited or not transient or attempt==2:
                            self.error=error
                            raise
                        await asyncio.sleep(10*(attempt+1))
                    finally:
                        record["finished"]=now()
                        save(path,record)
        task=asyncio.create_task(request())
        self.pending[digest]=task
        def finished(result):
            self.pending.pop(digest,None)
            if not result.cancelled():
                result.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def complete(self,prompt,system_prompt=None,history_messages=None,**kwargs):
        messages=([{"role":"system","content":system_prompt}] if system_prompt else [])+list(history_messages or [])
        return await self.invoke(messages+[{"role":"user","content":prompt}])

    async def vision(self,prompt,system_prompt=None,history_messages=None,image_data=None,messages=None,**kwargs):
        if messages is None:
            messages=([{"role":"system","content":system_prompt}] if system_prompt else [])+list(history_messages or [])
            content=prompt
            if image_data:
                url=image_data if image_data.startswith("data:") else "data:image/png;base64,"+image_data
                content=[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":url}}]
            messages.append({"role":"user","content":content})
        return await self.invoke(messages)


def native_app(slot,parser_output,hooks,embed,*,parallel=4,model_parallel=8):
    from raganything import RAGAnything,RAGAnythingConfig
    from lightrag.utils import EmbeddingFunc
    from scripts.rag_comparison_20261116.framework_adapters import FRAMEWORK_SETTINGS

    settings={**FRAMEWORK_SETTINGS,"llm_model_max_async":model_parallel,"max_parallel_insert":parallel,"default_llm_timeout":900,
              "chunk_token_size":440,"chunk_overlap_token_size":40,"llm_model_name":SOL_MODEL}
    app=RAGAnything(config=RAGAnythingConfig(working_dir=str(slot),parser="docling",parser_output_dir=str(parser_output),
        max_concurrent_files=parallel,
        enable_image_processing=True,enable_table_processing=True,enable_equation_processing=True,enable_audio_processing=False,enable_video_processing=False),
        llm_model_func=hooks.complete,vision_model_func=hooks.vision,
        embedding_func=EmbeddingFunc(embedding_dim=1024,max_token_size=512,model_name="BAAI/bge-m3",func=embed),lightrag_kwargs=settings)
    # Finalize before recording snapshot hashes, with no later atexit writes.
    atexit.unregister(app.close)
    return app


async def verify_indexed_document(app,row,parsed,owned):
    from kg_pilot.native_pages import validate_indexed_pages

    info=await app.lightrag.doc_status.get_by_id(row["id"])
    if not info or info.get("status")!="processed" or not info.get("multimodal_processed"):
        raise ValueError("Framework has incomplete native document status")
    validate_indexed_pages(parsed["items"], owned.values(), row["pages"])
    actual=Counter(c["original_type"] for c in owned.values() if c.get("is_multimodal"))
    first=next((c for c in owned.values() if not c.get("is_multimodal")),next(iter(owned.values())))
    hits=await app.lightrag.chunks_vdb.query(first["content"][:650],top_k=20)
    known=next((h.get("id") for h in hits if h.get("id") in owned),None)
    if known is None:
        raise ValueError("Native vector retrieval did not return the source")
    return {"chunks":len(owned),"multimodal":dict(actual),"sample_chunk":known,"sample_page":owned[known]["page_idx"]+1,
            "sha256":row["sha256"],"pages":row["pages"],"title":row["title"],"index":"library","origin":"sol_high","published":now(),
            "parse_policy":parsed["identity"]["policy"],"page_sources":dict(Counter(p["source"] for p in parsed["pages"])),
            "skipped_decorations":len(parsed["skipped_decorations"]), "transcription":parsed["transcription"]}


async def finalize_native_app(app,*hooks):
    try:
        for handler in hooks:
            if handler.pending:
                await asyncio.gather(*list(handler.pending.values()),return_exceptions=True)
        if app.lightrag is not None:
            await asyncio.gather(app.lightrag.llm_model_func.shutdown(),app.lightrag.embedding_func.func.shutdown())
    finally:
        await app.finalize_storages()


async def ingest(pilot,row,pdf,slot,selection=None):
    from kg_pilot.native_embeddings import NativeEncoder
    from kg_pilot.native_pages import mixed_pages
    from raganything.callbacks import ProcessingCallback
    import torch
    root=directory(pilot)
    job=root/"documents"/row["id"]
    job.mkdir(parents=True,exist_ok=True)
    hooks=SubscriptionHooks(job,selection=selection,row=row)
    torch.set_num_threads(4)
    backend = root/"embedding_backend.json"
    device = read_json(backend)["device"] if backend.is_file() else "cpu"
    bge = None
    first_batch_requested = False
    metrics_file = job/"embedding_run.json"
    def checkpoint(stage, **counts):
        save(job/"index-stage.json", {"sha256":row["sha256"], "stage":stage, "at":now(), **counts})

    class IndexProgress(ProcessingCallback):
        def on_multimodal_start(self, **kwargs):
            checkpoint("multimodal")

        def on_document_complete(self, **kwargs):
            checkpoint("checking")

    async def embed(texts,**_):
        nonlocal first_batch_requested
        first_batch = not first_batch_requested
        if first_batch:
            first_batch_requested = True
            save(metrics_file, {**bge.identity, "state":"first_batch_requested", "at":now()})
        vectors = await asyncio.to_thread(bge.encode, texts)
        if first_batch:
            save(metrics_file, {**bge.identity, "state":"first_batch_encoded", "rows":len(vectors), "at":now()})
        return vectors
    app=native_app(slot,job/"parser",hooks,embed)
    app.callback_manager.register(IndexProgress())
    try:
        bge = NativeEncoder(pilot, device, cache_path=job / "embedding-cache.sqlite3")
        save(metrics_file, {**bge.identity, "state":"ready", "at":now()})
        result=await app._ensure_lightrag_initialized()
        if not result.get("success"):
            raise RuntimeError("Could not initialize RAG-Anything")
        checkpoint("crop_review")
        parsed=await mixed_pages(pilot,row,pdf,job,hooks,review_crops=True)
        checkpoint("indexing")
        await app.insert_content_list(parsed["items"],file_path=row["id"]+".pdf",doc_id=row["id"])
        if hooks.error:
            raise RuntimeError(hooks.error)
        chunks=read_json(slot/"kv_store_text_chunks.json")
        owned={k:c for k,c in chunks.items() if c.get("full_doc_id")==row["id"]}
        record=await verify_indexed_document(app,row,parsed,owned)
        checkpoint("verified", chunks=len(owned))
        return {**record,"embedding":bge.metrics()}
    finally:
        try:
            await finalize_native_app(app,hooks)
        finally:
            if bge is not None:
                bge.close()
                save(metrics_file, {**bge.metrics(), "state":"closed", "at":now()})


def run_document(pilot,doc_id):
    root=directory(pilot)
    trial=root/"two_lane_trial.json"
    if trial.is_file():
        from kg_pilot.native_pair import run_selected_pair
        result=run_selected_pair(pilot,doc_id,read_json(trial))
        if result is not None:
            return result
    return publish_document(pilot,doc_id)


def publish_document(pilot,doc_id,*,selection=None):
    root=directory(pilot)
    with file_lock(root/"writer.lock"):
        plan=read_json(root/"queue.json")["documents"]
        row=next((r for r in plan if r["id"]==doc_id),None)
        if row is None:
            raise ValueError("Document is outside the migration queue")
        check_pause(root,selection=selection,row=row)
        manifest=initialize(pilot)
        if doc_id in manifest["documents"]:
            if manifest["documents"][doc_id]["sha256"]!=row["sha256"]:
                raise ValueError("Existing revision differs")
            return {"status":"already_published","doc_id":doc_id}
        marked=read_json(root/"marked_revisions.json") if (root/"marked_revisions.json").exists() else {}
        if not eligible(row,marked):
            raise ValueError("Marked document is outside the approved scope")
        pdf=verify_source(pilot,row)
        started=time.monotonic()
        status={"doc_id":doc_id,"title":row["title"],"started":now(),"status":"running",
                "writer":{"pid":os.getpid(),"created":psutil.Process().create_time()}}
        save(root/"current.json",status)
        try:
            slot=prepare_slot(pilot,manifest)
            job=root/"documents"/doc_id
            with file_lock(job/"work.lock"):
                record=asyncio.run(ingest(pilot,row,pdf,slot,selection))
                validate_previous_documents(slot,manifest)
                digest=storage_signature(slot,sync=True)
                check_pause(root,selection=selection,row=row)
                save(root/"manifest.previous.json",manifest)
                record["seconds"]=round(time.monotonic()-started,2)
                manifest["documents"][doc_id]=record
                manifest.update(storage=str(slot.relative_to(pilot)),generation=manifest["generation"]+1,updated=now(),storage_sha256=digest)
                save(root/"manifest.json",manifest)
                try:
                    (job/"embedding-cache.sqlite3").unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning("Could not release the published vector cache for %s",doc_id)
            status.update(status="published",finished=now(),**{"seconds":record["seconds"]})
            pause_at_group_boundary(root,manifest)
        except Exception as exc:
            stopped=isinstance(exc,PauseRequested) or (selection is None and (root/"pause.json").exists())
            status.update(status="paused" if stopped else "interrupted",error=str(exc)[:1400],finished=now())
            raise
        finally:
            save(root/"current.json",status)
        return {"status":"published","doc_id":doc_id,"published":len(manifest["documents"]),"seconds":record["seconds"]}


def search(pilot,query,limit=8,*,encoder=None,doc_ids=None):
    """Read native chunk vectors and resolve results to the registered original PDF."""
    from nano_vectordb import NanoVectorDB
    root=directory(pilot)
    if encoder is None:
        from sentence_transformers import SentenceTransformer
        import torch
        torch.set_num_threads(4)
        config=read_json(pilot/"model_config.json")
        encoder=SentenceTransformer(str(pilot/"model_cache/models--BAAI--bge-m3/snapshots"/config["revision"]),device="cpu",local_files_only=True)
        encoder.max_seq_length=512
    vector=encoder.encode(query,normalize_embeddings=True,show_progress_bar=False)
    papers={p["doc_id"]:p for p in read_json(pilot/"catalog/papers.json")}
    for attempt in range(2):
        manifest=read_json(root/"manifest.json")
        hits=[]
        try:
            for label,key in (("library","storage"),("gao_adeia","retained_storage")):
                if doc_ids is not None and not any(doc in doc_ids and info["index"]==label
                                                   for doc,info in manifest["documents"].items()):
                    continue
                store=pilot/manifest[key]
                if not (store/"vdb_chunks.json").is_file():
                    raise FileNotFoundError("Published chunk vectors are missing")
                db=NanoVectorDB(1024,storage_file=str(store/"vdb_chunks.json"))
                chunks=read_json(store/"kv_store_text_chunks.json")
                scope=None if doc_ids is None else lambda item: item.get("full_doc_id") in doc_ids
                for raw in db.query(query=vector,top_k=limit,filter_lambda=scope):
                    chunk=chunks[raw["__id__"]]
                    doc=chunk["full_doc_id"]
                    info=manifest["documents"].get(doc)
                    if not info or info["index"]!=label or info["sha256"]!=papers[doc]["sha256"]:
                        continue
                    page=chunk.get("page_idx")
                    end=chunk.get("page_idx_end")
                    end=page if end is None else end
                    if type(page) is int and type(end) is int and 0<=page<=end<info["pages"]:
                        hits.append({"doc_id":doc,"title":info["title"],"pdf":papers[doc]["local_path"],"page":page+1,
                                     "page_end":end+1,"pdf_sha256":info["sha256"],
                                     "score":float(raw["__metrics__"]),"chunk_id":raw["__id__"],"excerpt":chunk["content"][:650]})
        except (OSError,ValueError,KeyError):
            if attempt or read_json(root/"manifest.json")["generation"]==manifest["generation"]:
                raise
            continue
        if read_json(root/"manifest.json")["generation"]==manifest["generation"]:
            return {"generation":manifest["generation"],"hits":sorted(hits,key=lambda x:-x["score"])[:limit]}
    raise RuntimeError("Published index changed during lookup; retry the search")
