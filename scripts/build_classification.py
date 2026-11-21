"""Validate reviewed classification inputs and publish a reproducible catalog sidecar."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot.classification import CATEGORY_LABELS, FACET_DEFINITIONS, VERSION, taxonomy
from kg_pilot.pipeline import PILOT, read_json, write_json, jsonl


def quote_key(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def validate_records(papers, records, evidence_by_page, *, check_files=True):
    errors = []
    catalog = {p["doc_id"]: p for p in papers}
    ids = [r["doc_id"] for r in records]
    if len(ids) != len(set(ids)):
        errors.append("重复的分类文献 ID")
    if set(ids) != set(catalog):
        errors.append(f"分类覆盖范围不符：缺少 {sorted(set(catalog) - set(ids))}；多余 {sorted(set(ids) - set(catalog))}")
    for row in records:
        doc_id = row["doc_id"]
        if doc_id not in catalog:
            continue
        paper = catalog[doc_id]
        if row.get("primary_category") not in CATEGORY_LABELS:
            errors.append(f"{doc_id}: 主分类无效")
        related = row.get("related_stages")
        if not isinstance(related, list) or not set(related).issubset(CATEGORY_LABELS):
            errors.append(f"{doc_id}: 关联阶段无效")
        elif row["primary_category"] in related or len(related) != len(set(related)):
            errors.append(f"{doc_id}: 关联阶段重复")
        for field, _, multiple, names in FACET_DEFINITIONS:
            value = row.get(field)
            if multiple:
                if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                    errors.append(f"{doc_id}: {field} 应为字符串数组")
                    continue
                values = value
                if len(values) != len(set(values)):
                    errors.append(f"{doc_id}: {field} 存在重复标签")
            else:
                values = [value]
            if names and any(v not in names for v in values):
                errors.append(f"{doc_id}: {field} 未使用标准词表")
        if len(row.get("classification_reason", "")) < 15:
            errors.append(f"{doc_id}: 缺少具体归类理由")
        if row.get("classification_review") not in {"agent_classified", "agent_reviewed", "independent_agent_reviewed", "api_reviewed", "api_model_reviewed"}:
            errors.append(f"{doc_id}: 复核方式标记无效")
        if not row.get("evidence"):
            errors.append(f"{doc_id}: 没有分类依据原文")
        for evidence in row.get("evidence", []):
            page, quote = evidence.get("pdf_page"), evidence.get("quote", "")
            if not isinstance(page, int) or not 1 <= page <= paper["page_count"]:
                errors.append(f"{doc_id}: 分类依据页码无效 {page}")
                continue
            sources = evidence_by_page.get((doc_id, page), [])
            if len(quote.strip()) < 8 or not any(quote_key(quote) in quote_key(source) for source in sources):
                errors.append(f"{doc_id} p{page}: 原文未匹配 {quote[:85]}")
        if check_files and hashlib.sha256(Path(paper["local_path"]).read_bytes()).hexdigest() != paper["sha256"]:
            errors.append(f"{doc_id}: PDF 与采集哈希不符，需要重新审核分类")
    return errors


def load_classification_inputs(pilot=None):
    """Retain the original two inputs and append each published batch sidecar."""
    pilot = PILOT if pilot is None else Path(pilot)
    paths = [pilot / "catalog/classification_process.json", pilot / "catalog/classification_reliability.json"]
    paths.extend(sorted((pilot / "catalog/classification_batches").glob("*.json")))
    records = []
    for path in paths:
        payload = read_json(path)
        if isinstance(payload, dict):
            if payload.get("version", VERSION) != VERSION:
                raise ValueError("分类批次版本不符合当前标准：" + path.name)
            payload = payload.get("records")
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("分类输入不是有效的记录数组：" + path.name)
        records.extend(payload)
    return records


def prepare_record(row, paper, topic_groups, *, previous=None, today=None):
    """Map legacy fine topics, preserving already grouped batch tags and dates."""
    topic_map = {keyword: group for group, keywords in topic_groups.items() for keyword in keywords}
    if len(topic_map) != sum(len(keywords) for keywords in topic_groups.values()):
        raise ValueError("关注问题词表含重复的细分词")
    result = dict(row)
    grouped = []
    for value in row["topics"]:
        group = value if value in topic_groups else topic_map.get(value)
        if group is None:
            raise ValueError(f"{row['doc_id']}: 关注问题没有对应的中文大类：{value}")
        grouped.append(group)
    keywords = row.get("keywords", row["topics"])
    if not isinstance(keywords, list) or not all(isinstance(value, str) and value.strip() for value in keywords):
        raise ValueError(f"{row['doc_id']}: 精确关键词格式无效")
    result["topics"] = list(dict.fromkeys(grouped))
    result["keywords"] = list(dict.fromkeys(keywords))
    old_date = (previous or {}).get("classified_date") if (previous or {}).get("source_sha256") == paper["sha256"] else None
    result.update(source_sha256=paper["sha256"],
                  classified_date=row.get("classified_date") or old_date or today or datetime.now().astimezone().date().isoformat(),
                  standard_version=VERSION)
    return result


def main():
    papers = read_json(PILOT / "catalog/papers.json")
    records = load_classification_inputs()
    sources = defaultdict(list)
    for paper in papers:
        for page in read_json(PILOT / "parsed" / paper["doc_id"] / "pages.json"):
            sources[(paper["doc_id"], page["pdf_page"])].append(page["text"])
        # Unsorted native extraction can preserve column sentences that geometric
        # sort interleaves; either must still come from the same physical PDF page.
        with pymupdf.open(paper["local_path"]) as pdf:
            for i, page in enumerate(pdf):
                sources[(paper["doc_id"], i + 1)].append(page.get_text())
    grouped = defaultdict(list)
    for item in jsonl(PILOT / "parsed/effective_evidence.jsonl"):
        grouped[(item["doc_id"], item["pdf_page"])].append(item.get("text", ""))
    for key, blocks in grouped.items():
        sources[key].append("\n".join(blocks))
    errors = validate_records(papers, records, sources)
    if errors:
        print(json.dumps({"status": "failed", "errors": errors}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    by_id = {p["doc_id"]: p for p in papers}
    topic_groups = read_json(PILOT / "catalog/topic_vocabulary.json")
    existing_path = PILOT / "catalog/classifications.json"
    existing = read_json(existing_path).get("records", []) if existing_path.is_file() else []
    previous = {row["doc_id"]: row for row in existing}
    today = datetime.now().astimezone().date().isoformat()
    records = [prepare_record(row, by_id[row["doc_id"]], topic_groups,
                              previous=previous.get(row["doc_id"]), today=today) for row in records]
    records.sort(key=lambda r: r["doc_id"])
    write_json(PILOT / "catalog/classifications.json", {"version": VERSION, "records": records})
    enriched = [p | {"classification": next(r for r in records if r["doc_id"] == p["doc_id"])} for p in papers]
    write_json(PILOT / "catalog/taxonomy.json", taxonomy(enriched))
    report = {"date": today, "standard_version": VERSION, "status": "passed",
              "documents": len(records), "evidence_quotes": sum(len(r["evidence"]) for r in records),
              "primary_categories": dict(Counter(r["primary_category"] for r in records)),
              "relevance": dict(Counter(r["relevance"] for r in records)),
              "checks": [f"{len(papers)}-document catalog coverage", "unique main classification", "controlled vocabulary",
                         "physical PDF page bounds", "quotes match saved native or effective OCR source", "original PDF SHA-256"],
              "scope": "Agent-assigned paper classifications with source-location checks; not expert approval, not exhaustive claim extraction. Original papers.json and claim stages unchanged."}
    write_json(PILOT / "reports/classification_validation.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
