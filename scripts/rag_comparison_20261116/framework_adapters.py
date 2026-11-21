"""Controlled text/table adapters using the installed LightRAG and RAGAnything."""
from __future__ import annotations

import atexit
from dataclasses import asdict, dataclass, replace
import importlib.metadata
import json
import logging
import re
import shutil

from .common import BudgetExceeded, Evidence, sha256_file, sha256_text, write_json


DELIMITER = "\n[[BENCHMARK_CHUNK_BREAK]]\n"
EMBEDDING_CHUNK_TOKENS = 480
QUERY_PARAMETERS = {
    "mode": "mix", "top_k": 20, "chunk_top_k": 14, "max_total_tokens": 14000,
    "max_entity_tokens": 2500, "max_relation_tokens": 3500, "enable_rerank": False,
    "include_references": True, "only_need_context": True, "stream": False,
}
FRAMEWORK_SETTINGS = {
    "kv_storage": "JsonKVStorage", "vector_storage": "NanoVectorDBStorage",
    "graph_storage": "NetworkXStorage", "doc_status_storage": "JsonDocStatusStorage",
    "entity_extract_max_gleaning": 0, "force_llm_summary_on_merge": 100000,
    "summary_context_size": 12000, "max_total_tokens": 14000,
    "cosine_better_than_threshold": 0.20, "max_parallel_insert": 1,
    "llm_model_max_async": 1, "default_llm_timeout": 530,
    "embedding_func_max_async": 1, "embedding_batch_num": 8, "default_embedding_timeout": 600,
    "enable_llm_cache": True, "enable_llm_cache_for_entity_extract": True,
    "addon_params": {"language": "Chinese"},
}


@dataclass(frozen=True)
class Chunk:
    content: str
    records: tuple[Evidence, ...]


def _record_text(record):
    return f"p{record.page} [{record.kind}] {record.text}"


def plan_chunks(corpus, tokenizer):
    """Deterministically pack page-labelled fragments under the BGE embedding limit."""
    per_doc = {doc: [] for doc in corpus.documents}
    for record in corpus.records.values():
        per_doc[record.doc_id].append(record)

    def fits(text):
        return len(tokenizer.encode(text, add_special_tokens=True, verbose=False)) <= EMBEDDING_CHUNK_TOKENS

    result = {}
    for doc, records in per_doc.items():
        title = corpus.documents[doc]["title"]
        if not title or not isinstance(title, str):
            raise ValueError(f"Document title missing for {doc}.")
        header = f"{doc} | {title}\n"
        if not fits(header):
            raise ValueError(f"Document title too long for {doc}.")
        chunks, group, content = [], [], header
        for item in sorted(records, key=lambda r: r.page):
            text = item.text
            while text:
                fragment = replace(item, text=text)
                segment = _record_text(fragment)
                separator = "\n" if group else ""
                if fits(content + separator + segment):
                    group.append(fragment)
                    content += separator + segment
                    break
                if group:
                    # Start the record intact in an empty chunk when possible.
                    chunks.append(Chunk(content, tuple(group)))
                    content, group = header, []
                    continue
                low, high = 0, len(text)
                while low + 1 < high:
                    cut = (low + high) // 2
                    stub = replace(item, text=text[:cut])
                    if fits(header + _record_text(stub)):
                        low = cut
                    else:
                        high = cut
                if not low:
                    raise ValueError(f"Could not fit any text for {item.evidence_id}.")
                last_space = max(text.rfind(" ", 0, low), text.rfind("\n", 0, low))
                cut = last_space if last_space > low * 0.9 else low
                fragment = replace(item, text=text[:cut].strip())
                chunks.append(Chunk(header + _record_text(fragment), (fragment,)))
                text = text[cut:].strip()
        if group:
            chunks.append(Chunk(content, tuple(group)))
        if not chunks or any(DELIMITER in chunk.content for chunk in chunks):
            raise ValueError(f"Chunk preparation failed for {doc}.")
        result[doc] = chunks
    return result


def merge_native_duplicates(planned):
    """Match LightRAG's content identity while retaining all frozen source locators."""
    from lightrag.utils import sanitize_text_for_encoding

    merged = {}
    for doc_id, chunks in planned.items():
        unique = {}
        for chunk in chunks:
            normalized = sanitize_text_for_encoding(chunk.content)
            prior = unique.get(normalized)
            if prior is None:
                unique[normalized] = chunk
            else:
                unique[normalized] = replace(prior, records=tuple(dict.fromkeys(prior.records + chunk.records)))
        merged[doc_id] = list(unique.values())
    return merged


def split_planned_chunks(tokenizer, content, split_by_character, split_by_character_only,
                         overlap_token_size, chunk_token_size):
    if split_by_character != DELIMITER or not split_by_character_only:
        raise ValueError("Framework must preserve the frozen chunk boundaries.")
    segments = [text.strip() for text in content.split(DELIMITER)]
    return [{"content": text, "tokens": len(tokenizer.encode(text)), "chunk_order_index": index}
            for index, text in enumerate(filter(None, segments))]


