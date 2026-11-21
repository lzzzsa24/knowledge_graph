"""Stage new research-map links by identifying actual specimen groups before indexing results."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re

import review_map_relations as reader
import review_map_semantics as semantic
from kg_pilot.map_relations import claims_digest, load_mapping_reviews
from kg_pilot.map_semantics import pair, mapped_candidates


WORK = reader.ROOT / ".cache/map-sample-methods"
RULE_VERSION = "sample-binding-v2-stress-endpoint"
STRESS = re.compile(r"温循|热循环|温度循环|热冲击|湿热|高温存储|thermal cycl|temperature cycl|thermal shock|\btct\b|\bhast\b",re.I)
CHANGE = re.compile(r"变化|增加|增长|下降|上升|漂移|劣化|稳定性|change|increase|drift|degrad|variation",re.I)
GROUPS = """阅读本篇实验原页，先整理实际制备或测量过的样品组和工艺流程，不要预测矩阵连线。分开示范用图、作者实际样品、对照组、见证片、模拟与转述。对不同批次、结构、界面、剂量、热压与后退火、可靠性应力要分组。记录明确适用该组的固定条件以及可区分的处理分支：可以是试片编号/共同流程明确覆盖所有相关组/同一原页句中直接说明的处理。不要因为某步骤在行业常见就写入。每项按给定行语义归类，热压时的温度压力记 alignment_contact，独立退火才记 annealing。返回 {"groups":[{"label":"原文样品组/载具或明确全称，中文可补释","scope":"这组成员及明确例外，原文如何知道","row":"一个给定行ID","treatment":"被记录的具体工艺","evidence":{"pdf_page":物理页,"quote":"12–280字符连续原文，包含可核实条件或样品组"}}]}，别把同一条件重复抄给实质相同的一组。无清晰样品关系时返回空组。"""
MATCH = """把本篇确实测到的结果与已从原页提取的样品组和工艺条件配对。逐条核对给出的中心 claim 的受测对象、端点、图表标号和分母；只提议尚未覆盖的格子。能回指同一具名组/样品或明确共同流程时，即使该因素固定也可标 condition，说明其仅是设置。无需接近同一句；必须指出为何跨页仍是同组。组别不同、原文仅在引言讲过、仅在测试前用来筛选、模型输入、失效推测都不要连。若一条记录确实同时测量了两项独立物理终点，可按原文分别说明，良率阈值或剔除标准本身不能变另一项结果。真正改变当前工艺并测各组同类结果才标contrast。逐对标明张图、分母、前后流程的适用边界。返回 {"links":[{"claim_id":"候选既有论断","group_index":给定整数,"column":"结果列ID","relation":"condition或contrast","scope":"一到两句中文，包括具体样品、处理、实测结果与对应关系","limit":"比较边界","sample_link":"中文一句，解释为何是同一组或共同流程如何覆盖对应测量","same_sample_evidence":{"pdf_page":物理页,"quote":"直接支持样品/组/流程适用或同一标号的原文"},"result_evidence":{"pdf_page":该claim举证的物理页,"quote":"支持本条测量的连续原文，可用给定原文选项 ref"}}]}。因果和同文不等同；找不到适用依据就放弃。"""
CHECK = semantic.RUBRIC + """
给定一份文献及已经逐字核准的候选。对每一条独立尝试推翻样品身份、处理时序、端点和分母。需要三点均成立：指定组确实有该处理；本条确实测了所述端点；原页可以把两者连到同一试样/同一明确组。共同工艺可以是条件，但不能把 blanket、界面、结构或另一压力组套到本组结果；同文同材料不足以证明。对照须有实测组间差异，筛选和规格不能当实测。特别审查 fixed condition 的确切组别、来源归属和 summary 是否夸大。返回 {"decisions":[{"id":整数,"accepted":true或false,"reason":"与署名试样和物理页相符的中文依据"}]}，每个ID一次，不确定即false。"""


def vetted_anchor(raw, options, norms, paper=None):
    if isinstance(raw, dict) and isinstance(raw.get("ref"), str):
        raw = options.get(raw["ref"])
    match = semantic.at_page(raw,norms)
    if (match or not isinstance(raw,dict) or not paper or type(raw.get("pdf_page")) is not int
            or raw["pdf_page"] not in norms):
        return match
    page = raw["pdf_page"]
    if len(norms[page]) < 3:
        alternative = semantic.alternate_text(paper["local_path"],paper["sha256"],page)
        norms[page] = (*norms[page],reader.norm(alternative))
    return semantic.at_page(raw,norms)


def original_material(paper, claims):
    native, ordered, norms = semantic.originals(paper["local_path"], paper["sha256"])
    if len(native) != paper["page_count"]:
        raise ValueError("Catalog page count mismatch")
    # Text repaired at intake is useful for locating a figure. Require quotes
    # to match the native PDF, sorted PDF stream or independent Poppler decode.
    reading = {p["pdf_page"]: p["text"] for p in reader.load(reader.PILOT/"parsed"/paper["doc_id"]/"pages.json")}
    options = reader.source_options(claims, native, norms)
    return ordered, reading, norms.copy(), options


def validate_links(proposals, groups, by_claim, blocked, options, norms, paper):
    links = []
    seen = set()
    for idea in proposals:
        if not isinstance(idea,dict):
            continue
        cid, group_id = idea.get("claim_id"), idea.get("group_index")
        if cid not in by_claim or type(group_id) is not int or not 0 <= group_id < len(groups):
            continue
        group = groups[group_id]
        # A stress-induced resistance delta is an ageing readout. Its unit
        # cannot turn it into the initial resistance of that bonding process.
        metric = str(by_claim[cid].get("quantity","")).replace("_"," ")
        if idea.get("column")=="electrical_resistance" and STRESS.search(metric) and CHANGE.search(metric):
            continue
        evidence = vetted_anchor(idea.get("result_evidence"), options, norms, paper)
        join = vetted_anchor(idea.get("same_sample_evidence"), options, norms, paper)
        binding = idea.get("sample_link")
        if (not evidence or not join or not isinstance(binding,str) or not 4 <= len(binding) <= 400
                or idea.get("relation") not in {"condition","contrast"}):
            continue
        link = {k:idea.get(k) for k in ("claim_id","column","relation","scope","limit")}
        link.update(doc_id=by_claim[cid]["doc_id"],row=group["row"],process_evidence=group["evidence"],
                    result_evidence=evidence,sample_link=binding,sample_group=group["label"],
                    supporting_evidence=[{**join,"role":"sample"}],origin="sample_methods")
        if reader.validate(link,by_claim,norms) or pair(link) in blocked or pair(link) in seen:
            continue
        seen.add(pair(link))
        links.append(link)
    return links


def examine(paper, claims, measurements, blocked):
    ident = paper["doc_id"]
    work = WORK / ident
    envelope = {"doc_id":ident,"source_sha256":paper["sha256"],"claims_digest":claims_digest(claims)}
    key = reader.digest([RULE_VERSION,envelope, sorted(c["claim_id"] for c in measurements), sorted(blocked), GROUPS, MATCH, CHECK])
    path = work / "accepted.json"
    if path.exists() and reader.load(path).get("key") == key:
        return reader.load(path)
    ordered,reading,norms,options = original_material(paper,measurements)
    # A full-text PDF read is bounded to relevant physical pages, with a
    # second rendering/order on the pages used for the independent check.
    pages = reader.excerpts(ordered,measurements,65000)
    group_packet = {"title":paper["title"],"rows":reader.ROWS, "pages":pages,
        "study_readouts":[[c["claim_id"],c.get("sample_id"),c.get("quantity")] for c in measurements]}
    group_output = reader.call_reader(work,"specimens",GROUPS,group_packet)
    groups = []
    for g in group_output["groups"]:
        if (not isinstance(g,dict) or g.get("row") not in reader.ROW_IDS or
                not all(isinstance(g.get(k),str) and g[k].strip() for k in ("label","scope","treatment"))):
            continue
        anchor = vetted_anchor(g.get("evidence"),{},norms,paper)
        if anchor:
            groups.append({k:g[k] for k in ("label","scope","treatment","row")} | {"evidence":anchor})
    by_claim = {c["claim_id"]: c for c in measurements}
    result = {**envelope,"key":key,"groups":len(groups),"links":[],"proposed":0,"literal_validated":0,"decisions":[]}
    if groups:
        context = {"title":paper["title"],"groups":[dict(g,group_index=i) for i,g in enumerate(groups)],
                   "columns":reader.COLUMNS,"measurement_claims":measurements,"source_options":options,
                   "reading_pages":reader.excerpts(reading,measurements,57000),"covered_or_already_challenged":sorted(blocked)}
        suggestions = reader.call_reader(work,"link",MATCH,context)["links"]
        links = validate_links(suggestions,groups,by_claim,blocked,options,norms,paper)
        result.update(proposed=len(suggestions),literal_validated=len(links))
        if links:
            cited = {e["pdf_page"] for l in links for e in (l["process_evidence"],l["result_evidence"],*l["supporting_evidence"])}
            check_packet = {"title":paper["title"],"rows":reader.ROWS,"columns":reader.COLUMNS,
                "claims":[c for c in measurements if c["claim_id"] in {l["claim_id"] for l in links}],
                "candidates":[dict(l,id=i) for i,l in enumerate(links)],
                "native_pages":reader.excerpts({n:ordered[n] for n in cited},measurements,62000),
                "reading_pages":reader.excerpts({n:reading[n] for n in cited if n in reading},measurements,37000)}
            decisions = reader.call_reader(work,"confirm",CHECK,check_packet)["decisions"]
            if (not isinstance(decisions,list) or len(decisions)!=len(links)
                    or any(not isinstance(d,dict) or type(d.get("id")) is not int or type(d.get("accepted")) is not bool or not d.get("reason") for d in decisions)
                    or {d["id"] for d in decisions} != set(range(len(links)))):
                raise ValueError("Incomplete independent check")
            result["decisions"]=decisions
            result["links"]=[links[d["id"]] for d in decisions if d["accepted"]]
    reader.save(path,result)
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot",type=Path)
    ap.add_argument("--sha256",required=True)
    ap.add_argument("--output",required=True,type=Path)
    ap.add_argument("--workers",type=int,choices=range(1,11),default=4)
    ap.add_argument("--sample",type=int,default=0,help="Deterministic first n eligible papers (0 means all)")
    ap.add_argument("--offset",type=int,default=0,help="Skip a completed pilot prefix when staging the remaining papers")
    args=ap.parse_args()
    if args.offset < 0 or args.sample < 0:
        ap.error("Sample and offset must be nonnegative")
    if hashlib.sha256(args.snapshot.read_bytes()).hexdigest()!=args.sha256:
        raise SystemExit("Frozen matrix signature mismatch")
    baseline=reader.load(args.snapshot)
    papers=reader.load(reader.PILOT/"catalog/papers.json")
    claims=[json.loads(s) for s in (reader.PILOT/"annotations/claims.jsonl").read_text("utf-8-sig").splitlines() if s]
    grouped=defaultdict(list)
    measured=defaultdict(list)
    locked=defaultdict(set)
    for c in claims:
        grouped[c["doc_id"]].append(c)
        if baseline["record_types"].get(c["claim_id"])=="experiment" and c["claim_type"]=="reported_experiment":
            measured[c["doc_id"]].append(c)
    upstream,withheld,_=load_mapping_reviews(reader.PILOT,papers,claims)
    candidate_by_doc=mapped_candidates(papers,claims,upstream,withheld)
    claim_doc={c["claim_id"]:c["doc_id"] for c in claims}
    for d,items in candidate_by_doc.items():
        locked[d].update(pair(item) for item in items)
    for entry in withheld:
        locked[claim_doc[entry[0]]].add(entry)
    for cell in baseline["cells"]:
        for cid in cell["records"]:
            locked[claim_doc[cid]].add((cid,cell["row"],cell["column"]))
    selected=sorted((p for p in papers if measured[p["doc_id"]]),
                    key=lambda p:reader.digest(["sample-methods-order-v1",p["doc_id"]]))
    selected=selected[args.offset:]
    if args.sample>0:
        selected=selected[:args.sample]
    print("Staging sample-specific protocols:",len(selected),"sources",flush=True)
    results=[]
    failed=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs={pool.submit(examine,p,grouped[p["doc_id"]],measured[p["doc_id"]],locked[p["doc_id"]]):p["doc_id"] for p in selected}
        for f in as_completed(jobs):
            try:
                result=f.result()
                results.append(result)
                print(result["doc_id"],result["groups"],"groups,",len(result["links"]),"accepted",flush=True)
            except Exception as exc:
                failed.append(jobs[f])
                print("Incomplete",jobs[f],type(exc).__name__,str(exc)[:100],flush=True)
    if failed:
        raise SystemExit("Incomplete reads. Resume without changing the frozen inputs.")
    docs=[{k:d[k] for k in ("doc_id","source_sha256","claims_digest","links")} for d in sorted(results,key=lambda d:d["doc_id"])]
    reader.save(args.output,{"schema":1,"comparison_snapshot":args.sha256,"documents":docs})
    print("Accepted",sum(len(d["links"]) for d in docs),"by role",Counter(l["relation"] for d in docs for l in d["links"]))


if __name__ == "__main__":
    main()
