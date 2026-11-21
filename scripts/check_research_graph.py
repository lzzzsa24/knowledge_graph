"""Verify backend research-graph contracts without model calls or browser control.

Use --base-url to check a running server; by default run the same ASGI app in-process.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def verify(client):
    def get(path, **kwargs):
        response = client.get(path, **kwargs)
        response.raise_for_status()
        return response

    papers = get("/api/papers").json()
    claims = get("/api/claims").json()
    network = get("/api/research-network").json()
    notes = get("/api/research-notes").json()
    assert all(isinstance(p["authors"], list) and p["short_label"] for p in papers)
    assert all(p["authors"] or p.get("organization") for p in papers)
    assert all(e.get("basis") in {"source_text", "rule", "agent_review"} and e.get("basis_note") for e in network["links"])
    results = []
    for route in (None, "W2W", "D2W", "D2D"):
        params = {"route": route} if route else {}
        start = time.monotonic()
        response = get("/api/research-graph", params=params, headers={"Accept-Encoding": "gzip"})
        elapsed = time.monotonic() - start
        graph = response.json()
        matrix = get("/api/research-map", params=params).json()
        selected = {p["doc_id"] for p in papers if not route or route in p["classification"].get("routes", [])}
        scoped_claims = {c["claim_id"] for c in claims if c["doc_id"] in selected}
        pairs = {}
        for cell in matrix["cells"]:
            for cid in cell["records"]:
                pairs.setdefault(cid, []).append([cell["row"], cell["column"]])
        assert graph["claim_cells"] == pairs
        assert set(graph["document_ids"]) == selected
        assert graph["totals"] == matrix["totals"]
        assert set(pairs).isdisjoint(matrix["unmapped"])
        assert set(pairs) | set(matrix["unmapped"]) == scoped_claims
        assert set(matrix["unmapped"].values()) <= set(matrix["unmapped_reason_labels"])
        for small, full in zip(graph["cells"], matrix["cells"], strict=True):
            assert all(full[key] == value for key, value in small.items())
        expected_links = {e["id"]: e for e in network["links"] if e["source"] in selected and e["target"] in selected}
        assert {e["id"] for e in graph["links"]} == set(expected_links)
        for edge in graph["links"]:
            assert "evidence" not in edge
            assert all(expected_links[edge["id"]][k] == v for k, v in edge.items())
        assert response.headers.get("content-encoding") == "gzip"
        assert len(response.content) <= 150_000
        assert int(response.headers["content-length"]) <= 150_000
        cached = client.get("/api/research-graph", params=params,
                            headers={"Accept-Encoding": "gzip", "If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304
        results.append({"route": route, **graph["totals"], "links": len(graph["links"]),
                        "unmapped_reasons": dict(Counter(matrix["unmapped"].values())),
                        "json_bytes": len(response.content), "http_gzip_bytes": int(response.headers["content-length"]),
                        "elapsed_seconds": round(elapsed, 3), "snapshot_id": graph["snapshot_id"]})
        if route is None:
            assert notes["study_connections"] == matrix["study_connections"]
            for edge in graph["links"]:
                # Original evidence remains available by unchanged link ID.
                detail = get("/api/research-network/links/" + edge["id"],
                             params={"snapshot_id": graph["network_snapshot_id"]}).json()
                assert detail["evidence"] == expected_links[edge["id"]]["evidence"]
    return {"status": "passed", "routes": results,
            "metadata": {"papers": len(papers), "with_person_authors": sum(bool(p["authors"]) for p in papers),
                         "organization_only": sum(not p["authors"] and bool(p.get("organization")) for p in papers),
                         "labels": len({p["doc_id"] for p in papers if p["short_label"]})},
            "links_by_basis": dict(Counter(e["basis"] for e in network["links"])),
            "study_connection_cells": [{"id": c["id"], "cells": c["cells"]} for c in notes["study_connections"]],
            "size_definition": "http_gzip_bytes is the encoded HTTP body; json_bytes is the complete decoded UTF-8 JSON. No claims or links are dropped to meet a size limit."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--output", type=Path, default=ROOT / "pilot/reports/research_backend_20261107/backend_acceptance.json")
    args = parser.parse_args()
    if args.base_url:
        import httpx
        client = httpx.Client(base_url=args.base_url.rstrip("/"), trust_env=False, timeout=60)
    else:
        from fastapi.testclient import TestClient
        from web.server import app
        client = TestClient(app)
    with client:
        report = verify(client)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output), "routes": report["routes"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
