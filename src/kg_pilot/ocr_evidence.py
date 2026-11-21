"""Replace only an identified broken text layer with a traceable OCR alternative."""
from kg_pilot.pipeline import (PILOT, LegacyRebuildError, ensure_legacy_rebuild_safe,
                               read_json, jsonl, write_jsonl, write_json, normalize)


def main():
    ensure_legacy_rebuild_safe(PILOT)
    doc_id = "reliability_small_die_2022"
    source_path = PILOT / "benchmarks" / "docling" / f"{doc_id}_p1-7" / "document.json"
    document = read_json(source_path)
    paper = next(x for x in read_json(PILOT / "catalog" / "papers.json") if x["doc_id"] == doc_id)
    sha = paper["sha256"]
    records = []
    seen = set()
    for i, item in enumerate(document.get("texts", [])):
        for prov in item.get("prov", []):
            identity = (i, prov["page_no"])
            if identity in seen:
                continue
            seen.add(identity)
            records.append({"evidence_id": f"{doc_id}:p{prov['page_no']}:ocr{i}", "doc_id": doc_id,
                            "pdf_page": prov["page_no"], "bbox": prov["bbox"], "kind": "text",
                            "text": item["text"], "normalized_text": normalize(item["text"]),
                            "document_sha256": sha, "parser": "docling_rapidocr", "source_ref": item["self_ref"],
                            "source_path": str(source_path), "review_status": "ocr_source_requires_claim_level_review"})
    # OCR output is audited, never decoded using another layer's native glyphs.
    from kg_pilot.text_quality import repair_document_text
    _, records, _, quality = repair_document_text(paper["local_path"], [], records, document_sha256=sha)
    write_json(PILOT / "parsed" / doc_id / "text_quality_ocr.json", quality)
    native = jsonl(PILOT / "parsed" / "evidence.jsonl")
    effective = [x for x in native if x["doc_id"] != doc_id] + records
    write_jsonl(PILOT / "parsed" / "effective_evidence.jsonl", effective)
    write_jsonl(PILOT / "parsed" / doc_id / "elements_ocr.jsonl", records)
    write_json(PILOT / "reports" / "evidence_routing.json", {"native_elements": len(native), "effective_elements": len(effective),
               "ocr_replaced_document": doc_id, "ocr_elements": len(records),
               "native_source_preserved": True, "reason": "font text mapping is garbled; zero replacement characters did not detect this"})


if __name__ == "__main__":
    try:
        main()
    except LegacyRebuildError as error:
        raise SystemExit(str(error)) from None