def native_text_chunks(context):
    """Read only LightRAG's document chunk section, never its generated graph text."""
    if not isinstance(context, str):
        raise TypeError("The text query unexpectedly returned a stream.")
    found = re.search(r"(?m)^Document Chunks[^\n]*:\s*\n```json\n(.*?)\n```\n",
                      context, flags=re.S)
    if not found:
        if context.strip():
            raise ValueError("Native context returned no recognizable document chunk section.")
        return []
    return [json.loads(line)["content"] for line in found.group(1).splitlines() if line.strip()]


def native_structured_chunks(native):
    if not isinstance(native, dict) or not native:
        raise ValueError("LightRAG returned empty structured metadata, possibly from keyword extraction.")
    if native.get("status") == "success":
        return [row["content"] for row in native["data"]["chunks"]]
    if native.get("metadata", {}).get("failure_reason") == "no_results":
        return []
    raise ValueError("LightRAG retrieval failed. See the retained native response.")


class FrameworkAdapter:
    def __init__(self, corpus, runtime, output_dir, planned, *, checkpoint=None, resume_from=None):
        if checkpoint and resume_from:
            raise ValueError("Checkpoint query clone and construction recovery are separate operations.")
        self.corpus, self.runtime, self.out = corpus, runtime, output_dir
        self.planned, self.checkpoint = planned, checkpoint
        self.resume_from = resume_from
        self.workspace = "controlled_raganything" if checkpoint else "controlled_lightrag"
        self.storage = self.out / "storage"
        self.metadata = {
            "ingestion": ("Pre-query LightRAG checkpoint cloned; independent building not measured"
                          if checkpoint else "LightRAG.ainsert with native LLM entity/relation extraction"),
            "retrieval": ("RAGAnything.aquery (text, only_need_context)" if checkpoint else "LightRAG.aquery_data"),
            "storage": FRAMEWORK_SETTINGS,
            "query_parameters": QUERY_PARAMETERS,
            "chunk_bge_token_limit": EMBEDDING_CHUNK_TOKENS,
            "native_multimodal_processing": False,
            "final_context": "Original text/table/claim/qualification fragments from native ranked source chunks.",
        }
        self.rag = None
        self.app = None
        self._sources = {}
        self._query_number = 0
        self._handler = None

    async def build(self):
        from lightrag import LightRAG
        from lightrag.utils import EmbeddingFunc, compute_mdhash_id, sanitize_text_for_encoding

        if importlib.metadata.version("lightrag-hku") != "1.4.16":
            raise ValueError("Controlled pre-run requires LightRAG 1.4.16.")
        self.out.mkdir(exist_ok=True, parents=True)
        if self.checkpoint:
            if importlib.metadata.version("raganything") != "1.4.4":
                raise ValueError("Controlled pre-run requires RAGAnything 1.4.4.")
        source_checkpoint = self.checkpoint or self.resume_from
        if source_checkpoint:
            destination = self.storage / self.workspace
            source_hashes = {p.name: sha256_file(p) for p in source_checkpoint.iterdir() if p.is_file()}
            shutil.copytree(source_checkpoint, destination)
            if source_hashes != {p.name: sha256_file(p) for p in destination.iterdir() if p.is_file()}:
                raise ValueError("Framework checkpoint clone hash mismatch.")
            self.metadata["checkpoint_sha256"] = source_hashes
            self.metadata["checkpoint_source"] = str(source_checkpoint)
        self._handler = logging.FileHandler(self.out / "framework.log", encoding="utf8")
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("lightrag").addHandler(self._handler)
        logging.getLogger("raganything").addHandler(self._handler)

        # LightRAG snapshots its dataclass configuration via asdict(). A
        # bound method would make deepcopy traverse the runtime's locks and
        # loaded model. Plain forwarding functions preserve shared ownership.
        async def embed(texts, **kwargs):
            return await self.runtime.embed(texts, **kwargs)

        async def complete(prompt, system_prompt=None, history_messages=None, **kwargs):
            return await self.runtime.complete(prompt, system_prompt=system_prompt,
                                               history_messages=history_messages, **kwargs)

        self.rag = LightRAG(
            working_dir=str(self.storage), workspace=self.workspace,
            embedding_func=EmbeddingFunc(embedding_dim=1024, max_token_size=512,
                                         model_name="BAAI/bge-m3", func=embed),
            llm_model_func=complete, llm_model_name=self.runtime.model_config["model"],
            tiktoken_model_name="gpt-4o", chunking_func=split_planned_chunks,
            **FRAMEWORK_SETTINGS,
        )
        await self.rag.initialize_storages()
        if self.checkpoint:
            from raganything import RAGAnything, RAGAnythingConfig

            config = RAGAnythingConfig(
                working_dir=str(self.storage), parser_output_dir=str(self.out / "parser"),
                parser="mineru", enable_image_processing=False, enable_table_processing=False,
                enable_equation_processing=False, enable_audio_processing=False,
                enable_video_processing=False, display_content_stats=False,
            )
            self.app = RAGAnything(lightrag=self.rag, config=config)
        packed = {doc: [{"chunk_id": compute_mdhash_id(sanitize_text_for_encoding(chunk.content), prefix="chunk-"),
                         "sha256": sha256_text(chunk.content),
                         "records": [asdict(r) for r in chunk.records]}
                        for chunk in chunks] for doc, chunks in self.planned.items()}
        write_json(self.out / "chunk_plan.json", packed)
        self.metadata["chunk_counts"] = {doc: len(chunks) for doc, chunks in self.planned.items()}
        for chunks in self.planned.values():
            for chunk in chunks:
                content = sanitize_text_for_encoding(chunk.content).strip()
                if content in self._sources:
                    raise ValueError("Ambiguous preplanned chunk content.")
                self._sources[content] = chunk.records
        for doc, chunks in self.planned.items():
            expected_ids = {row["chunk_id"] for row in packed[doc]}
            prior = await self.rag.doc_status.get_by_id(doc) if self.resume_from else None
            if prior:
                if set(prior.get("chunks_list") or []) != expected_ids:
                    raise RuntimeError(f"Recovered chunk IDs differ from the frozen plan for {doc}.")
                if prior.get("status") == "processed":
                    self.runtime.event("framework_document_reused", doc_id=doc, planned_chunks=len(chunks))
                    print(f"{self.workspace} {doc} {len(chunks)} chunks reused", flush=True)
                    continue
                if prior.get("status") != "failed":
                    raise RuntimeError(f"Unexpected recovered native status for {doc}.")
                await self.rag.apipeline_process_enqueue_documents(
                    split_by_character=DELIMITER, split_by_character_only=True,
                )
            elif self.checkpoint is None:
                await self.rag.ainsert(DELIMITER.join(c.content for c in chunks),
                                       split_by_character=DELIMITER, split_by_character_only=True,
                                       ids=doc, file_paths=f"{doc}.pdf")
            if self.runtime.stopped:
                raise BudgetExceeded("Pre-run API budget reached during native indexing.")
            state = await self.rag.doc_status.get_by_id(doc)
            if not state or state.get("status") != "processed":
                raise RuntimeError(f"Native indexing failed for {doc}: {state.get('error_msg') if state else 'missing status'}.")
            if set(state.get("chunks_list") or []) != expected_ids:
                raise RuntimeError(f"Native chunk IDs differ from the frozen plan for {doc}.")
            self.runtime.event("framework_document_ready", doc_id=doc, planned_chunks=len(chunks))
            print(f"{self.workspace} {doc} {len(chunks)} chunks ready", flush=True)
        graph = self.storage / self.workspace / "graph_chunk_entity_relation.graphml"
        if not graph.is_file():
            raise RuntimeError("Framework did not persist a graph.")
        import networkx as nx

        counts = nx.read_graphml(graph)
        self.metadata["graph"] = {"nodes": counts.number_of_nodes(), "edges": counts.number_of_edges()}
        if not counts.number_of_nodes() or not counts.number_of_edges():
            raise RuntimeError("Framework persisted an empty graph.")

    async def retrieve(self, question):
        from lightrag import QueryParam

        self._query_number += 1
        raw_path = self.out / "native" / f"{self._query_number:02d}.json"
        if self.app is None:
            native = await self.rag.aquery_data(question, param=QueryParam(**QUERY_PARAMETERS))
            write_json(raw_path, native)
            if self.runtime.stopped:
                raise BudgetExceeded("Pre-run API budget reached during LightRAG retrieval.")
            texts = native_structured_chunks(native)
        else:
            native = await self.app.aquery(question, vlm_enhanced=False, **QUERY_PARAMETERS)
            write_json(raw_path, {"native_context": native})
            from lightrag.prompt import PROMPTS

            if self.runtime.stopped:
                raise BudgetExceeded("Pre-run API budget reached during RAGAnything retrieval.")
            if not isinstance(native, str) or not native.strip() or native == PROMPTS["fail_response"]:
                raise ValueError("RAGAnything returned its failure sentinel without native cause metadata.")
            texts = native_text_chunks(native)
        result = []
        for text in texts:
            if text not in self._sources:
                raise ValueError(f"Framework returned unrecognized chunk {sha256_text(text)}.")
            result.extend(self._sources[text])
        return result

    async def close(self):
        if self.app is not None:
            await self.app.finalize_storages()
            atexit.unregister(self.app.close)
        elif self.rag is not None:
            await self.rag.finalize_storages()
        if self._handler is not None:
            logging.getLogger("lightrag").removeHandler(self._handler)
            logging.getLogger("raganything").removeHandler(self._handler)
            self._handler.close()
