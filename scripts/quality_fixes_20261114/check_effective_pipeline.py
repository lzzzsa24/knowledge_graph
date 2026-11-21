"""Verify the priority-one source changes locally, without generation or credentials."""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import pymupdf
import numpy as np
from fastapi.testclient import TestClient

from kg_pilot import ingestion_publish, innovation, qa
from kg_pilot.effective_evidence import FIELD, load_effective_evidence
from kg_pilot.pipeline import jsonl
from web import server


PILOT = ROOT / "pilot"
REVIEW = PILOT / "reports/quality_review_20261113"
BASE_COUNTS = {"papers": 213, "claims": 1439, "claim_fts": 1439, "evidence": 11731,
               "evidence_fts": 11731, "graph_nodes": 4166, "graph_edges": 5518}
ADEIA = "doc_0086822b49e2d5df8658"
LEE = "doc_b9a25e99b026b40039f4"
KIM = "journals_mi13040537"
IPC = "doc_e32cb835d14010241a5a"
WRONG = {9: (r"2\s*[-–−]\s*15\s*mm\b", r"10\s*mW\b"),
         13: (r"(?:10|50|200)\s*mm\b",), 16: (r"(?:50|15)\s*mm\b",), 17: (r"(?:200|10)\s*mm\b",)}
RIGHT = {9: (r"2\s*[-–−]\s*15\s*µm\b", r"10\s*mΩ\b"),
         13: (r"50(?:\s+or|\s*[、/])\s*200\s*µm\b|50\s*µm\b", r"200\s*µm\b", r"10\s*µm\b"),
         16: (r"50\s*µm\b", r"15\s*µm\b"), 17: (r"200\s*µm\b", r"10\s*µm\b")}
SOURCE_CASES = {"unit_pad": (ADEIA, (9,)), "unit_die": (ADEIA, (13,)), "stack_12": (ADEIA, (16,)),
              "no_drop": (ADEIA, (17,)), "misalignment_conflict": (LEE, (4,)),
              "ipc_filters": (IPC, (1, 4))}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def connect_read_only(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True))


def current_pdf(row):
    path = ROOT / row["path"]
    with pymupdf.open(path) as document:
        pages = document.page_count
    return {**row, "sha256": sha(path), "bytes": path.stat().st_size, "pages": pages}


def correct_units(text, page):
    normalized = str(text).replace("μ", "µ")
    return (all(re.search(pattern, normalized) for pattern in RIGHT[page])
            and all(not re.search(pattern, normalized) for pattern in WRONG[page]))


