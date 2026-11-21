"""Web API smoke tests against the real pilot data, without a browser or model API."""
from pathlib import Path
import json

from fastapi.testclient import TestClient

from web.server import app

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


def test_summary_matches_catalog_and_claims():
    papers = json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig"))
    claims = [line for line in (ROOT / "pilot/annotations/claims.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    summary = client.get("/api/summary").json()
    assert summary["papers"] == len(papers)
    assert summary["claims"] == len(claims)
    assert sum(summary["stages"].values()) == len(claims)


def test_papers_hide_local_paths():
    rows = client.get("/api/papers").json()
    assert rows and all("local_path" not in row and "sha256" not in row for row in rows)
    assert sum(row["claim_count"] for row in rows) == len(client.get("/api/claims").json())


def test_graph_edges_reference_existing_nodes():
    graph = client.get("/api/graph").json()
    ids = {node["id"] for node in graph["nodes"]}
    assert {node["kind"] for node in graph["nodes"]} == {"document", "claim", "sample"}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in graph["edges"])
    full = client.get("/api/graph", params={"evidence": True}).json()
    assert any(edge["kind"] == "supported_by" for edge in full["edges"])


def test_keyword_search_returns_source_pages():
    rows = client.get("/api/search", params={"q": "pumping", "mode": "keyword", "limit": 5}).json()
    assert rows and all(row["pdf_page"] and row["method"] == "bm25" for row in rows)
    assert "pumping" in rows[0]["text"].casefold()


def test_page_render_and_bounds():
    response = client.get("/api/papers/imec_06/pages/1.png")
    assert response.status_code == 200 and response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/api/papers/imec_06/pages/999.png").status_code == 404
    assert client.get("/api/papers/not_a_doc/pdf").status_code == 404


def test_frontend_is_served():
    assert "混合键合知识图谱" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
