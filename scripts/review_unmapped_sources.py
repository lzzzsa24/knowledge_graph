"""Read a frozen sample of unlinked sources to explain useful results outside grid counts."""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import pymupdf

import review_map_relations as reader


KINDS = {"measurement_method", "device_or_material_result", "in_grid_reading", "model",
         "specification_or_guidance", "patent_design", "background", "insufficient"}
READ = """请从以下 PDF 物理原页给研究生选出一处值得读的、具体的研究结果或方法，说明这份资料可以怎样帮助混合键合研究。资料当前未连入结构化矩阵，但文献分类可能有误。既有claims只用于区分当前结构化记录覆盖到哪，不据其补全原文。严禁推断全文不存在其它结果，也不宣称某因果或全球空白。区别作者自己实验、他人研究、计算预测、器件测试、专利披露、规格、方法能力。按实际证据说明真实对象、一个数字或对照（原文没有合适数字可不给）及不能代表什么。优先可在所给PDF正文核实的结果；引句必须来自正文物理页，不要参考条目。只选一种分类：measurement_method（测量方法性能）、device_or_material_result（有关材料/器件但端点或样品不同）、in_grid_reading（存在可以进一步核查的具体工艺×结果实验）、model（计算预测）、specification_or_guidance、patent_design、background、insufficient。返回 {"kind":"...","finding":"约40至100字，清楚写作者做过什么","scope":"一句界定测量对象/条件/分母，不能号称全文空白","evidence":[{"pdf_page":数字,"quote":"在原页中能逐字找到的12至280字符英文或中文原句"}]}。至多两段原文证据；无法验证不要猜。"""
CHECK = """独立核对给出的论述、限定、类型及引句能否准确代表该篇实际原页。至少一条引用直接支持核心事实。数值应核对每个数值和分母、样品/材料、对应工艺；图上单纯推测或串起不同试件即拒绝。页码以 PDF 物理页为准。已有矩阵没连线不允许推断全文没研究；不得把综述、仿真或厂商规格写成实测。仅返回 {"accepted":true或false,"reason":"中文说明"}。"""


def examine(paper, claims):
    ident = paper["doc_id"]
    dest = reader.ROOT / ".cache/unmapped-source-readers" / ident
    src = Path(paper["local_path"])
    if hashlib.sha256(src.read_bytes()).hexdigest() != paper["sha256"]:
        raise ValueError("PDF fingerprint mismatch")
    with reader.PDF_LOCK, pymupdf.open(src) as pdf:
        native = {i+1: page.get_text("text") for i,page in enumerate(pdf)}
    parsed = {p["pdf_page"]:p["text"] for p in reader.load(reader.PILOT/"parsed"/ident/"pages.json")}
    if len(native) != paper["page_count"] or set(parsed) != set(native):
        raise ValueError("PDF page inventory mismatch")
    excerpts = reader.excerpts(parsed, claims, cap=61000)
    packet = {"title":paper["title"], "catalog_source_type":paper.get("source_type"), "rows":reader.ROWS,
              "columns":reader.COLUMNS, "structured_records":[{k:c.get(k) for k in ("claim_id","statement","quantity")} for c in claims],
              "pages":excerpts}
    proposal = reader.call_reader(dest,"read",READ,packet)
    state = proposal.get("kind")
    envelope = {"doc_id":ident,"source_sha256":paper["sha256"],"claims_digest":reader.claims_digest(claims)}
    if state not in KINDS:
        raise ValueError("Unknown category")
    evidence = proposal.get("evidence")
    if state == "insufficient" or not isinstance(evidence,list) or not 1 <= len(evidence) <= 2:
        return envelope | {"status":"no_verified_passage"}
    for e in evidence:
        pg, quote = e.get("pdf_page"), e.get("quote")
        if (type(pg) is not int or pg not in native or not isinstance(quote,str) or not 8 <= len(quote) <= 360
                or all(reader.norm(quote) not in reader.norm(body) for body in (native[pg],parsed[pg]))):
            return envelope | {"status":"literal_quote_failed"}
    if not all(isinstance(proposal.get(k),str) and 4 <= len(proposal[k]) <= 600 for k in ("finding","scope")):
        return envelope | {"status":"incomplete"}
    areas = sorted({n for e in evidence for n in range(e["pdf_page"]-1,e["pdf_page"]+2) if n in native})
    excerpt = reader.excerpts({n:native[n] for n in areas}, claims, cap=45000)
    verify = reader.call_reader(dest,"verify",CHECK,{"title":paper["title"],"proposal":proposal,"pdf_pages":excerpt,
                  "reading_text":{n:parsed[n] for n in areas if n in parsed and len(parsed[n])<11000}})
    if verify.get("accepted") is not True or not isinstance(verify.get("reason"),str) or not verify["reason"].strip():
        return envelope | {"status":"independent_check_withheld"}
    return envelope | {"status":"verified","kind":state,"finding":proposal["finding"],"scope":proposal["scope"],
                       "evidence":[{"pdf_page":e["pdf_page"],"quote":e["quote"]} for e in evidence],
                       "review_reason":verify["reason"]}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot",type=Path)
    ap.add_argument("--sha256",required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--workers",type=int,choices=range(1,11),default=5)
    args=ap.parse_args()
    if hashlib.sha256(args.snapshot.read_bytes()).hexdigest()!=args.sha256:
        raise SystemExit("Frozen snapshot mismatch")
    snapshot=reader.load(args.snapshot)
    active={r["doc_id"] for r in snapshot["record_details"].values()}
    papers=reader.load(reader.PILOT/"catalog/papers.json")
    labels={r["doc_id"]:r["content_nature"] for r in reader.load(reader.PILOT/"catalog/classifications.json")["records"]}
    by_doc=defaultdict(list)
    for line in (reader.PILOT/"annotations/claims.jsonl").read_text("utf-8-sig").splitlines():
        if line.strip():
            claim=json.loads(line)
            by_doc[claim["doc_id"]].append(claim)
    categories=defaultdict(list)
    for paper in papers:
        if paper["doc_id"] not in active:
            categories[labels.get(paper["doc_id"],"unknown")].append(paper)
    chosen=list(categories.pop("original_research",[]))
    for items in categories.values():
        chosen.append(min(items,key=lambda p:hashlib.sha256(("unlinked-source-review-v1|"+p["doc_id"]).encode()).digest()))
    print("Source-first reassessment:",len(chosen),"documents, frozen",args.sha256,flush=True)
    answers=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs={pool.submit(examine,p,by_doc[p["doc_id"]]):p["doc_id"] for p in chosen}
        failed=[]
        for f in as_completed(jobs):
            try:
                doc=f.result()
                answers.append(doc)
                print(doc["doc_id"],doc["status"],flush=True)
            except Exception as exc:
                failed.append(jobs[f])
                print("Incomplete",jobs[f],type(exc).__name__,str(exc)[:90],flush=True)
    if failed:
        raise SystemExit("Resume missing source reads before publishing")
    reader.save(args.output,{"schema":1,"selection_snapshot":args.sha256,"selected_documents":len(chosen),
       "selection":"当前快照未连线的全部目录原创研究，另按固定散列从其余每种内容性质各取一例；已展示条目均有两次独立上下文原页阅读。",
       "documents":sorted(answers,key=lambda d:d["doc_id"])})
    print(Counter(d["status"] for d in answers))


if __name__ == "__main__":
    main()