def check_units(result, claims, client):
    markdown = client.get(f"/api/papers/{ADEIA}/markdown")
    assert markdown.status_code == 200
    claims_response = client.get("/api/claims")
    assert claims_response.status_code == 200
    visible_claims = {c["claim_id"]: c for c in claims_response.json() if c["doc_id"] == ADEIA}
    sections = re.split(r"(?m)^## PDF 第 (\d+) 页[^\n]*\n", markdown.text)
    by_page = dict(zip(map(int, sections[1::2]), sections[2::2], strict=True))
    vector_records = read_json(PILOT / "benchmarks/embedding/all_records.json")
    rows = []
    with connect_read_only(PILOT / "indexes/pilot.sqlite") as db, connect_read_only(
        PILOT / "indexes/qdrant/collection/pilot_evidence_bge_m3/storage.sqlite"
    ) as vector_db:
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in BASE_COUNTS}
        counts["qdrant"] = vector_db.execute("SELECT count(*) FROM points").fetchone()[0]
        counts["vector_records"] = len(vector_records)
        for page, suffix in ((9, "002"), (13, "004"), (16, "005"), (17, "007")):
            claim_id = f"{ADEIA}_c{suffix}"
            eid = f"{ADEIA}:p{page}:b0_0"
            claim_text, = db.execute("SELECT payload FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
            evidence, = db.execute("SELECT text FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
            fts, = db.execute("SELECT text FROM evidence_fts WHERE evidence_id=?", (eid,)).fetchone()
            claim_fts, = db.execute("SELECT text FROM claim_fts WHERE claim_id=?", (claim_id,)).fetchone()
            vector_blobs = [payload for payload, in vector_db.execute(
                "SELECT point FROM points WHERE instr(point, ?) > 0", (eid.encode(),))]
            vector_parts = [r["embedding_text"] for r in vector_records if r["evidence_id"] == eid]
            # The pickle payload is never executed. Source text is contiguous UTF-8
            # in the local store, and must match the published embedding text.
            vector_storage = bool(vector_parts) and all(any(text.encode() in payload for payload in vector_blobs)
                                                        for text in vector_parts)
            search = client.get("/api/search", params={"q": {9: "misalignment tolerance", 13: "thinned",
                                 16: "stacking yield", 17: "minimum anneal temperature"}[page],
                                 "mode": "keyword", "limit": 50})
            hit = next((r for r in search.json() if r["evidence_id"] == eid), {}) if search.is_success else {}
            item = {"paper": ADEIA, "pdf_page": page, "claim_id": claim_id, "evidence_id": eid,
                    "claim_sqlite_equals_published": json.loads(claim_text) == claims[claim_id],
                    "claims_api_without_wrong_units": not any(re.search(p, str(visible_claims[claim_id]))
                                                             for p in WRONG[page]),
                    "evidence": correct_units(evidence, page), "fts": correct_units(fts, page),
                    "claim_fts_without_wrong_units": not any(re.search(p, claim_fts) for p in WRONG[page]),
                    "vector_payload": correct_units("\n".join(vector_parts), page) and vector_storage,
                    "reading_markdown": correct_units(by_page.get(page, ""), page),
                    "keyword_api_hit": hit.get("text") == evidence and bool(hit.get(FIELD)),
                    "lateral_mm_preserved": bool(re.search(r"8\s*[x×]\s*12\s*mm\b", evidence))}
            rows.append(item)
    result["live_counts"] = counts
    result["unit_checks"] = rows
    return (all(counts[k] == n for k, n in BASE_COUNTS.items())
            and counts["qdrant"] == counts["vector_records"] == 10919 and all(
        all(v for k, v in item.items() if k not in {"paper", "pdf_page", "claim_id", "evidence_id"}) for item in rows)
    )


def check_retrieval(result, effective, *, report_key="local_retrieval_regression"):
    cases = {c["case_id"]: c for c in read_json(REVIEW / "qa_regression_cases.json")["cases"]}
    probes = {key: (cases[key]["question"], doc, pages, set(cases[key]["claim_ids"]))
              for key, (doc, pages) in SOURCE_CASES.items()}
    probes.update({
        "kim_sam": ("Kim Nguyen Choa 2022 Effect of the Wafer Warpage on the Hybrid Bonding 的 SAM 空洞检测下限是多少？", KIM, (5, 6), {"j036"}),
        "kim_edge_distance": ("Kim Nguyen Choa 2022 的 Fig. 12 倒角区空洞离晶圆边缘多远？", KIM, (12,), {"j036"}),
    })
    checks = []
    for case, (question, doc, pages, claim_ids) in probes.items():
        fetched, warnings = qa.retrieve_sources(question)
        selected = [source for source in fetched if source["doc_id"] == doc]
        contexts = [qa._source_context(s) for s in selected]
        qualifications = {q["id"]: q for c in contexts for q in c.get(FIELD, [])}
        known = {q["id"] for page in pages for q in effective.qualifications(doc, pdf_page=page)}
        held_pages = {c["pdf_page"] for c in contexts}
        actual_claims = {cl["claim_id"] for c in contexts for cl in c.get("reviewed_claims", [])}
        checks.append({"case_id": case, "question": question, "doc_id": doc, "expected_pages": list(pages),
                       "retrieved_target_pages": sorted(held_pages), "retrieved_target_claims": sorted(claim_ids & actual_claims),
                       "qualification_ids": sorted(qualifications), "expected_qualification_ids": sorted(known),
                       "warnings": warnings,
                       "status": "passed" if (known and known <= qualifications.keys()
                                              and all(qualifications[q]["status"] == "active" for q in known)
                                              and set(pages) <= held_pages) else "failed"})
    result[report_key] = checks
    return all(c["status"] == "passed" for c in checks)


def check_map_and_innovation(result, client):
    data = client.get("/api/research-map").json()
    graph = client.get("/api/research-graph").json()
    network = client.get("/api/research-network").json()
    map_data = {k: data["record_details"].get(f"{ADEIA}_c{k}") for k in ("002", "007")}
    anneal = next(cell for cell in data["cells"] if (cell["row"], cell["column"]) == ("annealing", "yield"))
    anneal_link = anneal["reviewed_records"].get(f"{ADEIA}_c007", {})
    map_checks = {"conflict_qualification": LEE in data.get(FIELD, {}),
                  "adeia_claims_agree": all(detail and detail.get(FIELD) for detail in map_data.values()),
                  "adeia_anneal_link_reports_figure_and_limits": (anneal_link.get("relation") == "condition"
                                                               and "94–95%" in anneal_link.get("limit", "")),
                  "lee_misalignment_not_counted": f"{LEE}_c009" not in data["record_details"],
                  "graph_resolves": bool(graph.get("snapshot_id")),
                  "network_resolves": bool(network.get("snapshot_id"))}
    result["map_checks"] = map_checks
    preview = innovation.preview_ideas("这项Cu/polymer键合证明了1.82nm对准精度吗？", count=1)
    records = [q for c in preview["contexts"] if c["doc_id"] == LEE for q in c.get(FIELD, [])]
    invention = {"provider_called": preview["provider_called"],
                 "lee_qualifications": sorted({q["id"] for q in records if q["status"] == "active"})}
    result["innovation_preview"] = invention
    return all(map_checks.values()) and invention["provider_called"] is False and bool(invention["lee_qualifications"])


def check_backup(result, original, claims):
    before_claims = {row["claim_id"]: row for row in jsonl(original / "annotations/claims.jsonl")}
    changed_claims = sorted(cid for cid in before_claims.keys() & claims.keys() if before_claims[cid] != claims[cid])
    expected_claims = [f"{ADEIA}_c{suffix}" for suffix in ("002", "004", "005", "007")]
    old_embeddings, new_embeddings = [p / "benchmarks/embedding" for p in (original, PILOT)]
    old_records, new_records = [read_json(p / "all_records.json") for p in (old_embeddings, new_embeddings)]
    with (old_embeddings / "all_vectors.npy").open("rb") as f:
        old_vectors = np.load(f, allow_pickle=False)
    with (new_embeddings / "all_vectors.npy").open("rb") as f:
        new_vectors = np.load(f, allow_pickle=False)
    expected_pages = {4, 6, 7, 8, 9, 10, 11, 13, 14, 16, 17, 18, 19}
    newer = {e["vector_id"]: i for i, e in enumerate(new_records)}
    stable_vector_ids = set(newer) == {e["vector_id"] for e in old_records}
    vectors_changed = set()
    vector_texts_changed = set()
    if stable_vector_ids:
        for position, old in enumerate(old_records):
            new_position = newer[old["vector_id"]]
            if not np.array_equal(old_vectors[position], new_vectors[new_position]):
                vectors_changed.add(old["evidence_id"])
            if old != new_records[new_position]:
                vector_texts_changed.add(old["evidence_id"])
    expected_ids = {f"{ADEIA}:p{page}:b0_0" for page in expected_pages}
    evidence_old = {e["evidence_id"]: e for e in jsonl(original / "parsed/effective_evidence.jsonl")}
    evidence_new = {e["evidence_id"]: e for e in jsonl(PILOT / "parsed/effective_evidence.jsonl")}
    evidence_changed = {eid for eid in evidence_old.keys() & evidence_new.keys()
                        if evidence_old[eid] != evidence_new[eid]}
    with connect_read_only(original / "indexes/pilot.sqlite") as old, connect_read_only(PILOT / "indexes/pilot.sqlite") as new:
        sql_evidence = "SELECT evidence_id, text FROM evidence"
        old_sqlite = dict(old.execute(sql_evidence))
        new_sqlite = dict(new.execute(sql_evidence))
        sqlite_changed = {eid for eid in old_sqlite.keys() & new_sqlite.keys() if old_sqlite[eid] != new_sqlite[eid]}
    result["backup_comparison"] = {"original": str(original), "claims_changed": changed_claims,
                                  "evidence_ids_changed": sorted(evidence_changed),
                                  "sqlite_evidence_ids_changed": sorted(sqlite_changed),
                                  "embedding_records_changed": sorted(vector_texts_changed),
                                  "vectors_changed": sorted(vectors_changed),
                                  "stable_claim_ids": before_claims.keys() == claims.keys(),
                                  "stable_evidence_ids": evidence_old.keys() == evidence_new.keys(),
                                  "stable_vector_ids": stable_vector_ids}
    return (before_claims.keys() == claims.keys() and changed_claims == expected_claims
            and evidence_old.keys() == evidence_new.keys() and evidence_changed == expected_ids
            and old_sqlite.keys() == new_sqlite.keys() and sqlite_changed == expected_ids
            and stable_vector_ids and vector_texts_changed == expected_ids and vectors_changed == expected_ids)


def verify(original=None, *, dense=False):
    frozen = read_json(REVIEW / "local_screen.json")
    with ThreadPoolExecutor(max_workers=4) as executor:
        source_pdfs = list(executor.map(current_pdf, frozen["physical_source_pdf_manifest"]))
    manifest_sha = hashlib.sha256(json.dumps(source_pdfs, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
    result = {"method": "Local read-only source fixtures, FTS, structured retrieval and map. No generation.",
              "physical_pdf_count": len(source_pdfs), "physical_pdf_manifest_sha256": manifest_sha,
              "frozen_files_changed_since_audit": [name for name, before in frozen["frozen_input_sha256"].items()
                                                  if sha(ROOT / name) != before]}
    result["physical_pdfs_unchanged"] = manifest_sha == frozen["physical_source_pdf_manifest_sha256"]
    effective = load_effective_evidence(PILOT)
    result["registry"] = {doc: [{"id": item["id"], "status": item["status"], "pages": item["pdf_pages"]}
                               for item in effective.qualifications(doc)] for doc in (ADEIA, LEE, KIM, IPC)}
    claims = {c["claim_id"]: c for c in jsonl(PILOT / "annotations/claims.jsonl")}
    if ingestion_publish.maintenance_status(PILOT).get("active"):
        raise RuntimeError("Cannot verify while the corpus awaits publication or recovery")
    # TestClient invokes the shipped HTTP handlers in process. Bypass lock-file
    # creation while the coordinated publication run leaves this snapshot idle.
    with (patch.object(ingestion_publish, "dataset_lock", lambda *a, **kw: nullcontext()),
          patch.object(qa, "retrieval_model", lambda: None), TestClient(server.app) as client):
        unit_pass = check_units(result, claims, client)
        retrieval_pass = check_retrieval(result, effective)
        map_pass = check_map_and_innovation(result, client)
    dense_pass = True
    if dense:
        # Use the standard local vector path with local_files_only loading. The
        # client briefly takes its store lock, so run after publishing, when the
        # web app is idle. Retry/fallback is reported by QA and counted here.
        dense_pass = check_retrieval(result, effective, report_key="local_dense_retrieval_regression")
        dense_pass = dense_pass and all(not c["warnings"] for c in result["local_dense_retrieval_regression"])
        result["local_dense_retrieval_passed"] = dense_pass
    backup_pass = check_backup(result, original, claims) if original is not None else True
    result["status"] = ("passed" if all((result["physical_pdfs_unchanged"], unit_pass,
                                        retrieval_pass, map_pass, dense_pass, backup_pass)) else "failed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional path for the JSON report")
    parser.add_argument("--backup-original", type=Path, help="Optional original/ path of the publication backup")
    parser.add_argument("--dense", action="store_true", help="Also run local BGE/Qdrant retrieval while the server is idle")
    options = parser.parse_args()
    result = verify(original=options.backup_original, dense=options.dense)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if options.output:
        options.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
