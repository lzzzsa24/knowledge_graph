"""Offline regression of effective evidence through the published entry points."""

import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
import qdrant_client

from kg_pilot import innovation, qa, retrieval
from kg_pilot.map_relations import claims_digest
from web import server


CONFLICT_DOC = "alignment"
CONTROL_DOC = "control"
CONFLICT_CLAIM = "alignment-c1"
CONFLICT_ID = "alignment-text-figure"
OLD_VALUE = "1.82 nm"
FIGURE_VALUE = "1.81 µm"
AUDIT_ONLY = "historical uncorrected 1.82 mm"
QUESTION = "What misalignment did the Alignment study measure after CMP?"
MAIN = "alignment:p1:b1"
NEIGHBOR = "alignment:p1:b0"
OTHER_PAGE = "alignment:p2:b0"
CONTROL = "control:p1:b0"
MODEL = "synthetic-model"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def write_claims(path, claims):
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in claims), encoding="utf-8")


def make_pdf(text):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 40), text)
        doc.new_page().insert_text((30, 40), "Independent observation on another page.")
        return doc.tobytes(garbage=4)


def assertion(claim_id, doc_id, pdf_page, eid, statement, value, unit):
    return {
        "claim_id": claim_id, "doc_id": doc_id, "statement": statement,
        "material_system": "Cu/SiO2", "sample_id": "sample", "stage": "surface_planarization",
        "quantity": "contact_resistance" if pdf_page == 2 else "alignment_accuracy",
        "value_text": value, "unit": unit, "conditions": {},
        "claim_type": "reported_experiment", "review_status": "independent_agent_reviewed",
        "evidence": [{"evidence_id": eid, "pdf_page": pdf_page, "quote": statement}],
    }


