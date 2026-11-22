"""Local JSON API and static web frontend for the hybrid-bonding pilot.

Serves the same pilot data as app.py (catalog, agent-reviewed claims, persisted graph,
keyword/semantic retrieval, PDF page renders) to the browser UI in web/static,
with a separate resumable PDF ingestion workflow and atomic publication.
"""
from __future__ import annotations

import json
import gzip
import hashlib
import re
import sqlite3
from contextlib import closing
import sys
import threading
import base64
import asyncio
import weakref
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pymupdf
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from urllib.parse import urlsplit
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "pilot"
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot.retrieval import is_crosslingual_query, lexical, retrieval_model as semantic_model, retrieve, semantic  # noqa: E402
from kg_pilot.qa import QAError, answer_question, configure_provider, provider_status, subscription_status  # noqa: E402
from kg_pilot.innovation import generate_ideas, preview_ideas  # noqa: E402
from kg_pilot.diagnostics import list_runs  # noqa: E402
from kg_pilot.classification import classified_papers, taxonomy, filter_papers, FACET_IDS  # noqa: E402
from kg_pilot.effective_evidence import FIELD, STALE_TEXT, load_effective_evidence  # noqa: E402
from kg_pilot.bilingual_retrieval import plan_query as plan_research_terms  # noqa: E402
from kg_pilot.research_map import build_research_map  # noqa: E402
from kg_pilot.map_errata import apply_errata  # noqa: E402
from kg_pilot.research_network import build_network  # noqa: E402
from kg_pilot.research_graph import build_research_graph  # noqa: E402
from kg_pilot.map_coverage import load_coverage_reviews  # noqa: E402
from kg_pilot.paper_metadata import apply_paper_metadata  # noqa: E402
from kg_pilot.map_citations import citations as read_citations  # noqa: E402
from kg_pilot.map_text_leads import FACTORS, OUTCOMES, text_leads  # noqa: E402
from kg_pilot.map_semantics import apply_semantic_reviews  # noqa: E402
from kg_pilot.map_sample_bindings import apply_sample_bindings  # noqa: E402
from kg_pilot.map_source_kinds import load_source_kind_reviews  # noqa: E402
from kg_pilot.map_relations import load_mapping_reviews, source_readings  # noqa: E402
from kg_pilot.research_notes import attach_screened_directions, research_notes as read_research_notes  # noqa: E402
from kg_pilot.reading_sources import qualify_reading_copy  # noqa: E402
from web.research_cache import ALGORITHM as MAP_ALGORITHM, DATA_FILES as MAP_INPUTS, digest_files  # noqa: E402
from web.markdown_reader import render_markdown  # noqa: E402
from web import native  # noqa: E402

DATABASE = PILOT / "indexes" / "pilot.sqlite"

app = FastAPI(title="混合键合知识图谱试点", docs_url="/api/docs", redoc_url=None)
_READ_GATES = weakref.WeakKeyDictionary()
_STARTED_MONOTONIC = time.monotonic()


@app.middleware("http")
async def protect_dataset_during_publish(request: Request, call_next):
    # A publication lock serializes short dataset switches with readers in this
    # process and CLI workers. Intake control stays available for recovery.
    # Liveness and in-memory progress must remain readable during a long model
    # call. These endpoints do not query SQLite or open a vector-store client.
    if request.url.path in {"/api/debug/health", "/api/debug/ideas/runs", "/api/chat/status", "/api/chat/configure",
                            "/api/chat/subscription/configure"} or request.url.path.startswith("/api/native/"):
        return await call_next(request)
    if request.url.path.startswith("/api/ingestion"):
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "请求长度无效。"})
        if size > 90 * 1024 ** 2:
            return JSONResponse(status_code=413, content={"detail": "单批上传内容过大。"})
        return await call_next(request)
    if request.url.path.startswith("/api/") and request.url.path != "/api/docs":
        from kg_pilot.ingestion_publish import dataset_lock, DatasetBusy, maintenance_status
        if maintenance_status(PILOT).get("active"):
            return JSONResponse(status_code=503, content={"detail": "资料索引正在切换或等待恢复，请稍后重试。"})
        loop = asyncio.get_running_loop()
        gate = _READ_GATES.setdefault(loop, asyncio.Lock())
        async with gate:
            try:
                with dataset_lock(PILOT, blocking=False):
                    if maintenance_status(PILOT).get("active"):
                        return JSONResponse(status_code=503, content={"detail": "资料库等待恢复，请前往批量入库页面。"})
                    return await call_next(request)
            except DatasetBusy:
                return JSONResponse(status_code=503, content={"detail": "资料库正在更新，请稍后重试。"})
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    # Validation errors must never echo submitted API credentials back to a client.
    return JSONResponse(status_code=422, content={"detail": "请求格式或长度不符合要求，请检查输入。"})


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def connect():
    return closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True))


def load_papers() -> list[dict]:
    papers = read_json(PILOT / "catalog" / "papers.json")
    if not isinstance(papers, list):
        raise HTTPException(503, "试点目录尚未就绪，请先运行项目数据流程。")
    return papers


def load_claims() -> list[dict]:
    path = PILOT / "annotations" / "claims.jsonl"
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        raise HTTPException(503, "知识记录正在更新或暂时不可读取，请稍后刷新。")


