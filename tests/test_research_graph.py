"""Contract and cache checks for the lightweight research overview."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from kg_pilot.research_graph import CELL_FIELDS
from kg_pilot.research_notes import attach_connection_cells
from web import server


@pytest.mark.parametrize("route", [None, "W2W", "D2W", "D2D"])
def test_current_overview_matches_matrix_and_preserves_link_evidence_ids(route):
    with TestClient(server.app) as client:
        params = {"route": route} if route else {}
        response = client.get("/api/research-graph", params=params)
        assert response.status_code == 200
        graph = response.json()
        matrix = client.get("/api/research-map", params=params).json()
        network = client.get("/api/research-network").json()
        papers = client.get("/api/papers").json()
        assert graph["totals"] == matrix["totals"]
        assert graph["cells"] == [{k: cell[k] for k in CELL_FIELDS} for cell in matrix["cells"]]
        expected = {}
        for cell in matrix["cells"]:
            for cid in cell["records"]:
                expected.setdefault(cid, []).append([cell["row"], cell["column"]])
        assert graph["claim_cells"] == expected
        ids = {p["doc_id"] for p in papers if not route or route in p["classification"].get("routes", [])}
        assert set(graph["document_ids"]) == ids
        source_links = {e["id"]: e for e in network["links"] if e["source"] in ids and e["target"] in ids}
        assert {e["id"] for e in graph["links"]} == set(source_links)
        for edge in graph["links"]:
            assert edge["basis"] in {"source_text", "rule", "agent_review"}
            assert edge["basis_note"] and "evidence" not in edge
            assert all(source_links[edge["id"]][k] == v for k, v in edge.items())
        assert response.headers["content-encoding"] == "gzip"
        assert len(response.content) <= 150_000
        assert int(response.headers["content-length"]) <= 150_000
        assert client.get("/api/research-graph", params=params,
                          headers={"If-None-Match": response.headers["etag"]}).status_code == 304
        if graph["links"]:
            edge = graph["links"][0]
            detail = client.get("/api/research-network/links/" + edge["id"], params={"snapshot_id": graph["network_snapshot_id"]}).json()
            assert detail["evidence"] == source_links[edge["id"]]["evidence"]
            assert client.get("/api/research-network/links/" + edge["id"], params={"snapshot_id": "expired"}).status_code == 409


def test_connections_use_only_validated_member_records_and_do_not_mutate_notes():
    connections = [{"id": "data", "members": [{"doc_id": "a", "claim_ids": ["c1"]},
                                                {"doc_id": "b", "claim_ids": ["expired"]}]},
                   {"id": "figure", "members": [{"doc_id": "a", "claim_ids": []}]}]
    before = copy.deepcopy(connections)
    matrix = {"cells": [{"row": "r1", "column": "out", "records": ["c1"]},
                        {"row": "r2", "column": "out", "records": ["unrelated_same_paper"]}]}
    result = attach_connection_cells(connections, matrix)
    assert result[0]["cells"] == [{"row": "r1", "column": "out"}]
    assert result[1]["cells"] == []
    assert connections == before


def test_compression_negotiation_and_invalid_route():
    with TestClient(server.app) as client:
        compressed = client.get("/api/research-graph", headers={"Accept-Encoding": "gzip"})
        plain = client.get("/api/research-graph", headers={"Accept-Encoding": "gzip ;q=0, *;q=1"})
        assert "content-encoding" not in plain.headers
        assert plain.json() == compressed.json()
        assert plain.headers["etag"] != compressed.headers["etag"]
        assert plain.headers["vary"] == "Accept-Encoding"
        weak = client.get("/api/research-graph", headers={"Accept-Encoding": "gzip", "If-None-Match": "W/" + compressed.headers["etag"]})
        assert weak.status_code == 304 and not weak.content
        assert client.get("/api/research-graph?route=BAD").status_code == 422
        assert client.get("/api/research-network/links/unknown-link").status_code == 404


def test_snapshot_changes_and_midbuild_revision_is_retried(monkeypatch, tmp_path):
    version_file = tmp_path / "review.json"
    version_file.write_text("first", encoding="utf-8")
    empty = {"rows": [], "columns": [], "cells": [], "studies": [], "directions": [], "totals": {"claims": 0}}
    monkeypatch.setattr(server, "PILOT", tmp_path)
    monkeypatch.setattr(server, "MAP_INPUTS", ("review.json",))
    monkeypatch.setattr(server, "NETWORK_INPUTS", ("review.json",))
    monkeypatch.setattr(server, "_map_response_bytes", lambda *args: json.dumps(empty).encode())
    monkeypatch.setattr(server, "classified_catalog", lambda: ([], []))
    monkeypatch.setattr(server, "_network_bytes", lambda *args: b'{"links":[]}')
    server._research_graph_bytes.cache_clear()
    revision = server.digest_files(tmp_path, server.NETWORK_INPUTS)
    first = server._research_graph_bytes(str(tmp_path), revision, None)[2]
    version_file.write_text("second", encoding="utf-8")
    revision = server.digest_files(tmp_path, server.NETWORK_INPUTS)
    second = server._research_graph_bytes(str(tmp_path), revision, None)[2]
    assert first != second
    assert server._research_graph_bytes(str(tmp_path), revision, "W2W")[2] != second
    server._research_graph_bytes.cache_clear()

    calls = []
    def changing_network(*args):
        calls.append(1)
        if len(calls) == 1:
            version_file.write_text("third", encoding="utf-8")
        return b'{"links":[]}'
    monkeypatch.setattr(server, "_network_bytes", changing_network)
    with TestClient(server.app) as client:
        response = client.get("/api/research-graph")
        assert response.status_code == 200
        assert len(calls) == 2
        assert response.json()["snapshot_id"] not in {first, second}
    server._research_graph_bytes.cache_clear()
