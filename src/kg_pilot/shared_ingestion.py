"""Shared PDF ingestion and audited API-only indexing of published page evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import time
from importlib.metadata import version

import httpx

from kg_pilot import ingestion, ingestion_runners as runners, llm_provider as llm
from kg_pilot.pipeline import PILOT, read_json


class ApiRun:
    """Pin one ingestion connection across extraction, review, indexing and evaluation."""

    def __init__(self, log, model, token_limit, *, pilot=PILOT):
        self.log = Path(log)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.pilot = Path(pilot)
        self.config = runners._effective_config()
        self.endpoint = llm.connection(self.config)["base_url"]
        self.model = model
        if self.endpoint != llm.PARATERA_BASE_URL or model != runners._model(self.config):
            raise ValueError("Select the requested Paratera model in the ingestion configuration first.")
        self.key = runners._api_key(self.config)
        runners._check_key(self.key)
        self.lock = threading.Lock()
        self.limit = token_limit
        self.charged = 0
        self.reserved = 0
        if self.log.exists():
            for line in self.log.read_text(encoding="utf8").splitlines():
                event = json.loads(line)
                if event["model"] != model or event["endpoint"] != self.endpoint:
                    raise ValueError("The existing run uses a different connection.")
                self.charged += event["charged_tokens"]

    def transport(self, stage, doc_id=None, packet=None):
        run = self

        class Metered(httpx.BaseTransport):
            def handle_request(self, request):
                payload = json.loads(request.content)
                if str(request.url) != run.endpoint + "/chat/completions" or payload["model"] != run.model:
                    raise ValueError("Model or destination changed during the batch.")
                if any(not isinstance(m.get("content"), str) for m in payload["messages"]):
                    raise ValueError("This shared-text workflow accepts text only.")
                if "v4" in run.model.casefold():
                    payload.setdefault("thinking", {"type": "disabled"})
                if stage == "extract":
                    payload["messages"][0]["content"] += (
                        "逐条核对分类和论断引文。引用短且连续的原文词组，足以定位数字和其样品，"
                        "长句有断行时可拆成同页的多段短引，不合并断词。不足以可靠引用时应少取。"
                        "每条 claim 的 quantity、value_text、sample_id、material_system 都必须有非空字符串，"
                        "定性结果可用原文短语填写 value_text，缺乏相应依据时舍弃该条。")
                # UTF-8 bytes conservatively bound ordinary text tokens plus chat framing.
                reserve = len(request.content) + 100 + payload.get("max_tokens", 16000)
                with run.lock:
                    if run.charged + run.reserved + reserve > run.limit:
                        raise ValueError("Run token limit reached before sending.")
                    run.reserved += reserve
                began = time.monotonic()
                usage, status, validation, reported_model = {}, None, None, None
                try:
                    with httpx.Client(timeout=httpx.Timeout(240, connect=15), follow_redirects=False) as upstream:
                        response = upstream.request(request.method, request.url,
                            headers={"Authorization": request.headers["Authorization"]}, json=payload)
                        status = response.status_code
                        body = response.read()
                        response.close()
                        if status == 200:
                            raw = json.loads(body)
                            usage = raw.get("usage") or {}
                            reported_model = raw.get("model")
                            if packet is not None and raw.get("choices"):
                                try:
                                    runners._parse_result(raw["choices"][0]["message"].get("content"), stage, packet)
                                except runners._FormatError as error:
                                    validation = error.reason
                        return httpx.Response(status, headers={"Content-Type": response.headers.get("Content-Type", "application/json")}, content=body)
                finally:
                    reported = usage.get("total_tokens")
                    charged = reported if isinstance(reported, int) else reserve
                    record = dict(stage=stage, doc_id=doc_id, model=run.model, endpoint=run.endpoint,
                                  seconds=round(time.monotonic()-began, 3), http_status=status,
                                  usage=usage, charged_tokens=charged, thinking=payload.get("thinking"),
                                  validation=validation, reported_model=reported_model)
                    with run.lock:
                        run.charged += charged
                        run.reserved -= reserve
                        with run.log.open("a", encoding="utf8") as fp:
                            fp.write(json.dumps(record) + "\n")
        return Metered()

    def intake_task(self, mode, task, packet, workdir):
        if mode not in ("api", "paratera"):
            raise ValueError("An API-only batch cannot invoke the CLI runner.")
        return runners.run_task(mode, task, packet, workdir,
                                transport=self.transport(task, packet["doc_id"], packet))

    def complete(self, messages, stage, doc_id=None, max_tokens=4096):
        payload = dict(model=self.model, messages=messages, temperature=0, max_tokens=max_tokens,
                       stream=False, thinking={"type": "disabled"})
        data = llm.request("POST", "/chat/completions", self.key, payload, config=self.config,
                           timeout=240, transport=self.transport(stage, doc_id))
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop" or not choice["message"].get("content"):
            raise ValueError("The ingestion model did not return a complete answer.")
        return choice["message"]["content"]


def page_items(paper, pages, tables):
    """Convert the published reading copy to RAG-Anything's zero-based physical pages."""
    if [row["pdf_page"] for row in pages] != list(range(1, paper["page_count"] + 1)):
        raise ValueError("Published physical page sequence is incomplete.")
    by_page = {}
    for table in tables:
        number = table["pdf_page"]
        if (table.get("document_sha256") != paper["sha256"] or
                table.get("doc_id", paper["doc_id"]) != paper["doc_id"] or
                type(number) is not int or not 1 <= number <= len(pages)):
            raise ValueError("Table does not belong to this PDF revision and page.")
        if table.get("text", "").strip():
            by_page.setdefault(number, []).append(table)
    items = []
    for page in pages:
        number = page["pdf_page"]
        if page["text"].strip():
            items.append(dict(type="text", text=page["text"], page_idx=number-1))
        for table in by_page.get(number, []):
            items.append(dict(type="table", table_body=table["text"], page_idx=number-1,
                              table_caption=[table["table_caption"]] if table.get("table_caption") else []))
    if not items:
        raise ValueError("The published PDF has no indexable text or tables.")
    return items


