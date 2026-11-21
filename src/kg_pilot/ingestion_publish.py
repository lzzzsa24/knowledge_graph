"""Staged, recoverable publication of validated ingestion batches.

No provider credentials or APIs are accessed here. Embeddings default to the
locally cached BGE-M3 model; injected embedders are for isolated tests. Readers
must hold ``dataset_lock`` while accessing the catalog/indexes. The lock file is
permanent (never unlink it); its OS lock is automatically released on exit.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from kg_pilot.pipeline import PILOT, jsonl, normalize, read_json, write_json, write_jsonl
from kg_pilot.reading_sources import REVIEW_INPUTS, render_paper_reading_copy
from kg_pilot.validation import Claim, quote_supported

JOURNAL = "ingestion/publication_journal.json"
MAINTENANCE = "runs/ingestion_maintenance.json"
LOCK = "runs/ingestion_dataset.lock"
COLLECTION = "pilot_evidence_bge_m3"


class PublicationError(RuntimeError):
    pass


class DatasetBusy(PublicationError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextmanager
def dataset_lock(pilot: Path = PILOT, *, blocking: bool = False):
    """Cross-process exclusive OS lock; keep the lock file after releasing it."""
    path = Path(pilot) / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(stream.fileno(), mode, 1)
            except OSError:
                raise DatasetBusy("文献库正在读取或发布，请稍后重试。") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError:
                raise DatasetBusy("文献库正在读取或发布，请稍后重试。") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def maintenance_status(pilot: Path = PILOT) -> dict:
    path = Path(pilot) / MAINTENANCE
    return read_json(path) if path.exists() else {"active": False}


def _sha(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_id(value, field):
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value) or value.endswith(".") or value.split(".")[0].upper() in reserved:
        raise PublicationError(f"无效的 {field}")
    return value


def _doi(value):
    value = str(value or "").strip().lower()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)


def _load_optional(path, default):
    return read_json(path) if path.exists() else default


def _validate_prepared(prepared, pilot, old):
    _safe_id(prepared.get("batch_id"), "batch_id")
    documents = prepared.get("documents")
    if not isinstance(documents, list) or not documents:
        raise PublicationError("批次中没有已验证文献。")
    ids = {p["doc_id"].casefold() for p in old}
    dois = {_doi(p.get("doi")) for p in old if _doi(p.get("doi"))}
    hashes = {p["sha256"].lower() for p in old}
    evidence_ids, claim_ids = set(), set()
    for document in documents:
        paper = document["paper"]
        doc_id = _safe_id(paper.get("doc_id"), "doc_id")
        sha = str(paper.get("sha256", "")).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise PublicationError("文献缺少有效 SHA-256。")
        doi = _doi(paper.get("doi"))
        if doc_id.casefold() in ids or sha in hashes or (doi and doi in dois):
            raise PublicationError(f"文献重复，禁止覆盖：{doc_id}")
        if (pilot / "parsed" / doc_id).exists() or (pilot / "collection/batches" / prepared["batch_id"] / (doc_id + ".pdf")).exists():
            raise PublicationError(f"目标文献目录已存在：{doc_id}")
        ids.add(doc_id.casefold())
        hashes.add(sha)
        if doi:
            dois.add(doi)
        pdf = Path(paper["local_path"])
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf" or _sha(pdf) != sha:
            raise PublicationError(f"暂存 PDF 的 SHA-256 不符：{doc_id}")
        import pymupdf
        with pymupdf.open(pdf) as parsed:
            if parsed.page_count != paper["page_count"]:
                raise PublicationError(f"PDF 页数不符：{doc_id}")
        pages = document["pages"]
        by_page = {p["pdf_page"]: p for p in pages}
        if len(by_page) != len(pages) or set(by_page) != set(range(1, paper["page_count"] + 1)):
            raise PublicationError(f"解析页码不连续：{doc_id}")
        # Recheck even old prepared batches: a stored model review is not
        # authority to publish a corrupt unit, or to bypass a new parser gate.
        from kg_pilot.text_quality import repair_document_text, guard_claim_text_quality
        try:
            pages, elements, _, quality = repair_document_text(pdf, pages, document["elements"],
                                                               document_sha256=sha)
        except ValueError as error:
            raise PublicationError(str(error)) from error
        document.update(pages=pages, elements=elements, text_quality=quality)
        by_page = {p["pdf_page"]: p for p in pages}
        classification = document["classification"]
        if classification.get("doc_id") != doc_id or classification.get("source_sha256") != sha:
            raise PublicationError(f"分类来源不一致：{doc_id}")
        if classification.get("classification_review") not in {"api_reviewed", "api_model_reviewed", "independent_agent_reviewed"}:
            raise PublicationError(f"分类尚未复核：{doc_id}")
        local_elements = {}
        for item in document["elements"]:
            eid = item.get("evidence_id")
            if not isinstance(eid, str) or not eid.startswith(doc_id + ":") or eid in evidence_ids:
                raise PublicationError(f"证据 ID 重复或不属于文献：{doc_id}")
            page = item.get("pdf_page")
            if item.get("doc_id") != doc_id or item.get("document_sha256") != sha or page not in by_page:
                raise PublicationError(f"证据来源不一致：{eid}")
            if item.get("kind") == "table":
                from kg_pilot.table_evidence import validate_table_element
                try:
                    validate_table_element(item, by_page[page])
                except ValueError as error:
                    raise PublicationError(f"表格证据无效：{eid}（{error}）") from error
            else:
                if item.get("kind") != "text" or item.get("parser") not in {"pymupdf", "rapidocr"} or not isinstance(item.get("bbox"), list) or len(item["bbox"]) != 4:
                    raise PublicationError(f"证据格式不一致：{eid}")
                if item["parser"] != by_page[page].get("parser", "pymupdf"):
                    raise PublicationError(f"证据解析层与页文本来源不一致：{eid}")
            if not item.get("text") or item.get("normalized_text") != normalize(item["text"]):
                raise PublicationError(f"证据文本不一致：{eid}")
            if not quote_supported(item["text"], by_page[page]["text"]):
                raise PublicationError(f"证据文本不在解析页中：{eid}")
            evidence_ids.add(eid)
            local_elements[eid] = item
        if not local_elements:
            raise PublicationError(f"文献没有可发布的原文证据：{doc_id}")
        for claim in document["claims"]:
            Claim.model_validate(claim)
            try:
                guard_claim_text_quality(claim, pages=pages, elements=document["elements"])
            except ValueError as error:
                raise PublicationError(f"{claim.get('claim_id')}：{error}") from error
            cid = claim["claim_id"]
            if claim["doc_id"] != doc_id or cid in claim_ids or claim["review_status"] not in {"api_reviewed", "api_model_reviewed", "independent_agent_reviewed"}:
                raise PublicationError(f"事实 ID 或复核状态不一致：{cid}")
            claim_ids.add(cid)
            for reference in claim["evidence"]:
                source = local_elements.get(reference.get("evidence_id"))
                if source is None or reference["pdf_page"] != source["pdf_page"] or not quote_supported(reference["quote"], source["text"]):
                    raise PublicationError(f"事实引用无效：{cid}")


_SOURCE_FILES = ["catalog/papers.json", "catalog/classifications.json", "catalog/statistics.json",
                 "catalog/ingestion_readiness.json", "annotations/claims.jsonl", "parsed/evidence.jsonl",
                 "parsed/effective_evidence.jsonl", "benchmarks/embedding/all_records.json",
                 "benchmarks/embedding/all_vectors.npy", "model_config.json", "collection/batches/manifest.json"]


def _fingerprint(pilot, sources=None):
    """Hash publication inputs, optionally including backfill-specific files."""
    return {name: _sha(pilot / name) if (pilot / name).is_file() else None
            for name in (_SOURCE_FILES if sources is None else sources)}


def _ensure_idle(pilot):
    if maintenance_status(pilot).get("active"):
        raise DatasetBusy("发布恢复尚未完成，请先恢复文献库。")
    journal_path = pilot / JOURNAL
    if journal_path.exists() and read_json(journal_path).get("status") not in {"committed", "rolled_back"}:
        raise DatasetBusy("检测到未结束的发布日志，请先恢复文献库。")


def _already_published(pilot, prepared, papers):
    readiness = _load_optional(pilot / "catalog/ingestion_readiness.json", {"documents": {}}).get("documents", {})
    documents = prepared.get("documents")
    if not isinstance(documents, list) or not documents:
        return None
    batch_id = prepared["batch_id"]
    proposed = {d["paper"]["doc_id"]: d["paper"] for d in documents}
    published = {key for key, row in readiness.items() if row.get("batch_id") == batch_id}
    if len(proposed) != len(documents) or set(proposed) != published:
        return None
    catalog = {p["doc_id"]: p for p in papers}
    for doc_id, paper in proposed.items():
        row = readiness[doc_id]
        if catalog.get(doc_id, {}).get("sha256") != paper.get("sha256") or row.get("source_sha256") != paper.get("sha256") or not all(row.get(key) for key in ("keyword_ready", "semantic_ready", "graph_ready")):
            return None
    backup_path = ""
    for path in sorted((pilot / "ingestion/backups").glob("*/journal.json")):
        previous = read_json(path)
        if previous.get("batch_id") == batch_id and previous.get("status") == "committed":
            backup_path = str(path.parent)
            break
    vectors = _load_optional(pilot / "benchmarks/embedding/all_records.json", [])
    return {"status": "already_published", "already_published": True, "batch_id": batch_id,
            "added_documents": 0, "added_claims": 0, "added_evidence": 0,
            "vector_count": len({r["vector_id"] for r in vectors}), "backup_path": backup_path}


def _local_embedder(pilot):
    # Deliberately independent of qa._config(), provider keys and network access.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from sentence_transformers import SentenceTransformer
    config = read_json(pilot / "model_config.json")
    revision = config["revision"]
    snapshot = pilot / "model_cache/models--BAAI--bge-m3/snapshots" / revision
    model = SentenceTransformer(str(snapshot) if snapshot.is_dir() else "BAAI/bge-m3",
                                revision=revision, cache_folder=str(pilot / "model_cache"),
                                device="cpu", local_files_only=True)
    model.max_seq_length = 512
    return model


def _vectors(stage, pilot, evidence, new_ids, embedder, *, vector_source=None,
             new_evidence_ids=None):
    import numpy as np
    vector_source = vector_source or pilot
    old_records_path = vector_source / "benchmarks/embedding/all_records.json"
    old_vectors_path = vector_source / "benchmarks/embedding/all_vectors.npy"
    if old_records_path.exists() != old_vectors_path.exists():
        raise PublicationError("旧向量记录和矩阵不完整，未进行发布。")
    if old_records_path.exists():
        records = read_json(old_records_path)
        vectors = np.load(old_vectors_path, allow_pickle=False)
        if vectors.ndim != 2 or len(vectors) != len(records) or not np.isfinite(vectors).all():
            raise PublicationError("旧向量矩阵与记录不一致。")
        # Historical embedding exports may repeat exactly the same segment.
        # Match retrieval.build_vector_index's ID semantics without silently
        # choosing between conflicting vectors or payloads.
        keep, seen = [], {}
        for i, record in enumerate(records):
            previous = seen.get(record["vector_id"])
            if previous is not None:
                if record != records[previous] or not np.array_equal(vectors[i], vectors[previous]):
                    raise PublicationError("旧向量 ID 存在内容冲突。")
            else:
                seen[record["vector_id"]] = i
                keep.append(i)
        records = [records[i] for i in keep]
        vectors = vectors[keep]
        eligible = [e for e in evidence if
                    (e["evidence_id"] in new_evidence_ids if new_evidence_ids is not None
                     else e["doc_id"] in new_ids) and e["normalized_text"].strip()]
    else:
        records, vectors = [], None
        eligible = [e for e in evidence if e["normalized_text"].strip()]
    explicit = embedder is not None
    model = embedder if explicit else (_local_embedder(pilot) if eligible else None)
    from kg_pilot.embedding_chunks import source_chunks
    extra = []
    for item in eligible:
        text = item["normalized_text"]
        tokenizer = getattr(model, "tokenizer", None)
        if tokenizer is not None:
            chunks = source_chunks(tokenizer, text)
        else:
            # Only explicitly injected test embedders can lack the BGE tokenizer.
            if not explicit:
                raise PublicationError("本地 BGE-M3 缺少 tokenizer。")
            chunks = [text]
        extra.extend({"vector_id": item["evidence_id"] + f":seg{i}", "evidence_id": item["evidence_id"],
                      "doc_id": item["doc_id"], "pdf_page": item["pdf_page"], "embedding_text": part}
                     for i, part in enumerate(chunks))
    if extra:
        texts = [r["embedding_text"] for r in extra]
        encoded = model.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False) if hasattr(model, "encode") else model(texts)
        encoded = np.asarray(encoded, dtype=np.float32)
        if encoded.ndim != 2 or len(encoded) != len(extra) or not np.isfinite(encoded).all() or not (np.linalg.norm(encoded, axis=1) > 0).all():
            raise PublicationError("向量编码返回了无效矩阵。")
        if not explicit and encoded.shape[1] != 1024:
            raise PublicationError("真实 BGE-M3 向量维度必须为 1024。")
        if vectors is not None and vectors.shape[1] != encoded.shape[1]:
            raise PublicationError("新增向量维度与旧库不同。")
        vectors = encoded if vectors is None else np.concatenate([vectors, encoded])
    if vectors is None:
        vectors = np.empty((0, 1024), dtype=np.float32)
    records.extend(extra)
    if len({r["vector_id"] for r in records}) != len(records):
        raise PublicationError("向量 ID 重复。")
    by_evidence = {e["evidence_id"]: e for e in evidence}
    for record in records:
        source = by_evidence.get(record["evidence_id"])
        if source is None or record["doc_id"] != source["doc_id"] or record["pdf_page"] != source["pdf_page"]:
            raise PublicationError("向量指向不存在或不一致的原文证据。")
    out = stage / "benchmarks/embedding"
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "all_records.json", records)
    write_json(out / "all_ids.json", [r["evidence_id"] for r in records])
    np.save(out / "all_vectors.npy", vectors)
    return len(records)


def _build_indexes(stage):
    import networkx as nx
    import numpy as np
    from qdrant_client import QdrantClient, models
    papers = read_json(stage / "catalog/papers.json")
    claims = jsonl(stage / "annotations/claims.jsonl")
    evidence = jsonl(stage / "parsed/effective_evidence.jsonl")
    out = stage / "indexes"
    out.mkdir(parents=True, exist_ok=True)
    graph = nx.MultiDiGraph()
    with closing(sqlite3.connect(out / "pilot.sqlite")) as db:
        db.executescript("""
            CREATE TABLE papers(doc_id TEXT PRIMARY KEY,title TEXT,doi TEXT,metadata TEXT);
            CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY,doc_id TEXT,pdf_page INTEGER,text TEXT,bbox TEXT);
            CREATE TABLE claims(claim_id TEXT PRIMARY KEY,doc_id TEXT,payload TEXT);
            CREATE TABLE graph_nodes(node_id TEXT PRIMARY KEY,kind TEXT,attributes TEXT);
            CREATE TABLE graph_edges(edge_id TEXT PRIMARY KEY,source TEXT,target TEXT,kind TEXT,attributes TEXT);
            CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED,doc_id UNINDEXED,text,tokenize='unicode61');
            CREATE VIRTUAL TABLE claim_fts USING fts5(claim_id UNINDEXED,text,tokenize='porter unicode61');
        """)
        for p in papers:
            db.execute("INSERT INTO papers VALUES (?,?,?,?)", (p["doc_id"], p["title"], p.get("doi"), json.dumps(p, ensure_ascii=False)))
            graph.add_node(p["doc_id"], kind="document", title=p["title"])
        for e in evidence:
            db.execute("INSERT INTO evidence VALUES (?,?,?,?,?)", (e["evidence_id"], e["doc_id"], e["pdf_page"], e["text"], json.dumps(e["bbox"])))
            db.execute("INSERT INTO evidence_fts VALUES (?,?,?)", (e["evidence_id"], e["doc_id"], e["normalized_text"]))
        for c in claims:
            db.execute("INSERT INTO claims VALUES (?,?,?)", (c["claim_id"], c["doc_id"], json.dumps(c, ensure_ascii=False)))
            text = " ".join([c["statement"], c["material_system"], c["stage"].replace("_", " "), c["quantity"].replace("_", " "), json.dumps(c["conditions"], ensure_ascii=False), " ".join(e["quote"] for e in c["evidence"])])
            db.execute("INSERT INTO claim_fts VALUES (?,?)", (c["claim_id"], text))
            graph.add_node(c["claim_id"], kind="claim", statement=c["statement"], stage=c["stage"])
            graph.add_edge(c["claim_id"], c["doc_id"], kind="reported_in")
            sample = c["doc_id"] + ":sample:" + c["sample_id"]
            graph.add_node(sample, kind="sample", material=c["material_system"])
            graph.add_edge(c["claim_id"], sample, kind="about_sample")
            for ev in c["evidence"]:
                eid = ev.get("evidence_id") or f"{c['doc_id']}:p{ev['pdf_page']}"
                graph.add_node(eid, kind="evidence", pdf_page=ev["pdf_page"])
                graph.add_edge(c["claim_id"], eid, kind="supported_by")
        for node, attrs in graph.nodes(data=True):
            db.execute("INSERT INTO graph_nodes VALUES (?,?,?)", (node, attrs["kind"], json.dumps(attrs, ensure_ascii=False)))
        for i, (a, b, attrs) in enumerate(graph.edges(data=True)):
            db.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?)", (f"edge_{i}", a, b, attrs["kind"], json.dumps(attrs, ensure_ascii=False)))
        db.commit()
    nx.write_graphml(graph, out / "pilot.graphml")
    records = read_json(stage / "benchmarks/embedding/all_records.json")
    vectors = np.load(stage / "benchmarks/embedding/all_vectors.npy", allow_pickle=False)
    with closing(QdrantClient(path=str(out / "qdrant"))) as client:
        client.create_collection(COLLECTION, vectors_config=models.VectorParams(size=vectors.shape[1], distance=models.Distance.COSINE))
        for offset in range(0, len(records), 128):
            points = [models.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, r["vector_id"])), vector=v.tolist(), payload=r)
                      for r, v in zip(records[offset:offset + 128], vectors[offset:offset + 128], strict=True)]
            client.upsert(COLLECTION, points=points)
        if client.count(COLLECTION, exact=True).count != len(records):
            raise PublicationError("暂存向量索引计数不符。")


def _validate_stage(stage, *, quality_doc_ids=None):
    import networkx as nx
    from qdrant_client import QdrantClient
    papers = read_json(stage / "catalog/papers.json")
    evidence = jsonl(stage / "parsed/effective_evidence.jsonl")
    claims = jsonl(stage / "annotations/claims.jsonl")
    ids = {p["doc_id"] for p in papers}
    quality_scope = ids if quality_doc_ids is None else set(quality_doc_ids)
    if isinstance(quality_doc_ids, str) or not quality_scope.issubset(ids):
        raise PublicationError("文本质量校验范围含未知文献。")
    eids = {e["evidence_id"]: e for e in evidence}
    if len(ids) != len(papers) or len(eids) != len(evidence) or len({c["claim_id"] for c in claims}) != len(claims):
        raise PublicationError("合并后的库存在重复主键。")
    if any(e["doc_id"] not in ids for e in evidence) or any(c["doc_id"] not in ids for c in claims):
        raise PublicationError("索引存在未知文献引用。")
    for c in claims:
        from kg_pilot.text_quality import guard_claim_text_quality
        if c["doc_id"] in quality_scope:
            try:
                guard_claim_text_quality(c)
            except ValueError as error:
                raise PublicationError(f"{c.get('claim_id')}：{error}") from error
        for ref in c["evidence"]:
            eid = ref.get("evidence_id")
            if eid and (eid not in eids or eids[eid]["doc_id"] != c["doc_id"] or eids[eid]["pdf_page"] != ref["pdf_page"]):
                raise PublicationError("索引存在无效事实证据引用。")
    with closing(sqlite3.connect(stage / "indexes/pilot.sqlite")) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PublicationError("暂存 SQLite 完整性校验失败。")
        for table, expected in [("papers", len(papers)), ("evidence", len(evidence)), ("claims", len(claims)), ("evidence_fts", len(evidence)), ("claim_fts", len(claims))]:
            if db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != expected:
                raise PublicationError(f"暂存索引计数不符：{table}")
        expected_papers = {p["doc_id"]: p for p in papers}
        for doc_id, metadata in db.execute("SELECT doc_id,metadata FROM papers"):
            if json.loads(metadata) != expected_papers.get(doc_id):
                raise PublicationError("暂存 SQLite 文献内容不符。")
        for eid, doc_id, page, text, bbox in db.execute("SELECT * FROM evidence"):
            expected = eids.get(eid)
            if expected is None or (doc_id, page, text, json.loads(bbox)) != (expected["doc_id"], expected["pdf_page"], expected["text"], expected["bbox"]):
                raise PublicationError("暂存 SQLite 原文内容不符。")
        expected_claims = {c["claim_id"]: c for c in claims}
        for claim_id, payload in db.execute("SELECT claim_id,payload FROM claims"):
            if json.loads(payload) != expected_claims.get(claim_id):
                raise PublicationError("暂存 SQLite 事实内容不符。")
    graph = nx.read_graphml(stage / "indexes/pilot.graphml")
    if not ids.issubset(graph.nodes) or not {c["claim_id"] for c in claims}.issubset(graph.nodes):
        raise PublicationError("暂存图索引缺少文献或事实。")
    graph_links = {(a, b, attrs.get("kind")) for a, b, attrs in graph.edges(data=True)}
    for c in claims:
        if (c["claim_id"], c["doc_id"], "reported_in") not in graph_links:
            raise PublicationError("暂存图索引缺少文献来源链接。")
        for ref in c["evidence"]:
            eid = ref.get("evidence_id") or f"{c['doc_id']}:p{ref['pdf_page']}"
            if (c["claim_id"], eid, "supported_by") not in graph_links:
                raise PublicationError("暂存图索引缺少证据链接。")
    records = read_json(stage / "benchmarks/embedding/all_records.json")
    with closing(QdrantClient(path=str(stage / "indexes/qdrant"))) as client:
        if client.count(COLLECTION, exact=True).count != len(records):
            raise PublicationError("暂存 Qdrant 计数校验失败。")
        expected_vectors = {str(uuid.uuid5(uuid.NAMESPACE_URL, r["vector_id"])): r for r in records}
        offset = None
        while True:
            points, offset = client.scroll(COLLECTION, limit=256, offset=offset, with_payload=True, with_vectors=False)
            for point in points:
                if point.payload != expected_vectors.get(str(point.id)):
                    raise PublicationError("暂存 Qdrant 证据引用或原文内容不符。")
            if offset is None:
                break


def _copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _inside(pilot, relative):
    path = (pilot / relative).resolve()
    if not path.is_relative_to(pilot.resolve()) or path == pilot.resolve():
        raise PublicationError("发布日志包含越界路径。")
    return path


def _restore(pilot, journal):
    """Restore from retained originals; each step is restartable after a crash."""
    backup = _inside(pilot, journal["backup_relative"])
    journal["status"] = "recovering"
    _atomic_json(pilot / JOURNAL, journal)
    for entry in reversed(journal["targets"]):
        relative = entry["relative"]
        target = _inside(pilot, relative)
        original = backup / "original" / relative
        if not original.exists() and entry["existed"]:
            # This target had not yet moved when the process stopped.
            continue
        if target.exists():
            discarded = backup / "failed_outputs" / uuid.uuid4().hex / relative
            discarded.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, discarded)
        if entry["existed"]:
            restore = target.with_name(target.name + ".restore-" + uuid.uuid4().hex)
            _copy(original, restore)
            os.replace(restore, target)
    journal["status"] = "rolled_back"
    journal["recovered_at"] = _now()
    _atomic_json(pilot / JOURNAL, journal)
    _atomic_json(backup / "journal.json", journal)
    (pilot / MAINTENANCE).unlink(missing_ok=True)


def recover_publication(pilot: Path = PILOT, *, expected_batch_id: str | None = None) -> dict:
    """Recover a journal; an expected owner constrains MCP recovery to one batch.

    Ownership is checked while holding the dataset lock and before removing any
    maintenance marker or restoring files. The original global CLI operation
    remains available by omitting ``expected_batch_id``.
    """
    pilot = Path(pilot)
    with dataset_lock(pilot):
        path = pilot / JOURNAL
        if not path.exists():
            if (pilot / MAINTENANCE).exists():
                raise PublicationError("存在维护标记但缺少恢复日志；请检查备份后人工恢复。")
            return {"status": "idle"}
        journal = read_json(path)
        if expected_batch_id is not None:
            if journal.get("batch_id") != expected_batch_id:
                raise PublicationError("发布恢复日志属于其他批次；请通过该批次原入口恢复，当前操作未修改资料。")
            marker = pilot / MAINTENANCE
            if marker.exists() and read_json(marker).get("batch_id") != expected_batch_id:
                raise PublicationError("文献库维护标记属于其他批次；请通过该批次原入口恢复，当前操作未修改资料。")
        if journal["status"] in {"committed", "rolled_back"}:
            (pilot / MAINTENANCE).unlink(missing_ok=True)
            return {"status": journal["status"], "batch_id": journal["batch_id"]}
        try:
            _restore(pilot, journal)
        except Exception:
            journal["status"] = "recovery_failed"
            _atomic_json(path, journal)
            raise PublicationError("自动恢复未完成；旧版本仍保存在备份中，维护状态保留。") from None
        return {"status": "rolled_back", "batch_id": journal["batch_id"], "backup_path": str(pilot / journal["backup_relative"])}


def _commit(stage, pilot, targets, source_fingerprint, batch_id):
    backup_relative = "ingestion/backups/" + batch_id + "-" + uuid.uuid4().hex
    backup = pilot / backup_relative
    with dataset_lock(pilot):
        _ensure_idle(pilot)
        if _fingerprint(pilot, source_fingerprint) != source_fingerprint:
            raise DatasetBusy("准备期间文献库已变化，请重新发布批次。")
        for relative in targets:
            is_new = (relative.parts[:2] == ("collection", "batches") and relative.name != "manifest.json") or (relative.parts[0] == "parsed" and len(relative.parts) == 2 and relative.suffix == "") or relative.parts[:2] == ("catalog", "classification_batches")
            if is_new and (pilot / relative).exists():
                raise PublicationError("发布目标已存在，禁止覆盖新文献目录。")
        journal = {"version": 1, "status": "publishing", "batch_id": batch_id, "created_at": _now(),
                   "backup_relative": backup_relative,
                   "targets": [{"relative": str(p).replace("\\", "/"), "existed": (pilot / p).exists()} for p in targets]}
        backup.mkdir(parents=True)
        _atomic_json(pilot / JOURNAL, journal)
        _atomic_json(backup / "journal.json", journal)
        _atomic_json(pilot / MAINTENANCE, {"active": True, "batch_id": batch_id, "started_at": _now()})
        try:
            for entry in journal["targets"]:
                relative = entry["relative"]
                target = _inside(pilot, relative)
                original = backup / "original" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if entry["existed"]:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, original)
                os.replace(stage / relative, target)
            journal["status"] = "committed"
            journal["committed_at"] = _now()
            _atomic_json(pilot / JOURNAL, journal)
            _atomic_json(backup / "journal.json", journal)
            (pilot / MAINTENANCE).unlink(missing_ok=True)
        except Exception:
            try:
                _restore(pilot, journal)
            except Exception:
                journal["status"] = "recovery_failed"
                _atomic_json(pilot / JOURNAL, journal)
                raise PublicationError("发布失败且恢复未完成；文献库保持维护状态，可重试恢复。") from None
            raise PublicationError("发布失败，已恢复原库；暂存及备份均保留。") from None
    return backup


def publish_batch(batch_dir: Path, pilot: Path = PILOT, *, embedder=None, index_builder=None) -> dict:
    """Publish a root-validated prepared.json without replacing existing documents.

    ``index_builder(stage)`` optionally replaces the default isolated index
    build. Its output must pass the same SQLite/GraphML/Qdrant validation.
    """
    batch_dir, pilot = Path(batch_dir).resolve(), Path(pilot).resolve()
    prepared = read_json(batch_dir / "prepared.json")
    batch_id = _safe_id(prepared.get("batch_id"), "batch_id")
    stage = pilot / "ingestion/staging" / (batch_id + "-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        with dataset_lock(pilot):
            _ensure_idle(pilot)
            fingerprint = _fingerprint(pilot, list(_SOURCE_FILES) + list(REVIEW_INPUTS))
            papers = _load_optional(pilot / "catalog/papers.json", [])
            completed = _already_published(pilot, prepared, papers)
            if completed:
                return completed
            _validate_prepared(prepared, pilot, papers)
            classifications = _load_optional(pilot / "catalog/classifications.json", {"version": "1.0", "records": []})
            claims = jsonl(pilot / "annotations/claims.jsonl")
            native = jsonl(pilot / "parsed/evidence.jsonl")
            effective_path = pilot / "parsed/effective_evidence.jsonl"
            effective = jsonl(effective_path) if effective_path.exists() else list(native)
            stats = _load_optional(pilot / "catalog/statistics.json", {})
            readiness = _load_optional(pilot / "catalog/ingestion_readiness.json", {"version": 1, "documents": {}})
            manifest = _load_optional(pilot / "collection/batches/manifest.json", {"papers": []})
            if isinstance(manifest, list):
                manifest = {"papers": manifest}
            if not isinstance(manifest.get("papers"), list):
                raise PublicationError("批量入库原始 manifest 格式无效。")
            # Preserve existing embedding trial artifacts when replacing the directory.
            embedding_dir = pilot / "benchmarks/embedding"
            if embedding_dir.exists():
                _copy(embedding_dir, stage / "benchmarks/embedding")
            # Freeze reusable vectors, never read a later live version mid-build.
            for relative in ("benchmarks/embedding/all_records.json", "benchmarks/embedding/all_vectors.npy"):
                if (pilot / relative).exists():
                    _copy(pilot / relative, stage / "source" / relative)
        old_claim_ids = {c["claim_id"] for c in claims}
        old_evidence_ids = {e["evidence_id"] for e in native + effective}
        new_ids = {d["paper"]["doc_id"] for d in prepared["documents"]}
        targets = [Path(p) for p in ["catalog/papers.json", "catalog/classifications.json", "catalog/statistics.json", "catalog/ingestion_readiness.json", "annotations/claims.jsonl", "parsed/evidence.jsonl", "parsed/effective_evidence.jsonl", "indexes", "benchmarks/embedding", "collection/batches/manifest.json"]]
        classification_rel = Path("catalog/classification_batches") / (batch_id + ".json")
        targets.append(classification_rel)
        added_claims = added_evidence = 0
        for document in prepared["documents"]:
            paper = dict(document["paper"])
            doc_id = paper["doc_id"]
            for c in document["claims"]:
                if c["claim_id"] in old_claim_ids:
                    raise PublicationError("新增事实 ID 与旧库冲突。")
            if any(e["evidence_id"] in old_evidence_ids for e in document["elements"]):
                raise PublicationError("新增证据 ID 与旧库冲突。")
            pdf_rel = Path("collection/batches") / batch_id / (doc_id + ".pdf")
            _copy(Path(paper["local_path"]), stage / pdf_rel)
            if _sha(stage / pdf_rel) != paper["sha256"]:
                raise PublicationError("暂存 PDF 在复制期间变化。")
            paper["local_path"] = str(pilot / pdf_rel)
            papers.append(paper)
            # Legacy pipeline.assemble reads ``id`` rather than ``doc_id``.
            # Persist both so a future collection rebuild retains this identity.
            manifest["papers"].append({**paper, "id": doc_id})
            parsed_rel = Path("parsed") / doc_id
            write_json(stage / parsed_rel / "pages.json", document["pages"])
            write_jsonl(stage / parsed_rel / "elements.jsonl", document["elements"])
            write_json(stage / parsed_rel / "text_quality.json", document["text_quality"])
            quarantined = document.get("quarantined_claims", [])
            write_json(stage / parsed_rel / "text_quality_quarantine.json", dict(doc_id=doc_id,
                source_sha256=paper["sha256"], quarantined_claims=len(quarantined), records=quarantined,
                status="pending_source_review" if quarantined else "clear"))
            tables = [element for element in document["elements"] if element["kind"] == "table"]
            write_json(stage / parsed_rel / "tables.json", tables)
            (stage / parsed_rel / "fulltext.md").write_text(
                render_paper_reading_copy(pilot, paper, document["pages"], document["elements"],
                                         markdown_path=pilot / parsed_rel / "fulltext.md",
                                         tables=tables, source_pdf=stage / pdf_rel),
                encoding="utf-8", newline="\n")
            targets.extend([pdf_rel, parsed_rel])
            classifications["records"].append(document["classification"])
            readiness["documents"][doc_id] = {"source_sha256": paper["sha256"], "batch_id": batch_id, "keyword_ready": True, "semantic_ready": True, "graph_ready": True,
                "text_quality_status": document["text_quality"]["status"],
                "quarantined_claims": len(quarantined),
                "unresolved_critical": document["text_quality"]["unresolved_critical"]}
            claims.extend(document["claims"])
            native.extend(document["elements"])
            effective.extend(document["elements"])
            added_claims += len(document["claims"])
            added_evidence += len(document["elements"])
        stats.update(unique_papers=len(papers), pages=sum(p["page_count"] for p in papers), reviews=sum(bool(p.get("is_review")) for p in papers), scope_counts=dict(Counter(str(p.get("scope", "needs_scope_review")) for p in papers)))
        write_json(stage / "catalog/papers.json", papers)
        write_json(stage / "catalog/classifications.json", classifications)
        write_json(stage / "catalog/statistics.json", stats)
        write_json(stage / "catalog/ingestion_readiness.json", readiness)
        write_json(stage / classification_rel, {"version": classifications.get("version", "1.0"), "records": [d["classification"] for d in prepared["documents"]]})
        write_json(stage / "collection/batches/manifest.json", manifest)
        write_jsonl(stage / "annotations/claims.jsonl", claims)
        write_jsonl(stage / "parsed/evidence.jsonl", native)
        write_jsonl(stage / "parsed/effective_evidence.jsonl", effective)
        vector_count = _vectors(stage, pilot, effective, new_ids, embedder, vector_source=stage / "source")
        (index_builder or _build_indexes)(stage)
        _validate_stage(stage, quality_doc_ids=new_ids)
        backup = _commit(stage, pilot, targets, fingerprint, batch_id)
        result = {"status": "published", "batch_id": batch_id, "added_documents": len(new_ids), "added_claims": added_claims, "added_evidence": added_evidence, "vector_count": vector_count, "backup_path": str(backup)}
        _atomic_json(stage / "publication_result.json", result)
        return result
    except Exception as error:
        _atomic_json(stage / "publication_failure.json", {"batch_id": batch_id, "status": "failed", "error_type": type(error).__name__, "time": _now()})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["publish", "recover", "status"])
    parser.add_argument("batch_dir", nargs="?")
    parser.add_argument("--pilot", type=Path, default=PILOT)
    args = parser.parse_args(argv)
    if args.action == "publish":
        if not args.batch_dir:
            parser.error("publish requires batch_dir")
        result = publish_batch(Path(args.batch_dir), args.pilot)
    elif args.action == "recover":
        result = recover_publication(args.pilot)
    else:
        result = maintenance_status(args.pilot)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