@pytest.fixture
def corpus(monkeypatch):
    # Inherit project ACLs rather than using Windows pytest temp directories.
    parent = Path(__file__).resolve().parents[1] / ".cache"
    parent.mkdir(exist_ok=True)
    root = parent / ("quality-regression-" + uuid.uuid4().hex)
    pilot = root / "pilot"
    try:
        for directory in ("catalog", "annotations", "indexes", "collection"):
            (pilot / directory).mkdir(parents=True)
        papers = []
        for doc_id in (CONFLICT_DOC, CONTROL_DOC):
            pdf = pilot / "collection" / f"{doc_id}.pdf"
            pdf.write_bytes(make_pdf(f"{doc_id.capitalize()} misalignment: {OLD_VALUE}."))
            papers.append({"doc_id": doc_id, "title": f"{doc_id.capitalize()} study",
                           "local_path": str(pdf), "page_count": 2,
                           "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()})
        papers_file = pilot / "catalog/papers.json"
        write_json(papers_file, papers)

        conflict_statement = f"The misalignment after CMP was {OLD_VALUE}."
        control_statement = f"Control misalignment after CMP was {OLD_VALUE}."
        normal_statement = "After CMP, the contact resistance was 8 mΩ."
        claims = [assertion(CONFLICT_CLAIM, CONFLICT_DOC, 1, MAIN, conflict_statement, "1.82", "nm"),
                  assertion("control-c1", CONTROL_DOC, 1, CONTROL, control_statement, "1.82", "nm"),
                  assertion("alignment-c2", CONFLICT_DOC, 2, OTHER_PAGE, normal_statement, "8", "mΩ")]
        claims[0]["raw_text_fields"] = {"statement": AUDIT_ONLY}
        claims[0]["evidence"][0]["raw_quote"] = AUDIT_ONLY
        claims_file = pilot / "annotations/claims.jsonl"
        write_claims(claims_file, claims)

        qualification = {"id": CONFLICT_ID, "kind": "source_conflict",
                         "claim_ids": [CONFLICT_CLAIM], "pdf_pages": [1],
                         "disputed_value": True,
                         "summary": f"The text says {OLD_VALUE}. The horizontal figure mark says {FIGURE_VALUE}.",
                         "usage_limit": "Keep both readings together. Do not use either for alignment ranking.",
                         "evidence": [{"pdf_page": 1, "locator": "text", "quote": OLD_VALUE},
                                      {"pdf_page": 1, "locator": "Fig. 8 horizontal", "quote": FIGURE_VALUE}]}
        write_json(pilot / "catalog/evidence_qualifications.json", {"schema": 1, "documents": [{
            "doc_id": CONFLICT_DOC, "source_sha256": papers[0]["sha256"],
            "claims_digest": claims_digest([claims[0], claims[2]]), "qualifications": [qualification]}]})

        source_rows = [
            (NEIGHBOR, CONFLICT_DOC, 1, f"An adjacent block repeats misalignment {OLD_VALUE}.", "[0,0,100,10]"),
            (MAIN, CONFLICT_DOC, 1, conflict_statement + " Long source paragraph." * 400, "[0,20,100,30]"),
            (OTHER_PAGE, CONFLICT_DOC, 2, normal_statement, "[0,0,100,10]"),
            (CONTROL, CONTROL_DOC, 1, control_statement, "[0,0,100,10]"),
        ]
        reading = pilot / "parsed/alignment/fulltext.md"
        reading.parent.mkdir(parents=True)
        reading.write_text("# Alignment study\n\n## PDF 第 1 页\n\n"
                           "[查看原 PDF 第 1 页](../../collection/alignment.pdf#page=1)\n\n"
                           f"{conflict_statement}\n\n## PDF 第 2 页\n\n{normal_statement}\n", encoding="utf-8")
        database = pilot / "indexes/pilot.sqlite"
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript("""
                CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, doc_id TEXT, pdf_page INTEGER, text TEXT, bbox TEXT);
                CREATE TABLE graph_nodes (node_id TEXT, kind TEXT, attributes TEXT);
                CREATE TABLE graph_edges (source TEXT, target TEXT, kind TEXT);
                CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED, doc_id UNINDEXED, text);
            """)
            db.executemany("INSERT INTO evidence VALUES (?,?,?,?,?)", source_rows)
            db.executemany("INSERT INTO evidence_fts VALUES (?,?,?)", [(r[0], r[1], r[3]) for r in source_rows])
            db.executemany("INSERT INTO graph_nodes VALUES (?,?,?)", [
                (c["claim_id"], "claim", json.dumps({"statement": c["statement"]})) for c in claims])
            db.executemany("INSERT INTO graph_edges VALUES (?,?,?)", [
                (c["claim_id"], c["evidence"][0]["evidence_id"], "supported_by") for c in claims])

        for module in (qa, retrieval, server):
            monkeypatch.setattr(module, "PILOT", pilot)
        monkeypatch.setattr(server, "ROOT", root)
        monkeypatch.setattr(server, "DATABASE", database)
        class Vector(list):
            def tolist(self):
                return list(self)

        model = SimpleNamespace(encode=lambda *args, **kwargs: [Vector([1.0])])
        monkeypatch.setattr(qa, "retrieval_model", lambda: model)
        monkeypatch.setattr(server, "semantic_model", lambda: model)

        def forbidden(*args, **kwargs):
            pytest.fail("Quality regression must stay offline")

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
        # Simulate a ranked hit from before the revision. The content and metadata
        # carried by it are deliberately inconsistent with the identified source.
        hit = {"evidence_id": MAIN, "doc_id": CONTROL_DOC, "pdf_page": 99,
               "text": "old vector payload", "embedding_text": "old vector payload", "method": "dense", "score": 1.0}
        point = SimpleNamespace(payload=hit, score=1.0)
        mocked_qdrant = SimpleNamespace(query_points=lambda *a, **kw: SimpleNamespace(points=[point]),
                                       close=lambda: None)
        monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kwargs: mocked_qdrant)
        with TestClient(server.app) as client:
            yield {"client": client, "pilot": pilot, "papers": papers, "claims": claims,
                   "papers_file": papers_file, "claims_file": claims_file, "rows": source_rows}
    finally:
        assert root.resolve().parent == parent.resolve()
        if root.is_dir():
            shutil.rmtree(root)


def qualified(record, status="active"):
    entry = next(q for q in record["evidence_qualifications"] if q["id"] == CONFLICT_ID)
    assert entry["status"] == status and entry["pdf_pages"] == [1]
    assert entry["claim_ids"] == [CONFLICT_CLAIM] and entry["usage_limit"]
    if status == "active":
        assert OLD_VALUE in entry["summary"] and FIGURE_VALUE in entry["summary"]
    else:
        assert OLD_VALUE not in str(record) and FIGURE_VALUE not in str(record)
    return entry


def test_conflict_is_bound_in_keyword_semantic_qa_and_map(corpus):
    client = corpus["client"]
    for mode in ("keyword", "semantic"):
        response = client.get("/api/search", params={"q": "misalignment", "mode": mode})
        assert response.status_code == 200
        result = next(r for r in response.json() if r["evidence_id"] == MAIN)
        assert (result["doc_id"], result["pdf_page"]) == (CONFLICT_DOC, 1)
        assert "old vector payload" not in str(result)
        qualified(result)
    claims_response = client.get("/api/claims")
    assert claims_response.status_code == 200
    claims = {c["claim_id"]: c for c in claims_response.json()}
    qualified(claims[CONFLICT_CLAIM])
    assert AUDIT_ONLY not in str(claims[CONFLICT_CLAIM])
    assert AUDIT_ONLY in corpus["claims_file"].read_text(encoding="utf-8")
    assert not claims["control-c1"].get("evidence_qualifications")
    assert claims["control-c1"]["statement"] == corpus["claims"][1]["statement"]

    map_response = client.get("/api/research-map")
    assert map_response.status_code == 200
    data = map_response.json()
    assert data["totals"]["documents"] == 2 and data["totals"]["claims"] == 3
    assert CONFLICT_ID in json.dumps(data, ensure_ascii=False)
    assert not any(CONFLICT_CLAIM in cell["records"] for cell in data["cells"])

    sources, _ = qa.retrieve_sources(QUESTION)
    source = next(s for s in sources if s["evidence_id"] == MAIN)
    assert len(source["text"]) > 3500
    context = qa._source_context(source)
    assert context["text_truncated"]
    assert qualified(source) == qualified(context) == qualified(claims[CONFLICT_CLAIM])
    assert claims[CONFLICT_CLAIM]["value_text"] is None and claims[CONFLICT_CLAIM]["unit"] is None
    graph = client.get("/api/graph", params={"evidence": "true"}).json()
    qualified(next(node for node in graph["nodes"] if node["id"] == CONFLICT_CLAIM))
    reading = client.get("/api/papers/alignment/markdown")
    assert reading.status_code == 200 and OLD_VALUE in reading.text and FIGURE_VALUE in reading.text


@pytest.mark.parametrize("changed_input", ["pdf", "catalog_sha", "claim"])
def test_stale_binding_withholds_only_affected_sources(corpus, changed_input):
    client, pilot = corpus["client"], corpus["pilot"]
    pdf = pilot / "collection/alignment.pdf"
    initial_map = client.get("/api/research-map")
    assert initial_map.status_code == 200
    assert CONFLICT_ID in initial_map.text

    if changed_input == "pdf":
        pdf.write_bytes(make_pdf("New edition without confirmed alignment data."))
    elif changed_input == "catalog_sha":
        corpus["papers"][0]["sha256"] = "a" * 64
        write_json(corpus["papers_file"], corpus["papers"])
    else:
        corpus["claims"][0]["sample_id"] = "new-sample"
        write_claims(corpus["claims_file"], corpus["claims"])

    results = client.get("/api/search", params={"q": "misalignment", "mode": "keyword"}).json()
    by_id = {source["evidence_id"]: source for source in results}
    qualified(by_id[MAIN], "stale")
    qualified(by_id[NEIGHBOR], "stale")
    assert by_id[CONTROL]["text"] == corpus["rows"][-1][3]
    assert not by_id[CONTROL].get("evidence_qualifications")
    same_doc_other_page = client.get("/api/search", params={"q": "resistance", "mode": "keyword"}).json()
    assert same_doc_other_page[0]["evidence_id"] == OTHER_PAGE
    assert same_doc_other_page[0]["text"] == corpus["rows"][2][3]

    new_map = client.get("/api/research-map")
    assert new_map.status_code == 200 and new_map.headers["etag"] != initial_map.headers["etag"]
    assert FIGURE_VALUE not in new_map.text and CONFLICT_ID in new_map.text
    source_pdf = client.get("/api/papers/alignment/pdf")
    assert source_pdf.status_code == 200 and source_pdf.content == pdf.read_bytes()
    reading = client.get("/api/papers/alignment/markdown")
    assert reading.status_code == 200 and OLD_VALUE not in reading.text and FIGURE_VALUE not in reading.text
    assert "8 mΩ" in reading.text and "#page=1" in reading.text

    sources, _ = qa.retrieve_sources(QUESTION)
    source = next(s for s in sources if s["evidence_id"] == MAIN)
    qualified(source, "stale")
    qualified(qa._source_context(source), "stale")
    public_claims = client.get("/api/claims").json()
    stale_claim = next(c for c in public_claims if c["claim_id"] == CONFLICT_CLAIM)
    qualified(stale_claim, "stale")
    assert AUDIT_ONLY not in str(stale_claim)


def test_qa_and_innovation_render_conflict_before_ordinary_context_exhausts_budget(corpus, monkeypatch):
    fetched, _ = qa.retrieve_sources(QUESTION)
    source = next(s for s in fetched if s["evidence_id"] == MAIN)
    source = {**source, "id": "S20"}
    ordinary = [{"id": f"S{i}", "evidence_id": f"regular-{i}", "doc_id": CONTROL_DOC, "pdf_page": 1,
                 "text": "Ordinary evidence " * 250, "reviewed_claims": []} for i in range(1, 20)]
    monkeypatch.setattr(qa, "retrieve_sources", lambda *args: ([*ordinary, source], []))
    monkeypatch.setattr(qa, "provider_status", lambda: {"configured": True})
    monkeypatch.setattr(qa, "_connection_snapshot", lambda: (
        {"provider_id": "paratera", "base_url": qa.BASE_URL, "model": MODEL}, "offline-fixture"))
    monkeypatch.setattr(qa, "_format_event", lambda **kwargs: None)

    def handle(request):
        messages = json.loads(request.content)["messages"]
        evidence = json.loads(messages[-1]["content"])["evidence"]
        assert len(evidence) < 20
        shown = next(c for c in evidence if c["id"] == "S20")
        assert shown["text_truncated"] and qualified(shown)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content":
            json.dumps({"answer": "The synthetic text and figure disagree. [S20]", "answerability": "partial"})}}]})

    answer = qa.answer_question(QUESTION, transport=httpx.MockTransport(handle))
    assert answer["answerability"] == "partial" and any(s["id"] == "S20" for s in answer["sources"])
    included, contexts = innovation._bounded_context([*ordinary, source])
    assert len(included) < 20
    assert qualified(next(c for c in contexts if c["doc_id"] == CONFLICT_DOC))


