"""Replay bilingual full-text searches, verify source anchors, and render the research notes.

Run with the project's Python and --refresh after editing the review JSON. Without
that flag the command checks both published artifacts without changing them.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "pilot"
REVIEW = PILOT / "research_map/opportunity_reviews.json"
REPORT = PILOT / "reports/research_map_20261107/opportunities.md"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("\u00ad", "")
    text = re.sub(r"(?<=[a-z])-\s*\n\s*(?=[a-z])", "", text)
    return " ".join(text.split())


def keyword_pattern(terms: list[str]) -> re.Pattern:
    alternatives = []
    for term in terms:
        value = re.escape(normalized(term)).replace(r"\ ", r"[\s\-‐‑–]*")
        if term.isascii() and term[0].isalpha():
            value = r"(?<![a-z])" + value
            if len(term) <= 2 or (term.upper() == term and len(term) <= 5):
                value += r"(?![a-z])"
        alternatives.append(value)
    return re.compile("|".join(alternatives))


def search(groups: dict, pages: dict) -> dict:
    patterns = {name: keyword_pattern(terms) for name, terms in groups.items()}
    single_group = Counter()
    document_hits = []
    same_page_hits = {}
    near_hits = {key: [] for key in groups}
    for doc_id, texts in sorted(pages.items()):
        seen = set()
        same_pages = []
        for number, (native, parsed) in texts.items():
            hits = {name for name, pattern in patterns.items()
                    if pattern.search(native) or pattern.search(parsed)}
            seen.update(hits)
            if len(hits) == len(patterns):
                same_pages.append(number)
        single_group.update(seen)
        for omitted in groups:
            if set(groups) - {omitted} <= seen:
                near_hits[omitted].append(doc_id)
        if len(seen) == len(patterns):
            document_hits.append(doc_id)
        if same_pages:
            same_page_hits[doc_id] = same_pages
    return {
        "individual_group_document_counts": {name: single_group[name] for name in patterns},
        "all_groups_same_document_count": len(document_hits),
        "all_groups_same_document_ids": document_hits,
        "all_groups_same_page_document_count": len(same_page_hits),
        "all_groups_same_page_count": sum(map(len, same_page_hits.values())),
        "all_groups_same_page_hits": same_page_hits,
        "leave_one_group_out_document_hits": near_hits,
    }


def load_sources():
    catalog_bytes = (PILOT / "catalog/papers.json").read_bytes()
    papers = {p["doc_id"]: p for p in json.loads(catalog_bytes)}
    claims_bytes = (PILOT / "annotations/claims.jsonl").read_bytes()
    claims = {c["claim_id"]: c for c in map(json.loads, claims_bytes.splitlines())}
    pages = {}
    signature = []
    parsed_signature = []
    weak_native = weak_both = 0
    for doc_id, paper in sorted(papers.items()):
        source = Path(paper["local_path"])
        if not source.exists():
            source = PILOT / str(paper["local_path"]).replace("\\", "/").split("/pilot/", 1)[-1]
        sha = digest(source.read_bytes())
        if sha != paper["sha256"]:
            raise ValueError(f"PDF signature mismatch: {doc_id}")
        signature.append(f"{doc_id}:{sha}\n")
        parsed_bytes = (PILOT / "parsed" / doc_id / "pages.json").read_bytes()
        parsed_signature.append(f"{doc_id}:{digest(parsed_bytes)}\n")
        parsed = {p["pdf_page"]: normalized(p["text"]) for p in json.loads(parsed_bytes)}
        with pymupdf.open(source) as pdf:
            if set(parsed) != set(range(1, len(pdf) + 1)):
                raise ValueError(f"page range mismatch: {doc_id}")
            text = {}
            for n, page in enumerate(pdf, 1):
                native = normalized(page.get_text())
                text[n] = (native, parsed[n])
                weak_native += len(native) < 80
                weak_both += max(len(native), len(parsed[n])) < 80
            pages[doc_id] = text
    source_pairs = sorted((p["doc_id"], p["sha256"]) for p in papers.values())
    canonical_pairs = json.dumps(source_pairs, ensure_ascii=False, separators=(",", ":"))
    return papers, claims, pages, {
        "documents": len(papers), "pdf_pages": sum(map(len, pages.values())),
        "claims": len(claims), "catalog_sha256": digest(catalog_bytes),
        "claims_sha256": digest(claims_bytes),
        "source_digest": digest(canonical_pairs.encode("utf-8")),
        "source_digest_format": "SHA-256 of sorted [doc_id, pdf_sha256] pairs encoded as compact UTF-8 JSON",
        "document_signature_sha256": digest("".join(signature).encode()),
        "parsed_pages_signature_sha256": digest("".join(parsed_signature).encode()),
        "signature_format": "UTF-8, sorted by doc_id, one doc_id:sha256 plus LF per document",
        "native_text_under_80_chars_pages": weak_native,
        "both_text_channels_under_80_chars_pages": weak_both,
    }


def check_anchor(anchor, papers, claims, pages):
    doc_id, n = anchor["doc_id"], anchor["pdf_page"]
    native, parsed = pages[doc_id][n]
    quote = normalized(anchor["quote"])
    channels = [key for key, value in (("pdf_native", native), ("parsed_page", parsed))
                if quote in value]
    if not channels:
        raise ValueError(f"quotation absent from page: {doc_id}, p{n}, {quote!r}")
    claim_id = anchor.get("claim_id")
    if claim_id:
        claim = claims[claim_id]
        matches = [e for e in claim["evidence"] if e["pdf_page"] == n
                   and quote in normalized(e["quote"])]
        if claim["doc_id"] != doc_id or not matches:
            raise ValueError(f"quotation absent from claim: {claim_id}, p{n}")
        anchor["evidence_id"] = matches[0].get("evidence_id")
    paper = papers[doc_id]
    anchor.update(quote_verified_in=channels, title=paper["title"], year=paper.get("year"),
                  pdf_sha256=paper["sha256"], doi=paper.get("doi"),
                  source_url=paper.get("publisher_url") or paper.get("source_url"),
                  retrieved_date=paper.get("retrieved_date") or paper.get("retrieval_date"))


def report_text(review) -> str:
    corpus = review["corpus"]
    lines = ["# 混合键合研究方向复核", "", f"复核日期 {review['reviewed_on']}（北京时间）。",
             f"核对 {corpus['documents']} 份 PDF 共 {corpus['pdf_pages']} 页，覆盖目录中的 {corpus['claims']} 条已抽取记录。"
             f"下列 {len(review['opportunities'])} 个问题保留为有条件的研究设想，列出的对照、判据与资源均为拟议方案。",
             "", "## 范围与方法", "", review["method"],
             f"原生文字少于 80 字符的有 {corpus['native_text_under_80_chars_pages']} 页，两种文本均少于该阈值的有 "
             f"{corpus['both_text_channels_under_80_chars_pages']} 页。图中文字、补充材料、非公开资料和未被检索词覆盖的措辞仍可能漏检。",
             f"PDF 清单签名 `{corpus['source_digest']}`。精确目录、解析页集及 claim 文件的 SHA-256、"
             "全部同文命中 ID 和同页命中页码保存在配套 JSON。", ""]
    for record in [*review["opportunities"], *review["rejected"]]:
        lines.extend([f"## {record['title']}", "", f"{record['classification']} · {record['system_label']} · {record['row']} × {record['column']}", "",
                      f"**适用范围**　{record['route_basis']}", "",
                      f"**研究问题**　{record['question']}", "", f"**已有实测**　{record['prior_work']}",
                      "", f"**剩余边界**　{record['gap']}", ""])
        if record["classification"] == "potential":
            lines.extend(["**实验设计**", "", *["- "+c for c in record["controls"]], "",
                          f"**如何测量**　{record['measurement']}", "",
                          f"**如何判断**　{record['success_criterion']}", "",
                          f"**反例与停线条件**　{record['counterexample']}", "",
                          f"**起步条件**　{record['feasibility']}", ""])
        lines.extend(["**原始证据**", ""])
        for anchor in record["evidence"]:
            cid = anchor.get("claim_id") or "原页补充"
            lines.append(f"- {anchor['scope']}　《{anchor['title']}》，PDF p.{anchor['pdf_page']}，{anchor['locator']}，"
                         f"`{cid}`。原文 “{anchor['quote']}” [来源]({anchor['source_url']})")
        lines.extend(["", "**查重记录**", ""])
        for name, terms in record["search"]["groups"].items():
            lines.append(f"- {name}　{' / '.join(terms)}。")
        result = record["search"]["results"]
        lines.extend(["", f"全部词组同文命中 {result['all_groups_same_document_count']} 份，同页命中 "
                      f"{result['all_groups_same_page_count']} 页、{result['all_groups_same_page_document_count']} 份。"
                      "命中数包括参考文献及通用语言，不等同于目标实验数。"
                      "每次放宽一个词组后的同文命中数依次为 " +
                      "、".join(str(len(v)) for v in result["leave_one_group_out_document_hits"].values()) +
                      "。全部命中 ID 可在 JSON 中复查。", "", record["search"]["nearby"], "",
                      "外部定向检索（" + review["reviewed_on"] + "）使用 " + " / ".join(record["search"]["external_queries"]) + "。", ""])
        for ext in record["external_sources"]:
            lines.append(f"- {ext['finding']} [{ext['title']}]({ext['url']})" +
                         (f" · DOI `{ext['doi']}`" if ext.get("doi") else ""))
        lines.extend(["", record["search"]["limitations"], ""])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="refresh search results, source stamps, and report")
    args = parser.parse_args()
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    papers, claims, pages, review["corpus"] = load_sources()
    for record in [*review["opportunities"], *review["rejected"]]:
        for anchor in record["evidence"]:
            check_anchor(anchor, papers, claims, pages)
        if len({anchor["doc_id"] for anchor in record["evidence"]}) < 2:
            raise ValueError(f"need two different source documents for {record['id']}")
        record["search"]["results"] = search(record["search"]["groups"], pages)
        hits = record["search"]["results"]
        print(record["id"], "doc hits", hits["all_groups_same_document_count"],
              "same-page docs", hits["all_groups_same_page_document_count"], flush=True)
    outputs = {REVIEW: json.dumps(review, ensure_ascii=False, indent=2)+"\n", REPORT: report_text(review)}
    for path, content in outputs.items():
        if args.refresh:
            path.write_text(content, encoding="utf-8", newline="\n")
        elif path.read_text(encoding="utf-8") != content:
            raise SystemExit(f"artifact requires review and refresh: {path}")


if __name__ == "__main__":
    main()
