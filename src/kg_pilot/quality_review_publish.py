"""Publish the 2026-11-13 Adeia source review through the dataset journal."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

from kg_pilot import ingestion_publish as publication
from kg_pilot import text_quality_backfill as backfill
from kg_pilot.pipeline import PILOT, jsonl, read_json
from kg_pilot.text_quality import guard_claim_text_quality
from kg_pilot.validation import quote_supported


DOC_ID = "doc_0086822b49e2d5df8658"
SOURCE_SHA256 = "0086822b49e2d5df865817665501c8403bef4127469a8ce5d326c437342669df"
SOURCE_REVIEW = "reports/quality_review_20261113/deep/risk.md"
BATCH_PREFIX = "adeia-source-review-"
EXTRA_PATHS = {
    "catalog/evidence_qualifications.json",
    "research_map/reviewed_relations.json",
    "research_map/semantic_reviews.json",
}

# Each replacement belongs to a reviewed claim and field. Quotes are repaired
# separately by locating the exact source glyph, including on mixed-unit pages.
CLAIM_CHANGES = {
    "c002": {
        "page": 9,
        "source_terms": ("Pad size ranging from 2 - 15 µm", "~10 mΩ per link"),
        "fields": {
            "statement": (
                "Adeia对8 x 12 mm测试芯片开展对准偏移测试，全部受测焊盘尺寸的错位容限约为焊盘直径的80%。",
                "Adeia在8 x 12 mm测试芯片上对2–15 µm焊盘做对准偏移测试，电测最大通过偏移约为焊盘直径的80%。",
            ),
            "sample_id": (
                "8 x 12 mm对准偏移测试载体，焊盘尺寸范围按原文为2 - 15 mm",
                "8 x 12 mm对准偏移测试载体，焊盘直径为2–15 µm",
            ),
        },
        "conditions": {"pad_size_range_as_written": ("2 - 15 mm", "2 - 15 µm")},
    },
    "c004": {
        "page": 13,
        "source_terms": ("10 µm bond pads", "thinned to 50 or 200 µm", "50 µm required"),
        "fields": {
            "statement": (
                "Adeia比较两种厚度的8 x 12 mm测试芯片，报告较薄芯片的良率没有下降，两种厚度的良率在统计上等效；厚度按所给原文记为50 mm和200 mm。",
                "Adeia比较50 µm和200 µm厚的8 x 12 mm测试芯片，报告较薄组的电测良率未下降，并称两组统计等效。该页未给出各批芯片数、等效界值或检验方法。",
            ),
            "sample_id": (
                "8 x 12 mm、10 mm焊盘、分别减薄至50或200 mm的测试芯片，尺寸单位均按原文",
                "8 x 12 mm测试芯片，使用10 µm焊盘，分别减薄至50 µm或200 µm",
            ),
            "value_text": ("较薄芯片良率未下降，两者统计等效", "作者称较薄组电测良率未下降，两组统计等效"),
        },
        "conditions": {
            "bond_pad_size_as_written": ("10 mm", "10 µm"),
            "die_thicknesses_as_written": (["50 mm", "200 mm"], ["50 µm", "200 µm"]),
            "process_comparison": (
                "除减薄工艺外流程相同；50 mm厚度需要临时键合至载体",
                "除减薄工艺外流程相同；50 µm厚度需要临时键合至载体",
            ),
        },
    },
    "c005": {
        "page": 16,
        "source_terms": ("50 µm thick die", "15 µm bond pads", "12-die stack had overall yield of 56%"),
        "fields": {
            "statement": (
                "Adeia报告其12芯片堆叠测试结构的整体良率为56%。",
                "Adeia报告12芯片堆叠结构的整体无空洞良率为56%。按图中计数，第1层有48个堆叠，第12层剩27个。",
            ),
            "sample_id": (
                "12芯片堆叠结构；单芯片按原文为8 x 12 mm x 50 mm厚、15 mm焊盘",
                "12芯片堆叠结构；单芯片边长8 x 12 mm、厚50 µm、焊盘直径15 µm",
            ),
            "quantity": ("堆叠结构整体良率", "堆叠结构整体无空洞良率"),
        },
        "conditions": {
            "die_thickness_as_written": ("50 mm", "50 µm"),
            "bond_pad_size_as_written": ("15 mm", "15 µm"),
        },
    },
    "c007": {
        "page": 17,
        "source_terms": ("200 µm thick Die", "10 µm bond pads", "No drop in electrical yield down to 200°C"),
        "fields": {
            "statement": (
                "Adeia将键合晶圆劈成若干片，各片在设定温度退火2小时；退火温度降低至200°C时，所测阵列的电学良率未下降。",
                "Adeia将键合晶圆分片后在设定温度退火2小时，演示正文称降至200 °C时电学良率未下降。",
            ),
            "sample_id": (
                "由8 x 12 mm x 200 mm厚、10 mm焊盘测试芯片组成的键合晶圆劈裂片，尺寸单位按原文",
                "键合晶圆劈裂片；所用芯片边长8 x 12 mm、厚200 µm、焊盘直径10 µm",
            ),
            "value_text": ("No drop in electrical yield down to 200°C", "作者称降至200 °C时电学良率未下降"),
        },
        "conditions": {
            "die_thickness_as_written": ("200 mm", "200 µm"),
            "bond_pad_size_as_written": ("10 mm", "10 µm"),
        },
    },
}


def _apply_fields(fields, changes, claim_id):
    for field, (old, new) in changes.items():
        if fields.get(field) == new:
            continue
        if fields.get(field) != old:
            raise publication.PublicationError(f"{claim_id} 的 {field} 已变化，请重新对照原页。")
        fields[field] = deepcopy(new)


def _review_claim(claim, review, source):
    claim_id = claim["claim_id"]
    if not all(quote_supported(term, source["text"]) for term in review["source_terms"]):
        raise publication.PublicationError(f"{claim_id} 的修订无法在绑定的 PDF 页文本中验证。")
    for ref in claim["evidence"]:
        if (ref.get("evidence_id") != source["evidence_id"] or ref.get("pdf_page") != source["pdf_page"]
                or not quote_supported(ref["quote"], source["text"])):
            raise publication.PublicationError(f"{claim_id} 含有未通过原页核对的引文。")

    fields = deepcopy(claim)
    _apply_fields(fields, review["fields"], claim_id)
    _apply_fields(fields["conditions"], review["conditions"], claim_id)
    if fields["conditions"].get("die_dimensions") != "8 x 12 mm":
        raise publication.PublicationError(f"{claim_id} 的毫米边长与 PDF 不一致。")

    healed = [field for field in (*review["fields"], "conditions") if fields[field] != claim[field]]
    if healed:
        raw = fields.setdefault("raw_text_fields", {})
        for field in healed:
            raw.setdefault(field, deepcopy(claim[field]))
        audit = fields.setdefault("text_quality", {})
        audit["changed_fields"] = sorted(set(audit.get("changed_fields", ())) | set(healed))
    fields["source_review"] = {
        "pdf_page": review["page"], "source_sha256": SOURCE_SHA256, "report": SOURCE_REVIEW,
    }
    guard_claim_text_quality(fields)
    return fields


def _add_review_sidecars(pilot, snapshot, extra_updates):
    if not isinstance(extra_updates, dict):
        raise publication.PublicationError("审查更新需要按相对文件路径提供。")
    for relative, update in extra_updates.items():
        if relative not in EXTRA_PATHS or not isinstance(update, dict) or set(update) != {"expected_sha256", "value"}:
            raise publication.PublicationError(f"无法识别的审查文件更新：{relative}")
        target = publication._inside(pilot, relative)
        before = publication._sha(target) if target.is_file() else None
        snapshot["fingerprint"][relative] = before
        if target.is_file() and read_json(target) == update["value"]:
            continue
        if before != update["expected_sha256"]:
            raise publication.DatasetBusy(f"{relative} 已变化，请重新核对审查更新。")
        snapshot["updates"][Path(relative)] = ("json", update["value"])


def _prepare_snapshot(pilot, snapshot, *, extra_updates, preview):
    document, = snapshot["documents"]
    if document["doc_id"] != DOC_ID or document["source_sha256"] != SOURCE_SHA256:
        raise publication.PublicationError("Adeia 修订与 PDF SHA-256 不符。")

    evidence = {e["pdf_page"]: e for e in snapshot["effective"] if e["doc_id"] == DOC_ID}
    old_claims = {c["claim_id"]: c for c in jsonl(pilot / "annotations/claims.jsonl") if c["doc_id"] == DOC_ID}
    by_id = {f"{DOC_ID}_{suffix}": review for suffix, review in CLAIM_CHANGES.items()}
    claims = []
    differences = {}
    for claim in snapshot["claims"]:
        claim_id = claim["claim_id"]
        if claim_id in by_id:
            review = by_id[claim_id]
            amended = _review_claim(claim, review, evidence[review["page"]])
            old = old_claims[claim_id]
            changed = {field: {"before": old.get(field), "after": amended.get(field)}
                       for field in (*review["fields"], "conditions", "evidence")
                       if old.get(field) != amended.get(field)}
            differences[claim_id] = {"pdf_page": review["page"], "fields": changed}
            claims.append(amended)
        elif claim_id in old_claims and claim != old_claims[claim_id]:
            raise publication.PublicationError(f"本轮修订范围外的事实发生变化：{claim_id}")
        else:
            claims.append(claim)
    if set(differences) != set(by_id) or document["quarantined_claim_ids"]:
        raise publication.PublicationError("Adeia 待修订的事实或引文不完整。")
    snapshot["claims"] = claims
    changed_claims = [c for c in claims if c["claim_id"] in by_id and c != old_claims[c["claim_id"]]]
    document["changed_claims"] = len(changed_claims)
    if changed_claims:
        snapshot["updates"][Path("annotations/claims.jsonl")] = ("jsonl", claims)
    document["source_review"] = {"report": SOURCE_REVIEW, "changes": differences}
    if preview:
        document["source_review"]["claims_after"] = [c for c in claims if c["claim_id"] in by_id]
    _add_review_sidecars(pilot, snapshot, extra_updates)


def preview_source_review(pilot=PILOT, *, extra_updates=None):
    return backfill.preview_text_quality(pilot, doc_ids=[DOC_ID], prepare_snapshot=lambda root, state:
        _prepare_snapshot(root, state, extra_updates=extra_updates or {}, preview=True))


def publish_source_review(pilot=PILOT, *, extra_updates=None, embedder=None, index_builder=None, progress=None):
    return backfill.backfill_text_quality(pilot, doc_ids=[DOC_ID], embedder=embedder,
        index_builder=index_builder, progress=progress, batch_prefix=BATCH_PREFIX, prepare_snapshot=lambda root, state:
        _prepare_snapshot(root, state, extra_updates=extra_updates or {}, preview=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=PILOT)
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--apply", action="store_true")
    commands.add_argument("--recover", metavar="BATCH_ID")
    parser.add_argument("--extra-updates", type=Path)
    args = parser.parse_args(argv)
    if args.recover:
        if args.extra_updates:
            parser.error("恢复时不能同时载入审查更新。")
        if not re.fullmatch(BATCH_PREFIX + r"[0-9a-f]{32}", args.recover):
            parser.error("请提供本次 Adeia 原页修订自己的批次编号。")
        result = publication.recover_publication(args.pilot, expected_batch_id=args.recover)
    else:
        extra = read_json(args.extra_updates) if args.extra_updates else {}
        operation = publish_source_review if args.apply else preview_source_review
        result = operation(args.pilot, extra_updates=extra)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