def test_exact_qualification_query_materializes_both_source_pages(corpus, monkeypatch):
    registry = corpus["pilot"] / "catalog/evidence_qualifications.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    qualification, = payload["documents"][0]["qualifications"]
    qualification["pdf_pages"].append(2)
    qualification["evidence"].append({"pdf_page": 2, "locator": "Appendix", "quote": "Contrary reading on page 2."})
    qualification["query_aliases"] = [["ABC-2.2.22", "滤波器"]]
    write_json(registry, payload)
    # Simulate retrieval finding only an unrelated study. Identity comes from
    # the distinctive method code plus topic, not a publication/title match.
    monkeypatch.setattr(qa, "retrieve", lambda *args, **kwargs: [{"evidence_id": CONTROL, "doc_id": CONTROL_DOC}])
    sources, _ = qa.retrieve_sources("ＡＢＣ-2.2.22 要不要加滤波器？")
    qualified_pages = {s["pdf_page"] for s in sources if s.get("evidence_qualifications")}
    assert qualified_pages == {1, 2}
    assert any(s["doc_id"] == CONTROL_DOC for s in sources)


@pytest.mark.parametrize("prompt", [QUESTION, ""])
def test_innovation_preview_keeps_neighbor_conflict_and_does_not_read_provider(corpus, monkeypatch, prompt):
    def forbidden():
        pytest.fail("Provider configuration read in preview")

    monkeypatch.setattr(qa, "provider_status", forbidden)
    monkeypatch.setattr(innovation.random, "shuffle", lambda rows: None)
    response = corpus["client"].post("/api/debug/ideas/preview", json={"prompt": prompt, "count": 1})
    assert response.status_code == 200
    preview = response.json()
    assert preview["provider_called"] is False and preview["mode"] == ("guided" if prompt else "random")
    by_eid = {s["evidence_id"]: s for s in preview["sources"]}
    assert by_eid[NEIGHBOR]["adjacent_to"] == MAIN
    by_context_id = {c["id"]: c for c in preview["contexts"]}
    for eid in (MAIN, NEIGHBOR):
        source = by_eid[eid]
        assert qualified(source) == qualified(by_context_id[source["id"]])
