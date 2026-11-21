"""Offline search entrypoint routing and original-source regressions.

Retrieval and model loading are mocked. The source fixture is an in-memory
SQLite database, so these tests never open the live Qdrant/index stores.
"""
from contextlib import contextmanager
import sqlite3

import pytest
from fastapi.testclient import TestClient

from web import server


def test_read_request_releases_the_connection_before_publication(monkeypatch):
    # Retain a reference so this checks explicit close, independent of Python GC.
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(server.sqlite3, "connect", lambda *args, **kwargs: connection)
    with server.connect() as reader:
        assert reader.execute("select 1").fetchone() == (1,)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("select 1")


@pytest.fixture
def search_api(monkeypatch):
    calls = []
    model = object()

    def result(method):
        return [{"evidence_id": "source:p4:paragraph", "doc_id": "wrong-vector-doc",
                 "pdf_page": 99, "text": "tokenizer reconstructed text", "score": .8,
                 "method": method, "embedding_text": "not source text",
                 **({"rrf_score": .031, "retrieval_score": .05,
                     "retrieval_reasons": [{"method": "bilingual_fts", "terms": ["annealing"]}]}
                    if method == "hybrid" else {})}]

    def lexical(query, limit):
        calls.append(("lexical", query, limit))
        return result("bm25")

    def semantic(query, selected_model, limit):
        assert selected_model is model
        calls.append(("semantic", query, limit))
        return result("dense")

    def retrieve(query, selected_model=None, limit=20, **kwargs):
        selected_model = kwargs.get("model", selected_model)
        assert selected_model is model or selected_model is None
        calls.append(("retrieve" if selected_model is model else "fallback", query, limit))
        return result("hybrid")

    @contextmanager
    def connect():
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE evidence (evidence_id TEXT, doc_id TEXT, pdf_page INTEGER, text TEXT)")
            db.execute("INSERT INTO evidence VALUES (?,?,?,?)",
                       ("source:p4:paragraph", "source", 4, "Original English paragraph: annealing at 200 °C."))
            yield db

    monkeypatch.setattr(server, "lexical", lexical)
    monkeypatch.setattr(server, "semantic", semantic)
    monkeypatch.setattr(server, "retrieve", retrieve)
    monkeypatch.setattr(server, "semantic_model", lambda: model)
    monkeypatch.setattr(server, "connect", connect)
    # Exercise FastAPI parsing while bypassing only the publication lock, which
    # otherwise creates a lockfile in the real pilot directory for every request.
    monkeypatch.setattr(server.app, "user_middleware", [])
    monkeypatch.setattr(server.app, "middleware_stack", None)
    yield TestClient(server.app), calls
    server.app.middleware_stack = None


@pytest.mark.parametrize("query,mode,expected", [
    ("退火温度与接触电阻", None, "retrieve"),
    ("Cu/SiCN 低温键合", "auto", "retrieve"),
    ("铜焊盘下凹", "semantic", "retrieve"),
    ("铜焊盘下凹", "keyword", "lexical"),
    ("Cu pumping", None, "lexical"),
    ("Cu pumping", "auto", "lexical"),
    ("Cu pumping", "keyword", "lexical"),
    ("Cu pumping", "semantic", "semantic"),
])
def test_language_routing_keeps_literal_and_english_modes(search_api, query, mode, expected):
    client, calls = search_api
    params = {"q": query, "limit": 5}
    if mode is not None:
        params["mode"] = mode
    response = client.get("/api/search", params=params)
    assert response.status_code == 200
    assert calls == [(expected, query, 5)]
    row = response.json()[0]
    assert (row["doc_id"], row["pdf_page"], row["text"]) == (
        "source", 4, "Original English paragraph: annealing at 200 °C.")
    assert "embedding_text" not in row
    if expected == "retrieve":
        assert row["rrf_score"] == .031
        assert row["retrieval_score"] == .05
        assert row["retrieval_reasons"] == [{"method": "bilingual_fts", "terms": ["annealing"]}]


def test_chinese_search_falls_back_when_model_is_unavailable(search_api, monkeypatch):
    client, calls = search_api

    def unavailable():
        raise RuntimeError("mock model unavailable")

    monkeypatch.setattr(server, "semantic_model", unavailable)
    response = client.get("/api/search", params={"q": "退火温度", "limit": 5})
    assert response.status_code == 200
    assert calls == [("fallback", "退火温度", 5)]
    assert "本地术语与关键词检索" in response.json()[0]["retrieval_notice"]


def test_unknown_mode_is_rejected_before_retrieval(search_api):
    client, calls = search_api
    response = client.get("/api/search", params={"q": "annealing", "mode": "unknown"})
    assert response.status_code == 422
    assert not calls


def test_stale_search_hit_cannot_be_displayed_as_evidence(search_api, monkeypatch):
    client, _ = search_api
    monkeypatch.setattr(server, "retrieve", lambda *args, **kwargs: [
        {"evidence_id": "missing:p9", "text": "reconstructed", "pdf_page": 9}])
    response = client.get("/api/search", params={"q": "厚度"})
    assert response.status_code == 200 and response.json() == []
