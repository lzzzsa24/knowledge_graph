"""Recheck potential pairs excluded at the quote/page validation stage, using actual PDF excerpts.

Only alternative exact original anchors and a second scientific reading can
recover a link. A cross-page result supplement never replaces the original
claim page. Earlier substantive rejections are outside this command's scope.
"""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import sys

import review_map_relations as reader
from audit_map_rule_links import read_doc, INSTRUCTIONS
from kg_pilot.map_relations import claims_digest

WORK=reader.ROOT/".cache/map-recovered-anchors"
CAUSES={"引句未逐字命中原页","结果页不属于该论断原始依据"}
CHECK=INSTRUCTIONS+"""
这是引文或物理页未通过自动验证后的一次独立回查。必须能从此 claim 原来举证的一页选出逐字引文，证明本条观测、样品或明确同批试样；数值只在其他页时，使用 result_evidence 数组，第一项为原claim的物理证据页，下一项为实际测量图表/段落，scope明确如何知道系同一对象。原文不足则弃权。表面粗糙度、纯Cu、聚合物膜和真正混合键合、热压加热和独立退火、筛选条件和被测结果必须区分。每一条引证必须是源页连续文字或合法ref，不能为了恢复覆盖率放松标准。"""


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers",type=int,choices=range(1,11),default=2)
    ap.add_argument("--publish",action="store_true")
    opt=ap.parse_args()
    papers={p["doc_id"]:p for p in reader.load(reader.PILOT/"catalog/papers.json")}
    claims=defaultdict(list)
    for line in (reader.PILOT/"annotations/claims.jsonl").read_text("utf-8-sig").splitlines():
        if line.strip():
            c=json.loads(line);claims[c["doc_id"]].append(c)
    docs=[]
    inputs=[]
    for d,paper in papers.items():
        source=reader.WORK/d/"accepted.json"
        if not source.is_file():continue
        scan=reader.load(source)
        if (scan["source_sha256"]!=paper["sha256"] or scan["claims_digest"]!=claims_digest(claims[d])
                or scan["version"]!=reader.VERSION):continue
        successes={(e["claim_id"],e["row"],e["column"]) for e in scan["links"]}
        queued={}
        pages=set()
        for r in scan["rejected"]:
            edge=r["link"]
            if r["reason"] not in CAUSES or not isinstance(edge,dict):continue
            pair=tuple(edge.get(k) for k in ("claim_id","row","column"))
            if pair in successes or not all(pair):continue
            queued[pair]=dict(zip(("claim_id","row","column"),pair))
            for field in ("process_evidence","result_evidence"):
                reference=edge.get(field)
                if isinstance(reference,dict) and type(reference.get("pdf_page")) is int:
                    pages.add(reference["pdf_page"])
        if queued: inputs.append((d,list(queued.values()),pages))
    print("recoverable source checks",len(inputs),"papers",sum(len(p) for _,p,_ in inputs),"candidate pairs",flush=True)
    errors=[]
    with ThreadPoolExecutor(max_workers=opt.workers) as pool:
        jobs={pool.submit(read_doc,papers[d],claims[d],pairs,work=WORK,instructions=CHECK,extra_pages=pages):d for d,pairs,pages in inputs}
        for f in as_completed(jobs):
            d=jobs[f]
            try:
                result=f.result()
                links=[{**r["link"],"origin":"recovered_original_anchors"} for r in result["decisions"] if r["status"]=="supported"]
                docs.append({k:result[k] for k in ("doc_id","source_sha256","claims_digest")}|{"links":links})
                print(d,"recovered",len(links),"of",len(result["decisions"]),flush=True)
            except Exception as exc:
                errors.append(d);print("incomplete",d,type(exc).__name__,str(exc)[:120],flush=True)
    if opt.publish and not errors:
        reader.save(reader.PILOT/"research_map/recovered_relations.json",{"schema":1,"reviewed_at":datetime.now(timezone.utc).isoformat(),
                    "documents":sorted(docs,key=lambda d:d["doc_id"])})
    if errors: sys.exit(2)


if __name__=="__main__":main()
