"""Auditable text-only RAG-Anything/LightRAG trial with a live session-agent bridge.

No LLM API, fake embeddings, or canned answer implementation is used. While this
script runs, a session agent reads each bridge/*.request.json and writes the
corresponding *.response.json with {"content": "...", "provider":
"session_agent_bridge"}. This deliberately is not an unattended service.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "pilot"


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    temporary.replace(path)


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf8").splitlines() if line.strip()]


class SessionAgentBridge:
    def __init__(self, out: Path, timeout=1200):
        self.out = out
        self.timeout = timeout
        self.count = 0
        self.current_query = None
        self.calls = []

    async def __call__(self, prompt, system_prompt=None, history_messages=None, **kwargs):
        self.count += 1
        key = f"{self.count:03d}"
        started = time.perf_counter()
        request = {
            "call_id": key,
            "provider": "session_agent_bridge",
            "query_id": self.current_query,
            "purpose": "keyword_extraction" if kwargs.get("keyword_extraction") else "answer_generation",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "prompt": prompt,
            "system_prompt": system_prompt,
            "history_messages": history_messages or [],
            "kwargs": {k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool, type(None)))},
            "protocol": "A live session agent must read this exact request and author the response; no precomputed answer selection.",
        }
        request_path = self.out / "bridge" / f"{key}.request.json"
        response_path = self.out / "bridge" / f"{key}.response.json"
        write_json(request_path, request)
        if system_prompt and "<retrieved_context>" in system_prompt:
            context = system_prompt.split("<retrieved_context>", 1)[1].split("</retrieved_context>", 1)[0].strip()
            write_json(self.out / "queries" / f"{self.current_query}.retrieved_context.json", {
                "query_id": self.current_query, "bridge_call_id": key, "context": context,
                "source": "actual LightRAG context inserted into answer system prompt",
            })
        print(f"BRIDGE_PENDING {request_path}", flush=True)
        while not response_path.exists():
            if time.perf_counter() - started > self.timeout:
                raise TimeoutError(f"No session-agent response for {request_path}")
            await asyncio.sleep(0.5)
        response = json.loads(response_path.read_text(encoding="utf8"))
        if response.get("provider") != "session_agent_bridge" or not isinstance(response.get("content"), str):
            raise ValueError("Bridge response requires provider=session_agent_bridge and string content")
        elapsed = time.perf_counter() - started
        record = {"call_id": key, "query_id": self.current_query, "purpose": request["purpose"],
                  "elapsed_seconds_including_agent_and_orchestration": round(elapsed, 3),
                  "request_path": str(request_path), "response_path": str(response_path)}
        self.calls.append(record)
        write_json(self.out / "bridge_calls.json", self.calls)
        return response["content"]


def make_custom_kg(out: Path):
    chosen = {f"j{i:03d}" for i in list(range(1, 7)) + list(range(19, 31))}
    source = PILOT / "annotations" / "claims_journals.jsonl"
    candidates = [c for c in read_jsonl(source) if c["claim_id"] in chosen]
    assert len(candidates) == 18
    # This frozen input is explicitly pending-review, independently of later master updates.
    write_json(out / "candidate_snapshot.json", {
        "status": "agent-extracted candidate snapshot, not an independently validated final knowledge base",
        "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "claims": candidates,
    })
    papers = json.loads((PILOT / "catalog" / "papers.json").read_text(encoding="utf8"))
    paper_lookup = {p.get("id", p.get("doc_id")): p for p in papers}
    kg = {"chunks": [], "entities": [], "relationships": []}
    paper_entities = {}
    for c in candidates:
        cid, doc = c["claim_id"], c["doc_id"]
        title = paper_lookup.get(doc, {}).get("title", doc)
        pdf = str(PILOT / "collection" / "journals" / (doc.removeprefix("journals_") + ".pdf"))
        pages = sorted({e["pdf_page"] for e in c["evidence"]})
        filepath = pdf + "#page=" + str(pages[0])
        summary = (f"Candidate claim {cid}; paper {doc}; title {title}. "
                   f"Material system: {c['material_system']}. Sample: {c['sample_id']}. "
                   f"Stage: {c['stage']}; claim type: {c['claim_type']}. "
                   f"{c['statement']} Quantity: {c['quantity']}; value: {c['value_text']}; unit: {c['unit']}. "
                   f"Conditions: {json.dumps(c['conditions'], ensure_ascii=False)}. Physical PDF pages: {pages}.")
        content = summary + "\nSource quotations:\n" + "\n".join(
            f"[physical PDF page {e['pdf_page']}] {e['quote']}" for e in c["evidence"])
        kg["chunks"].append({"content": content, "source_id": cid, "file_path": filepath})
        claim_node = "CLAIM:" + cid
        paper_node = "PAPER:" + doc
        kg["entities"].append({"entity_name": claim_node, "entity_type": "CONDITION_BOUND_CLAIM",
            "description": summary, "source_id": cid, "file_path": filepath})
        paper_entities.setdefault(doc, {"entity_name": paper_node, "entity_type": "PAPER",
            "description": f"{title}; document_id={doc}; candidate claims must retain their own sample and processing conditions.",
            "source_id": cid, "file_path": filepath})
        kg["relationships"].append({"src_id": paper_node, "tgt_id": claim_node,
            "description": summary, "keywords": f"reported claim, {c['quantity']}, {c['material_system']}, {c['stage']}",
            "weight": 1.0, "source_id": cid, "file_path": filepath})
    kg["entities"].extend(paper_entities.values())
    write_json(out / "custom_kg.json", kg)
    return kg


async def run(out: Path):
    out.mkdir(parents=True, exist_ok=False)
    os.environ["HF_HOME"] = str(PILOT / "model_cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from huggingface_hub import file_download
    file_download.are_symlinks_supported = lambda cache_dir=None: False
    import torch
    torch.set_num_threads(4)
    from sentence_transformers import SentenceTransformer
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc, setup_logger
    from raganything import RAGAnything, RAGAnythingConfig

    setup_logger("lightrag", log_file_path=str(out / "lightrag.log"))
    started = time.perf_counter()
    report = {
        "run_started_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running", "llm_provider": "session_agent_bridge",
        "versions": {p: importlib.metadata.version(p) for p in ["raganything", "lightrag-hku", "sentence-transformers", "torch"]},
        "scope": "18 pending-review agent claims; custom-KG import; actual RAGAnything.aquery text path; no PDF or multimodal ingestion",
        "storage": {"kv": "JsonKVStorage", "vector": "NanoVectorDBStorage", "graph": "NetworkXStorage", "doc_status": "JsonDocStatusStorage"},
        "llm_service_limit": "Requires live session-agent file responses. No independently running LLM API service tested.",
        "embedding": {"model": "BAAI/bge-m3", "device": "CPU", "dimension": 1024, "max_seq_length": 512, "normalize": True, "batch_size": 8, "threads": 4},
        "query_mode": "mix", "reranker": None, "query_results": [],
        "sources": ["https://github.com/HKUDS/LightRAG", "https://github.com/HKUDS/RAG-Anything"],
    }
    write_json(out / "report.json", report)
    bridge = SessionAgentBridge(out)
    rag = None
    embedding_events = []
    try:
        model = SentenceTransformer("BAAI/bge-m3", cache_folder=str(PILOT / "model_cache"), device="cpu", local_files_only=True)
        model.max_seq_length = 512
        report["model_load_seconds"] = round(time.perf_counter() - started, 3)
        embedding_lock = asyncio.Lock()

        async def embed(texts, **kwargs):
            async with embedding_lock:
                t0 = time.perf_counter()
                vectors = await asyncio.to_thread(model.encode, list(texts), batch_size=8,
                    normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
                assert vectors.shape == (len(texts), 1024)
                embedding_events.append({"count": len(texts), "seconds": round(time.perf_counter() - t0, 3),
                    "input_sha256": [hashlib.sha256(t.encode("utf8")).hexdigest() for t in texts]})
                write_json(out / "embedding_calls.json", embedding_events)
                return vectors

        rag = LightRAG(working_dir=str(out / "storage"),
            embedding_func=EmbeddingFunc(embedding_dim=1024, max_token_size=512, model_name="BAAI/bge-m3", func=embed),
            llm_model_func=bridge, llm_model_name="session_agent_bridge", llm_model_max_async=1,
            default_llm_timeout=1250, default_embedding_timeout=600,
            embedding_func_max_async=1, embedding_batch_num=8,
            top_k=8, chunk_top_k=8, max_total_tokens=14000,
            max_entity_tokens=3500, max_relation_tokens=4500,
            enable_llm_cache=True, cosine_better_than_threshold=0.20,
            addon_params={"language":"Chinese"})
        await rag.initialize_storages()
        custom = make_custom_kg(out)
        insert_start = time.perf_counter()
        await rag.ainsert_custom_kg(custom)
        report["import"] = {"claims":18, "nodes_expected":len(custom['entities']), "edges_expected":len(custom['relationships']),
            "chunks_expected":len(custom['chunks']), "elapsed_seconds":round(time.perf_counter()-insert_start, 3),
            "condition_preservation":"Every claim has its own node; unique paper-to-claim edges avoid undirected endpoint-pair deduplication across conditions."}
        app = RAGAnything(lightrag=rag, config=RAGAnythingConfig(working_dir=str(out / "storage"),
            enable_image_processing=False, enable_table_processing=False, enable_equation_processing=False,
            enable_audio_processing=False, enable_video_processing=False))
        system_prompt = (
            "你是混合键合文献试点的证据问答助手。只依据下面实际检索上下文回答，"
            "不要使用未检索的常识补足论文事实。材料、样品、温度、时间、测试指标必须保持对应；"
            "区分面积比例、电学良率、组件测试和完整接头测试。上下文来自待独立审核候选快照，"
            "回答需说明该限制。找不到足够证据时明确说明不足，不从短期观察外推长期寿命。"
            "回答用中文，并在具体事实后引用 claim_id、doc_id 和物理PDF页码。"
            "不要把本会话的文件桥接描述为独立部署的API服务。\n"
            "<retrieved_context>\n{context_data}\n</retrieved_context>"
        )
        questions = [
            {"id":"q1", "expectation":"supported", "question":"(111)取向纳米孪晶Cu/SiO2研究的200 °C键合与后退火分别持续多久？交叉条Kelvin结构的比接触电阻是多少？"},
            {"id":"q2", "expectation":"supported_with_comparison_caveat", "question":"Ti钝化Cu/介质混合键合的电学良率比未钝化组高多少？两组退火条件相同吗，能否据此得出等热预算下的改善幅度？"},
            {"id":"q3", "expectation":"insufficient_evidence", "question":"Cu/MoS2/Cu与SiCN混合键合是否已经证明在150 °C连续服役10年可靠？请给出有统计置信度的寿命保证。"},
        ]
        write_json(out / "questions.json", questions)
        write_json(out / "report.json", report)
        for question in questions:
            bridge.current_query = question["id"]
            query_start = time.perf_counter()
            answer = await app.aquery(question["question"], mode="mix", system_prompt=system_prompt,
                enable_rerank=False, include_references=True, stream=False,
                top_k=8, chunk_top_k=8, max_total_tokens=14000,
                max_entity_tokens=3500, max_relation_tokens=4500)
            record = {**question, "answer":answer, "wall_seconds_including_live_agent":round(time.perf_counter()-query_start,3),
                "llm_provider":"session_agent_bridge", "api_tested":False}
            write_json(out / "queries" / f"{question['id']}.result.json", record)
            report["query_results"].append(record)
            write_json(out / "report.json", report)
            print(f"QUERY_COMPLETE {question['id']}",flush=True)
        report["status"] = "completed"
        report["bridge_calls"] = bridge.calls
        report["total_wall_seconds_including_live_agent"] = round(time.perf_counter()-started,3)
        report["embedding_seconds"] = round(sum(x["seconds"] for x in embedding_events),3)
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)
        report["traceback"] = traceback.format_exc()
        raise
    finally:
        if rag is not None:
            await rag.finalize_storages()
        write_json(out / "report.json", report)
        print(json.dumps({"status":report['status'],"report":str(out/'report.json')},ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-name',default=datetime.now().strftime('%Y%m%d_%H%M%S'))
    args=parser.parse_args()
    if Path(args.run_name).name != args.run_name:
        raise ValueError('run-name must be a single directory name')
    asyncio.run(run(PILOT/'benchmarks'/'framework'/args.run_name))


if __name__=='__main__':
    main()