def paper_by_id(doc_id: str) -> dict:
    for paper in load_papers():
        if paper["doc_id"] == doc_id:
            return paper
    raise HTTPException(404, f"未知文献：{doc_id}")


def local_pdf(paper: dict) -> Path | None:
    """Rebase catalog paths when the project is moved; serve only project files."""
    raw = str(paper.get("local_path") or "").replace("\\", "/")
    match = re.search(r"(?:^|/)pilot/", raw, flags=re.IGNORECASE)
    path = ROOT / (raw[match.start():].lstrip("/") if match else raw)
    path = path.resolve()
    if not path.is_relative_to(ROOT) or not path.is_file() or path.suffix.lower() != ".pdf":
        return None
    return path


def local_markdown(paper: dict) -> Path | None:
    """Locate only this catalog paper's reading copy, inside the parsed tree."""
    doc_id = str(paper.get("doc_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", doc_id):
        return None
    parsed = (PILOT / "parsed").resolve()
    path = (parsed / doc_id / "fulltext.md").resolve()
    if not path.is_relative_to(parsed) or not path.is_file():
        return None
    return path


def public_paper(paper: dict) -> dict:
    # Absolute local paths and hashes stay on the server; the UI only needs metadata.
    keys = ("doc_id", "title", "doi", "year", "authors", "topic", "relevance_reason", "source_type",
            "is_review", "page_count", "publisher_url", "pdf_url", "language", "scope", "publisher",
            "document_number", "version", "status", "source_url", "accessed_at", "organization", "short_label")
    return {k: paper.get(k) for k in keys} | {"has_pdf": local_pdf(paper) is not None,
                                           "has_markdown": local_markdown(paper) is not None}


@app.get("/api/summary")
def summary():
    papers, claims = load_papers(), load_claims()
    return {
        "papers": len(papers),
        "claims": len(claims),
        "pages": sum(p.get("page_count", 0) for p in papers),
        "reviews": sum(bool(p.get("is_review")) for p in papers),
        "samples": len({(c["doc_id"], c.get("sample_id")) for c in claims}),
        "stages": Counter(c["stage"] for c in claims),
        "claim_types": Counter(c["claim_type"] for c in claims),
        "years": Counter(p.get("year") for p in papers if p.get("year")),
        "validation": {"accepted_claims": len(claims), "accepted_documents": len({c["doc_id"] for c in claims}),
                       "review_status_counts": Counter(c.get("review_status", "unknown") for c in claims),
                       "label": "automatic_review_not_expert_gold",
                       "original_pilot_audit": read_json(PILOT / "reports" / "claim_validation_summary.json", {})},
    }


def classified_catalog():
    try:
        records = load_claims()
        return classified_papers(load_papers(), records, pilot=PILOT), records
    except (ValueError, KeyError, OSError):
        raise HTTPException(503, "文献分类暂时不可读取，请稍后刷新。") from None


@app.get("/api/taxonomy")
def paper_taxonomy():
    rows, _ = classified_catalog()
    return taxonomy(rows)


@app.get("/api/papers")
def papers(request: Request):
    rows, records = classified_catalog()
    rows = apply_paper_metadata(rows, PILOT)
    counts = Counter(c["doc_id"] for c in records)
    params = request.query_params
    year = params.get("year")
    if year and (not year.isdecimal() or len(year) > 4):
        raise HTTPException(422, "发表年份格式不正确。")
    rows = filter_papers(rows, categories=params.getlist("category"),
                         include_related=params.get("include_related", "false").lower() in ("true", "1"),
                         facets={key: params.getlist(key) for key in FACET_IDS},
                         query=params.get("q", ""), year=int(year) if year else None)
    view = load_effective_evidence(PILOT, papers=rows, claims=records)
    return [public_paper(p) | {"claim_count": counts[p["doc_id"]],
                               "classification": view.anchors(p["classification"], doc_id=p["doc_id"])} for p in rows]


@app.get("/api/claims")
def claims():
    records = load_claims()
    try:
        view = load_effective_evidence(PILOT, papers=load_papers(), claims=records)
        return [view.claim(record) for record in records]
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "源证据复核状态暂时无法读取。") from None


def research_map(route: Literal["W2W", "D2W", "D2D"] | None = None, system: str | None = None, *,
                 sample_release_path: Path | None = None, errata_path: Path | None = None):
    """Build a read-only projection of the protected current source snapshot."""
    if system and not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", system):
        raise HTTPException(422, "材料体系标识无效。")
    records = load_claims()
    try:
        rows = classified_papers(load_papers(), records, pilot=PILOT)
        scoped = [p for p in rows if not route or route in p.get("classification", {}).get("routes", [])]
        links, withheld, audit = load_mapping_reviews(PILOT, scoped, records)
        links, withheld, semantic_audit = apply_semantic_reviews(PILOT,scoped,records,links,withheld)
        audit["semantics"] = semantic_audit
        kinds = load_source_kind_reviews(PILOT / "research_map/source_kind_reviews.json")
        links, sample_audit = apply_sample_bindings(PILOT,scoped,records,links,withheld,kind_reviews=kinds,
                                                   release_path=sample_release_path)
        audit["sample_bindings"] = sample_audit
        links, withheld, audit["source_errata"] = apply_errata(
            errata_path or PILOT / "research_map/source_errata.json", scoped, records, links, withheld)
        coverage, audit["coverage"] = load_coverage_reviews(PILOT, scoped, records)
        result = build_research_map(rows, records, route=route, system=system, reviewed=links, withheld=withheld,
                                    kind_reviews=kinds, coverage_reviewed=coverage)
        result["relation_audit"] = audit
        result["source_readings"] = source_readings(PILOT,scoped,records)
        result = attach_screened_directions(result, read_research_notes(PILOT, rows, records), route)
        view = load_effective_evidence(PILOT, papers=scoped, claims=records)
        for detail in result["record_details"].values():
            projected = view.claim(detail)
            if FIELD in projected:
                for key in (FIELD, "sample_id", "conditions", "value_text", "unit", "evidence"):
                    detail[key] = projected[key]
        result[FIELD] = {doc_id: qualifications for paper in scoped if
                         (qualifications := view.qualifications(doc_id := paper["doc_id"]))}
        return view.anchors(result)
    except (ValueError, KeyError, OSError):
        raise HTTPException(503, "研究地图资料暂时不可读取，请稍后刷新。") from None


