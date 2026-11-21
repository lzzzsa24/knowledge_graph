"""Independently review a frozen specimen candidate and stage an additive projection.

This command never writes the production research_map directory. Candidate rows
are immutable during this pass; a bad axis, quote, sample, or scope is rejected.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

import review_map_relations as reader
import review_map_semantics as semantics
from kg_pilot.classification import classified_papers
from kg_pilot.map_relations import claims_digest, load_mapping_reviews
from kg_pilot.map_sample_bindings import (SCHEMA, SIDECAR, VERSION, ROLES, anchors, base_files,
                                          candidate_index, eligible, physical_matches, sha)
from kg_pilot.map_semantics import AXES_DIGEST, apply_semantic_reviews, digest, mapped_candidates, pair
from kg_pilot.map_source_kinds import load_source_kind_reviews


PILOT = reader.PILOT
FROZEN = PILOT / "reports/research_map_20261107/final_frozen"
BASELINE = FROZEN / "map_before_second_blind_score.json"
WORK = reader.ROOT / ".cache/map-sample-third-read"
PROMPT = """你是独立第三位原文核对者。只核对给定候选，不能改写、换格或补边。候选和原论断都可能有错，其他阅读者的结论不作为证据。三处引文要各自在原页承担指定角色，理由必须来自论文。
先问清原 claim 的观测对象、工艺时序、测量数值/方向、分母与读数指代。候选的工艺因素要在第一引文中实际用于目标试样；端点引文要正面证明本 claim 本身的被测端点；样品引文要把两端绑定为同一编号、图表样品或明确共同流程覆盖的同一组。三段可以同页或共句；仅同篇、同材料、同工艺大类、邻段、同一种工艺的不同批次、作者推测不能自动连线。显式共同流程或清楚区分的实验分支能覆盖被测组时可成立；综合流程中确实适用于样品的具体固定条件亦可索引。分别检查载具、表面/膜/见证片与接头、成品、测试前后、尺寸/几何、分母与子样本；不要用本篇其它读数替换署名 claim。
行：需真实、有具体内容的该因素。单独写“无需某清洗/未退火”而没有实际处理组和同端点对照，不能据此给该行加实证；真正的无处理对照须有相应处理组和同一范围的测量。同步带压热接触与独立后退火分开，后退火若单列工步也可能施压；测量等待、仪器载荷、可靠性应力不是制备条件。铜膜或铜填充本身不是晶粒/取向的组织变量；平坦化后的形貌端点不能倒推未经证明的CMP参数。
列：静态/初始电阻或独立报告的绝对电阻与温循、湿热、储存或电应力造成的漂移/保留量不同。温循后的电阻增减或稳定性属于热机械/环境可靠性。芯片级电流应力/寿命区别于初始电连续性。筛掉空洞再做的电测不能成为空洞端点，合格判据、工艺设定、设计声明、模拟图、拟合、参考/预期、设备重复性均不能变为制程实测。明确区分整链/接头/比电阻、面积占比/芯片通过率、完成与计划。若表或图实际呈现另外一组结论，拒绝。
关系：contrast 必须真实操纵这个具体因素并比较同类端点，候选需如实说明共变和比较局限；condition 是该实测样品的固定材料或制程设置，不能把观察到的结果反写成设定或声称因果。必须核对候选 scope、limit 和 sample_link 的具体事实。限定不充分、错配或阶段/端点错位均不要批准；原页无法判断或图表读不清则 abstain。只要真实、具体、范围准确的研究覆盖即可接受，不追求覆盖数。
对每条输出 id、verdict（accept/reject/uncertain）、issue（stage/endpoint/specimen/contrast/attribution/scope/evidence/none）、具体中文 reason。accept 还必须逐项写 anchor_checks：factor、endpoint、sample 各给其相应原引文的物理 pdf_page 与中文 basis（说明这段原文具体核准什么、同组如何关联；不允许仅写“已核对”）。拒绝及不确定给具体页码、哪一步不足，可简洁。输出 {"decisions":[{"id":0,"verdict":"accept","issue":"none","reason":"...","anchor_checks":{"factor":{"pdf_page":1,"basis":"..."},"endpoint":{"pdf_page":2,"basis":"..."},"sample":{"pdf_page":1,"basis":"..."}}}]}。每个ID恰好一次。"""


def pool_inputs(candidate_path, expected_sha, baseline_path, *, require_full):
    raw = candidate_path.read_bytes()
    if sha(raw)!=expected_sha:
        raise ValueError("候选文件 SHA-256 不匹配")
    candidate = json.loads(raw)
    documents, entries = candidate_index(candidate)
    original = baseline_path.read_bytes()
    if sha(original)!=candidate["comparison_snapshot"]:
        raise ValueError("候选所依据地图的 SHA-256 不一致")
    baseline = json.loads(original)
    protocol_bytes = (FROZEN/"sample_binding_third_read_protocol.json").read_bytes()
    protocol = json.loads(protocol_bytes)
    if (protocol.get("prompt")!=PROMPT or protocol.get("prompt_sha256")!=sha(PROMPT.encode())
            or protocol.get("axes_digest")!=AXES_DIGEST or protocol.get("reader_model")!=reader.MODEL
            or protocol.get("version")!=VERSION):
        raise ValueError("当前三读判据与先行冻结的审核协议不符")
    claims = [json.loads(s) for s in (PILOT/"annotations/claims.jsonl").read_text("utf-8-sig").splitlines() if s]
    papers = classified_papers(reader.load(PILOT/"catalog/papers.json"),claims,pilot=PILOT)
    by_paper = {p["doc_id"]:p for p in papers}
    grouped = defaultdict(list)
    for c in claims:
        grouped[c["doc_id"]].append(c)
    measured = [c for c in claims if c["claim_type"]=="reported_experiment" and baseline["record_types"].get(c["claim_id"])=="experiment"]
    cohort = {c["doc_id"] for c in measured}
    if not set(documents) <= cohort or (require_full and set(documents)!=cohort):
        raise ValueError("候选资料集合与冻结地图的实测队列不一致")
    for doc_id, doc in documents.items():
        paper = by_paper[doc_id]
        if (paper["sha256"]!=doc["source_sha256"] or claims_digest(grouped[doc_id])!=doc["claims_digest"]
                or not physical_matches(paper)):
            raise ValueError("候选原 PDF 或论断集已过期：" + doc_id)
    kinds = load_source_kind_reviews(PILOT/"research_map/source_kind_reviews.json")
    upstream, withheld, _ = load_mapping_reviews(PILOT,papers,claims)
    initial = mapped_candidates(papers,claims,upstream,withheld)
    links, hidden, _ = apply_semantic_reviews(PILOT,papers,claims,upstream,withheld)
    current = mapped_candidates(papers,claims,links,hidden)
    locked = set(hidden)|set(withheld)
    for group in initial.values():
        locked.update(map(pair,group))
    current_edges = {pair(x):x["previous"] for group in current.values() for x in group}
    frozen_edges = {(c,cell["row"],cell["column"]):cell.get("reviewed_records",{}).get(c)
                    for cell in baseline["cells"] for c in cell["records"]}
    if current_edges!=frozen_edges:
        raise ValueError("当前语义基座与冻结配对/已签名连线不同")
    locked.update(current_edges)
    return dict(raw=raw, hash=expected_sha, candidate=candidate, documents=documents, entries=entries,
                by_paper=by_paper, claims=grouped, by_claim={c["claim_id"]:c for c in claims}, kinds=kinds,
                locked=locked, base=current_edges, base_files=base_files(PILOT),
                protocol_sha256=sha(protocol_bytes),cohort_documents=len(cohort), cohort_claims=len(measured))


def row_dir(state):
    return WORK/state["hash"]/digest([VERSION,PROMPT,reader.MODEL,AXES_DIGEST,state["base_files"]])


def row_path(state, link):
    return row_dir(state)/"records"/(digest(link)+".json")


def common(state,link):
    doc = state["documents"][link["doc_id"]]
    return dict(doc_id=doc["doc_id"],claim_id=link["claim_id"],row=link["row"],column=link["column"],
                input_digest=digest(link),candidate_sha256=state["hash"],
                source_sha256=doc["source_sha256"],claims_digest=doc["claims_digest"])


def source_packet(state, doc_id, items):
    paper = state["by_paper"][doc_id]
    claims = state["claims"][doc_id]
    parsed, norms = semantics.material(paper,claims,[dict(claim_id=l["claim_id"],previous=l) for l in items])
    native, ordered, _ = semantics.originals(str(Path(paper["local_path"]).resolve()),paper["sha256"])
    pages = defaultdict(list)
    errors = {}
    for l in items:
        for role, e in anchors(l).items():
            if not semantics.at_page(e,norms):
                errors[pair(l)] = "物理页字面核验失败，角色为 "+role
            else:
                pages[e["pdf_page"]].append(e["quote"])
    # Submit complete source text on the cited pages, preserving figure/table
    # order as well as the full material and methods context around short anchors.
    for page in parsed["pages"]:
        n = page["pdf_page"]
        if n in pages:
            page["text_excerpts"] = [native[n]] if len(native[n])<=16000 else semantics.snippets(native[n],pages[n])
            if reader.norm(ordered[n])!=reader.norm(native[n]):
                page["layout_excerpts"] = semantics.snippets(ordered[n],pages[n])
    parsed.pop("options",None)
    return parsed,errors


def packet_jobs(rows, max_rows=9, max_docs=3):
    grouped = defaultdict(list)
    for l in sorted(rows,key=lambda x:(x["doc_id"],pair(x))):
        grouped[l["doc_id"]].append(l)
    current = []
    for items in grouped.values():
        for start in range(0,len(items),max_rows):
            piece = items[start:start+max_rows]
            if current and (len(current)>=max_docs or sum(map(len,current))+len(piece)>max_rows):
                yield current
                current = []
            current.append(piece)
    if current:
        yield current


def assess_job(state,job,run):
    packet = dict(rows=reader.ROWS,columns=reader.COLUMNS,sources=[],items=[])
    all_links = []
    outputs = []
    for items in job:
        doc_id = items[0]["doc_id"]
        paper = state["by_paper"][doc_id]
        valid = []
        for l in items:
            why = None
            if pair(l) in state["locked"]:
                why = "该坐标已由既有映射或排除裁决占用。"
            elif not eligible(l,paper,state["by_claim"].get(l["claim_id"]),state["kinds"]):
                why = "既有实测论断、三端证据结构或结果页归属未核准。"
            if why:
                outputs.append(dict(common(state,l),verdict="ineligible",issue="eligibility",reason=why))
            else:
                valid.append(l)
        if not valid:
            continue
        context, failures = source_packet(state,doc_id,valid)
        packet["sources"].append(context)
        for l in valid:
            if pair(l) in failures:
                outputs.append(dict(common(state,l),verdict="ineligible",issue="evidence",reason=failures[pair(l)]))
                continue
            packet["items"].append(dict(l,id=len(all_links)))
            all_links.append(l)
    if run and all_links:
        directory = row_dir(state)/"calls"/digest(packet)
        decisions, prompt = semantics.assessment_pass(directory,"third",PROMPT,packet,range(len(all_links)),
                                                       "verdict",{"accept","reject","uncertain"})
        for i, l in enumerate(all_links):
            d = decisions[i]
            record = dict(common(state,l),verdict=d["verdict"],reason=d["reason"][:1400],issue=d.get("issue","evidence"),
                          review=dict(model=reader.MODEL,effort="ultra",packet_sha256=digest([prompt,packet]),
                                      reply_sha256=digest(d),candidate_id=i))
            checks = d.get("anchor_checks")
            if d["verdict"]=="accept":
                physical = anchors(l)
                if (not isinstance(checks,dict) or any(not isinstance(checks.get(r),dict)
                        or type(checks[r].get("pdf_page")) is not int or checks[r]["pdf_page"]!=physical[r]["pdf_page"]
                        or not isinstance(checks[r].get("basis"),str) or len(checks[r]["basis"].strip())<6 for r in ROLES)):
                    record.update(verdict="uncertain",issue="evidence",reason="第三读未逐项核准全部三个原页角色。"+record["reason"])
                else:
                    record["anchor_checks"] = {r:{"pdf_page":checks[r]["pdf_page"],"basis":checks[r]["basis"][:1000]} for r in ROLES}
            outputs.append(record)
    if run:
        for d in outputs:
            reader.save(row_path(state,state["entries"][pair(d)]),d)
    return Counter(d["verdict"] for d in outputs),len(all_links)


def validate_replies(state, records):
    """Recompute prompt and raw reply hashes and join each ballot to its exact candidate payload."""
    traces = {}
    for f in (row_dir(state)/"calls").glob("*/*-input.json"):
        received = f.with_name(f.name.removesuffix("-input.json")+".json")
        if not received.is_file():
            continue
        original, answers = reader.load(f),reader.load(received)
        prompt, packet = original["prompt"],original["packet"]
        if (answers.get("key")!=reader.digest([reader.VERSION,reader.MODEL,prompt,packet])
                or original["packet_sha256"]!=digest([prompt,packet])):
            continue
        items = {d["id"]:d for d in packet["items"]}
        replies = answers.get("output",{}).get("decisions")
        if not isinstance(replies,list):
            continue
        for answer in replies:
            if not isinstance(answer,dict) or type(answer.get("id")) is not int or answer["id"] not in items:
                continue
            proposed = dict(items[answer["id"]])
            proposed.pop("id")
            traces[original["packet_sha256"],answer["id"],digest(answer)] = proposed,answer
    for d in records:
        if d["verdict"]=="ineligible":
            continue
        review = d["review"]
        proposed, answer = traces[review["packet_sha256"],review["candidate_id"],review["reply_sha256"]]
        candidate = state["entries"][pair(d)]
        same_verdict = answer.get("verdict")==d["verdict"]
        unverified_positive = (answer.get("verdict")=="accept" and d["verdict"]=="uncertain"
                               and d["reason"].startswith("第三读未逐项核准全部三个原页角色。"))
        if (proposed!=candidate or digest(candidate)!=d["input_digest"] or not (same_verdict or unverified_positive)
                or (same_verdict and d["reason"]!=answer["reason"][:1400])
                or (d["verdict"]=="accept" and (answer.get("verdict")!="accept" or any(
                    answer.get("anchor_checks",{}).get(r,{}).get("basis","")[:1000] != d["anchor_checks"][r]["basis"]
                    or answer["anchor_checks"][r].get("pdf_page")!=d["anchor_checks"][r]["pdf_page"] for r in ROLES)))):
            raise ValueError("第三读原始答复与候选/原页角色不一致")


def projected_snapshot_hash(release_file):
    from web.research_cache import DATA_FILES,digest_files
    return digest_files(PILOT,DATA_FILES,overrides={SIDECAR:release_file})


def freeze_copy(proposed, destination):
    """A previously frozen candidate can be replayed, never silently replaced."""
    if destination.is_file():
        if sha(destination.read_bytes())!=sha(proposed.read_bytes()):
            raise ValueError("已有不同的冻结文件，拒绝覆盖：" + destination.name)
        return
    # Both source and destination lie in the project. Atomic rename from a
    # sibling keeps readers away from any partially written document.
    temporary = destination.with_name(destination.name+"."+uuid.uuid4().hex+".tmp")
    temporary.write_bytes(proposed.read_bytes())
    temporary.replace(destination)


def audit_text(result,rows,state):
    counts,relations = result["decisions"],result["new_relations"]
    lines = ["# 样品组新增配对第三读", "",
             f"在 {result['cohort_documents']} 份资料的 {result['cohort_experimental_claims']} 条既有实测论断中，"
             f"前序复核提交 {len(rows)} 个格内候选。第三读批准 {counts.get('accept',0)} 个，"
             f"拒绝 {counts.get('reject',0)} 个，证据不足 {counts.get('uncertain',0)} 个，"
             f"未通过原页、论断或坐标资格核验 {counts.get('ineligible',0)} 个。", "",
             f"投影保留全部 {result['base_pairs']} 个原配对及其签名载荷，另增 {relations.get('condition',0)} 个固定条件和 "
             f"{relations.get('contrast',0)} 个对照，合计 {result['final_pairs']} 个配对。"
             f"其中 {result['newly_mapped_claims']} 条论断、{result['newly_mapped_documents']} 份资料首次入图。", "",
             f"核准新增项的 {result['verified_positive_anchors']} 个工艺、结果及样品原页角色已重新从 PDF 核对。"
             "三读使用隔离的同家族模型上下文；已拒绝和无法确定的候选均不计数。", "",
             "| 未通过时的首要问题 | 配对 |", "|---|---:|"]
    labels={"stage":"工艺阶段","endpoint":"结果端点","specimen":"试样／组别","contrast":"处理对照",
            "attribution":"来源归属","scope":"适用范围","evidence":"原页依据","eligibility":"坐标或论断资格"}
    for key,n in result["issues"].most_common():
        lines.append(f"| {labels.get(key,key)} | {n} |")
    lines += ["","## 原页追溯示例", "",
              "从批准、拒绝和待核项中各取一条，选择规则为候选载荷 SHA-256 排序最小值。"]
    for verdict,label in (("accept","核准"),("reject","拒绝"),("uncertain","待核")):
        examples = sorted((d for d in rows if d["verdict"]==verdict),key=lambda d:d["input_digest"])
        if not examples:
            continue
        chosen=examples[0]; link=state["entries"][pair(chosen)]; refs=anchors(link)
        pages="、".join(str(refs[r]["pdf_page"]) for r in ROLES)
        lines += ["",f"- **{label}**　`{link['claim_id']}`，{link['row']} × {link['column']}。"
                  f"工艺、结果、样品组锚点分别位于物理第 {pages} 页。{chosen['reason']}"]
    lines += ["","## 冻结标识","",
              f"- 三读协议 `{result['protocol_sha256']}`",
              f"- 原候选文件 `{result['candidate_sha256']}`",
              f"- 复核 sidecar 候选 `{result['release_sha256']}`",
              f"- 完整拟上线投影 `{result['projection_sha256']}`",""]
    return "\n".join(lines)


def stage(state):
    from web.server import research_map
    if base_files(PILOT)!=state["base_files"]:
        raise ValueError("复核期间基座变更")
    rows = [reader.load(row_path(state,l)) for l in state["entries"].values()]
    if any(row["candidate_sha256"]!=state["hash"] or row["input_digest"]!=digest(state["entries"][pair(row)]) for row in rows):
        raise ValueError("复核决定不属于当前候选")
    validate_replies(state,rows)
    # Fresh, independent literal replay on every accepted triple before staging.
    grouped = defaultdict(list)
    for d in rows:
        if d["verdict"]=="accept":
            grouped[d["doc_id"]].append(state["entries"][pair(d)])
    for doc_id, items in grouped.items():
        if not physical_matches(state["by_paper"][doc_id]) or source_packet(state,doc_id,items)[1]:
            raise ValueError("发布前原始三锚未通过：" + doc_id)
    release = dict(schema=SCHEMA,version=VERSION,axes_digest=AXES_DIGEST,base_files=state["base_files"],
                   comparison_snapshot=state["candidate"]["comparison_snapshot"],candidate_sha256=state["hash"],
                   candidate_utf8=state["raw"].decode("utf-8"),decisions=rows)
    release["release_digest"] = digest(release)
    staged = row_dir(state)/"validation"
    path = staged/"sample_binding_release_candidate.json"
    reader.save(path,release)
    projection = research_map(sample_release_path=path)
    projected = {(c,cell["row"],cell["column"]):cell.get("reviewed_records",{}).get(c)
                 for cell in projection["cells"] for c in cell["records"]}
    if any(projected.get(key)!=value for key,value in state["base"].items()):
        raise ValueError("投影改写了既有签名边")
    added = set(projected)-set(state["base"])
    accepted = {pair(d) for d in rows if d["verdict"]=="accept"}
    if added!=accepted or added & state["locked"]:
        raise ValueError("三读同意项与实际投影新增项不一致")
    projection["snapshot_id"] = projected_snapshot_hash(path)
    projection_file = staged/"sample_methods_projected_map.json"
    reader.save(projection_file,projection)
    result = dict(created_at=datetime.now(timezone.utc).isoformat(),candidate_sha256=state["hash"],
                  protocol_sha256=state["protocol_sha256"],
                  release_sha256=sha(path.read_bytes()),projection_sha256=sha(projection_file.read_bytes()),
                  base=state["candidate"]["comparison_snapshot"],base_pairs=len(state["base"]),final_pairs=len(projected),
                  cohort_documents=state["cohort_documents"],cohort_experimental_claims=state["cohort_claims"],
                  candidate_pairs=len(rows),decisions=Counter(d["verdict"] for d in rows),
                  issues=Counter(d["issue"] for d in rows if d["verdict"]!="accept"),new_relations=Counter(projected[k]["relation"] for k in added),
                  contributing_documents=len(grouped),
                  newly_mapped_claims=len({k[0] for k in projected}-{k[0] for k in state["base"]}),
                  newly_mapped_documents=len({state["by_claim"][k[0]]["doc_id"] for k in projected}
                                              - {state["by_claim"][k[0]]["doc_id"] for k in state["base"]}),
                  verified_positive_anchors=3*len(added),totals=projection["totals"],
                  existing_pairs_unchanged=True,production_written=False)
    freeze_copy(path,FROZEN/path.name)
    freeze_copy(projection_file,FROZEN/projection_file.name)
    audit = staged/"sample_binding_third_read_audit.md"
    audit.write_text(audit_text(result,rows,state),encoding="utf-8")
    freeze_copy(audit,FROZEN/audit.name)
    result["audit_sha256"] = sha(audit.read_bytes())
    manifest = FROZEN/"sample_binding_release_manifest.json"
    if manifest.exists():
        old = reader.load(manifest)
        if {k:v for k,v in old.items() if k!="created_at"}!={k:v for k,v in result.items() if k!="created_at"}:
            raise ValueError("已有不同的冻结清单，拒绝覆盖")
        return old
    reader.save(manifest,result)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("candidate",type=Path)
    ap.add_argument("--sha256",required=True)
    ap.add_argument("--baseline",type=Path,default=BASELINE)
    ap.add_argument("--preflight",action="store_true")
    ap.add_argument("--run",action="store_true")
    ap.add_argument("--stage",action="store_true")
    ap.add_argument("--workers",type=int,choices=range(1,11),default=4)
    args = ap.parse_args()
    if not (args.preflight or args.run or args.stage):
        ap.error("选择 --preflight、--run、--stage")
    state = pool_inputs(args.candidate,args.sha256,args.baseline,require_full=args.run or args.stage)
    pending = [l for l in state["entries"].values() if not args.run or not row_path(state,l).is_file()]
    jobs = list(packet_jobs(pending))
    print("Frozen",state["hash"],len(state["documents"]),"documents",len(state["entries"]),"pairs",len(jobs),"batches",flush=True)
    if args.run or args.preflight:
        errors = []
        totals = Counter()
        checked = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            active = {pool.submit(assess_job,state,j,args.run):idx for idx,j in enumerate(jobs)}
            for n, f in enumerate(as_completed(active),1):
                try:
                    counts, literal = f.result()
                    checked += literal
                    totals.update(counts)
                    print(n,len(jobs),active[f],dict(counts),literal,"eligible",flush=True)
                except Exception as exc:
                    errors.append(active[f]);print("error",active[f],type(exc).__name__,str(exc)[:300],flush=True)
        print("summary",dict(totals),"literal",checked,flush=True)
        if errors:
            raise SystemExit(2)
    if args.stage:
        print(json.dumps(stage(state),ensure_ascii=False),flush=True)


if __name__=="__main__":
    sys.path.insert(0,str(reader.ROOT))
    main()