def shared_sources(pilot, batch_id):
    from kg_pilot.ingestion_publish import dataset_lock
    batch = read_json(ingestion.batch_path(batch_id, pilot) / "batch.json")
    if batch["status"] != "completed" or batch["mode"] not in ("api", "paratera"):
        raise ValueError("Expected a fully published API intake batch.")
    docs = {}
    with dataset_lock(pilot):
        catalog = {p["doc_id"]: p for p in read_json(pilot / "catalog/papers.json")}
        for entry in batch["documents"]:
            if entry["status"] == "skipped":
                continue
            doc_id = entry.get("duplicate_of") or entry["doc_id"]
            paper = catalog[doc_id]
            source = Path(paper["local_path"])
            if (paper["sha256"] != entry["sha256"] or
                    hashlib.sha256(source.read_bytes()).hexdigest() != paper["sha256"]):
                raise ValueError("Published PDF revision differs from the intake batch.")
            base = pilot / "parsed" / doc_id
            pages, tables = read_json(base / "pages.json"), read_json(base / "tables.json")
            docs[doc_id] = (paper, page_items(paper, pages, tables))
    if not docs:
        raise ValueError("No published documents in this batch.")
    return docs


def make_framework(storage, run, llm_hook):
    from lightrag.utils import EmbeddingFunc
    from raganything import RAGAnything, RAGAnythingConfig
    from kg_pilot.retrieval import retrieval_model

    async def embed(texts, **kwargs):
        return await asyncio.to_thread(lambda: retrieval_model().encode(
            texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False))

    config = RAGAnythingConfig(working_dir=str(storage), parser="docling",
                               parser_output_dir=str(storage / "parser"),
                               enable_table_processing=True, enable_image_processing=False,
                               enable_equation_processing=False, enable_video_processing=False,
                               enable_audio_processing=False)
    settings = dict(llm_model_name=run.model, llm_model_max_async=3, embedding_func_max_async=1,
                    embedding_batch_num=8, chunk_token_size=440, chunk_overlap_token_size=40,
                    entity_extract_max_gleaning=0, force_llm_summary_on_merge=100000,
                    tiktoken_model_name="gpt-4o", cosine_better_than_threshold=0.20,
                    default_llm_timeout=300, default_embedding_timeout=600,
                    addon_params={"language": "Chinese"})
    return RAGAnything(config=config, llm_model_func=llm_hook, embedding_func=EmbeddingFunc(
        embedding_dim=1024, max_token_size=512, model_name="BAAI/bge-m3", func=embed),
        lightrag_kwargs=settings)


def storage_hashes(storage):
    required = ("kv_store_text_chunks.json", "vdb_chunks.json", "vdb_entities.json",
                "vdb_relationships.json", "graph_chunk_entity_relation.graphml")
    return {name: hashlib.sha256((storage / name).read_bytes()).hexdigest() for name in required}


