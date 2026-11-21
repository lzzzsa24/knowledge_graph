"""Audit and recoverably repair published text using each PDF's own glyphs.

Preview is read-only apart from the permanent dataset lock. Apply uses the
existing publication journal, maintenance flag, retained originals and staged
SQLite/GraphML/Qdrant validation. It never downloads a PDF or a model. Unknown
characters remain verbatim with their audit findings; no OCR or guessed symbol
substitution is introduced.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
import json
from pathlib import Path
import uuid

from kg_pilot import ingestion_publish as publication
from kg_pilot.pipeline import PILOT, jsonl, normalize, read_json, write_json, write_jsonl
from kg_pilot import reading_glyphs
from kg_pilot.reading_sources import REVIEW_INPUTS, render_paper_reading_copy
from kg_pilot.validation import quote_supported


REGISTRY = "catalog/text_quality_backfills.json"
AUDIT = "reports/text_quality_backfill.json"
QUARANTINE = "annotations/text_quality_quarantine.jsonl"
CLAIM_FIELDS = ("statement", "material_system", "sample_id", "quantity", "value_text", "unit", "conditions")


def _pages(value):
    records = value.get("pages") if isinstance(value, dict) else value
    if not isinstance(records, list):
        raise publication.PublicationError("逐页文本格式无效。")
    return records


def _active(value):
    """Do not reinterpret preserved raw text or prior audit examples as active."""
    if isinstance(value, dict):
        return {key: _active(item) for key, item in value.items()
                if not key.startswith("raw_") and key not in {"text_quality", "text_quality_audit", "native_text"}}
    if isinstance(value, list):
        return [_active(item) for item in value]
    return value


def _issues(value):
    from kg_pilot.text_quality import scan_text_quality
    def walk(item, path=""):
        if isinstance(item, str):
            return [dict(issue, field=path) for issue in scan_text_quality(item)]
        if isinstance(item, dict):
            return [issue for key, value in item.items() for issue in walk(value, f"{path}.{key}".strip("."))]
        if isinstance(item, (list, tuple)):
            return [issue for index, value in enumerate(item) for issue in walk(value, f"{path}[{index}]")]
        return []
    return walk(_active(value))


def _transfer_quote(quote, original, corrected, *, extra_candidates=()):
    """Pull only proven character changes at exact source occurrence offsets.

    Whitespace differs in legacy claim quotes, so alignment excludes whitespace
    but keeps every other character exact. Multiple matching occurrences must
    produce the same result. Partial first/last PDF lines are fully supported.
    """
    def glyph_candidate(char):
        return reading_glyphs.is_glyph_candidate(char, extra_candidates)

    quote_offsets = [i for i, c in enumerate(quote) if not c.isspace() or glyph_candidate(c)]
    before = "".join(c for c in original if not c.isspace() or glyph_candidate(c))
    after = "".join(c for c in corrected if not c.isspace() or glyph_candidate(c))
    needle = "".join(quote[i] for i in quote_offsets)
    if not needle:
        return None
    correspondence = {}
    for kind, lo, hi, new_lo, new_hi in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if kind == "equal" or kind == "replace" and hi - lo == new_hi - new_lo:
            correspondence.update({i: after[new_lo + i - lo] for i in range(lo, hi)})
        elif kind == "replace" and hi - lo == 1 and glyph_candidate(before[lo]):
            # A source-verified ligature can legitimately expand one bad code
            # point into several letters (for example Bo<bad>om -> Bottom).
            correspondence[lo] = after[new_lo:new_hi]
    candidates, start = set(), 0
    while True:
        offset = before.find(needle, start)
        if offset < 0:
            break
        result = list(quote)
        for index, char in enumerate(needle):
            transferred = correspondence.get(offset + index)
            if transferred is None:
                break
            if glyph_candidate(char):
                result[quote_offsets[index]] = transferred
            elif char != transferred:
                # A non-glyph source change cannot authorize this conversion.
                break
        else:
            candidates.add("".join(result))
        start = offset + 1
    return next(iter(candidates)) if len(candidates) == 1 else None


def repair_claim_text_quality(pdf_path, claims, pages, elements, *, original_pages=None, original_elements=None,
                              extra_candidates=()):
    """Repair a claim only from its source-aligned quotations.

    A per-claim mapping is propagated to derived unit/statement/condition text
    only when all quoted occurrences of a PUA character agree. It is never a
    document-wide or corpus-wide character replacement. Every changed quote
    must still occur exactly in the corrected source record. Raw canonical
    fields and raw quotes remain alongside the corrected values.
    """
    def glyph_candidate(char):
        return reading_glyphs.is_glyph_candidate(char, extra_candidates)

    source_pages = {page["pdf_page"]: page for page in (original_pages or pages)}
    new_pages = {page["pdf_page"]: page for page in pages}
    source_elements = {item["evidence_id"]: item for item in (original_elements or elements)}
    new_elements = {item["evidence_id"]: item for item in elements}
    repaired, audit = [], []
    for original in claims:
        result = deepcopy(original)
        alternatives = defaultdict(set)
        for reference in result.get("evidence", []):
            quote = reference.get("quote", "")
            if not any(glyph_candidate(c) for c in quote):
                continue
            page_number = reference.get("pdf_page")
            eid = reference.get("evidence_id")
            changed = None
            for old, new in ((source_elements.get(eid, {}), new_elements.get(eid, {})),
                             (source_pages.get(page_number, {}), new_pages.get(page_number, {}))):
                if quote_supported(quote, old.get("text", "")):
                    candidate = _transfer_quote(quote, old.get("text", ""), new.get("text", ""),
                                                extra_candidates=extra_candidates)
                    if candidate is not None and quote_supported(candidate, new.get("text", "")):
                        changed = candidate
                        break
            if changed is None:
                for char in quote:
                    if glyph_candidate(char):
                        alternatives[char].add(None)
                continue
            for kind, lo, hi, new_lo, new_hi in SequenceMatcher(None, quote, changed, autojunk=False).get_opcodes():
                if kind == "equal":
                    for char in quote[lo:hi]:
                        if glyph_candidate(char):
                            alternatives[char].add(None)
                elif kind == "replace" and hi - lo == 1 and glyph_candidate(quote[lo]):
                    alternatives[quote[lo]].add(changed[new_lo:new_hi])
                elif kind == "replace" and hi - lo == new_hi - new_lo:
                    for before, after in zip(quote[lo:hi], changed[new_lo:new_hi]):
                        if glyph_candidate(before):
                            alternatives[before].add(after)
                else:
                    for char in quote[lo:hi]:
                        if glyph_candidate(char):
                            alternatives[char].add(None)
            if changed != quote:
                reference.setdefault("raw_quote", quote)
                reference["quote"] = changed
        # A source-mapped ASCII letter is only proven at the quoted position.
        # Derived fields can contain the same letter with a different meaning.
        mapping = {before: next(iter(choices)) for before, choices in alternatives.items()
                   if not (before.isascii() and before.isprintable()) and len(choices) == 1 and None not in choices}

        def replace(value):
            if isinstance(value, str):
                return "".join(mapping.get(c, c) for c in value)
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        raw_fields = dict(result.get("raw_text_fields", {}))
        for field in CLAIM_FIELDS:
            if field in result:
                corrected = replace(result[field])
                if corrected != result[field]:
                    raw_fields.setdefault(field, deepcopy(result[field]))
                    result[field] = corrected
        if raw_fields:
            result["raw_text_fields"] = raw_fields
        before, after = _issues(original), _issues(result)
        changed_fields = [field for field in CLAIM_FIELDS if original.get(field) != result.get(field)]
        quote_changes = sum(a.get("quote") != b.get("quote") for a, b in zip(original.get("evidence", []), result.get("evidence", [])))
        if changed_fields or quote_changes or before or after:
            finding = {"claim_id": original["claim_id"], "changed_fields": changed_fields,
                       "changed_quotes": quote_changes, "remaining_issues": after,
                       "method": "source_aligned_claim_references", "status": "unresolved" if after else "repaired"}
            if result != original or "text_quality" not in result:
                result["text_quality"] = finding
            audit.append(finding)
        repaired.append(result)
    return repaired, audit


def _changed_payload(old, new):
    return _active(old) != _active(new)


def _read_snapshot(pilot, doc_ids):
    from kg_pilot.text_quality import guard_claim_text_quality, repair_document_text

    papers = read_json(pilot / "catalog/papers.json")
    catalog = {paper["doc_id"]: paper for paper in papers}
    if len(catalog) != len(papers):
        raise publication.PublicationError("文献目录存在重复 doc_id。")
    selected = list(catalog) if doc_ids is None else list(doc_ids)
    if not selected or len(set(selected)) != len(selected) or not set(selected).issubset(catalog):
        raise publication.PublicationError("请选择不重复的已发布 doc_id。")
    for doc_id in selected:
        publication._safe_id(doc_id, "doc_id")
    sources = list(publication._SOURCE_FILES) + list(REVIEW_INPUTS) + [REGISTRY, AUDIT, QUARANTINE, "reports/unit_normalization.json"]
    previous_registry = read_json(pilot / REGISTRY) if (pilot / REGISTRY).exists() else {"documents": {}}
    old_native = jsonl(pilot / "parsed/evidence.jsonl")
    effective_path = pilot / "parsed/effective_evidence.jsonl"
    old_effective = jsonl(effective_path) if effective_path.exists() else deepcopy(old_native)
    old_claims = jsonl(pilot / "annotations/claims.jsonl")
    old_classifications = read_json(pilot / "catalog/classifications.json")
    classification_records = old_classifications.get("records", [])
    classifications_by_doc = {row["doc_id"]: row for row in classification_records}
    new_classifications = {}
    old_quarantine = jsonl(pilot / QUARANTINE)
    quarantined = {row["claim_id"]: row for row in old_quarantine}
    updates, documents, changed_ids = {}, [], set()
    native_by_doc, effective_by_doc, claims_by_doc = defaultdict(list), defaultdict(list), defaultdict(list)
    for rows, grouped in ((old_native, native_by_doc), (old_effective, effective_by_doc), (old_claims, claims_by_doc)):
        for row in rows:
            grouped[row["doc_id"]].append(row)
    active_claim_ids = {row["claim_id"] for row in old_claims}
    for row in old_quarantine:
        if row["claim_id"] in active_claim_ids:
            raise publication.PublicationError("事实同时存在于活动库和乱码待复核区。")
        claims_by_doc[row["doc_id"]].append(row["claim"])
    new_native, new_effective, new_claims = {}, {}, {}
    for doc_id in selected:
        paper = catalog[doc_id]
        pdf = Path(paper["local_path"]).resolve()
        if not pdf.is_file() or publication._sha(pdf) != paper["sha256"]:
            raise publication.PublicationError(f"已发布 PDF 的 SHA-256 不符：{doc_id}")
        relative = Path("parsed") / doc_id
        page_file = read_json(pilot / relative / "pages.json")
        old_pages = _pages(page_file)
        if len(old_pages) != paper["page_count"] or {p["pdf_page"] for p in old_pages} != set(range(1, paper["page_count"] + 1)):
            raise publication.PublicationError(f"逐页文本与已发布页数不符：{doc_id}")
        elements = jsonl(pilot / relative / "elements.jsonl")
        ocr_path = pilot / relative / "elements_ocr.jsonl"
        ocr_elements = jsonl(ocr_path)
        tables_path = pilot / relative / "tables.json"
        tables = read_json(tables_path) if tables_path.exists() else []
        groups = [elements, native_by_doc[doc_id], effective_by_doc[doc_id], ocr_elements]
        # Load the embedded fonts once for this PDF. Repeated records are the
        # actual independently stored local/native/effective copies, all of
        # which need an identical repair policy and retained raw fields.
        pages, repaired_records, tables_new, document_audit = repair_document_text(
            pdf, old_pages, [row for group in groups for row in group], tables=tables,
            document_sha256=paper["sha256"])
        split, offset = [], 0
        for group in groups:
            split.append(repaired_records[offset:offset + len(group)])
            offset += len(group)
        elements_new, native_new, effective_new, ocr_new = split
        document_audit["record_scope"] = "pages_and_stored_local_native_effective_ocr_copies"
        document_audit["counting_note"] = "Counts are stored field occurrences; duplicated text/normalized/table fields are not unique physical glyphs."
        extra_candidates = reading_glyphs.reviewed_input_characters(paper["sha256"])
        claims_new, claim_audit = repair_claim_text_quality(pdf, claims_by_doc[doc_id], pages, effective_new,
            original_pages=old_pages, original_elements=effective_by_doc[doc_id], extra_candidates=extra_candidates)
        classification = classifications_by_doc.get(doc_id)
        classification_changed = False
        if classification is not None and classification.get("evidence"):
            # Classification decisions/tags are unchanged; only their quoted
            # explanatory source excerpts follow the canonical source repair.
            repaired_classification, _ = repair_claim_text_quality(pdf,
                [{"claim_id": doc_id + ":classification", "evidence": classification["evidence"]}],
                pages, effective_new, original_pages=old_pages, original_elements=effective_by_doc[doc_id],
                extra_candidates=extra_candidates)
            if repaired_classification[0]["evidence"] != classification["evidence"]:
                new_classifications[doc_id] = dict(classification, evidence=repaired_classification[0]["evidence"])
                classification_changed = True
        eligible_claims = []
        for claim in claims_new:
            try:
                guard_claim_text_quality(claim)
            except ValueError:
                quarantined[claim["claim_id"]] = {"claim_id": claim["claim_id"], "doc_id": doc_id,
                    "status": "text_quality_needs_review", "reason": "unresolved_quantitative_text",
                    "source_sha256": paper["sha256"], "claim": claim}
            else:
                eligible_claims.append(claim)
                quarantined.pop(claim["claim_id"], None)
        for old, new in zip(effective_by_doc[doc_id], effective_new):
            if old.get("normalized_text") != new.get("normalized_text") or old.get("text") != new.get("text"):
                changed_ids.add(old["evidence_id"])
        new_native.update({row["evidence_id"]: row for row in native_new})
        new_effective.update({row["evidence_id"]: row for row in effective_new})
        new_claims.update({row["claim_id"]: row for row in eligible_claims})
        page_output = dict(page_file, pages=pages) if isinstance(page_file, dict) else pages
        candidates = {relative / "pages.json": (page_file, page_output, "json"),
                      relative / "elements.jsonl": (elements, elements_new, "jsonl")}
        audit_path = pilot / relative / "text_quality.json"
        candidates[relative / "text_quality.json"] = (read_json(audit_path) if audit_path.exists() else None,
                                                      document_audit, "json")
        if tables_path.exists():
            candidates[relative / "tables.json"] = (tables, tables_new, "json")
        if ocr_path.exists():
            candidates[relative / "elements_ocr.jsonl"] = (ocr_elements, ocr_new, "jsonl")
        for path, (before, after, mode) in candidates.items():
            sources.append(str(path))
            if before != after:
                updates[path] = (mode, after)
        markdown_path = pilot / relative / "fulltext.md"
        sources.extend([str(relative / "fulltext.md"), str(relative / "elements_ocr.jsonl"), str(relative / "fulltext.txt"), str(relative / "fulltext.raw.txt"), str(pdf)])
        markdown = render_paper_reading_copy(pilot, paper, pages, elements_new, markdown_path=markdown_path,
                                            tables=tables_new, ocr_elements=ocr_new)
        if not markdown_path.exists() or markdown_path.read_bytes() != markdown.encode("utf-8"):
            updates[relative / "fulltext.md"] = ("text", markdown)
        fulltext_path = pilot / relative / "fulltext.txt"
        if fulltext_path.exists() and any(a.get("text") != b.get("text") for a, b in zip(old_pages, pages)):
            # Legacy exports used layout text; retain that exact original and
            # rebuild the active convenience text from the corrected pages.
            raw_path = relative / "fulltext.raw.txt"
            if not (pilot / raw_path).exists():
                updates[raw_path] = ("bytes", fulltext_path.read_bytes())
            updates[relative / "fulltext.txt"] = ("text", "\n\n".join(page["text"] for page in pages))
        if publication._sha(pdf) != paper["sha256"]:
            raise publication.PublicationError(f"检查期间原 PDF 已变化：{doc_id}")
        documents.append({"doc_id": doc_id, "source_sha256": paper["sha256"], "page_count": len(pages),
                          "audit": document_audit, "claims": claim_audit,
                          "changed_pages": sum(_changed_payload(a, b) for a, b in zip(old_pages, pages)),
                          "changed_blocks": sum(_changed_payload(a, b) for a, b in zip(elements, elements_new)),
                          "changed_claims": sum(_changed_payload(a, b) for a, b in zip(claims_by_doc[doc_id], claims_new)),
                          "changed_classification_quotes": classification_changed,
                          "detected_active_issues_before": sum(len(_issues(row.get("text", ""))) for row in effective_by_doc[doc_id]),
                          "remaining_parsed_issues": len(_issues(pages)) + len(_issues(elements_new)) + len(_issues(tables_new)) + len(_issues(ocr_new)),
                          "remaining_active_issues": sum(len(_issues(row.get("text", ""))) for row in effective_new),
                          "unresolved_evidence_ids": [row["evidence_id"] for row in effective_new if _issues(row.get("text", ""))],
                          "remaining_claim_issues": len(_issues(claims_new)),
                          "quarantined_claim_ids": [row["claim_id"] for row in claims_new if row["claim_id"] in quarantined]})
    native = [new_native.get(row["evidence_id"], row) for row in old_native]
    effective = [new_effective.get(row["evidence_id"], row) for row in old_effective]
    claims = [new_claims.get(row["claim_id"], row) for row in old_claims if row["claim_id"] not in quarantined]
    claims.extend(row for key, row in new_claims.items() if key not in active_claim_ids)
    quarantine = list(quarantined.values())
    if quarantine != old_quarantine:
        updates[Path(QUARANTINE)] = ("jsonl", quarantine)
    if new_classifications:
        updates[Path("catalog/classifications.json")] = ("json", dict(old_classifications,
            records=[new_classifications.get(row["doc_id"], row) for row in classification_records]))
    for name, before, after in (("parsed/evidence.jsonl", old_native, native),
                                ("parsed/effective_evidence.jsonl", old_effective, effective),
                                ("annotations/claims.jsonl", old_claims, claims)):
        if before != after:
            updates[Path(name)] = ("jsonl", after)
    effective_map = {row["evidence_id"]: row for row in effective}
    # Re-encode existing embeddings containing corrupt characters, and legacy
    # decoded chunks that were not faithful substrings (e.g. "µm" -> "m").
    # Do not add embeddings to previously ineligible short/empty evidence.
    old_records_path = pilot / "benchmarks/embedding/all_records.json"
    records = read_json(old_records_path) if old_records_path.exists() else []
    changed_ids.intersection_update({row["evidence_id"] for row in records})
    for row in records:
        source = effective_map.get(row["evidence_id"])
        if source is not None and source["doc_id"] in selected:
            text = row.get("embedding_text", "")
            previous = previous_registry.get("documents", {}).get(source["doc_id"], {})
            migrated = previous.get("embedding_migration_version") == 1 and previous.get("source_sha256") == catalog[source["doc_id"]]["sha256"]
            if (_issues(text) and not migrated) or normalize(text) not in normalize(source["normalized_text"]):
                changed_ids.add(row["evidence_id"])
    fingerprint = publication._fingerprint(pilot, sources)
    for document in documents:
        fingerprint[str(Path(catalog[document["doc_id"]]["local_path"]).resolve())] = document["source_sha256"]
    return dict(papers=papers, native=native, effective=effective, claims=claims,
                updates=updates, documents=documents, changed_ids=changed_ids,
                fingerprint=fingerprint, records=records, quarantine=quarantine)


def _report(snapshot):
    documents = snapshot["documents"]
    return {"version": 1, "checked_documents": len(documents),
            "checked_pages": sum(row["page_count"] for row in documents),
            "changed_pages": sum(row["changed_pages"] for row in documents),
            "changed_blocks": sum(row["changed_blocks"] for row in documents),
            "changed_claims": sum(row["changed_claims"] for row in documents),
            "changed_classifications": sum(row["changed_classification_quotes"] for row in documents),
            "affected_documents": sum(bool(row["changed_pages"] or row["changed_blocks"] or row["changed_claims"]) for row in documents),
            "refreshed_embedding_evidence": len(snapshot["changed_ids"]),
            "refreshed_embedding_evidence_ids": sorted(snapshot["changed_ids"]),
            "remaining_active_issues": sum(row["remaining_active_issues"] for row in documents),
            "detected_active_issues_before": sum(row["detected_active_issues_before"] for row in documents),
            "remaining_parsed_issues": sum(row["remaining_parsed_issues"] for row in documents),
            "remaining_claim_issues": sum(row["remaining_claim_issues"] for row in documents),
            "quarantined_claims": sum(len(row["quarantined_claim_ids"]) for row in documents),
            "changed_files": sorted(str(path).replace("\\", "/") for path in snapshot["updates"]),
            "changed_file_count": len(snapshot["updates"]),
            "raw_preserved": True, "unresolved_policy": "retain_verbatim_and_flag_no_guessing",
            "documents": documents}


def preview_text_quality(pilot=PILOT, *, doc_ids=None, prepare_snapshot=None):
    pilot = Path(pilot).resolve()
    with publication.dataset_lock(pilot):
        publication._ensure_idle(pilot)
        snapshot = _read_snapshot(pilot, doc_ids)
        if prepare_snapshot is not None:
            prepare_snapshot(pilot, snapshot)
    return {"status": "preview", **_report(snapshot)}


def backfill_text_quality(pilot=PILOT, *, doc_ids=None, embedder=None, index_builder=None, progress=None,
                          prepare_snapshot=None, batch_prefix="text-quality-"):
    """Publish one recoverable snapshot; repeated applications are a no-op."""
    import numpy as np
    from kg_pilot.units import normalize_single

    pilot = Path(pilot).resolve()
    batch_id = batch_prefix + uuid.uuid4().hex
    stage = pilot / "ingestion/staging" / batch_id
    with publication.dataset_lock(pilot):
        publication._ensure_idle(pilot)
        snapshot = _read_snapshot(pilot, doc_ids)
        if prepare_snapshot is not None:
            prepare_snapshot(pilot, snapshot)
        report = _report(snapshot)
        if progress:
            progress({"phase": "snapshot_validated", "checked_documents": report["checked_documents"],
                      "refreshed_embedding_evidence": report["refreshed_embedding_evidence"],
                      "quarantined_claims": report["quarantined_claims"]})
        if not snapshot["updates"] and not snapshot["changed_ids"]:
            return {"status": "already_backfilled", **report}
        stage.mkdir(parents=True)
        # Freeze before releasing the lock, including exact old vectors.
        source = stage / "source/benchmarks/embedding"
        old_vectors_path = pilot / "benchmarks/embedding/all_vectors.npy"
        if old_vectors_path.exists() != (pilot / "benchmarks/embedding/all_records.json").exists():
            raise publication.PublicationError("旧向量记录和矩阵不完整。")
        if old_vectors_path.exists():
            vectors = np.load(old_vectors_path, allow_pickle=False)
            if vectors.ndim != 2 or len(vectors) != len(snapshot["records"]):
                raise publication.PublicationError("旧向量记录和矩阵不一致。")
            keep = [i for i, row in enumerate(snapshot["records"]) if row["evidence_id"] not in snapshot["changed_ids"]]
            source.mkdir(parents=True)
            write_json(source / "all_records.json", [snapshot["records"][i] for i in keep])
            np.save(source / "all_vectors.npy", vectors[keep])
        if (pilot / "benchmarks/embedding").exists():
            publication._copy(pilot / "benchmarks/embedding", stage / "benchmarks/embedding")
    try:
        targets = list(snapshot["updates"])
        for relative, (mode, value) in snapshot["updates"].items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if mode == "json":
                write_json(path, value)
            elif mode == "jsonl":
                write_jsonl(path, value)
            elif mode == "bytes":
                path.write_bytes(value)
            else:
                path.write_text(value, encoding="utf-8", newline="\n")
        # Index inputs are always complete, even if only a subset was repaired.
        write_json(stage / "catalog/papers.json", snapshot["papers"])
        write_jsonl(stage / "annotations/claims.jsonl", snapshot["claims"])
        write_jsonl(stage / "parsed/effective_evidence.jsonl", snapshot["effective"])
        vector_count = publication._vectors(stage, pilot, snapshot["effective"], set(), embedder,
            vector_source=stage / "source", new_evidence_ids=snapshot["changed_ids"])
        if progress:
            progress({"phase": "vectors_ready", "vector_count": vector_count})
        (index_builder or publication._build_indexes)(stage)
        publication._validate_stage(stage, quality_doc_ids={row["doc_id"] for row in snapshot["documents"]})
        if progress:
            progress({"phase": "staged_indexes_validated"})
        report.update(batch_id=batch_id, completed_at=publication._now())
        registry = {"version": 1, "documents": {}}
        if (pilot / REGISTRY).exists():
            registry = read_json(pilot / REGISTRY)
        for row in snapshot["documents"]:
            registry["documents"][row["doc_id"]] = {"source_sha256": row["source_sha256"],
                "batch_id": batch_id, "remaining_active_issues": row["remaining_active_issues"],
                "remaining_claim_issues": row["remaining_claim_issues"], "embedding_migration_version": 1}
        write_json(stage / REGISTRY, registry)
        write_json(stage / AUDIT, report)
        unit_rows = [{"claim_id": c["claim_id"], "original_value": c["value_text"], "original_unit": c["unit"],
                      **normalize_single(c["value_text"], c["unit"])} for c in snapshot["claims"]]
        write_json(stage / "reports/unit_normalization.json", unit_rows)
        targets.extend([Path("indexes"), Path("benchmarks/embedding"), Path(REGISTRY), Path(AUDIT), Path("reports/unit_normalization.json")])
        backup = publication._commit(stage, pilot, list(dict.fromkeys(targets)), snapshot["fingerprint"], batch_id)
        result = {"status": "backfilled", "backup_path": str(backup), "vector_count": vector_count, **report}
        publication._atomic_json(stage / "publication_result.json", result)
        return result
    except Exception as error:
        publication._atomic_json(stage / "publication_failure.json", {"batch_id": batch_id,
            "status": "failed", "error_type": type(error).__name__, "time": publication._now()})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preview", "apply", "recover", "status"])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="Audit all currently published papers")
    selection.add_argument("--doc-id", action="append")
    parser.add_argument("--pilot", type=Path, default=PILOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--threads", type=int, default=4, help="Bound local PyTorch CPU threads for apply (default: 4)")
    args = parser.parse_args(argv)
    if args.action in {"preview", "apply"} and not (args.all or args.doc_id):
        parser.error("preview/apply requires --all or explicit --doc-id")
    if args.action == "preview":
        result = preview_text_quality(args.pilot, doc_ids=args.doc_id)
    elif args.action == "apply":
        if args.threads < 1:
            parser.error("--threads must be positive")
        import torch
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
        result = backfill_text_quality(args.pilot, doc_ids=args.doc_id,
            progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    elif args.action == "recover":
        result = publication.recover_publication(args.pilot)
    else:
        result = publication.maintenance_status(args.pilot)
    if args.output:
        write_json(args.output, result)
    # Full audit stays in --output; console output is intentionally compact.
    print(json.dumps({key: value for key, value in result.items()
                      if key not in {"documents", "changed_files", "refreshed_embedding_evidence_ids"}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