class _MapRevisionChanged(Exception):
    pass


@lru_cache(maxsize=6)
def _map_response_bytes(location, revision, route, system):
    value = research_map(route,system)
    if revision != digest_files(Path(location),MAP_INPUTS):
        raise _MapRevisionChanged
    value["snapshot_id"] = revision
    return json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()


@app.get("/api/research-map")
def research_map_http(request: Request, route: Literal["W2W","D2W","D2D"] | None = None, system: str | None = None):
    if system and not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", system):
        raise HTTPException(422,"材料体系标识无效。")
    for _ in range(2):
        revision = digest_files(PILOT,MAP_INPUTS)
        etag = f'"{MAP_ALGORITHM[:16]}.{revision}.{route or "-"}.{system or "-"}"'
        headers = {"Cache-Control":"private, no-cache","ETag":etag}
        if etag in [tag.strip() for tag in request.headers.get("if-none-match","").split(",")]:
            return Response(status_code=304,headers=headers)
        try:
            body = _map_response_bytes(str(PILOT.resolve()),revision,route,system)
        except _MapRevisionChanged:
            continue
        return Response(body,media_type="application/json",headers=headers)
    raise HTTPException(503,"研究地图正在切换复核结果，请稍后刷新。")


@app.get("/api/research-terms")
def research_terms(q: str = Query(min_length=1,max_length=500)):
    plan = plan_research_terms(q)
    return {"concepts":[{"name":c.name,"phrases":list(c.phrases),"weight":c.weight} for c in plan.concepts]}


