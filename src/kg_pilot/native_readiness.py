"""Define the reviewed PDF inputs that may enter the published index."""
from collections import Counter
import hashlib
from math import isfinite

import pymupdf

from kg_pilot.native_library import file_hash, now, read_json, save


POLICY = "reviewed-native-input-v1"


def _inputs(job):
    return {name:file_hash(job / name) for name in ("crop-review.json", "mixed-parse.json")}


def preflight_receipt(job, sha):
    try:
        receipt = read_json(job / "preflight.json")
        if (receipt.get("policy") == POLICY and receipt.get("sha256") == sha
                and receipt.get("inputs") == _inputs(job)):
            return receipt
    except (OSError, ValueError):
        pass
    return {}


def _region_key(item):
    return item["type"], item["page_idx"], item["image_sha256"]


def _usable(value):
    return isinstance(value, str) and value.strip().lower() not in {"", "[no text]", "[illegible]"}


def validate_preflight(job, row, pdf_path, parsed):
    audit = read_json(job / "crop-review.json")
    if audit.get("revision") != row["sha256"] or audit.get("status") != "accepted" or audit.get("held"):
        raise ValueError("裁图复核尚未通过")
    if parsed.get("identity", {}).get("sha256") != row["sha256"]:
        raise ValueError("解析与来源 PDF 的版本不一致")
    pages = parsed["pages"]
    if [page["pdf_page"] for page in pages] != list(range(1, row["pages"] + 1)):
        raise ValueError("解析缺少原 PDF 页码或页码顺序不一致")

    texts, visuals = {}, []
    with pymupdf.open(pdf_path) as pdf:
        if len(pdf) != len(pages):
            raise ValueError("来源 PDF 与预处理页数不一致")
        for item in parsed["items"]:
            number = item["page_idx"]
            if type(number) is not int or not 0 <= number < len(pdf):
                raise ValueError("有内容缺少有效的原 PDF 页码")
            kind = item["type"]
            if kind == "text":
                if number in texts or not _usable(item["text"]):
                    raise ValueError(f"第 {number + 1} 页正文重复或不可用")
                texts[number] = item["text"]
                continue
            if kind not in {"image", "table", "equation"}:
                raise ValueError(f"第 {number + 1} 页存在未知的内容类型")
            box = item["source_bbox"]
            if len(box) != 4 or not all(isfinite(coord) for coord in box):
                raise ValueError(f"第 {number + 1} 页的裁图坐标无效")
            rect = pymupdf.Rect(box)
            if rect.is_empty or not pdf[number].rect.contains(rect):
                raise ValueError(f"第 {number + 1} 页的裁图超出原页")
            if file_hash(job.joinpath(item["img_path"])) != item["image_sha256"]:
                raise ValueError(f"第 {number + 1} 页的裁图文件已改变")
            if (kind == "table" and not _usable(item.get("table_body"))
                    or kind == "equation" and not _usable(item.get("latex"))):
                label = "表格" if kind == "table" else "公式"
                raise ValueError(f"第 {number + 1} 页存在未完整转写的{label}")
            visuals.append(item)

    if not texts and not visuals:
        raise ValueError("PDF 没有可索引的内容")
    for index, page in enumerate(pages):
        source = page.get("source")
        has_text = index in texts
        if (source not in {"legacy", "pdf_vision", "pdf_no_text"} or has_text == (source == "pdf_no_text")
                or page.get("text_sha256") != hashlib.sha256(texts.get(index, "").encode()).hexdigest()):
            raise ValueError(f"第 {index + 1} 页缺少可核验的正文结果")

    kept = Counter(map(_region_key, visuals))
    skipped = Counter(map(_region_key, parsed.get("skipped_decorations", [])))
    reviewed = Counter(map(_region_key, audit["items"]))
    filtered = Counter(_region_key(item) for item in audit["items"] if item.get("filter_reason"))
    if kept + skipped != reviewed or not filtered <= skipped:
        raise ValueError("解析的图表与公式未完整对应经复核的裁图")
    return {"pages": len(pages), "text_pages": len(texts), "visual_items": len(visuals)}


def verify_prepared(job, row, pdf_path, parsed):
    receipt = {"policy":POLICY, "sha256":row["sha256"], "inputs":_inputs(job), "at":now()}
    try:
        receipt.update(validate_preflight(job, row, pdf_path, parsed))
    except (KeyError, OSError, TypeError, ValueError) as error:
        message = f"预处理待复核：{error}"
        save(job / "preflight.json", {**receipt, "status":"needs_review", "error":message})
        raise ValueError(message) from error
    save(job / "preflight.json", {**receipt, "status":"ready"})
