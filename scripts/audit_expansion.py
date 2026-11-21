"""Read-only audit of published expansion batches; no rebuild or provider access.

Example: .venv/Scripts/python.exe scripts/audit_expansion.py --batch-ids BATCH --expected-total 36
Only the audit report and an optional, disposable offline query embedding cache
are written. The formal dataset is locked during inspection and fingerprinted.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from kg_pilot import ingestion, ingestion_publish, retrieval
from kg_pilot.ingestion_runners import _parse_result
from kg_pilot.pipeline import jsonl, normalize, read_json
from kg_pilot.validation import Claim, quote_supported


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def indexed(rows, key):
    result = {row[key]: row for row in rows}
    require(len(result) == len(rows), f"duplicate {key}")
    return result


def formal_fingerprint(pilot):
    """Exclude the permanent synchronization lock and local Qdrant lock inode."""
    paths = []
    for directory in ("catalog", "annotations", "parsed", "indexes", "benchmarks/embedding", "collection/batches"):
        base = pilot / directory
        paths.extend(p for p in base.rglob("*") if p.is_file() and p.name not in {".lock"})
    return {p.relative_to(pilot).as_posix(): sha(p) for p in sorted(set(paths))}


def baseline_audit(pilot, report_dir, papers, classes, claims):
    base = report_dir / "baseline"
    frozen = read_json(report_dir / "baseline.json")
    for name in ("catalog/papers.json", "catalog/classifications.json", "annotations/claims.jsonl", "reports/delivery_manifest.json"):
        require(sha(base / name) == frozen[name], f"baseline snapshot altered: {name}")
    old_papers = read_json(base / "catalog/papers.json")
    old_claims = jsonl(base / "annotations/claims.jsonl")
    old_classes = read_json(base / "catalog/classifications.json")["records"]
    require(len(old_papers) == 26 and len(old_claims) == 128, "baseline is not 26 documents / 128 claims")
    for row in old_papers:
        did = row["doc_id"]
        require(papers.get(did) == row, f"baseline paper dict changed: {did}")
        require(sha(row["local_path"]) == frozen["original_pdfs"][did] == row["sha256"], f"baseline PDF changed: {did}")
    for row in old_claims:
        require(claims.get(row["claim_id"]) == row, f"baseline claim dict changed: {row['claim_id']}")
    for row in old_classes:
        require(classes.get(row["doc_id"]) == row, f"baseline classification dict changed: {row['doc_id']}")
    return dict(status="passed", documents=26, claims=128, classifications=len(old_classes),
                exact_dict_preservation=True, original_pdf_hashes_unchanged=True,
                snapshot_files_verified_against_baseline_json=True)


def check_classification(c, doc_id, source_sha, pages, taxonomy):
    require(c["doc_id"] == doc_id and c["source_sha256"] == source_sha, f"classification identity: {doc_id}")
    require(c.get("classification_review") in {"api_reviewed", "api_model_reviewed", "independent_agent_reviewed"}, f"classification unreviewed: {doc_id}")
    categories = {x["id"] for x in taxonomy["categories"]}
    require(c["primary_category"] in categories and set(c["related_stages"]) <= categories, f"classification category: {doc_id}")
    for field, allowed in taxonomy["facets"].items():
        values = c.get(field, []) if field in {"routes", "methods"} else [c.get(field)]
        require(set(values) <= set(allowed), f"classification {field}: {doc_id}")
    require(set(c["topics"]) <= set(taxonomy["topic_groups"]), f"classification topics: {doc_id}")
    for ref in c["evidence"]:
        require(ref["pdf_page"] in pages and quote_supported(ref["quote"], pages[ref["pdf_page"]]["text"]), f"classification quote: {doc_id}")
        prefix = f"{doc_id}:p{ref['pdf_page']}"
        require(ref["evidence_id"] == prefix or ref["evidence_id"].startswith(prefix + ":"), f"classification page reference: {doc_id}")


# Independent visual inspection of the 24 registry renders confirmed section
# dividers, logos, closing/contact pages and one decorative blank image only.
# Freeze exact source/page pairs so a later broad registry edit cannot silently
# bypass technical figures. Images remain hash-bound by the registry below.
APPROVED_NONTEXT = {
    "4b75f9495e425a6f13fca4f1cc022ee9650088da9557e1cd2b1fc72bf30306c5": [24, 26],
    "160f1a31e048f0304680c1a56cf4287c6b2c7639badbaa649b5e1606e06138d7": [14],
    "6b234f22a6ce8b2848d60109db4c50e3d5a53c767ef13b8d4cbb0195a3bc2b66": [18, 24],
    "19c8c780cd21ec5572d9bedfbba52d9f96268c6865d99170155991f37de8564b": [19, 24],
    "235bc2bac283909b4e69fedd12b0af51e05c17bcbe3ae852e0276d2f04e6d0dd": [28],
    "09c97ed377db45631b3b40ffa2f127bf2c82320009d3f88f8b76491645ef964b": [19],
    "e01c7b77670d8c361fd4705ca258e6a668c0ae57200a7f2346076a6e26ae8314": [19],
    "d47fae6094ccc0b371ad70fa846edc78acd6bdd531fabfcffa1b9715a4008af0": [26],
    "4962eda3147b5cefc48a12a99d4ffe999e9cdd48d884eaf14c31ac086a26bf73": [3, 7, 15, 24],
    "8e41f3f50ea027998e50d845077920f5c21b9a053e2c0d01bb4c3fa507d3e11c": [7, 28],
    "94d7ed0131a88e125b21ab007d91afa20f7b41af91ee5335162cbe5c2c931dba": [59],
    "c5aa414c37d9c67ae5ad1bc32400797c4f856f42404cc6d4b134c9d35c7550cc": [7, 13, 43, 49, 59, 71],
}


def nontext_registry_audit(report_dir):
    path = report_dir / "nontext_page_reviews.json"
    registry = read_json(path)
    require(registry.get("schema") == "nontext-page-reviews-v1" and registry.get("approved_by_root") is True, "nontechnical registry lacks approval/schema")
    entries = indexed(registry["documents"], "source_sha256")
    require(set(entries) == set(APPROVED_NONTEXT), "nontechnical registry source set differs from independently inspected set")
    for digest, entry in entries.items():
        require(entry["doc_id"] == "doc_" + digest[:20] and sha(entry["source_path"]) == digest, "nontechnical registry source identity/hash mismatch")
        require(sorted(r["pdf_page"] for r in entry["reviews"]) == APPROVED_NONTEXT[digest], "nontechnical registry page set differs from independently inspected set")
        for review in entry["reviews"]:
            require(review.get("actor", "").startswith("/root/") and review.get("judgment"), "nontechnical page lacks visual reviewer/judgment")
            require(sha(review["render_path"]) == review["render_sha256"], "nontechnical visual render changed")
    return entries, dict(status="passed", registry_sha256=sha(path), sources=len(entries), pages=24,
                         exact_source_page_allowlist=True, source_and_render_hashes_verified=True,
                         visual_assessment="Independent agent inspected all 24 registered page renders as a labeled contact sheet; section/branding/closing pages only, no labeled technical chart or data.")


def review_audit(docdir, document, pages):
    """Read review requests/results only. Extraction requests are never read."""
    accepted, records, classifications = {}, [], []
    elements = indexed(document["elements"], "evidence_id")

    def reference_parser(ref):
        eid = ref.get("evidence_id")
        if eid in elements:
            return elements[eid]["parser"]
        # Older classification proposals linked the whole page directly.
        require(eid == f"{document['paper']['doc_id']}:p{ref['pdf_page']}", f"unknown review evidence: {eid}")
        return pages[ref["pdf_page"]]["parser"]
    for path in sorted(docdir.glob("chunk_*/review.agent_request.json")):
        req = read_json(path)
        require(req["task"] == "review", f"not a review: {path}")
        reply = read_json(path.with_name("review.agent_response.json"))
        require(reply["signature"] == req["signature"], f"review response signature mismatch: {path}")
        require(bool(reply.get("actor")), f"review actor absent: {path}")
        result = _parse_result(json.dumps(reply["response"], ensure_ascii=False), "review", req["packet"])
        ingestion.validate_review(result, req["packet"]["proposal"])
        committed = read_json(path.with_name("review.json"))
        meta = committed["output"].get("_result_meta", {})
        require(meta.get("request_signature") == req["signature"], f"consumed review request mismatch: {path}")
        require(meta.get("actor") == reply["actor"], f"consumed review actor mismatch: {path}")
        require(meta.get("response_sha256") == sha(path.with_name("review.agent_response.json")), f"consumed review response hash: {path}")
        require({k: v for k, v in committed["output"].items() if k != "_result_meta"} == result, f"consumed review differs: {path}")
        require(req["packet"]["doc_id"] == document["paper"]["doc_id"], f"review document mismatch: {path}")
        # Context pages can be intentionally truncated; all referenced pages must
        # carry the complete actual parsed-page text supplied to the reviewer.
        proposal = req["packet"]["proposal"]
        cited = {e["pdf_page"] for item in [proposal["classification"], *proposal["claims"]] for e in item["evidence"]}
        context = {p["pdf_page"]: p for p in req["packet"]["pages"]}
        for page in cited:
            require(context[page]["text"] == pages[page]["text"], f"review cited page differs from actual parsed page: {path}:{page}")
        references = [ref for item in [proposal["classification"], *proposal["claims"]] for ref in item["evidence"]]
        ocr_cited = sorted({ref["pdf_page"] for ref in references
                            if reference_parser(ref) in {"rapidocr", "rapidocr_table"}})
        required_visual = req.get("visual_review_required_pages", [])
        checked_visual = reply.get("visual_pages_checked", [])
        require(required_visual == ocr_cited, f"OCR visual request omits/adds cited pages: {path}")
        require(all(type(n) is int for n in checked_visual) and len(set(checked_visual)) == len(checked_visual), f"invalid visual review page list: {path}")
        require(set(ocr_cited) <= set(checked_visual), f"OCR cited pages not visually reviewed: {path}")
        require(meta.get("visual_pages_checked", []) == ocr_cited, f"consumed OCR visual coverage mismatch: {path}")
        if ocr_cited:
            visual_source = req.get("visual_source") or {}
            require(visual_source.get("sha256") == document["paper"]["sha256"], f"OCR visual source hash mismatch: {path}")
            require(Path(visual_source.get("pdf_path", "")).resolve() == Path(document["paper"]["local_path"]).resolve(), f"OCR visual source PDF path mismatch: {path}")
            require(sha(visual_source["pdf_path"]) == visual_source["sha256"], f"OCR visual source PDF changed: {path}")
        for item in [proposal["classification"], *proposal["claims"]]:
            for ref in item["evidence"]:
                require(quote_supported(ref["quote"], pages[ref["pdf_page"]]["text"]), f"review proposed quote differs from actual parsed page: {path}")
                parser = reference_parser(ref)
                expected_ocr = parser if parser in {"rapidocr", "rapidocr_table"} else None
                require(ref.get("parser") == expected_ocr, f"OCR provenance marker missing/wrong: {path}")
        decisions = {c["claim_id"]: c for c in result["claims"]}
        for claim in proposal["claims"]:
            cid = claim["claim_id"]
            require(cid not in accepted and decisions[cid]["status"] == "accepted", f"review claim duplicate/rejected: {cid}")
            accepted[cid] = claim
        classifications.append(proposal["classification"])
        records.append(dict(chunk=path.parent.name, request_signature=req["signature"], actor=reply["actor"], claims=len(result["claims"]), classification_approved=result["classification_approved"], ocr_cited_pages=ocr_cited, visual_pages_checked=checked_visual, visual_source=req.get("visual_source"), consumed_visual_pages_checked=meta.get("visual_pages_checked", [])))
    require(records, f"no review records: {docdir}")
    require(len(records) == document["paper"]["extraction_coverage"]["processed_chunks"], f"review chunk coverage: {docdir}")
    require(set(accepted) == {c["claim_id"] for c in document["claims"]}, f"review coverage differs from published claims: {docdir}")
    for claim in document["claims"]:
        without_final = {k: v for k, v in claim.items() if k not in {"review_status", "verification_label", "ingestion_batch_id"}}
        proposed = {k: v for k, v in accepted[claim["claim_id"]].items() if k not in {"review_status", "verification_label", "ingestion_batch_id"}}
        require(without_final == proposed, f"claim altered after review: {claim['claim_id']}")
    final_class = {k: v for k, v in document["classification"].items() if k not in {"classification_review", "ingestion_batch_id"}}
    require(final_class == classifications[0], f"classification altered after review: {docdir}")
    return records


def batch_audit(pilot, batch_id, papers, classes, claims, native, effective, selection, nontext_registry):
    import pymupdf
    directory = pilot / "ingestion/batches" / batch_id
    batch = read_json(directory / "batch.json")
    prepared = read_json(directory / "prepared.json")
    require(prepared["batch_id"] == batch_id, "prepared batch identity mismatch")
    source_docs = indexed(batch["documents"], "doc_id")
    readiness = read_json(pilot / "catalog/ingestion_readiness.json")["documents"]
    manifest = indexed(read_json(pilot / "collection/batches/manifest.json")["papers"], "doc_id")
    taxonomy = ingestion.taxonomy_packet(pilot)
    results = []
    for d in prepared["documents"]:
        p = d["paper"]; did = p["doc_id"]; source = source_docs[did]
        pub = papers[did]
        require(pub == {**p, "local_path": str(pilot / "collection/batches" / batch_id / (did + ".pdf"))}, f"published paper differs from prepared: {did}")
        require(pub["ingestion_batch_id"] == batch_id and source["sha256"] == p["sha256"], f"batch identity: {did}")
        require(sha(p["local_path"]) == sha(source["source_path"]) == sha(pub["local_path"]) == p["sha256"], f"new source PDF hash: {did}")
        require(d["pages"] == read_json(pilot / "parsed" / did / "pages.json"), f"published pages changed: {did}")
        require(d["elements"] == jsonl(pilot / "parsed" / did / "elements.jsonl"), f"published elements changed: {did}")
        pages = indexed(d["pages"], "pdf_page")
        require(list(pages) == list(range(1, p["page_count"] + 1)), f"non-contiguous PDF pages: {did}")
        marked_nontext = sorted(n for n, page in pages.items() if page.get("nontechnical_page_reviewed"))
        registry_entry = nontext_registry.get(p["sha256"])
        approved_nontext = APPROVED_NONTEXT.get(p["sha256"], [])
        require(marked_nontext == approved_nontext, f"nontechnical skipped pages differ from approved source/page pairs: {did}")
        if registry_entry:
            require(registry_entry["doc_id"] == did and registry_entry["page_count"] == p["page_count"], f"nontechnical registry document/page identity: {did}")
        ocr_pages = []
        elements = indexed(d["elements"], "evidence_id")
        with pymupdf.open(pub["local_path"]) as pdf:
            require(len(pdf) == p["page_count"], f"actual PDF page count: {did}")
            for number, page in enumerate(pdf, 1):
                parsed = pages[number]
                require(parsed["native_text"] == page.get_text(), f"native page text differs from actual PDF: {did}:{number}")
                if number in approved_nontext:
                    require(parsed["text"] == parsed["native_text"] and len(parsed["native_text"].strip()) < 80, f"nontechnical page discarded/rewritten text: {did}:{number}")
                if parsed.get("parser") == "pymupdf":
                    table_texts = [e["text"] for e in elements.values() if e["pdf_page"] == number and e["kind"] == "table"]
                    require(parsed["text"] == "\n\n".join([parsed["native_text"], *table_texts]), f"native parser text differs: {did}:{number}")
                else:
                    require(parsed.get("parser") == "rapidocr", f"unexpected parser: {did}:{number}")
                    ocr_pages.append(number)
        require(not ({e["pdf_page"] for e in elements.values()} & set(approved_nontext)), f"nontechnical page unexpectedly contributes evidence: {did}")
        # All technical pages retain text evidence. Only independently approved
        # nontechnical pages or physically empty/header-only pages may omit it.
        with pymupdf.open(pub["local_path"]) as pdf:
            covered = {e["pdf_page"] for e in elements.values()}
            for number, page in enumerate(pdf, 1):
                if number in covered or number in approved_nontext:
                    continue
                body = pymupdf.Rect(0, page.rect.height * .12, page.rect.width, page.rect.height * .88)
                header_only = (len(page.get_text().strip()) < 80 and not page.get_images()
                    and not any(pymupdf.Rect(span["bbox"]).intersects(body) for block in page.get_text("dict")["blocks"] if "lines" in block for line in block["lines"] for span in line["spans"] if span["text"].strip())
                    and not any(drawing["rect"].y1 >= body.y0 and drawing["rect"].y0 <= body.y1 for drawing in page.get_drawings())
                    and not list(page.annots() or []))
                require(header_only, f"technical source page omitted from evidence: {did}:{number}")
        require({k: v for k, v in native.items() if v["doc_id"] == did} == elements, f"native global references differ: {did}")
        require({k: v for k, v in effective.items() if v["doc_id"] == did} == elements, f"effective global references differ: {did}")
        for eid, e in elements.items():
            require(e["doc_id"] == did and e["document_sha256"] == p["sha256"] and e["pdf_page"] in pages, f"evidence identity: {eid}")
            if e["kind"] == "table":
                from kg_pilot.table_evidence import validate_table_element
                validate_table_element(e, pages[e["pdf_page"]])
            else:
                require(e["parser"] == pages[e["pdf_page"]]["parser"], f"evidence parser differs: {eid}")
            require(e["normalized_text"] == normalize(e["text"]), f"evidence normalization: {eid}")
            require(quote_supported(e["text"], pages[e["pdf_page"]]["text"]), f"element not on actual parsed page: {eid}")
        require(classes[did] == d["classification"], f"published classification differs: {did}")
        check_classification(classes[did], did, p["sha256"], pages, taxonomy)
        published_claims = {k: v for k, v in claims.items() if v["doc_id"] == did}
        require(published_claims == indexed(d["claims"], "claim_id"), f"published claims differ: {did}")
        ref_count = 0
        for c in d["claims"]:
            Claim.model_validate(c)
            require(c["doc_id"] == did and c.get("ingestion_batch_id") == batch_id and c["review_status"] in {"api_reviewed", "api_model_reviewed", "independent_agent_reviewed"}, f"claim identity/review: {c['claim_id']}")
            for ref in c["evidence"]:
                e = elements.get(ref.get("evidence_id"))
                require(e is not None and e["pdf_page"] == ref["pdf_page"] and quote_supported(ref["quote"], e["text"]) and quote_supported(ref["quote"], pages[ref["pdf_page"]]["text"]), f"claim quote/reference: {c['claim_id']}")
                ref_count += 1
        review_records = review_audit(directory / "documents" / source["id"], d, pages)
        prov = source.get("provenance", {})
        require(prov and p.get("provenance") == prov == pub.get("provenance"), f"provenance not preserved: {did}")
        for field in ("source_url", "version", "publisher", "document_number", "accessed_at"):
            require(pub.get(field) == prov.get(field), f"provenance field changed {field}: {did}")
        require(str(prov.get("source_url", "")).startswith(("https://", "http://")) and prov.get("document_type") and "version" in prov, f"source URL/type/version field absent: {did}")
        require(p["sha256"] in selection, f"published source outside approved expansion selection: {did}")
        selected = selection[p["sha256"]]
        require(sha(selected["local_path"]) == p["sha256"], f"selected acquisition source hash changed: {did}")
        for field in ("source_url", "version", "document_type"):
            require(selected.get(field) == prov.get(field), f"selection provenance changed {field}: {did}")
        require(pub["source_type"] == classes[did]["document_form"], f"source type vs classification: {did}")
        require(manifest[did] == {**pub, "id": did}, f"collection manifest reference differs: {did}")
        require(readiness[did] == dict(source_sha256=p["sha256"], batch_id=batch_id, keyword_ready=True, semantic_ready=True, graph_ready=True), f"readiness reference differs: {did}")
        results.append(dict(doc_id=did, title=p["title"], pages=len(pages), elements=len(elements), claims=len(d["claims"]), claim_references=ref_count, review_records=review_records, source_sha256=p["sha256"],
                            source_url=prov["source_url"], pdf_url=prov.get("pdf_url"), version=prov["version"], document_type=prov["document_type"], document_form=classes[did]["document_form"], publication_status=classes[did]["publication_status"], document_status=pub.get("status"), ocr_pages=ocr_pages,
                            nontechnical_approved_pages=approved_nontext, status="passed", validity_boundary="来源元数据逐字段保留及原页/解析引用检查；OCR引用的签名视觉复核记录与源PDF哈希已机械核验，本脚本不重新OCR或替代逐条科学判断；未联网重认标准现行性。"))
    return results


def vector_audit(pilot, new_ids):
    import numpy as np
    from qdrant_client import QdrantClient
    records = read_json(pilot / "benchmarks/embedding/all_records.json")
    evidence = indexed(jsonl(pilot / "parsed/effective_evidence.jsonl"), "evidence_id")
    for record in records:
        source = evidence.get(record["evidence_id"])
        require(source is not None and (record["doc_id"], record["pdf_page"]) == (source["doc_id"], source["pdf_page"]), f"vector references missing/wrong source: {record['vector_id']}")
    new_evidence = {eid for eid, source in evidence.items() if source["doc_id"] in new_ids and source["normalized_text"].strip()}
    covered_new = {r["evidence_id"] for r in records if r["doc_id"] in new_ids}
    require(covered_new == new_evidence, "new evidence missing from semantic vector records")
    matrix = np.load(pilot / "benchmarks/embedding/all_vectors.npy", allow_pickle=False, mmap_mode="r")
    require(matrix.shape == (len(records), 1024) and np.isfinite(matrix).all(), "vector matrix shape/finite check")
    require((np.linalg.norm(matrix, axis=1) > 0).all(), "zero persisted vector")
    expected = {str(uuid.uuid5(uuid.NAMESPACE_URL, r["vector_id"])): (r, i) for i, r in enumerate(records)}
    require(len(expected) == len(records), "duplicate vector IDs")
    compared, max_difference, offset = 0, 0.0, None
    with closing(QdrantClient(path=str(pilot / "indexes/qdrant"))) as client:
        while True:
            points, offset = client.scroll(ingestion_publish.COLLECTION, limit=256, offset=offset, with_payload=True, with_vectors=True)
            for point in points:
                require(str(point.id) in expected, "orphan Qdrant point")
                record, i = expected[str(point.id)]
                require(point.payload == record, "Qdrant payload differs")
                delta = float(np.max(np.abs(np.asarray(point.vector, dtype=np.float32) - matrix[i])))
                require(delta <= 1e-6, f"Qdrant stored vector differs: {record['vector_id']}")
                max_difference = max(max_difference, delta); compared += 1
            if offset is None:
                break
    require(compared == len(records), "Qdrant vector count differs")
    return dict(status="passed", vectors=compared, new_vectors=sum(r["doc_id"] in new_ids for r in records), new_evidence_covered=len(covered_new), dimensions=1024, all_vector_source_references_valid=True, all_vectors_compared_to_matrix=True, max_absolute_difference=max_difference, tolerance=1e-6)


QUERY_SPECS = [
    dict(id="cu_polymer_150c", target_title="Low-Temperature Nanocrystalline Cu/polymer", keyword="nanocrystalline polymer 150", semantic="纳米晶铜与聚合物混合键合在150摄氏度低温下采用什么键合时间和CMP表面条件？", relevance_criterion="目标论文的nc-Cu/polymer、150°C/60 min或对应CMP条件；其他材料的同温度结果不自动算相关。"),
    dict(id="aec_mcm_scope", target_title="AEC-Q104", keyword="directly soldered MCM printed circuit board", semantic="AEC-Q104的适用范围是否是直接焊接到印刷电路板上的成品多芯片模块，而不是单独的内部元件？", relevance_criterion="AEC-Q104成品MCM适用对象/内部元件边界原文；单纯页眉含AEC编号不足以证明相关。"),
    dict(id="npl_ra_shape", target_title="The Measurement of Surface Texture", keyword="Ra profile shape", semantic="为什么表面粗糙度参数Ra不能代表表面轮廓的形状，不同形状能有相同的Ra吗？", relevance_criterion="NPL指南关于Ra缺乏轮廓形状信息的说明或对应示例；仅提到Ra校准的段落不自动算相关。"),
]


def retrieval_audit(pilot, papers, new_ids, encoder, limit):
    # Use the project's actual retrieval functions without filtered candidates,
    # reranking or title boosts. Passing a PILOT override only selects the dataset.
    previous = retrieval.PILOT
    retrieval.PILOT = pilot
    results = []
    try:
        for spec in QUERY_SPECS:
            targets = {did for did in new_ids if spec["target_title"].casefold() in (papers[did]["title"] + " " + str(papers[did].get("document_number", ""))).casefold()}
            require(targets, f"query target not in requested published batches: {spec['id']}")
            for method, query in (("keyword", spec["keyword"]), ("semantic_zh", spec["semantic"])):
                started = time.monotonic()
                hits = retrieval.lexical(query, limit) if method == "keyword" else retrieval.semantic(query, encoder, limit)
                ranked = []
                for rank, hit in enumerate(hits, 1):
                    ranked.append({**hit, "rank": rank, "title": papers[hit["doc_id"]]["title"], "is_new_document": hit["doc_id"] in new_ids, "is_target_document": hit["doc_id"] in targets})
                target_hits = [h for h in ranked if h["is_target_document"]]
                results.append(dict(query_id=spec["id"], method=method, query=query, limit=limit, target_doc_ids=sorted(targets), new_doc_ids_hit=sorted({h["doc_id"] for h in ranked if h["is_new_document"]}), target_document_hit=bool(target_hits), first_target_rank=target_hits[0]["rank"] if target_hits else None,
                                    latency_seconds=round(time.monotonic()-started, 3), relevance_criterion=spec["relevance_criterion"], passage_relevance_judgment="requires independent reading of returned passages; not inferred from document identity", hits=ranked))
    finally:
        retrieval.PILOT = previous
    return dict(query_encoder=dict(encoder.identity, local_snapshot=str(encoder.snapshot), cache_stats=encoder.stats(), actual_local_model=True),
                natural_ranking=True, target_filtering=False, retrieval_limit=limit, results=results,
                all_six_queries_hit_target_document=all(r["target_document_hit"] for r in results),
                boundary="命中与排名是实际运行结果；目标文献命中不等于段落回答充分。独立人工相关性判读需另行记录。未进行答案生成、答案正确性或拒答质量评估；非人工金标准基准。")


def audit(args):
    pilot = args.pilot.resolve(); report_dir = pilot / "reports/expansion_20261103"
    report = dict(schema="expansion-readonly-audit-v1", started_at=now(), batch_ids=args.batch_ids, expected_total=args.expected_total,
                  audit_scope="structure_only" if args.skip_retrieval else "structure_and_actual_retrieval",
                  formal_dataset_read_only=True, no_provider_credentials_or_api=True, no_rebuild=True, errors=[])
    encoder = None
    if not args.skip_retrieval:
        from scripts.expansion_embeddings import CachedEmbedder
        # All encoder writes are to a disposable audit cache, outside the pilot.
        require(not args.query_cache.resolve().is_relative_to(pilot), "query cache must remain outside the formal pilot")
        encoder = CachedEmbedder(pilot, args.query_cache.resolve())
        encoder.encode([q["semantic"] for q in QUERY_SPECS], batch_size=3, normalize_embeddings=True)
    with ingestion_publish.dataset_lock(pilot):
        ingestion_publish._ensure_idle(pilot)
        before = formal_fingerprint(pilot)
        try:
            paper_list = read_json(pilot / "catalog/papers.json")
            require(len(paper_list) == args.expected_total, f"catalog total {len(paper_list)} != {args.expected_total}")
            papers = indexed(paper_list, "doc_id")
            classes = indexed(read_json(pilot / "catalog/classifications.json")["records"], "doc_id")
            claims = indexed(jsonl(pilot / "annotations/claims.jsonl"), "claim_id")
            native = indexed(jsonl(pilot / "parsed/evidence.jsonl"), "evidence_id")
            effective = indexed(jsonl(pilot / "parsed/effective_evidence.jsonl"), "evidence_id")
            require(set(classes) == set(papers), "catalog/classification document set differs")
            report["baseline"] = baseline_audit(pilot, report_dir, papers, classes, claims)
            selection = indexed(read_json(report_dir / "source_selection.json")["selected"], "sha256")
            nontext_registry, report["nontechnical_page_registry"] = nontext_registry_audit(report_dir)
            documents = []
            for batch_id in args.batch_ids:
                require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", batch_id) and not batch_id.endswith("."), "invalid batch ID")
                documents.extend(batch_audit(pilot, batch_id, papers, classes, claims, native, effective, selection, nontext_registry))
            new_ids = {d["doc_id"] for d in documents}
            baseline_ids = {p["doc_id"] for p in read_json(report_dir / "baseline/catalog/papers.json")}
            require(not baseline_ids & new_ids and len(new_ids) == len(documents), "duplicate/overlapping audited document IDs")
            require(set(papers) == baseline_ids | new_ids, "published catalog contains documents outside baseline + requested batches")
            report["documents"] = documents
            report["counts"] = dict(documents=len(papers), baseline_documents=len(baseline_ids), new_documents=len(new_ids), claims=len(claims), new_claims=sum(d["claims"] for d in documents), new_evidence=sum(d["elements"] for d in documents), review_records=sum(len(d["review_records"]) for d in documents), effective_evidence=len(effective))
            ingestion_publish._validate_stage(pilot)
            report["publisher_validator"] = dict(status="passed", callable="ingestion_publish._validate_stage(PILOT)", dataset_lock_held=True, backends=["SQLite integrity/counts/payloads", "GraphML document/claim/evidence links", "Qdrant count/IDs/payloads"], rebuilt=False)
            report["vectors"] = vector_audit(pilot, new_ids)
            if encoder is not None:
                report["retrieval"] = retrieval_audit(pilot, papers, new_ids, encoder, args.limit)
                require(report["retrieval"]["all_six_queries_hit_target_document"], "one or more natural keyword/semantic rankings missed their target document")
            else:
                report["retrieval"] = dict(status="skipped", reason="explicit --skip-retrieval")
        except Exception as exc:
            report["errors"].append(dict(type=type(exc).__name__, message=str(exc)))
        finally:
            after = formal_fingerprint(pilot)
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            report["formal_files_unchanged"] = dict(passed=not changed, files_fingerprinted=len(before), changed_files=changed, excluded_lock_files=["runs/ingestion_dataset.lock", "indexes/qdrant/.lock"])
            if changed:
                report["errors"].append(dict(type="FormalDataChanged", message="Formal files changed during read-only audit", paths=changed))
    report["completed_at"] = now()
    report["status"] = "passed" if not report["errors"] else "failed"
    report["limitations"] = ["逐字引文检查只折叠空白；不会修复连字、断词、数字或单位。", "schema、来源链、签名及索引一致性是机械校验，不证明科学论断自身或厂商能力真实。", "来源URL/版本/文档类型按采集记录和已复核原页保留；本次不联网重新核验所有链接或标准现行性。", "OCR页保留OCR来源标记；自动验收不替代原图视觉复核。", "复核记录来自独立Codex agent，为自动复核而非人工金标准。"]
    output = args.output.resolve() if args.output else report_dir / f"audit_{args.expected_total}.json"
    allowed = report_dir.resolve()
    require(output.parent == allowed and "audit" in output.name and output.suffix == ".json", "audit report must be an *audit*.json in expansion report directory")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(status=report["status"], output=str(output), counts=report.get("counts"), errors=report["errors"], formal_files_unchanged=report["formal_files_unchanged"]["passed"]), ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-ids", "--batch_ids", nargs="+", required=True)
    parser.add_argument("--expected-total", "--expected_total", type=int, required=True)
    parser.add_argument("--pilot", type=Path, default=ROOT / "pilot")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--query-cache", type=Path, default=ROOT / ".cache/expansion_audit_queries")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--skip-retrieval", action="store_true", help="structural diagnostic only; not a complete retrieval acceptance")
    args = parser.parse_args()
    require(args.expected_total >= 26 and args.limit >= 3, "invalid expected total/retrieval limit")
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
