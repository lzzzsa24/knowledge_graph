"""Query the promoted Gao/Adeia RAG-Anything index and resolve hits to original pages."""
from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import json
import logging
import re
import shutil
import tempfile
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import pymupdf

from kg_pilot import llm_provider as llm
from kg_pilot.bilingual_retrieval import plan_query
from kg_pilot.effective_evidence import FIELD, load_effective_evidence
from kg_pilot.pipeline import read_json
from kg_pilot.retrieval import retrieval_model


# Permission for page images applies to these PDF revisions at the configured Paratera endpoint.
PDF_REVISIONS = {
    "reliability_gao_d2w_2021": "1a2e32bba2fcdb48b921a0118b6bcd19ab34e44f9d26b4a94ae812e2fa1b0fd9",
    "doc_0086822b49e2d5df8658": "0086822b49e2d5df865817665501c8403bef4127469a8ce5d326c437342669df",
}
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="framework-retrieval")


@lru_cache(maxsize=1)
def _open_index(index):
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc
    # The native INFO log includes extracted query terms; keep production QA
    # diagnostics free of questions and their text-derived keywords.
    logging.getLogger("lightrag").setLevel(logging.WARNING)

    manifest = read_json(index / "manifest.json")
    if manifest["documents"] != PDF_REVISIONS:
        raise ValueError("Unexpected framework document revisions")
    # The framework opens mutable storages even for queries. Keep one private
    # copy and one event loop per web process, separate from the published files.
    directory = tempfile.TemporaryDirectory(prefix="kg-framework-")
    runner = asyncio.Runner()
    workspace = Path(directory.name).name
    working = Path(directory.name) / workspace
    try:
        working.mkdir()
        for name, expected in manifest["files"].items():
            if Path(name).name != name:
                raise ValueError("Invalid index filename")
            source = index / name
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise ValueError("Incomplete framework index")
            if name != "tables.json":
                shutil.copyfile(source, working / name)

        async def embed(texts, **kwargs):
            from kg_pilot.qa import _QUERY_LOCK
            with _QUERY_LOCK:
                return retrieval_model().encode(texts, normalize_embeddings=True, show_progress_bar=False)

        async def unexpected_generation(*args, **kwargs):
            raise RuntimeError("Retrieval-only index requires a per-request keyword callback")

        async def open_native():
            rag = LightRAG(working_dir=directory.name, workspace=workspace,
                           llm_model_func=unexpected_generation, tiktoken_model_name="gpt-4o",
                           embedding_func=EmbeddingFunc(embedding_dim=1024, max_token_size=512,
                                                        model_name="BAAI/bge-m3", func=embed),
                           enable_llm_cache=False, enable_llm_cache_for_entity_extract=False,
                           cosine_better_than_threshold=0.20, addon_params={"language": "Chinese"})
            await rag.initialize_storages()
            return rag

        rag = runner.run(open_native())
        tables = read_json(index / "tables.json")
    except Exception:
        runner.close()
        directory.cleanup()
        raise

    def close():
        try:
            runner.run(rag.finalize_storages())
        finally:
            runner.close()
            directory.cleanup()

    atexit.register(close)
    return runner, rag, tables


def _ranked_pages(index, question, connection, key, model, transport, usage):
    from lightrag import QueryParam
    runner, rag, tables = _open_index(index)

    async def keywords(prompt, **kwargs):
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        if connection.get("provider_id") == "paratera" and "v4" in model.lower():
            payload["thinking"] = {"type": "disabled"}
        try:
            raw = await asyncio.to_thread(llm.request, "POST", "/chat/completions", key, payload,
                                          config=connection, transport=transport)
        except llm.ProviderError:
            usage.append(("keywords", None))
            raise
        usage.append(("keywords", (raw.get("usage") or {}).get("total_tokens")))
        return raw["choices"][0]["message"]["content"]

    async def query():
        result = await rag.aquery_data(question, QueryParam(mode="mix", top_k=20, chunk_top_k=14,
                                       enable_rerank=False, model_func=keywords))
        if result.get("status") != "success":
            if result.get("metadata", {}).get("failure_reason") == "no_results":
                return []
            raise ValueError("Framework query failed")
        ids = [row["chunk_id"] for row in result["data"]["chunks"]]
        chunks = await rag.text_chunks.get_by_ids(ids)
        if any(chunk is None for chunk in chunks):
            raise ValueError("Missing framework page provenance")
        return list(zip(ids, chunks, strict=True))

    return runner.run(query()), tables