@app.get("/api/research-text-leads")
def research_text_leads(row: str, column: str, route: Literal["W2W","D2W","D2D"] | None = None,
                        system: str | None = None):
    if row not in FACTORS or column not in OUTCOMES or (system and not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", system)):
        raise HTTPException(422, "请选择地图已有的工艺、结果及材料体系。")
    papers, records = classified_catalog()
    try:
        return text_leads(PILOT, papers, records, row, column, route, system, limit=24)
    except (OSError, sqlite3.Error, ValueError):
        raise HTTPException(503, "正文索引暂时不可读取，请稍后重试。") from None


@app.get("/api/research-citations")
def research_citations():
    try:
        return read_citations(PILOT,load_papers())
    except (ValueError,KeyError,OSError):
        raise HTTPException(503,"引文索引暂时不可读取，请稍后刷新。") from None


NETWORK_INPUTS = (*MAP_INPUTS, "research_map/citation_edges.json", "research_map/citation_year_evidence.json")


@lru_cache(maxsize=2)
def _network_bytes(location, revision):
    papers, claims = classified_catalog()
    matrix = json.loads(_map_response_bytes(location,digest_files(PILOT,MAP_INPUTS),None,None))
    data = build_network(papers,taxonomy(papers)["categories"],matrix,read_citations(PILOT,papers),read_research_notes(PILOT,papers,claims))
    view = load_effective_evidence(PILOT, papers=papers, claims=claims)
    data = view.anchors(data)
    data[FIELD] = matrix.get(FIELD, {})
    if digest_files(PILOT,NETWORK_INPUTS) != revision:
        raise _MapRevisionChanged
    data["snapshot_id"] = hashlib.sha256(f"{MAP_ALGORITHM}.{GRAPH_ALGORITHM}.{revision}".encode()).hexdigest()
    return json.dumps(data,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()


@app.get("/api/research-network")
def research_network():
    for _ in range(2):
        try:
            body = _network_bytes(str(PILOT.resolve()),digest_files(PILOT,NETWORK_INPUTS))
            return Response(body,media_type="application/json",headers={"Cache-Control":"private, no-cache"})
        except _MapRevisionChanged:
            continue
    raise HTTPException(503,"研究网络正在切换资料版本，请稍后刷新。")


@app.get("/api/research-network/links/{link_id}")
def research_network_link(link_id: str, snapshot_id: str | None = Query(default=None, max_length=128)):
    data = json.loads(research_network().body)
    if snapshot_id is not None and snapshot_id != data["snapshot_id"]:
        raise HTTPException(409, "这条研究联系的依据版本已更新，请刷新研究地图。")
    for link in data["links"]:
        if link["id"] == link_id:
            return {**link, "snapshot_id": data["snapshot_id"]}
    raise HTTPException(404, "这条研究联系已不存在，请刷新研究地图。")


GRAPH_ALGORITHM = digest_files(ROOT, ("src/kg_pilot/research_graph.py", "src/kg_pilot/research_network.py"))


@lru_cache(maxsize=8)
def _research_graph_bytes(location, revision, route):
    matrix = json.loads(_map_response_bytes(location, digest_files(PILOT, MAP_INPUTS), route, None))
    network = json.loads(_network_bytes(location, revision))
    rows, _ = classified_catalog()
    result = build_research_graph(matrix, network, rows, route=route)
    # The overview stays lightweight; full summaries and quotes belong to the
    # matrix and per-link evidence responses.
    result[FIELD] = {doc_id: [{"id": q["id"], "kind": q["kind"], "status": q["status"], "pdf_pages": q["pdf_pages"]}
                             for q in qualifications]
                     for doc_id, qualifications in matrix.get(FIELD, {}).items()}
    if revision != digest_files(PILOT, NETWORK_INPUTS):
        raise _MapRevisionChanged
    result["snapshot_id"] = hashlib.sha256(f"{MAP_ALGORITHM}.{GRAPH_ALGORITHM}.{revision}.{route or '-'}".encode()).hexdigest()
    raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return raw, gzip.compress(raw, compresslevel=6, mtime=0), result["snapshot_id"]


def _accepts_gzip(header: str) -> bool:
    qualities = {}
    for item in header.lower().split(","):
        parts = item.strip().split(";")
        coding = parts[0].strip()
        if not coding:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        qualities[coding] = quality
    return qualities.get("gzip", qualities.get("*", 0)) > 0


@app.get("/api/research-graph")
def research_graph_http(request: Request, route: Literal["W2W", "D2W", "D2D"] | None = None):
    for _ in range(2):
        revision = digest_files(PILOT, NETWORK_INPUTS)
        try:
            raw, compressed, snapshot = _research_graph_bytes(str(PILOT.resolve()), revision, route)
        except _MapRevisionChanged:
            continue
        encoded = _accepts_gzip(request.headers.get("accept-encoding", ""))
        etag = f'"research-graph.{snapshot}.{"gzip" if encoded else "identity"}"'
        headers = {"Cache-Control": "private, no-cache", "ETag": etag, "Vary": "Accept-Encoding"}
        tags = [tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")]
        if etag in tags or "*" in tags:
            return Response(status_code=304, headers=headers)
        if encoded:
            headers["Content-Encoding"] = "gzip"
        return Response(compressed if encoded else raw, media_type="application/json", headers=headers)
    raise HTTPException(503, "研究地图正在切换资料版本，请稍后刷新。")


@app.get("/api/research-notes")
def research_notes():
    for _ in range(2):
        revision = digest_files(PILOT, MAP_INPUTS)
        try:
            matrix = json.loads(_map_response_bytes(str(PILOT.resolve()), revision, None, None))
            papers, records = load_papers(), load_claims()
            data = read_research_notes(PILOT, papers, records)
            data = load_effective_evidence(PILOT, papers=papers, claims=records).anchors(data)
            if revision != digest_files(PILOT, MAP_INPUTS):
                continue
            data["study_connections"] = matrix["study_connections"]
            return data
        except _MapRevisionChanged:
            continue
        except (ValueError, KeyError, OSError):
            raise HTTPException(503, "研究对照与方向正在更新，请稍后刷新。") from None
    raise HTTPException(503, "研究对照与方向正在更新，请稍后刷新。")


@app.get("/api/graph")
def graph(evidence: bool = False):
    """Persisted NetworkX graph (graph_nodes / graph_edges) written by pipeline.index."""
    with connect() as db:
        nodes = [{"id": n, "kind": k, **json.loads(a)} for n, k, a in
                 db.execute("SELECT node_id,kind,attributes FROM graph_nodes")]
        edges = [{"source": s, "target": t, "kind": k} for s, t, k in
                 db.execute("SELECT source,target,kind FROM graph_edges")]
    records = load_claims()
    view = load_effective_evidence(PILOT, papers=load_papers(), claims=records)
    qualified = {c["claim_id"]: projected for c in records if FIELD in (projected := view.claim(c))}
    masked_samples = {}
    for record in records:
        claim = qualified.get(record["claim_id"], record)
        if claim["statement"] == STALE_TEXT:
            sample = f"{record['doc_id']}:sample:{record['sample_id']}"
            masked_samples[sample] = record["doc_id"] + ":sample:review-required-" + hashlib.sha256(sample.encode()).hexdigest()
    for node in nodes:
        claim = qualified.get(node["id"])
        if claim and node["kind"] == "claim":
            node.update(statement=claim["statement"], evidence_qualifications=claim[FIELD])
        elif node["kind"] == "sample" and node["id"] in masked_samples:
            node["id"] = masked_samples[node["id"]]
    if masked_samples:
        for edge in edges:
            for endpoint in ("source", "target"):
                edge[endpoint] = masked_samples.get(edge[endpoint], edge[endpoint])
    if not evidence:
        nodes = [n for n in nodes if n["kind"] != "evidence"]
        edges = [e for e in edges if e["kind"] != "supported_by"]
    return {"nodes": nodes, "edges": edges}


@lru_cache(maxsize=64)
def render_page(path: str, modified_ns: int, page: int) -> bytes:
    # The modification timestamp invalidates cached renderings after PDF replacement.
    with pymupdf.open(path) as document:
        if not 1 <= page <= len(document):
            raise ValueError("原文页码超出 PDF 范围")
        return document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6)).tobytes("png")


def check_pdf_revision(path: Path, revision: str | None):
    if revision is not None:
        with path.open("rb") as pdf:
            if hashlib.file_digest(pdf, "sha256").hexdigest() != revision:
                raise HTTPException(409, "原文版本已变更，请重新检索来源。")


@app.get("/api/papers/{doc_id}/pages/{page}.png")
def page_image(doc_id: str, page: int, revision: str | None = None):
    path = local_pdf(paper_by_id(doc_id))
    if path is None:
        raise HTTPException(404, "原始 PDF 当前不可读取。")
    check_pdf_revision(path, revision)
    try:
        png = render_page(str(path), path.stat().st_mtime_ns, page)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/papers/{doc_id}/pdf")
def paper_pdf(doc_id: str, revision: str | None = None):
    path = local_pdf(paper_by_id(doc_id))
    if path is None:
        raise HTTPException(404, "未找到此文献的本地原始 PDF。")
    check_pdf_revision(path, revision)
    return FileResponse(path, media_type="application/pdf", filename=path.name,
                        content_disposition_type="inline")


@app.get("/api/papers/{doc_id}/markdown")
def paper_markdown(doc_id: str):
    _, source = _reading_copy(doc_id)
    return Response(source, media_type="text/markdown; charset=utf-8",
                    headers={"X-Content-Type-Options": "nosniff",
                             "Content-Disposition": f'attachment; filename="{doc_id}.fulltext.md"'})


def _reading_copy(doc_id):
    paper = paper_by_id(doc_id)
    path = local_markdown(paper)
    if path is None:
        raise HTTPException(404, "此文献尚未生成 Markdown 阅读版。")
    try:
        source = path.read_text(encoding="utf-8-sig")
        view = load_effective_evidence(PILOT)
        return path, qualify_reading_copy(source, view.qualifications(doc_id))
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "Markdown 阅读版暂时不可读取。") from None


