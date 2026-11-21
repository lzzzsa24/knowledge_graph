"""Read-only screening of the published corpus for the 2026-11-13 audit."""

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata
from urllib.parse import unquote

import pymupdf


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot.text_quality import guard_claim_text_quality

PILOT = ROOT / "pilot"
REPORT = PILOT / "reports" / "quality_review_20261113" / "local_screen.json"
LOW_VISIBLE = 80
MANIFESTS = (
    "collection/imec/manifest.json",
    "collection/journals/manifest.json",
    "collection/reliability/manifest.json",
    "collection/batches/manifest.json",
)
FROZEN = (
    "catalog/papers.json",
    "catalog/classifications.json",
    "catalog/paper_metadata_overrides.json",
    "catalog/ingestion_readiness.json",
    "catalog/text_quality_backfills.json",
    "annotations/claims.jsonl",
    "indexes/pilot.sqlite",
    "benchmarks/embedding/all_records.json",
    *MANIFESTS,
)
PROVENANCE = (
    "source_url", "publisher_url", "pdf_url", "resolved_url", "discovery_url",
    "accessed_at", "retrieved_date", "retrieval_date", "retrieved_on", "downloaded_date",
    "publisher", "doi", "year", "authors",
)
SPECIAL = str.maketrans({
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st", "\u00b5": "μ",
    "\u00ad": None, "\u200b": None, "\ufeff": None,
})
DOI = re.compile(r"^10\.\d{4,9}/[^\s]+$", re.IGNORECASE)
DOI_NOTE = re.compile(r"\b10\.\d{4,9}/[a-z0-9._;()/:-]+", re.IGNORECASE)
MD_PAGE = re.compile(r"^## PDF 第 (\d+) 页\s*$", re.MULTILINE)
DECODE_WARNING = re.compile(r"premature end of data|openjpeg warning|JPX numcomps", re.I)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_lines(path):
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def pdf_path(value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(PILOT / "collection") or path.suffix.lower() != ".pdf":
        raise ValueError(f"Path outside the PDF collection: {value}")
    return path


def spaced(value):
    return " ".join(value.split())


def normalized_quote(value):
    return spaced(unicodedata.normalize("NFC", value.translate(SPECIAL)))


def quote_mode(quote, source):
    if not spaced(quote):
        return None
    if spaced(quote) in spaced(source):
        return "whitespace"
    normalized = normalized_quote(quote)
    if normalized and normalized in normalized_quote(source):
        return "unicode"
    return None


def norm_doi(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", "", value.strip(), flags=re.I)
    value = unquote(value).casefold()
    return value if DOI.fullmatch(value) else None


def norm_title(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def stats(value):
    return {
        "visible_chars": sum(not char.isspace() for char in value),
        "replacement_chars": value.count("\ufffd"),
        "private_use_chars": sum(unicodedata.category(char) == "Co" for char in value),
        "unexpected_control_chars": sum(unicodedata.category(char) == "Cc" and not char.isspace()
                                        for char in value),
    }


def inspect_pdf(path):
    info = {"exists": path.is_file(), "opens": False, "page_count": None}
    pages = {}
    try:
        data = path.read_bytes()
        info.update(sha256=digest(data), bytes=len(data), header_marker=b"%PDF-" in data[:1024],
                    eof_marker=b"%%EOF" in data[-4096:])
        pymupdf.TOOLS.mupdf_warnings()
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            info.update(opens=True, page_count=len(doc), parser_repaired=bool(doc.is_repaired),
                        password_required=bool(doc.needs_pass))
            messages = pymupdf.TOOLS.mupdf_warnings().splitlines()
            page_messages = {}
            for page_number in range(1, len(doc) + 1):
                try:
                    pages[page_number] = doc[page_number - 1].get_text()
                except (RuntimeError, ValueError) as exc:
                    info.setdefault("page_errors", {})[page_number] = str(exc)
                page_warnings = pymupdf.TOOLS.mupdf_warnings().splitlines()
                if page_warnings:
                    page_messages[page_number] = list(dict.fromkeys(page_warnings))
                    messages.extend(page_warnings)
            if page_messages:
                info["page_parser_messages"] = page_messages
            if messages:
                info["parser_messages"] = list(dict.fromkeys(messages))
        if any(DECODE_WARNING.search(message) for message in messages):
            renders = {}
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                pymupdf.TOOLS.mupdf_warnings()
                for number, page in enumerate(doc, 1):
                    render = {}
                    try:
                        pix = page.get_pixmap(matrix=pymupdf.Matrix(0.35, 0.35), alpha=False)
                        render.update(ok=True, width=pix.width, height=pix.height, pixel_sha256=digest(pix.samples))
                    except (RuntimeError, ValueError) as exc:
                        render.update(ok=False, error=str(exc))
                    render["messages"] = list(dict.fromkeys(pymupdf.TOOLS.mupdf_warnings().splitlines()))
                    renders[number] = render
            info["decode_warning_render_replay"] = renders
    except (OSError, RuntimeError, ValueError) as exc:
        info["error"] = str(exc)
    info["extracted_pages"] = len(pages)
    return info, pages


def manifest_records():
    indexed = defaultdict(list)
    for name in MANIFESTS:
        data = load_json(PILOT / name)
        if isinstance(data, dict):
            data = data["papers"]
        for row in data:
            if row.get("local_path"):
                indexed[pdf_path(row["local_path"])].append((name, row))
    return indexed


def index_coverage():
    db_path = PILOT / "indexes" / "pilot.sqlite"
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        fts = dict(db.execute("SELECT doc_id, count(*) FROM evidence_fts GROUP BY doc_id"))
        graph = {row[0] for row in db.execute("SELECT node_id FROM graph_nodes WHERE kind='document'")}
        claims = dict(db.execute("SELECT doc_id, count(*) FROM claims GROUP BY doc_id"))
        catalog = {doc_id: json.loads(meta).get("sha256")
                   for doc_id, meta in db.execute("SELECT doc_id, metadata FROM papers")}
    vectors = Counter(row["doc_id"] for row in load_json(PILOT / "benchmarks" / "embedding" / "all_records.json"))
    return fts, graph, claims, catalog, vectors


def provenance(paper, manifest, overlay):
    nested = paper.get("provenance") or {}
    locations = {name: {} for name in PROVENANCE}
    for origin, record in (("catalog", paper), ("provenance", nested), ("manifest", manifest)):
        for field in PROVENANCE:
            if record.get(field):
                locations[field][origin] = record[field]
    source_fields = ("source_url", "publisher_url", "pdf_url", "resolved_url", "discovery_url")
    date_fields = ("accessed_at", "retrieved_date", "retrieval_date", "retrieved_on", "downloaded_date")
    urls = {f"{origin}.{field}": value for field in source_fields for origin, value in locations[field].items()
            if isinstance(value, str) and value.startswith(("http://", "https://"))}
    dates = {f"{origin}.{field}": value for field in date_fields for origin, value in locations[field].items()}
    explicit_dois = [norm_doi(v) for v in locations["doi"].values()]
    url_dois = [norm_doi(v) for v in urls.values() if re.match(r"https?://(?:dx\.)?doi\.org/", v, re.I)]
    doi_candidates = list(dict.fromkeys(value for value in [*explicit_dois, *url_dois] if value))
    note_dois = [norm_doi(match.group().rstrip(".;")) for match in DOI_NOTE.finditer(nested.get("notes") or "")]
    return {
        "source_urls": urls,
        "access_dates": dates,
        "doi_candidates": doi_candidates,
        "provenance_note_doi_candidates": list(dict.fromkeys(value for value in note_dois
                                                            if value and value not in doi_candidates)),
        "raw_catalog_missing": [field for field in PROVENANCE if not paper.get(field)],
        "metadata_alternatives": {key: locations[key] for key in ("publisher", "doi", "year", "authors")},
        "overlay_organization": overlay.get("organization"),
        "overlay_label_year": overlay.get("label_year"),
        "overlay_authors": overlay.get("authors"),
    }


def evidence_rows(doc_id, claims, raw_pages, parsed_pages, elements, sha, page_count):
    by_id = {row["evidence_id"]: row for row in elements if row.get("evidence_id")}
    by_ref = {row["source_ref"]: row for row in elements
              if row.get("source_ref") and row.get("parser") == "docling_rapidocr"}
    checked = []
    for claim in claims:
        claim_text_quality_error = None
        try:
            guard_claim_text_quality(claim)
        except ValueError as exc:
            claim_text_quality_error = str(exc)
        evidence = []
        for item in claim.get("evidence", []):
            page = item.get("pdf_page")
            quote = item.get("quote") or ""
            in_range = type(page) is int and page_count is not None and 1 <= page <= page_count
            parsed_page = parsed_pages.get(page, {})
            element = by_id.get(item.get("evidence_id")) or by_ref.get(item.get("source_ref"))
            bound = bool(element and element.get("doc_id") == doc_id and element.get("pdf_page") == page
                         and sha and element.get("document_sha256") == sha)
            match = {
                "pdf_native": quote_mode(quote, raw_pages.get(page, "")),
                "published_page_text": quote_mode(quote, parsed_page.get("text", "")),
                "bound_evidence_element": quote_mode(quote, element.get("text", "")) if bound else None,
            }
            parser = item.get("parser") or parsed_page.get("parser")
            if not in_range:
                result = "invalid_page"
            elif match["pdf_native"]:
                result = "native_match"
            elif any(match.values()):
                result = "ocr_only_match" if parser in {"rapidocr", "docling_rapidocr"} else "parsed_only_match"
            else:
                result = "no_text_match"
            evidence.append({
                "pdf_page": page, "evidence_id": item.get("evidence_id"), "quote": quote,
                "raw_quote": item.get("raw_quote"), "parser": parser, "explicit_parser": item.get("parser"),
                "source_ref": item.get("source_ref"), "page_in_range": in_range,
                "resolved_element_binding": ("bound" if bound else "mismatch") if element else "unresolved",
                "match": match, "result": result,
            })
        checked.append({"claim_id": claim["claim_id"], "review_status": claim.get("review_status"),
                        "claim_text_quality_error": claim_text_quality_error, "evidence": evidence})
    return checked


def screen_paper(paper, claims, classifications, overlays, readiness, backfills, manifests, indexes):
    doc_id = paper["doc_id"]
    path = pdf_path(paper["local_path"])
    pdf, raw = inspect_pdf(path)
    sha = pdf.get("sha256")
    page_count = pdf["page_count"]
    expected_pages = set(range(1, (page_count or 0) + 1))
    flags = []
    if not pdf["opens"] or not page_count or pdf["extracted_pages"] != page_count:
        flags.append("pdf_open_or_page_extraction_failure")
    if not pdf.get("header_marker") or not pdf.get("eof_marker") or pdf.get("parser_repaired"):
        flags.append("pdf_structure_candidate")
    if not sha or sha != paper.get("sha256"):
        flags.append("catalog_pdf_hash_mismatch")
    if not page_count or page_count != paper.get("page_count"):
        flags.append("catalog_page_count_mismatch")

    matches = manifests.get(path, [])
    source_manifests = [{"path": f"pilot/{name}", "sha256_recorded": row.get("sha256"),
                         "page_count_recorded": row.get("page_count", row.get("pages")),
                         "recorded_bytes": row.get("bytes")}
                        for name, row in matches]
    if not source_manifests or any(row["sha256_recorded"] != sha or row["page_count_recorded"] != page_count
                                   or (row["recorded_bytes"] is not None and row["recorded_bytes"] != pdf.get("bytes"))
                                   for row in source_manifests):
        flags.append("source_manifest_binding_gap")

    classification = classifications.get(doc_id, {})
    overlay = overlays.get(doc_id, {})
    ready = readiness.get(doc_id, {})
    quality = backfills.get(doc_id, {})
    bindings = {
        "classification_sha256": classification.get("source_sha256"),
        "metadata_overlay_sha256": overlay.get("sha256"),
        "readiness_sha256": ready.get("source_sha256"),
        "text_backfill_sha256": quality.get("source_sha256"),
    }
    if not sha or bindings["classification_sha256"] != sha:
        flags.append("classification_hash_gap")
    for record, key in ((overlay, "metadata_overlay"), (ready, "readiness"), (quality, "text_backfill")):
        if record and (not sha or bindings[f"{key}_sha256"] != sha):
            flags.append(f"{key}_hash_gap")
    valid_overlay = overlay if sha and overlay.get("sha256") == sha else {}
    source = provenance(paper, matches[0][1] if matches else {}, valid_overlay)
    if not source["source_urls"] or not source["access_dates"]:
        flags.append("source_url_or_access_date_gap")
    if len(source["doi_candidates"]) > 1:
        flags.append("doi_field_conflict_candidate")

    directory = PILOT / "parsed" / doc_id
    artifacts = {name: directory / name for name in ("pages.json", "fulltext.md", "elements.jsonl",
                                                      "elements_ocr.jsonl", "text_quality.json")}
    fingerprints = {name: digest(file.read_bytes()) for name, file in artifacts.items() if file.is_file()}
    page_rows = load_json(artifacts["pages.json"]) if "pages.json" in fingerprints else []
    parsed = {row["pdf_page"]: row for row in page_rows}
    markdown = artifacts["fulltext.md"].read_text(encoding="utf-8-sig") if "fulltext.md" in fingerprints else ""
    markdown_pages = [int(page) for page in MD_PAGE.findall(markdown)]
    if (len(page_rows) != len(parsed) or set(parsed) != expected_pages or
            markdown_pages != sorted(expected_pages) or not markdown or "elements.jsonl" not in fingerprints):
        flags.append("parsed_or_markdown_coverage_gap")
    quality_report = load_json(artifacts["text_quality.json"]) if "text_quality.json" in fingerprints else {}
    bindings["parsed_quality_sha256"] = quality_report.get("source_sha256")
    if not sha or bindings["parsed_quality_sha256"] != sha:
        flags.append("parsed_quality_hash_gap")

    pages = [{"pdf_page": page, "raw_text": stats(raw.get(page, "")) if page in raw else None,
              "published_page_text": stats(parsed[page].get("text", "")) if page in parsed else None,
              "published_parser": parsed.get(page, {}).get("parser"),
              "published_quality": parsed.get(page, {}).get("text_quality", {}).get("status"),
              "unresolved_critical": parsed.get(page, {}).get("text_quality", {}).get("unresolved_critical")}
             for page in sorted(expected_pages)]
    for layer in ("raw_text", "published_page_text"):
        candidates = {"low_visible": [], "replacement": [], "private_use": [], "control": []}
        for page in pages:
            stat = page[layer]
            if stat is None:
                continue
            for condition, key in ((stat["visible_chars"] < LOW_VISIBLE, "low_visible"),
                                   (stat["replacement_chars"], "replacement"),
                                   (stat["private_use_chars"], "private_use"),
                                   (stat["unexpected_control_chars"], "control")):
                if condition:
                    candidates[key].append(page["pdf_page"])
        pdf[f"{layer}_candidate_pages"] = candidates

    fts, graph, indexed_claims, indexed_catalog, vectors = indexes
    coverage = {
        "stored_flags": {key: ready.get(key) for key in ("keyword_ready", "semantic_ready", "graph_ready")},
        "explicit_readiness_present": bool(ready), "keyword_rows": fts.get(doc_id, 0),
        "semantic_vector_records": vectors.get(doc_id, 0), "graph_document_node": doc_id in graph,
        "sqlite_claim_rows": indexed_claims.get(doc_id, 0), "sqlite_catalog_sha256": indexed_catalog.get(doc_id),
    }
    if (not all((coverage["keyword_rows"], coverage["semantic_vector_records"], coverage["graph_document_node"]))
            or coverage["sqlite_claim_rows"] != len(claims) or coverage["sqlite_catalog_sha256"] != sha
            or (ready and not all(coverage["stored_flags"].values()))):
        flags.append("index_coverage_gap")

    elements = [row for name in ("elements.jsonl", "elements_ocr.jsonl") if name in fingerprints
                for row in json_lines(artifacts[name])]
    checked = evidence_rows(doc_id, claims, raw, parsed, elements, sha, page_count)
    claim_counts = Counter(item["result"] for claim in checked for item in claim["evidence"])
    if claim_counts["invalid_page"]:
        flags.append("claim_evidence_page_out_of_range")
    if claim_counts["no_text_match"]:
        flags.append("claim_evidence_text_unmatched")
    if any(not claim["evidence"] for claim in checked):
        flags.append("claim_without_evidence")
    if any(claim["claim_text_quality_error"] for claim in checked):
        flags.append("claim_text_quality_guard_failure")
    return {
        "doc_id": doc_id, "title": paper["title"], "source_type": paper.get("source_type"),
        "local_path": path.relative_to(ROOT).as_posix(), "catalog_sha256": paper.get("sha256"),
        "catalog_page_count": paper.get("page_count"), "pdf": pdf, "source_manifests": source_manifests,
        "bindings": bindings, "provenance": source, "pages": pages, "parsed_artifact_sha256": fingerprints,
        "parsed_page_count": len(page_rows), "markdown_pdf_pages": markdown_pages,
        "remaining_active_text_quality_issues": quality.get("remaining_active_issues"),
        "remaining_claim_text_quality_issues": quality.get("remaining_claim_issues"),
        "readiness": coverage, "claim_count": len(checked), "claim_evidence_results": dict(claim_counts),
        "claims": checked, "flags": flags,
    }


def duplicates(documents):
    by_sha, by_doi, by_title = defaultdict(set), defaultdict(set), defaultdict(set)
    for row in documents:
        doc_id = row["doc_id"]
        if row["pdf"].get("sha256"):
            by_sha[row["pdf"]["sha256"]].add(doc_id)
        for doi in row["provenance"]["doi_candidates"] + row["provenance"]["provenance_note_doi_candidates"]:
            by_doi[doi].add(doc_id)
        title = norm_title(row["title"])
        if title:
            by_title[title].add(doc_id)
    return {key: {value: sorted(ids) for value, ids in sorted(groups.items()) if len(ids) > 1}
            for key, groups in (("same_sha256", by_sha), ("same_doi", by_doi), ("same_normalized_title", by_title))}


def summarize(documents):
    issue_ids = defaultdict(list)
    for row in documents:
        for flag in row["flags"]:
            issue_ids[flag].append(row["doc_id"])
        for layer in ("raw_text", "published_page_text"):
            for name, pages in row["pdf"][f"{layer}_candidate_pages"].items():
                if pages:
                    issue_ids[f"{layer}_{name}_candidate"].append(row["doc_id"])
        if row["remaining_active_text_quality_issues"]:
            issue_ids["known_active_text_quality_issues"].append(row["doc_id"])
        if row["remaining_claim_text_quality_issues"]:
            issue_ids["known_claim_text_quality_issues"].append(row["doc_id"])
        if row["pdf"].get("parser_messages"):
            issue_ids["pdf_parser_warning_candidate"].append(row["doc_id"])
        if row["pdf"].get("decode_warning_render_replay"):
            issue_ids["pdf_decode_warning_candidate"].append(row["doc_id"])
        if any(page["unresolved_critical"] for page in row["pages"]):
            issue_ids["published_page_text_unresolved_quality_candidate"].append(row["doc_id"])
    evidence = [item for row in documents for claim in row["claims"] for item in claim["evidence"]]
    return {
        "documents": len(documents), "physical_pages": sum(row["pdf"]["page_count"] or 0 for row in documents),
        "native_extracted_pages": sum(row["pdf"]["extracted_pages"] for row in documents),
        "native_nonempty_pages": sum(bool(page["raw_text"] and page["raw_text"]["visible_chars"])
                                     for row in documents for page in row["pages"]),
        "documents_opened": sum(row["pdf"]["opens"] for row in documents),
        "explicit_readiness_documents": sum(row["readiness"]["explicit_readiness_present"] for row in documents),
        "documents_with_claims": sum(bool(row["claim_count"]) for row in documents),
        "claims": sum(row["claim_count"] for row in documents), "evidence_entries": len(evidence),
        "evidence_results": dict(Counter(item["result"] for item in evidence)),
        "evidence_match_by_layer": {layer: dict(Counter(item["match"][layer] or "none" for item in evidence))
                                    for layer in ("pdf_native", "published_page_text", "bound_evidence_element")},
        "ocr_explicit_parsers": dict(Counter(item["explicit_parser"] for item in evidence if item["explicit_parser"])),
        "resolved_evidence_element_bindings": dict(Counter(item["resolved_element_binding"] for item in evidence)),
        "claim_text_quality_guard_failures": sum(bool(claim["claim_text_quality_error"])
                                                 for row in documents for claim in row["claims"]),
        "raw_catalog_missing_field_counts": dict(Counter(key for row in documents
                                                         for key in row["provenance"]["raw_catalog_missing"])),
        "documents_without_catalog_doi": sum("doi" in row["provenance"]["raw_catalog_missing"] for row in documents),
        "documents_without_structured_doi_candidate": sum(not row["provenance"]["doi_candidates"] for row in documents),
        "documents_without_any_doi_candidate": sum(not row["provenance"]["doi_candidates"] and
                                                   not row["provenance"]["provenance_note_doi_candidates"] for row in documents),
        "issue_document_counts": {key: len(values) for key, values in sorted(issue_ids.items())},
        "issue_doc_ids": {key: sorted(values) for key, values in sorted(issue_ids.items())},
    }


def main():
    started = datetime.now(timezone.utc)
    inputs = {f"pilot/{name}": digest((PILOT / name).read_bytes()) for name in FROZEN}
    papers = load_json(PILOT / "catalog" / "papers.json")
    classifications = {row["doc_id"]: row for row in load_json(PILOT / "catalog" / "classifications.json")["records"]}
    overlays = load_json(PILOT / "catalog" / "paper_metadata_overrides.json")["documents"]
    readiness = load_json(PILOT / "catalog" / "ingestion_readiness.json")["documents"]
    backfills = load_json(PILOT / "catalog" / "text_quality_backfills.json")["documents"]
    claims = defaultdict(list)
    for claim in json_lines(PILOT / "annotations" / "claims.jsonl"):
        claims[claim["doc_id"]].append(claim)
    manifests = manifest_records()
    indexes = index_coverage()
    documents = [screen_paper(paper, claims.get(paper["doc_id"], []), classifications, overlays, readiness,
                              backfills, manifests, indexes) for paper in papers]
    physical_manifest = [{"doc_id": row["doc_id"], "path": row["local_path"],
                          "sha256": row["pdf"].get("sha256"), "bytes": row["pdf"].get("bytes"),
                          "pages": row["pdf"]["page_count"]}
                         for row in sorted(documents, key=lambda row: row["doc_id"])]
    finished = datetime.now(timezone.utc)
    doc_ids = {row["doc_id"] for row in papers}
    result = {
        "audit_date_asia_shanghai": started.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
        "started_at_utc": started.isoformat(), "checked_at_utc": finished.isoformat(),
        "pdf_parser": pymupdf.VersionBind, "low_visible_page_threshold": LOW_VISIBLE,
        "frozen_input_sha256": inputs,
        "frozen_inputs_changed_during_screen": [path for path, sha in inputs.items()
                                                if digest((ROOT / path).read_bytes()) != sha],
        "physical_source_pdf_manifest_sha256": digest(json.dumps(physical_manifest, ensure_ascii=False,
                                                                 sort_keys=True, separators=(",", ":")).encode("utf-8")),
        "physical_source_pdf_manifest": physical_manifest,
        "orphan_claim_doc_ids": sorted(set(claims) - doc_ids),
        "orphan_classification_doc_ids": sorted(set(classifications) - doc_ids),
        "orphan_metadata_overlay_doc_ids": sorted(set(overlays) - doc_ids),
        "duplicate_catalog_doc_ids": sorted(key for key, count in Counter(row["doc_id"] for row in papers).items() if count > 1),
        "duplicate_claim_ids": sorted(key for key, count in Counter(row["claim_id"] for rows in claims.values() for row in rows).items() if count > 1),
        "summary": summarize(documents), "duplicate_candidates": duplicates(documents), "documents": documents,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {key: val for key, val in result["summary"].items() if key != "issue_doc_ids"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(result["duplicate_candidates"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