def page_sources(pilot, question, history, baseline, connection, key, model, transport, allowed=None, usage=None):
    if llm.connection(connection)["base_url"] != llm.PARATERA_BASE_URL:
        return [], {}, ["两篇试点文献的原页增强使用已授权的并行智算云连接。"], 0
    if allowed is not None and not allowed.intersection(PDF_REVISIONS):
        return [], {}, [], 0
    previous = [m["content"] for m in history if m["role"] == "user"][-1:]
    query = "\n".join([*previous, question])
    index = pilot / "framework/gao_adeia"
    ranked, tables = _WORKER.submit(_ranked_pages, index, query, connection, key, model, transport,
                                    usage if usage is not None else []).result()
    catalog = {p["doc_id"]: p for p in read_json(pilot / "catalog/papers.json")}
    candidates = defaultdict(list)
    for chunk_id, chunk in ranked:
        doc = chunk["full_doc_id"]
        start = chunk.get("page_idx")
        end = chunk.get("page_idx_end", start)
        end = start if end is None else end
        if (doc in PDF_REVISIONS and doc in catalog and (allowed is None or doc in allowed)
                and type(start) is int and type(end) is int):
            if 0 <= start <= end < catalog[doc]["page_count"]:
                for page in range(start + 1, end + 2):
                    candidates[doc, page].append(chunk_id)

    revisions, page_data, local_tables = {}, {}, defaultdict(list)
    warnings = []
    for doc in dict.fromkeys(d for d, _ in candidates):
        paper = catalog[doc]
        try:
            pdf = Path(paper["local_path"]).read_bytes()
        except OSError:
            warnings.append(f"《{paper['title']}》当前无法打开，已跳过该文的框架原页。")
            continue
        if paper["sha256"] != PDF_REVISIONS[doc] or hashlib.sha256(pdf).hexdigest() != PDF_REVISIONS[doc]:
            warnings.append(f"《{paper['title']}》版本已变更，本轮跳过其旧框架索引。")
            continue
        revisions[doc] = pdf
        page_data[doc] = {p["pdf_page"]: p for p in read_json(pilot / "parsed" / doc / "pages.json")}
        for table in read_json(pilot / "parsed" / doc / "tables.json"):
            if table.get("document_sha256") == PDF_REVISIONS[doc] and table.get("text"):
                local_tables[doc, table["pdf_page"]].append(table["text"])

    concepts = plan_query(question).concepts
    primary = {(s["doc_id"], s["pdf_page"]) for s in baseline}
    view = load_effective_evidence(pilot)
    pages = []
    for (doc, number), chunks in candidates.items():
        if doc not in revisions:
            continue
        raw = page_data[doc][number]
        body = raw["text"]
        grids = local_tables.get((doc, number)) or tables[doc].get(str(number), [])
        text = "\n\n".join([body, *grids]).strip()
        if not text:
            continue
        source = view.source({"doc_id": doc, "title": catalog[doc]["title"], "pdf_page": number,
                              "pdf_sha256": PDF_REVISIONS[doc], "text": text,
                              "evidence_id": f"{doc}:page:{number}", "framework_page": True,
                              "retrieval_reasons": [{"method": "raganything_mix", "chunks": chunks}]})
        if any(q.get("status") == "stale" for q in source.get(FIELD, [])):
            continue
        folded = text.casefold()
        relevance = sum(c.weight for c in concepts if any(term.casefold() in folded for term in c.phrases))
        # Supplement native order with query matches on the actual page; full
        # library evidence from other papers retains its own budget upstream.
        score = relevance + int((doc, number) in primary)
        uncertain = any(q.get("kind") in ("source_conflict", "transcription_correction")
                        for q in source.get(FIELD, []))
        needs_image = bool(grids or uncertain or body != raw.get("native_text", body)
                           or re.search(r"图中|图表|曲线|显微|figure|micrograph", question, re.I))
        pages.append((score, source, needs_image))
    pages.sort(key=lambda row: -row[0])
    images = {}
    for score, source, needed in pages:
        if needed and score > 0 and len(images) < 6:
            doc, number = source["doc_id"], source["pdf_page"]
            with pymupdf.open(stream=revisions[doc], filetype="pdf") as pdf:
                page = pdf[number - 1]
                scale = 1600 / max(page.rect.width, page.rect.height)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                data = pix.tobytes("jpeg", jpg_quality=88)
            images[(doc, number)] = base64.b64encode(data).decode("ascii")
    return [row[1] for row in pages], images, warnings, len(ranked)


def bind_quotes(content, result, sources):
    """Expose only short quotes witnessed on the page that the UI will open."""
    from kg_pilot.qa import QAFormatError
    clean = content.strip().lstrip("\ufeff").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.I)
    quotes = json.loads(clean).get("quotes", {})
    used = set(re.findall(r"\[(S\d+)\]", result["answer"]))

    def normalized(text):
        text = unicodedata.normalize("NFKC", text).casefold()
        return "".join(text.translate(str.maketrans("–—−", "---")).split())

    if re.search(r"(?:PDF\s*)?第\s*[0-9一二三四五六七八九十]+\s*页|\bPDF\s*(?:p(?:age)?\.?\s*)?\d+", result["answer"], re.I):
        raise QAFormatError("页码须由来源编号定位，请只在句末使用 [S编号]。", "manual_page")
    verified = {}
    for source in sources:
        if source["id"] not in used:
            continue
        value = quotes.get(source["id"]) if isinstance(quotes, dict) else None
        spans = [value] if isinstance(value, str) else value
        if (not isinstance(spans, list) or not 1 <= len(spans) <= 4 or
                any(not isinstance(q, str) or not 4 <= len(q.strip()) <= 350 or
                    normalized(q) not in normalized(source["text"]) for q in spans)):
            raise QAFormatError(f"{source['id']} 的短引未在该来源中匹配，请逐字复制同页连续短语。", "page_quote")
        verified[source["id"]] = "\n…\n".join(q.strip() for q in spans)
    for source in sources:
        if source["id"] in verified:
            source["verified_quote"] = verified[source["id"]]