@app.get("/api/papers/{doc_id}/markdown/rendered")
def paper_markdown_rendered(doc_id: str):
    paper = paper_by_id(doc_id)
    path, source = _reading_copy(doc_id)
    return {"doc_id": doc_id, "html": render_markdown(source, markdown_path=path,
            pdf_path=local_pdf(paper), doc_id=doc_id, page_count=int(paper.get("page_count") or 1))}


_qdrant_lock = threading.Lock()


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), mode: Literal["auto", "keyword", "semantic"] = "auto",
           limit: int = Query(10, ge=1, le=50)):
    notice = None
    try:
        if mode != "keyword" and is_crosslingual_query(q):
            # Chinese and mixed-language input share the same local expansion and
            # evidence ranking as question answering. A missing model/cache must
            # not prevent the terminology/FTS branch from finding source text.
            try:
                with _qdrant_lock:
                    rows = retrieve(q, semantic_model(), limit=limit)
            except Exception:
                rows = retrieve(q, model=None, limit=limit)
                notice = "语义模型或向量检索暂不可用，本次使用本地术语与关键词检索。"
        elif mode == "semantic":
            # Local Qdrant storage allows one client at a time.
            with _qdrant_lock:
                rows = semantic(q, semantic_model(), limit=limit)
        else:
            rows = lexical(q, limit=limit)
    except Exception as exc:
        raise HTTPException(503, f"检索暂时不可用，请检查本地索引与模型缓存：{exc}")
    # Return only evidence that can be opened in the published corpus.
    verified = []
    try:
        view = load_effective_evidence(PILOT)
        with connect() as db:
            for row in rows:
                source = db.execute("SELECT doc_id,pdf_page,text FROM evidence WHERE evidence_id=?",
                                    (row["evidence_id"],)).fetchone()
                if not source:
                    continue
                text = {"doc_id": source[0], "pdf_page": source[1], "text": source[2]}
                hit = view.source({**row, **text})
                if notice:
                    hit["retrieval_notice"] = notice
                verified.append(hit)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "源证据复核状态暂时无法读取。") from None
    keep = ("evidence_id", "doc_id", "pdf_page", "text", "score", "method", "rrf_score", "retrieval_score",
            "retrieval_reasons", "retrieval_notice", FIELD)
    return [{k: r[k] for k in keep if k in r} for r in verified]


@app.get("/api/framework")
def framework():
    paths = sorted((PILOT / "benchmarks" / "framework").glob("**/report.json"),
                   key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in paths:
        report = read_json(path)
        if isinstance(report, dict):
            return {"path": str(path.relative_to(ROOT)).replace("\\", "/"), **report}
    return None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=16000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)
    retrieval_mode: Literal["library", "framework"] = "library"
    channel: Literal["api", "subscription"] = "api"


class NativeChatRequest(ChatRequest):
    channel: Literal["api", "subscription"] = "subscription"


class IdeaRequest(BaseModel):
    prompt: str = Field(default="", max_length=6000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)
    count: int = Field(default=3, ge=1, le=5, strict=True)
    channel: Literal["api", "subscription"] = "api"


class ConfigureRequest(BaseModel):
    api_key: str = Field(default="", max_length=4096, repr=False)
    model: str = Field(default="", max_length=200)
    provider: Literal["", "paratera", "openai_compatible", "openai-compatible"] = ""
    base_url: str = Field(default="", max_length=2048)

    def connection_options(self) -> dict:
        return self.model_dump(include={"provider", "base_url"}, exclude_unset=True)