@asynccontextmanager
async def framework_search(run, result):
    """Read a published batch index on a private copy; native query caches may write."""
    from lightrag import QueryParam

    root = Path(result["index_path"])
    info = read_json(root / "manifest.json")
    if info["identity"]["model"] != run.model or info["identity"]["endpoint"] != run.endpoint:
        raise ValueError("Use the original ingestion connection to query this run.")
    if storage_hashes(root / "storage") != info["retrieval_hashes"]:
        raise ValueError("Stored framework retrieval data changed.")

    async def unexpected(*args, **kwargs):
        raise ValueError("Expected a metered per-query callback.")

    with TemporaryDirectory(prefix="shared-search-") as folder:
        storage = Path(folder) / "storage"
        shutil.copytree(root / "storage", storage)
        app = make_framework(storage, run, unexpected)
        try:
            init = await app._ensure_lightrag_initialized()
            if not init.get("success"):
                raise RuntimeError("Cannot open published framework index.")

            async def ranked_pages(question, label=None):
                async def keywords(prompt, **kwargs):
                    return await asyncio.to_thread(run.complete, [{"role":"user", "content":prompt}],
                                                   "query_keywords", label)
                result = await app.lightrag.aquery_data(question, QueryParam(mode="mix", chunk_top_k=14,
                    top_k=20, enable_rerank=False, model_func=keywords))
                if result.get("status") != "success":
                    if result.get("metadata", {}).get("failure_reason") == "no_results":
                        return []
                    raise RuntimeError("Framework retrieval failed.")
                ids = [item["chunk_id"] for item in result["data"]["chunks"]]
                chunks = await app.lightrag.text_chunks.get_by_ids(ids)
                pages = []
                for chunk in chunks:
                    doc, first = chunk["full_doc_id"], chunk["page_idx"]
                    last = chunk.get("page_idx_end")
                    pages.extend((doc, i+1) for i in range(first, (last if last is not None else first)+1))
                return list(dict.fromkeys(pages))

            yield ranked_pages
        finally:
            await app.finalize_storages()


async def build_index(run, batch_id):
    """Resume per-document checkpoints, then publish the complete index by directory rename."""
    start = time.monotonic()
    docs = shared_sources(run.pilot, batch_id)
    identity = dict(schema=1, model=run.model, endpoint=run.endpoint,
                    versions={p: version(p) for p in ("raganything", "lightrag-hku")},
                    documents={doc: {"sha256": p["sha256"], "content_sha256": hashlib.sha256(
                        json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
                               for doc, (p, items) in docs.items()})
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    base = run.pilot / "framework/shared" / batch_id
    base.mkdir(parents=True, exist_ok=True)
    target, stage = base / digest, base / (".building-" + digest)
    with ingestion.file_lock(base / "index.lock"):
        if target.exists():
            info = read_json(target / "manifest.json")
            if info["identity"] != identity or storage_hashes(target / "storage") != info["retrieval_hashes"]:
                raise ValueError("Published index differs from the verified inputs or files.")
            return {**info, "index_path": str(target), "reused": True}
        stage.mkdir(exist_ok=True)
        doc_id = None

        async def complete(prompt, system_prompt=None, history_messages=None, **kwargs):
            messages = ([{"role": "system", "content": system_prompt}] if system_prompt else [])
            messages.extend(history_messages or [])
            messages.append({"role": "user", "content": prompt})
            return await asyncio.to_thread(run.complete, messages, "framework_build", doc_id, 8192)

        app = make_framework(stage / "storage", run, complete)
        rows = {}
        try:
            init = await app._ensure_lightrag_initialized()
            if not init.get("success"):
                raise RuntimeError("The configured framework storage could not be opened.")
            for doc_id, (paper, items) in docs.items():
                began = time.monotonic()
                status = await app.lightrag.doc_status.get_by_id(doc_id)
                reused = bool(status and status.get("status") == "processed"
                              and await app._get_multimodal_processed_flag(doc_id, status))
                if not reused:
                    await app.insert_content_list(items, file_path=paper["local_path"], doc_id=doc_id)
                    status = await app.lightrag.doc_status.get_by_id(doc_id)
                if not status or status.get("status") != "processed":
                    raise ValueError(f"Incomplete framework ingestion for {doc_id}")
                rows[doc_id] = dict(seconds=round(time.monotonic()-began, 3),
                                    reused_checkpoint=reused,
                                    items=dict(Counter(item["type"] for item in items)))
        finally:
            await app.finalize_storages()
        storage = stage / "storage"
        chunks = read_json(storage / "kv_store_text_chunks.json")
        seen = Counter()
        for chunk in chunks.values():
            doc = chunk["full_doc_id"]
            first = chunk.get("page_idx")
            last = chunk.get("page_idx_end", first)
            if last is None:
                last = first
            if (doc not in docs or type(first) is not int or type(last) is not int
                    or not 0 <= first <= last < docs[doc][0]["page_count"]):
                raise ValueError("Framework chunk has no valid physical page range.")
            seen[doc] += 1
        if set(seen) != set(docs):
            raise ValueError("Some documents have no retrievable chunks.")
        for doc in rows:
            rows[doc]["chunks"] = seen[doc]
        result = dict(identity=identity, documents=rows, retrieval_hashes=storage_hashes(storage),
                      seconds=round(time.monotonic()-start, 3),
                      storage_bytes=sum(p.stat().st_size for p in storage.rglob("*") if p.is_file()))
        ingestion.write_atomic(stage / "manifest.json", result)
        stage.rename(target)
        return {**result, "index_path": str(target), "reused": False}
