"""Read-only original-PDF and served retrieval/QA glyph verification.

Writes a report only. Does not change corpus, indexes, provider configuration or
chat history. --chat explicitly calls the already configured live provider.
--phase final enforces acceptance and exits 1 on failure or 2 when human
semantic inspection is required. Baseline phases only collect observations.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import pickle
import re
import sqlite3
import time
import unicodedata
import uuid

import httpx
import numpy as np
import pymupdf


ROOT = Path(__file__).resolve().parents[1]
DOC = "reliability_gao_d2w_2021"
EID = f"{DOC}:p2:b8"
VISUALLY_CHECKED_PDF_SHA256 = "1a2e32bba2fcdb48b921a0118b6bcd19ab34e44f9d26b4a94ae812e2fa1b0fd9"
BASELINE_REPORT = ROOT / "pilot/reports/glyph_qa_baseline_full_20261106_115809.json"
QUESTION = (
    "Gao 的《Low Temperature Hybrid Bonding for Die to Wafer Stacking Applications》"
    "中，测试芯片的厚度、TSV 直径和 TSV 间距分别是多少？请注明单位和原文页码。"
)


def private_chars(text: str) -> dict[str, int]:
    return {f"U+{ord(c):04X}": text.count(c) for c in sorted(set(text))
            if unicodedata.category(c) == "Co"}


def target_units(text: str) -> bool:
    text = re.sub(r"\s+", " ", text).replace("µ", "μ")
    return all(re.search(rf"(?<!\d){value}\s*μm\b", text) for value in (50, 5, 35))


def record(text: str, **kwargs) -> dict:
    return {**kwargs, "text": text, "private_chars": private_chars(text),
            "has_expected_three_units": target_units(text)}


def semantic_roles(text: str) -> dict:
    """Conservatively recognize explicit role/value phrases, never infer roles.

    An unfamiliar paraphrase requires human inspection. An explicit wrong
    number fails, including an answer that merely contains all three numbers.
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[*_`]+", "", text)
    labels = {
        "die_thickness": (r"(?:芯片|晶片|die|chip)?\s*(?:厚度|厚|thickness)", 50),
        "tsv_diameter": (r"(?:TSVs?|通孔)?\s*(?:直径|diameter)", 5),
        "tsv_pitch": (r"(?:TSVs?|通孔)?\s*(?:间距|节距|pitch)", 35),
    }
    checks = {}
    has_negation = bool(re.search(r"并非|不是|不为|而非|未报告|未给出|没有|\bnot\b|\bunknown\b|\bunreported\b", text, re.I))
    collective_marker = bool(re.search(r"分别|\brespectively\b", text, re.I))
    for role, (label, expected) in labels.items():
        pattern = rf"{label}(?P<between>[^0-9。；;\n]{{0,24}})(?P<value>\d+(?:\.\d+)?)\s*(?:μm\b|微米)"
        candidates = list(re.finditer(pattern, text, flags=re.I))
        matches = [match for match in candidates if not any(
            re.search(other_label, match["between"], re.I)
            for other_role, (other_label, _) in labels.items() if other_role != role)]
        values = [float(match["value"]) for match in matches]
        checks[role] = {"expected_value_micrometres": expected, "observed_values_micrometres": values,
                        "matched_phrases": [match[0] for match in matches],
                        "status": ("needs_human_review" if has_negation or not values else
                                   "pass" if all(value == expected for value in values) else "fail")}
    collective_mapping = collective_marker and any(not check["observed_values_micrometres"] for check in checks.values())
    if collective_mapping:
        for check in checks.values():
            check["status"] = "needs_human_review"
    status = ("fail" if any(c["status"] == "fail" for c in checks.values()) else
              "needs_human_review" if any(c["status"] == "needs_human_review" for c in checks.values()) else "pass")
    return {"status": status, "roles": checks, "negation_requires_human_review": has_negation,
            "collective_mapping_requires_human_review": collective_mapping,
            "limitation": "Recognizes explicit role-then-value wording only; reversed wording, negation and unfamiliar paraphrases require human review."}


def acceptance(report: dict, *, final: bool, chat_requested: bool) -> dict:
    checks = []

    def check(name, passed, *, pending=False):
        checks.append({"name": name, "status": "pass" if passed else
                       "needs_human_review" if pending else "fail"})

    original = report.get("original_pdf", {})
    check("original_pdf_hash_matches_verified_source_and_catalog", original.get("sha256") == VISUALLY_CHECKED_PDF_SHA256
          and original.get("catalog_sha256_matches") and original.get("manual_visual_grounding", {}).get("checked"))
    check("original_title_matches_catalog", original.get("page1_native_title_matches_catalog"))
    target = report.get("sqlite_target", {})
    parsed = report.get("parsed_target", {})

    def valid_source(row):
        return (row.get("evidence_id") == EID and row.get("pdf_page") == 2
                and not row.get("private_chars", {}) and "\ufffd" not in row.get("text", "")
                and row.get("has_expected_three_units"))

    check("parsed_and_sqlite_evidence_identity_page_and_units", valid_source(parsed) and valid_source(target)
          and parsed.get("text") == target.get("text") and target.get("doc_id") == DOC)
    reading = report.get("reading_target", {})
    check("reading_copy_units_are_clean", reading.get("has_expected_three_units") and not reading.get("private_chars")
          and "\ufffd" not in reading.get("text", ""))
    phrases = {row["phrase"]: row for row in report.get("native_fts_phrase_queries", [])}
    check("native_fts_exact_greek_mu_phrase_hits_target", phrases.get('"5 μm diameter"', {}).get("target_present"))
    probe = report.get("fts_tokenizer_probe", {})
    check("micro_sign_phrase_hits_when_tokenizer_supports_equivalence",
          bool(probe) and (not probe.get("micro_sign_equivalent")
                           or phrases.get('"5 µm diameter"', {}).get("target_present")))
    check("legacy_private_use_phrase_no_longer_hits_target",
          bool(phrases) and not phrases.get('"5 \uf06dm diameter"', {}).get("target_present", True))
    embeddings = report.get("embedding_targets", [])
    check("embedding_record_and_persisted_payload_are_clean_and_identical", bool(embeddings)
          and all(row.get("record_matches_persisted_payload") and not row["record"]["private_chars"]
                  and not row["qdrant_payload"]["private_chars"] for row in embeddings)
          and target_units(" ".join(row["qdrant_payload"]["text"] for row in embeddings)))
    check("stored_vectors_match_matrix", bool(embeddings)
          and all(row.get("matrix_and_persisted_vectors_agree") for row in embeddings))
    comparison = report.get("baseline_comparison", {})
    check("original_and_target_ids_preserved_from_baseline", comparison.get("original_pdf_hash_unchanged")
          and comparison.get("evidence_id_unchanged") and comparison.get("vector_ids_unchanged"))
    check("changed_target_embedding_was_recomputed", comparison.get("all_target_vector_hashes_changed"))
    check("served_summary_is_available", report.get("summary", {}).get("http_status") == 200)
    searches = {row["mode"]: row for row in report.get("search", [])}
    for mode in ("keyword", "semantic"):
        result = searches.get(mode, {})
        check(f"served_{mode}_search_has_correct_original_evidence", result.get("http_status") == 200
              and bool(result.get("target")) and all(valid_source(row) and row.get("text") == target.get("text")
                                                      for row in result.get("target", [])))
    chat = report.get("live_chat", {})
    assessment = report.get("live_assessment", {})
    check("actual_live_qa_was_requested_and_succeeded", chat_requested and chat.get("http_status") == 200
          and bool(assessment))
    check("actual_qa_returned_and_cited_correct_original_evidence", assessment.get("target_source_cited")
          and bool(assessment.get("target_sources"))
          and all(valid_source(row) and row.get("text") == target.get("text") for row in assessment.get("target_sources", [])))
    check("actual_answer_has_clean_symbols_and_valid_citations", bool(assessment)
          and not assessment.get("answer_private_chars") and not assessment.get("answer_has_replacement_character")
          and assessment.get("all_answer_citations_exist"))
    role_status = assessment.get("semantic_roles", {}).get("status")
    check("answer_assigns_50_5_35_to_thickness_diameter_pitch", role_status == "pass",
          pending=role_status == "needs_human_review")
    failed = [c["name"] for c in checks if c["status"] == "fail"]
    pending = [c["name"] for c in checks if c["status"] == "needs_human_review"]
    status = "fail" if failed else "needs_human_review" if pending else "pass"
    return {"enforced": final, "status": status, "passed": status == "pass", "checks": checks,
            "failed_checks": failed, "human_review_checks": pending,
            "exit_code": (1 if failed else 2 if pending else 0) if final else 0}


def request(client, method: str, path: str, **kwargs) -> dict:
    started = time.monotonic()
    try:
        response = client.request(method, path, **kwargs)
    except httpx.RequestError as error:
        return {"http_status": None, "elapsed_seconds": round(time.monotonic() - started, 2),
                "data": {"request_error_type": type(error).__name__}}
    try:
        data = response.json()
    except ValueError:
        data = {"non_json_response_length": len(response.content)}
    return {"http_status": response.status_code,
            "elapsed_seconds": round(time.monotonic() - started, 2), "data": data}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default="verification")
    parser.add_argument("--chat", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8600")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path, default=BASELINE_REPORT,
                        help="Read-only pre-repair report used to verify identity and vector recomputation.")
    args = parser.parse_args()
    report = {
        "tested_at": datetime.now().astimezone().isoformat(),
        "phase": args.phase,
        "scope": "Read-only source and actual HTTP API; no mock; no persisted chat history",
        "question": QUESTION,
        "expected": {"die_thickness": "50 μm", "tsv_diameter": "5 μm",
                     "tsv_pitch": "35 μm", "pdf_page": 2, "evidence_id": EID},
    }
    paper = next(p for p in json.loads((ROOT / "pilot/catalog/papers.json").read_text("utf-8"))
                 if p["doc_id"] == DOC)
    pdf = Path(paper["local_path"])
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as source:
        first_page_text = source[0].get_text()
        report["original_pdf"] = {
            "title": paper["title"], "doi": paper.get("doi"), "sha256": digest,
            "page1_native_title_matches_catalog": " ".join(paper["title"].split()) in " ".join(first_page_text.split()),
            "page1_native_start": first_page_text[:500],
            "catalog_sha256_matches": digest == paper["sha256"],
            "page_count": len(source),
            "page2_native_text": source[1].get_text(),
            "manual_visual_grounding": {
                "checked": digest == VISUALLY_CHECKED_PDF_SHA256,
                "visually_checked_sha256": VISUALLY_CHECKED_PDF_SHA256,
                "image": "tmp/gao_glyph_grounding_page2_20261106.png",
                "observation": "Original rendered physical page 2, TEST VEHICLE DESIGN AND FABRICATION: die size 8 mm x 12 mm x 50 μm; TSVs 5 μm diameter at 35 μm pitch. The 50 μm dimension is thickness, not TSV diameter or pitch.",
            },
        }
    parsed = ROOT / "pilot/parsed" / DOC
    elements = [json.loads(line) for line in (parsed / "elements.jsonl").read_text("utf-8").splitlines()
                if line.strip()]
    source = next(row for row in elements if row["evidence_id"] == EID)
    report["parsed_target"] = record(source["text"], evidence_id=EID, pdf_page=source["pdf_page"],
                                      bbox=source.get("bbox"))
    with sqlite3.connect(f"file:{(ROOT / 'pilot/indexes/pilot.sqlite').as_posix()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT doc_id,pdf_page,text FROM evidence WHERE evidence_id=?", (EID,)).fetchone()
        report["sqlite_target"] = record(row[2], evidence_id=EID, doc_id=row[0], pdf_page=row[1])
        report["native_fts_phrase_queries"] = []
        fts_schema = db.execute("SELECT sql FROM sqlite_master WHERE name='evidence_fts'").fetchone()[0]
        tokenizer_match = re.search(r"tokenize\s*=\s*['\"]([^'\"]+)['\"]", fts_schema, flags=re.I)
        tokenizer = tokenizer_match[1] if tokenizer_match else "unicode61"
        with sqlite3.connect(":memory:") as probe:
            # Copy the configured tokenizer into a disposable in-memory probe.
            probe.execute("CREATE VIRTUAL TABLE probe USING fts5(text, tokenize='" + tokenizer.replace("'", "''") + "')")
            probe.execute("INSERT INTO probe VALUES(?)", ("5 μm diameter",))
            equivalent = bool(probe.execute("SELECT count(*) FROM probe WHERE probe MATCH ?", ('"5 µm diameter"',)).fetchone()[0])
        report["fts_tokenizer_probe"] = {"tokenizer": tokenizer, "micro_sign_equivalent": equivalent}
        for phrase in ('"5 μm diameter"', '"5 µm diameter"', '"5 \uf06dm diameter"'):
            ids = [r[0] for r in db.execute(
                "SELECT evidence_id FROM evidence_fts WHERE evidence_fts MATCH ?", (phrase,))]
            report["native_fts_phrase_queries"].append({"phrase": phrase, "result_count": len(ids),
                                                         "target_present": EID in ids, "evidence_ids": ids})
    records = json.loads((ROOT / "pilot/benchmarks/embedding/all_records.json").read_text("utf-8"))
    vectors = np.load(ROOT / "pilot/benchmarks/embedding/all_vectors.npy", mmap_mode="r")
    report["embedding_targets"] = []
    # Read Qdrant's trusted local persisted points directly in read-only mode;
    # do not instantiate another Qdrant client or contend for its storage lock.
    storage = ROOT / "pilot/indexes/qdrant/collection/pilot_evidence_bge_m3/storage.sqlite"
    with sqlite3.connect(f"file:{storage.as_posix()}?mode=ro", uri=True) as db:
        for index, row in enumerate(records):
            if row.get("evidence_id") != EID:
                continue
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, row["vector_id"]))
            key = base64.b64encode(pickle.dumps(point_id, protocol=4)).decode("utf-8")
            stored = db.execute("SELECT point FROM points WHERE id=?", (key,)).fetchone()
            point = pickle.loads(stored[0]) if stored else None
            payload = point.payload if point else {}
            vector = np.asarray(vectors[index], dtype=np.float32)
            stored_vector = np.asarray(point.vector, dtype=np.float32) if point else None
            report["embedding_targets"].append({
                "vector_id": row["vector_id"], "point_id": point_id,
                "record": record(row.get("embedding_text", "")),
                "qdrant_payload": record(payload.get("embedding_text", "")),
                "record_matches_persisted_payload": row == payload,
                "matrix_vector_sha256": hashlib.sha256(vector.tobytes()).hexdigest(),
                "qdrant_vector_sha256": hashlib.sha256(stored_vector.tobytes()).hexdigest() if point else None,
                "matrix_and_persisted_vectors_agree": bool(point and np.allclose(vector, stored_vector, rtol=1e-6, atol=1e-7)),
                "matrix_row_index": index,
            })
    if args.baseline.is_file():
        baseline = json.loads(args.baseline.read_text("utf-8"))
        old_vectors = {r["vector_id"]: r["matrix_vector_sha256"] for r in baseline.get("embedding_targets", [])}
        new_vectors = {r["vector_id"]: r["matrix_vector_sha256"] for r in report["embedding_targets"]}
        report["baseline_comparison"] = {
            "baseline_report": str(args.baseline.resolve()),
            "original_pdf_hash_unchanged": baseline.get("original_pdf", {}).get("sha256") == digest,
            "evidence_id_unchanged": baseline.get("sqlite_target", {}).get("evidence_id") == report["sqlite_target"]["evidence_id"],
            "vector_ids_unchanged": bool(old_vectors) and old_vectors.keys() == new_vectors.keys(),
            "all_target_vector_hashes_changed": bool(old_vectors) and old_vectors.keys() == new_vectors.keys()
                and all(new_vectors[key] != value for key, value in old_vectors.items()),
            "baseline_vector_sha256": old_vectors, "current_vector_sha256": new_vectors,
        }
    reading = (parsed / "fulltext.md").read_text("utf-8")
    pos = reading.find("The die size is")
    report["reading_target"] = record(reading[max(pos-100, 0):pos+500])
    with httpx.Client(base_url=args.base_url, timeout=300.0, trust_env=False) as client:
        report["summary"] = request(client, "GET", "/api/summary")
        report["search"] = []
        for mode, query in [("keyword", "TSVs"), ("semantic", QUESTION)]:
            result = request(client, "GET", "/api/search", params={"q": query, "mode": mode, "limit": 50})
            rows = result["data"] if isinstance(result["data"], list) else []
            target = [record(row.get("text", ""), **{k: v for k, v in row.items() if k != "text"})
                      for row in rows if row.get("evidence_id") == EID]
            report["search"].append({"mode": mode, "query": query, "http_status": result["http_status"],
                                     "elapsed_seconds": result["elapsed_seconds"], "result_count": len(rows),
                                     "target_rank": next((i for i, row in enumerate(rows, 1) if row.get("evidence_id") == EID), None),
                                     "target": target, "result_ids": [r["evidence_id"] for r in rows]})
        if args.chat:
            report["live_chat"] = request(client, "POST", "/api/chat", json={"question": QUESTION, "history": []})
            data = report["live_chat"]["data"]
            if isinstance(data, dict) and "answer" in data:
                target = [row for row in data.get("sources", []) if row.get("evidence_id") == EID]
                cited = set(re.findall(r"\[(S\d+)\]", data["answer"]))
                source_ids = {row.get("id") for row in data.get("sources", [])}
                report["live_assessment"] = {
                    "answer_private_chars": private_chars(data["answer"]),
                    "answer_has_replacement_character": "\ufffd" in data["answer"],
                    "answer_has_expected_three_units": target_units(data["answer"]),
                    "all_answer_citations_exist": bool(cited) and cited <= source_ids,
                    "semantic_roles": semantic_roles(data["answer"]),
                    "target_source_present": bool(target),
                    "target_source_cited": any(row.get("id") in cited for row in target),
                    "target_sources": [record(row.get("text", ""), **{k: v for k, v in row.items() if k != "text"}) for row in target],
                    "all_source_private_char_count": sum(sum(private_chars(row.get("text", "")).values()) for row in data.get("sources", [])),
                }
    report["acceptance"] = acceptance(report, final=args.phase == "final", chat_requested=args.chat)
    output = args.output or ROOT / "pilot/reports" / f"glyph_qa_{args.phase}_{datetime.now():%Y%m%d_%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output.resolve()), "phase": args.phase,
                      "acceptance": report["acceptance"],
                      "parsed": report["parsed_target"]["private_chars"],
                      "sqlite": report["sqlite_target"]["private_chars"],
                      "fts": [{k:r[k] for k in ("phrase", "target_present")} for r in report["native_fts_phrase_queries"]],
                      "embedding": [{"vector_id": r["vector_id"], "private_chars": r["qdrant_payload"]["private_chars"],
                                     "stored_payload_matches_record": r["record_matches_persisted_payload"],
                                     "stored_vector_matches_matrix": r["matrix_and_persisted_vectors_agree"]} for r in report["embedding_targets"]],
                      "search": [{k:r[k] for k in ("mode", "http_status", "target_rank")} for r in report["search"]],
                      "live_assessment": report.get("live_assessment")}, ensure_ascii=False))
    return report["acceptance"]["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