def local_request(request: Request):
    """These local credential/model operations must not be triggered from another website."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(403, "模型配置和生成请求仅允许在本机使用。")
    origin = request.headers.get("origin")
    if origin and (urlsplit(origin).netloc != request.headers.get("host") or urlsplit(origin).scheme != request.url.scheme):
        raise HTTPException(403, "请从本机知识图谱页面发起请求。")


@app.get("/api/chat/status")
def chat_status(request: Request, channel: Literal["api", "subscription"] = "api"):
    local_request(request)
    return subscription_status() if channel == "subscription" else provider_status()


class SubscriptionConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=32)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    effort: str | None = Field(default=None, min_length=1, max_length=32)


@app.post("/api/chat/subscription/configure")
@app.post("/api/native/subscription/configure")
def subscription_configure(body: SubscriptionConfigureRequest, request: Request):
    local_request(request)
    status = subscription_status()
    option = next((row for row in status["providers"] if row["provider_id"] == body.provider_id), None)
    if option is None:
        raise HTTPException(422, "未知的 CLI 订阅提供方。")
    if not option["implemented"]:
        raise HTTPException(409, f'{option["provider"]} 尚未接入。')
    if any(value is not None and value != option[key] for key, value in (("model", body.model), ("effort", body.effort))):
        raise HTTPException(422, "当前提供方使用固定的模型和推理强度。")
    if not option["available"]:
        raise HTTPException(503, status["message"])
    return status


@app.get("/api/native/status")
def native_status(request: Request, progress: bool = False):
    local_request(request)
    try:
        return native.status(PILOT, progress=True) if progress else native.status(PILOT)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "无法读取原生索引的发布清单或队列状态。") from None


@app.get("/api/native/search")
def native_search(request: Request, q: str = Query(min_length=1, max_length=500), limit: int = Query(10, ge=1, le=50)):
    local_request(request)
    if not q.strip():
        raise HTTPException(422, "请输入检索内容。")
    try:
        result = native.search(PILOT, q.strip(), limit)
    except Exception:
        raise HTTPException(503, "原生索引或本地语义模型暂不可用，请稍后重试。") from None
    return [{"doc_id": hit["doc_id"], "pdf_page": hit["page"], "page_end": hit["page_end"],
             "pdf_sha256": hit["pdf_sha256"], "evidence_id": hit["chunk_id"], "method": "native",
             "text": hit["excerpt"], "score": hit["score"]} for hit in result["hits"]]


class NativeQueueCommand(BaseModel):
    action: Literal["resume", "pause"]


class NativeQueueSettings(BaseModel):
    documents: int = Field(strict=True, ge=1, le=native.native_library.MAX_DOCUMENT_CONCURRENCY)


@app.post("/api/native/queue/settings")
def native_queue_settings(body: NativeQueueSettings, request: Request):
    local_request(request)
    try:
        native.native_library.save(native.native_library.directory(PILOT) / "queue_settings.json",
                                   {"documents": body.documents})
    except OSError:
        raise HTTPException(503, "并行数未能保存，请重试。") from None
    return {"documents": body.documents}


@app.post("/api/native/queue", status_code=202)
def native_queue(body: NativeQueueCommand, request: Request):
    local_request(request)
    try:
        native.queue_command(PILOT, body.action)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError:
        raise HTTPException(503, "队列控制程序启动失败，请检查本地入库环境。") from None
    return {"requested": body.action}


@app.get("/api/native/documents/{doc_id}/graph")
def native_document_graph(doc_id: str, request: Request):
    local_request(request)
    try:
        result = native.document_graph(PILOT, doc_id)
    except (OSError, KeyError, ValueError):
        raise HTTPException(503, "原生文献图谱暂不可读，请刷新重试。") from None
    if result is None:
        raise HTTPException(404, "该文献尚未发布到原生索引。")
    return result


@app.get("/api/native/documents/{doc_id}/regions")
def native_regions(doc_id: str, request: Request):
    local_request(request)
    try:
        return native.region_list(PILOT, doc_id)
    except FileNotFoundError:
        raise HTTPException(404, "尚无这篇文献的裁图检查点。") from None
    except (OSError, ValueError, KeyError):
        raise HTTPException(409, "裁图清单与当前文献版本无法核对。") from None


@app.get("/api/native/documents/{doc_id}/regions/{digest}.png")
def native_region_image(doc_id: str, digest: str, request: Request):
    local_request(request)
    try:
        content = native.region_bytes(PILOT, doc_id, digest)
    except FileNotFoundError:
        raise HTTPException(404, "未找到这张原始裁图。") from None
    except (OSError, ValueError, KeyError):
        raise HTTPException(409, "裁图校验失败，请打开原 PDF 核对。") from None
    return Response(content, media_type="image/png", headers={"Cache-Control":"private, max-age=3600"})


@app.post("/api/native/chat")
def native_chat(body: NativeChatRequest, request: Request):
    local_request(request)
    if not body.question.strip():
        raise HTTPException(422, "请输入问题。")
    try:
        return native.answer(PILOT, body.question.strip(), [m.model_dump() for m in body.history], channel=body.channel)
    except QAError as exc:
        raise HTTPException(503, str(exc)) from None
    except Exception:
        raise HTTPException(503, "问答未完成，请检查原生索引与所选生成接口。") from None


@app.post("/api/chat/configure")
def chat_configure(body: ConfigureRequest, request: Request):
    local_request(request)
    try:
        return configure_provider(body.api_key, body.model, **body.connection_options())
    except QAError as error:
        raise HTTPException(503, str(error)) from None


@app.post("/api/chat")
def chat(body: ChatRequest, request: Request):
    local_request(request)
    try:
        history = [m.model_dump() for m in body.history]
        if body.channel == "subscription":
            return answer_question(body.question, history, retrieval_mode=body.retrieval_mode, channel=body.channel)
        if body.retrieval_mode == "framework":
            return answer_question(body.question, history, retrieval_mode="framework")
        return answer_question(body.question, history)
    except QAError as error:
        raise HTTPException(503, str(error)) from None


@app.post("/api/native/ideas")
@app.post("/api/ideas")
def ideas(body: IdeaRequest, request: Request):
    local_request(request)
    try:
        options = {}
        if request.url.path == "/api/native/ideas":
            options["source_lookup"] = lambda focus, count: native.idea_sources(PILOT, focus, count)
        if body.channel != "api":
            options["channel"] = body.channel
        return generate_ideas(body.prompt, [m.model_dump() for m in body.history], body.count, **options)
    except QAError as error:
        content = {"detail": str(error)}
        run_id = getattr(error, "run_id", None)
        if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id):
            content["run_id"] = run_id
        return JSONResponse(status_code=503, content=content)


@app.post("/api/debug/ideas/preview")
def idea_preview(body: IdeaRequest, request: Request):
    local_request(request)
    try:
        return preview_ideas(body.prompt, [m.model_dump() for m in body.history], body.count)
    except QAError as error:
        raise HTTPException(503, str(error)) from None


@app.get("/api/debug/ideas/runs")
def idea_runs(request: Request):
    local_request(request)
    return {"runs": list_runs(), "storage": "process_memory", "captures_content": False}


@app.get("/api/debug/health")
def debug_health(request: Request):
    local_request(request)
    catalog = read_json(PILOT / "catalog/papers.json", [])
    catalog = catalog if isinstance(catalog, list) else []
    try:
        claim_count = sum(bool(line.strip()) for line in
                          (PILOT / "annotations/claims.jsonl").read_text(encoding="utf-8-sig").splitlines())
    except OSError:
        claim_count = 0
    indexes = {"sqlite": DATABASE.is_file(), "qdrant": (PILOT / "indexes/qdrant").is_dir(),
               "embeddings": (PILOT / "benchmarks/embedding/all_vectors.npy").is_file()}
    return {"status": "ok" if catalog and all(indexes.values()) else "degraded",
            "service": "knowledge_graph", "uptime_seconds": round(time.monotonic() - _STARTED_MONOTONIC, 2),
            "corpus": {"papers": len(catalog), "claims": claim_count,
                       "pages": sum(p.get("page_count", 0) for p in catalog)},
            "indexes": indexes,
            "active_generations": sum(r.get("status") == "running" for r in list_runs()),
            "check_scope": "服务与资料文件存在性检查；不调用模型，不代表索引完整性审计。"}


@app.get("/")
def index():
    # Stamp each /static asset URL with its modification time so a page refresh always
    # picks up edited JS/CSS, even when the browser cached the previous copy heuristically.
    def stamp(match: re.Match) -> str:
        asset = STATIC / match.group(2)
        version = format(asset.stat().st_mtime_ns, "x") if asset.is_file() else "0"
        return f'{match.group(1)}{match.group(2)}?v={version}"'

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'(/static/)([\w.-]+\.(?:js|css))(?:\?v=[\w.-]*)?"', stamp, html)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


class IntakeFile(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    content_base64: str = Field(min_length=1, max_length=42 * 1024 ** 2)


class IntakeCreate(BaseModel):
    mode: Literal["api", "paratera"] = "api"
    files: list[IntakeFile] = Field(min_length=1, max_length=20)
    max_claims: int = Field(default=8, ge=1, le=30)
    auto_publish: bool = True


class IntakeConfigure(ConfigureRequest):
    reuse_qa_key: bool = False


def intake_call(function, *args, **kwargs):
    from kg_pilot.ingestion import IntakeError
    from kg_pilot.ingestion_runners import RunnerError
    from kg_pilot.ingestion_publish import DatasetBusy, PublicationError
    try:
        return function(*args, **kwargs)
    except (IntakeError, RunnerError) as error:
        raise HTTPException(400, str(error)) from None
    except DatasetBusy:
        raise HTTPException(409, "已有任务正在使用资料库，请稍后继续。") from None
    except PublicationError:
        raise HTTPException(409, "发布状态需要核对，请查看批次记录后重试或恢复。") from None


def web_intake_batch(batch_id: str):
    """Resolve only API batches before touching a worker lock or control file."""
    from kg_pilot.ingestion import batch_path, get_batch
    path = intake_call(batch_path, batch_id, PILOT)
    stored = read_json(path / "batch.json", {})
    if not isinstance(stored, dict) or stored.get("mode") not in {"api", "paratera"}:
        raise HTTPException(404, "未找到可用的 API 入库批次。")
    return intake_call(get_batch, batch_id, PILOT)


def web_intake_recovery():
    """Inspect ownership without acquiring a batch or dataset worker lock."""
    from kg_pilot.ingestion_publish import JOURNAL, MAINTENANCE
    journal_path = PILOT / JOURNAL
    journal = read_json(journal_path, {})
    marker = read_json(PILOT / MAINTENANCE, {})
    journal = journal if isinstance(journal, dict) else {}
    marker = marker if isinstance(marker, dict) else {}
    active = bool(marker.get("active")) or (journal_path.exists() and
             journal.get("status") not in {"committed", "rolled_back"})
    if not active:
        return {"active": False, "available": False}
    owner = journal.get("batch_id")
    if not isinstance(owner, str) or (marker.get("active") and marker.get("batch_id") != owner):
        return {"active": True, "available": False}
    if re.fullmatch(r"b_[a-f0-9]{20}", owner):
        batch = read_json(PILOT / "ingestion/batches" / owner / "batch.json", {})
        available = isinstance(batch, dict) and batch.get("mode") in {"api", "paratera"}
    else:
        # These shared maintenance tasks do not own ingestion batches.
        available = bool(re.fullmatch(r"(?:table-backfill|text-quality)-[a-f0-9]{32}", owner))
    return {"active": True, "available": available, "batch_id": owner}


@app.get("/api/ingestion/status")
def ingestion_status(request: Request):
    local_request(request)
    from kg_pilot.ingestion import LIMITS
    from kg_pilot.ingestion_runners import provider_status as intake_status
    chat_connection = provider_status()
    ingestion_connection = intake_status()
    if ingestion_connection.get("credential_source") == "qa" and (
        not chat_connection.get("configured") or ingestion_connection.get("base_url") != chat_connection.get("base_url")
    ):
        ingestion_connection = {**ingestion_connection, "configured": False,
                                "message": "问答连接已变更或不可用。请重新选择复用当前问答连接，或填写独立的入库配置。"}
    recovery = web_intake_recovery()
    return {"provider": ingestion_connection, "limits": LIMITS,
            "qa_configured": bool(chat_connection.get("configured")),
            "qa_connection": {name: chat_connection.get(name) for name in ("base_url", "provider_id", "model", "models")},
            "maintenance": recovery["active"], "recovery_available": recovery["available"]}


@app.post("/api/ingestion/configure")
def ingestion_configure(body: IntakeConfigure, request: Request):
    local_request(request)
    from kg_pilot.ingestion_runners import configure_provider as configure_intake
    return intake_call(configure_intake, body.api_key, body.model, reuse_qa_key=body.reuse_qa_key,
                       **body.connection_options())


@app.get("/api/ingestion/batches")
def ingestion_batches(request: Request):
    local_request(request)
    batches = []
    for path in sorted((PILOT / "ingestion/batches").glob("b_*/batch.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            batches.append(web_intake_batch(path.parent.name))
        except (HTTPException, OSError):
            continue
    return {"batches": batches}


@app.post("/api/ingestion/batches")
def ingestion_create(body: IntakeCreate, request: Request):
    local_request(request)
    from kg_pilot.ingestion import create_batch
    try:
        files = [(item.name, base64.b64decode(item.content_base64, validate=True)) for item in body.files]
    except ValueError:
        raise HTTPException(400, "上传内容编码无效。") from None
    return intake_call(create_batch, files, body.mode, body.max_claims, body.auto_publish, PILOT)


@app.get("/api/ingestion/batches/{batch_id}")
def ingestion_batch(batch_id: str, request: Request):
    local_request(request)
    return web_intake_batch(batch_id)


@app.post("/api/ingestion/batches/{batch_id}/run")
def ingestion_run(batch_id: str, request: Request):
    local_request(request)
    from kg_pilot.ingestion import start_batch
    web_intake_batch(batch_id)
    return intake_call(start_batch, batch_id, PILOT)


@app.post("/api/ingestion/batches/{batch_id}/publish")
def ingestion_publish(batch_id: str, request: Request):
    local_request(request)
    from kg_pilot.ingestion import start_batch
    batch = web_intake_batch(batch_id)
    if batch["status"] != "ready" or batch["running"]:
        raise HTTPException(409, "本批次尚未全部通过检查，不能发布。")
    return intake_call(start_batch, batch_id, PILOT, publish_only=True)


@app.post("/api/ingestion/batches/{batch_id}/cancel")
def ingestion_cancel(batch_id: str, request: Request):
    local_request(request)
    from kg_pilot.ingestion import cancel_batch
    web_intake_batch(batch_id)
    return intake_call(cancel_batch, batch_id, PILOT)


@app.post("/api/ingestion/batches/{batch_id}/documents/{item_id}/skip")
def ingestion_skip(batch_id: str, item_id: str, request: Request):
    local_request(request)
    from kg_pilot.ingestion import skip_document
    web_intake_batch(batch_id)
    return intake_call(skip_document, batch_id, item_id, PILOT)


@app.post("/api/ingestion/recover")
def ingestion_recover(request: Request):
    local_request(request)
    from kg_pilot.ingestion_publish import recover_publication
    recovery = web_intake_recovery()
    if not recovery["active"]:
        return {"status": "idle"}
    if not recovery["available"]:
        raise HTTPException(409, "资料库正在由其他任务维护，请通过原任务继续处理。")
    return intake_call(recover_publication, PILOT, expected_batch_id=recovery["batch_id"])


class RevalidatedStaticFiles(StaticFiles):
    """Make browsers revalidate (cheap 304 via ETag) so edited JS/CSS is never served stale."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStaticFiles(directory=STATIC), name="static")
