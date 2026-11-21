"""Review every currently mapped claim/row/column against signed physical PDF pages.

--freeze snapshots the upstream candidate set. --run rechecks physical sources
and uses isolated read-only Codex contexts. --publish merges only decisions
which still match current upstream evidence. The application never calls a model.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import unicodedata
import uuid

import pymupdf

import review_map_relations as reader
from kg_pilot.map_relations import claims_digest, load_mapping_reviews
from kg_pilot.map_semantics import (SCHEMA, VERSION, _valid, apply_semantic_reviews, digest,
                                    mapped_candidates, pair)


WORK = reader.ROOT / ".cache/map-semantic-review"
PILOT = reader.PILOT
REPORT = PILOT / "reports/research_map_20261107/semantics"
FROZEN = WORK / "frozen.json"
OVERLAY = PILOT / "research_map/semantic_reviews.json"

# Applied to every candidate from both upstream passes, before inspecting outcomes.
RUBRIC = """用行定义、列定义逐一核对署名 claim 和原始物理页。文献分开，不能在同篇不同试样、不同测量或不同作者之间拼接证据。
1) 先还原操作顺序：预处理/预退火、带载接触/热压/同步加热、接触后的独立热处理、测量等待/稳定、试验应力。明确独立退火步骤才进 annealing；作者明确称为退火的键合前材料退火可保留但务必界定对象。同步热压温度/保温时间归 alignment_contact（且应有明确接触、压力或同步工艺依据）。分别进行的后退火仍属于 annealing。只有温度时间且阶段不清则待查。测量时长不能变成工艺因果，热循环/高温存储/电应力与退火、静态电阻、最终寿命分开。
2) 查明 claim 真正被测或被声明的因变量、受测结构、阈值与分母。预筛无空洞后做电测：排除标准不是空洞测量结果，条件电测良率不能外推全样本。面内结合面积、芯片/链/堆叠良率和单接头良率分别看。电阻、片阻、比接触电阻与连续性依据实际对象注明。层内材料试验、界面接头、见证片、模拟输入、显微图位点不可互换。见证片/薄膜本身被研究且行列合适可以保留，但 scope 限于其自身。页码在 claim 证据页不够，还须引文证明这一 claim 的端点。
3) 分类：contrast 需原文有具体组别、改变此行因素并比较该被测结果。多个条件一并变化须明说共变，不能单因果。同试样固定条件可为 condition。测量方法、仪器能力和规范要求为 method，未执行的资格规范不可当通过实测。设计/模型解释、预测、综述和转引须标明层级；未测的拟议功能不能当作实际被测量结果。不能仅因同一流程的通用步骤而连边。
4) 验证旧引用和边界，结果必须来自本 claim 的证据物理页。若当前证据不能确定，不推测。不补其它 claim 的新边。引文可跨页提供，但须有同批样品/结构的连接；两端分别提供可定位的原句。"""

ASSESS = RUBRIC + """
对每条 items 的原格提一个决策。原值 previous 是待检；空值意味着只有词命中。只要前因、端点、层级、引文和范围完全正确，优先 keep（仅适用于 previous 非空且 previous_quotes_verified 为真）。若格、引文、关系或边界须修正且同一 claim 有充分原文，只提出唯一最佳 repair，包括规则边首次正式证实。若旧引文有字符不能核准，可从给定原文重新摘录足以证明该端点的连续短句；不要复写乱码。热压/退火同时存在仅能按此条 claim 问的关联判断；不要假设所有样品都经历讨论中每个步骤。无此关联且可定位为 exclude，证据缺失/截取看不清为 unsure。给中文具体理由，最多两句。keep 复用原有证据。其余尽量给两端证据：factor 为真实工艺或可说明误连的操作，endpoint 对应该 claim 中实际结果/规范原句。优先引用该文 options 的 ref 字符串，也可用 {"pdf_page":n,"quote":"逐字连续原句，8至350字符"}。正向证据须清晰。repair 同时给 row,column,relation,scope,limit；scope/limit 用中文写清试样阶段、指标口径、共变等。全程只用数据包原文。输出 {"decisions":[{"id":0,"action":"keep|repair|exclude|unsure","issue":"如 process_stage, endpoint, specimen, source_role, denominator, attribution, contrast, evidence, none","reason":"...","factor":"ref","endpoint":"ref","row":"...","column":"...","relation":"...","scope":"...","limit":"..."}]}，每个 id 恰好一次。"""

CONFIRM = RUBRIC + """
你是一个新的独立审阅者。请重新读原始页和原 claim，检查每一条待发布 link（可能是另一格、降级的解释或有更明确限制）。需看清工艺发生阶段、样品、实际结果、单位/分母和对照是否成立；同一页并不自动证明同一测量。尤其要逐字检查具体的 scope、limit 是否忠于材料。怀疑或语境缺失则不批准。不要去猜前一位审阅者怎么想。输出 {"decisions":[{"id":0,"accepted":true或false,"reason":"具体中文理由"}]}。每个 id 恰好一次。"""


def now():
    return datetime.now(timezone.utc).isoformat()


def inventory():
    papers = reader.load(PILOT / "catalog/papers.json")
    claims = [json.loads(s) for s in (PILOT / "annotations/claims.jsonl").read_text("utf-8-sig").splitlines() if s]
    links, withheld, _ = load_mapping_reviews(PILOT, papers, claims)
    grouped = defaultdict(list)
    for c in claims:
        grouped[c["doc_id"]].append(c)
    rows = mapped_candidates(papers, claims, links, withheld)
    by_doc = {p["doc_id"]: p for p in papers}
    docs = [dict(doc_id=d, source_sha256=by_doc[d]["sha256"], claims_digest=claims_digest(grouped[d]), items=rows[d])
            for d in sorted(rows)]
    return papers, claims, links, withheld, grouped, docs


def kernel():
    return digest([VERSION, SCHEMA, ASSESS, CONFIRM, reader.MODEL, reader.ROWS, reader.COLUMNS])


def write_snapshot(docs):
    fingerprint = digest([kernel(), docs])
    frozen = dict(schema=SCHEMA, version=VERSION, kernel=kernel(), fingerprint=fingerprint, frozen_at=now(), documents=docs)
    reader.save(WORK / "snapshots" / (fingerprint + ".json"), frozen)
    reader.save(FROZEN, frozen)
    return frozen


def load_snapshot():
    frozen = reader.load(FROZEN)
    if frozen.get("kernel") != kernel() or frozen.get("fingerprint") != digest([kernel(), frozen.get("documents")]):
        raise ValueError("冻结输入或审查版本已变化，需要重新冻结。")
    return frozen


def cache_key(item, doc):
    return digest([kernel(), doc["source_sha256"], doc["claims_digest"], item["input_digest"]])


def result_path(item, doc):
    return WORK / "pairs" / doc["doc_id"] / (cache_key(item, doc) + ".json")


def at_page(value, norm_pages):
    if not isinstance(value, dict):
        return None
    n, quote = value.get("pdf_page"), value.get("quote")
    if (type(n) is not int or n not in norm_pages or not isinstance(quote, str)
            or not 8 <= len(quote.strip()) <= 450 or "\ufffd" in quote
            or any(unicodedata.category(char) in {"Cc", "Co"} and not char.isspace() for char in quote)):
        return None
    needle = reader.norm(quote)
    if len(needle) < 6 or not any(needle in variant for variant in norm_pages[n]):
        return None
    return {"pdf_page": n, "quote": quote.strip()}


@lru_cache(maxsize=24)
def originals(path, sha):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("原始 PDF 哈希不匹配")
    with reader.PDF_LOCK, pymupdf.open(stream=raw, filetype="pdf") as pdf:
        native = {n: page.get_text("text", sort=False) for n, page in enumerate(pdf, 1)}
        reading = {n: page.get_text("text", sort=True) for n, page in enumerate(pdf, 1)}
    norms = {n: (reader.norm(t), reader.norm(reading[n])) for n, t in native.items()}
    return native, reading, norms


@lru_cache(maxsize=128)
def alternate_text(path, sha, n):
    """Recover real font glyphs Poppler decodes differently on the signed PDF."""
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("补充物理文本提取前 PDF 哈希发生变化")
    result = subprocess.run([exe, "-raw", "-f", str(n), "-l", str(n), "-enc", "UTF-8", "-", "-"],
                            input=raw, capture_output=True, timeout=90)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""


def snippets(text, quotes):
    """Retain context near each actual quote even for discontinuous layout."""
    if len(text) <= 3900:
        return [text]
    spans = [(0, 600), (max(0, len(text) - 400), len(text))]
    for quote in quotes:
        words = quote.split()
        for probe in (words[:9], words[-8:]):
            if not probe:
                continue
            match = re.search(r"\s+".join(map(re.escape, probe)), text)
            if match:
                spans.append((max(0, match.start() - 420), min(len(text), match.end() + 630)))
    merged = []
    for a,b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a,b])
    return [text[a:b] for a,b in merged]


def material(paper, claims, items):
    path = str(Path(paper["local_path"]).resolve())
    native, reading, base_norms = originals(path, paper["sha256"])
    norms = dict(base_norms)
    alternate = {}
    if len(native) != paper["page_count"]:
        raise ValueError("PDF 物理页数与目录不一致")
    # Only original PDF extraction is used for acceptance. Existing annotations
    # and older parsed exports cannot validate their own evidence.
    relevant = {i["claim_id"] for i in items}
    subset = [c for c in claims if c["claim_id"] in relevant]
    options = {}
    pages = defaultdict(list)
    def add(ev):
        verified = at_page(ev, norms)
        if not verified and isinstance(ev, dict) and type(ev.get("pdf_page")) is int and ev["pdf_page"] in native:
            n = ev["pdf_page"]
            if n not in alternate:
                alternate[n] = alternate_text(path, paper["sha256"], n)
                norms[n] = (*base_norms[n], reader.norm(alternate[n]))
            verified = at_page(ev, norms)
        if not verified:
            return
        if verified not in options.values():
            options[f"q{len(options)+1}"] = verified
        pages[verified["pdf_page"]].append(verified["quote"])
    for c in subset:
        for e in c["evidence"]:
            add(e)
            if e.get("pdf_page") in native:
                pages[e["pdf_page"]]
    for i in items:
        previous = i["previous"] or {}
        for e in [previous.get("process_evidence"), previous.get("result_evidence"), *previous.get("supporting_evidence", [])]:
            add(e)
    for value in reader.source_options(subset, native, norms).values():
        if value.get("pdf_page") in pages or len(options) < 12:
            add(value)
    # Full physical pages in original text order on short papers. On longer
    # documents include owner/evidence pages plus neighboring methods context.
    focus = set(pages)
    if len(native) <= 8:
        focus.update(native)
    else:
        focus.update(n for n in native if any(abs(n - k) == 1 for k in pages))
    excerpts = []
    for n in sorted(focus):
        qs = pages.get(n, [])
        pieces = snippets(native[n], qs) if qs else [native[n][:1750]]
        # Some PDFs interleave columns in native content order. Include a second
        # layout only when an actual evidence string uniquely requires it.
        row = {"pdf_page":n, "text_excerpts":pieces}
        layout_only = [q for q in qs if reader.norm(q) not in norms[n][0] and reader.norm(q) in norms[n][1]]
        if layout_only:
            row["layout_excerpts"] = snippets(reading[n], layout_only)
        if alternate.get(n):
            row["alternate_pdf_excerpts"] = snippets(alternate[n], qs)
        excerpts.append(row)
    fields = ("claim_id", "statement", "stage", "material_system", "sample_id", "quantity", "value_text",
              "unit", "conditions", "claim_type", "evidence")
    context = {"document":{k:paper.get(k) for k in ("doc_id", "title", "is_review", "source_type", "page_count")},
               "claims":[{k:c.get(k) for k in fields} for c in subset], "options":options, "pages":excerpts}
    return context, norms


def decisions(raw, ids):
    rows = raw.get("decisions") if isinstance(raw, dict) else None
    if (not isinstance(rows, list) or len(rows) != len(ids)
            or any(not isinstance(r, dict) or type(r.get("id")) is not int or not isinstance(r.get("reason"), str)
                   or not r["reason"].strip() for r in rows) or {r["id"] for r in rows} != set(ids)):
        raise ValueError("模型未返回逐条完备且唯一的决定")
    return {r["id"]: r for r in rows}


def provenance(prompt, packet, decision):
    return dict(model=reader.MODEL, effort="ultra", packet_sha256=digest([prompt, packet]),
                candidate_id=decision["id"], reason=decision["reason"][:1200])


def assessment_pass(directory, name, prompt, packet, ids, field, allowed):
    """Keep replayable received packets; one schema repair never changes the scientific rubric."""
    last = None
    for attempt in range(2):
        suffix = "" if attempt == 0 else "-format-retry"
        actual = prompt if not suffix else prompt + "\n上一回复结构不全，请严格按提供的每个 id 和字段输出 JSON。"
        reader.save(directory / (name + suffix + "-input.json"),
                    dict(prompt=actual, packet=packet, packet_sha256=digest([actual, packet]), model=reader.MODEL))
        try:
            result = decisions(reader.call_reader(directory, name + suffix, actual, packet), ids)
            if any(type(r.get(field)) not in (str, bool) or r[field] not in allowed for r in result.values()):
                raise ValueError("决策字段类型或取值无效")
            return result, actual
        except ValueError as exc:
            last = exc
    raise last


def assess_one(candidate, decision, claim, pages, norm_pages, options, audit):
    action = decision.get("action")
    if action not in {"keep", "repair", "exclude", "unsure"}:
        raise ValueError("模型判定类型无效")
    previous = candidate["previous"] or {}
    def resolve(field, original_field):
        value = decision.get(field)
        if action == "keep":
            value = previous.get(original_field)
        if isinstance(value, dict) and "ref" in value:
            value = value["ref"]
        if isinstance(value, str):
            value = options.get(value)
        return at_page(value, norm_pages)
    factor = resolve("factor", "process_evidence")
    endpoint = resolve("endpoint", "result_evidence")
    result = dict(candidate, status="uncertain", reason=decision["reason"][:1200], issue=decision.get("issue"),
                  assessment=audit, evidence={"factor":factor,"endpoint":endpoint})
    if action == "unsure":
        return result
    if not factor or not endpoint or endpoint["pdf_page"] not in {e["pdf_page"] for e in claim["evidence"]}:
        result["reason"] = "原始 PDF 两端连续引文或本 claim 的结果物理页未核准。" + result["reason"]
        return result
    if action == "exclude":
        result["status"] = "withheld"
        return result
    if action == "keep":
        secondary_valid = all(at_page(e, norm_pages) for e in previous.get("supporting_evidence", []))
        if candidate["previous"] and secondary_valid and _valid(dict(result, status="kept", link=previous), candidate, claim, pages):
            result.update(status="kept", link=previous)
        else:
            result["reason"] = "原配对缺少可直接保留的完整核准连线。" + result["reason"]
        return result
    link = {k: decision.get(k) for k in ("row", "column", "relation", "scope", "limit")}
    link.update(doc_id=candidate["doc_id"], claim_id=candidate["claim_id"], process_evidence=factor, result_evidence=endpoint)
    if reader.validate(link, {claim["claim_id"]:claim}, norm_pages):
        result["reason"] = "修正提案缺少可验证的轴、声明范围或原页。" + result["reason"]
        return result
    result.update(status="proposed", link=link)
    return result


def make_jobs(docs, max_items, max_docs, skip_done=True):
    """Bundles short papers; larger papers are split by stable claim ordering."""
    job = []
    for d in docs:
        pending = [item for item in d["items"] if not skip_done or not result_path(item, d).is_file()]
        for start in range(0, len(pending), max_items):
            piece = pending[start:start+max_items]
            if job and (len(job) >= max_docs or sum(len(items) for _,items in job) + len(piece) > max_items):
                yield job
                job = []
            job.append((d, piece))
    if job:
        yield job


def run_job(job, papers, claims):
    packet = {"rows":reader.ROWS, "columns":reader.COLUMNS, "sources":[], "items":[]}
    source_by_doc = {}
    candidates = []
    for doc, items in job:
        doc_id = doc["doc_id"]
        context, norms = material(papers[doc_id], claims[doc_id], items)
        source_by_doc[doc_id] = (doc, context, norms)
        packet["sources"].append(context)
        for item in items:
            prior = item["previous"] or {}
            verified = bool(prior) and all(at_page(e, norms) for e in [
                prior.get("process_evidence"), prior.get("result_evidence"), *prior.get("supporting_evidence", [])])
            packet["items"].append(dict(id=len(candidates), previous_quotes_verified=verified,
                                        **{k:item[k] for k in ("doc_id","claim_id","row","column","previous")}))
            candidates.append(item)
    target = WORK / "calls" / digest([kernel(), packet])
    ratings, first_prompt = assessment_pass(target, "assess", ASSESS, packet, range(len(candidates)),
                                             "action", {"keep", "repair", "exclude", "unsure"})
    rows = []
    repairs = []
    claim_lookup = {c["claim_id"]:c for doc_id in source_by_doc for c in claims[doc_id]}
    for i, candidate in enumerate(candidates):
        doc_id = candidate["doc_id"]
        _, context, norms = source_by_doc[doc_id]
        row = assess_one(candidate, ratings[i], claim_lookup[candidate["claim_id"]], papers[doc_id]["page_count"],
                         norms, context["options"], provenance(first_prompt, packet, ratings[i]))
        if row["status"] == "proposed":
            repairs.append(i)
        rows.append(row)
    if repairs:
        docs = {rows[i]["doc_id"] for i in repairs}
        verification = {k:packet[k] for k in ("rows", "columns")}
        verification["sources"] = [s for s in packet["sources"] if s["document"]["doc_id"] in docs]
        verification["items"] = [dict(id=i, link=rows[i]["link"]) for i in repairs]
        checks, second_prompt = assessment_pass(target, "independent", CONFIRM, verification, repairs, "accepted", {True, False})
        for i in repairs:
            check = checks[i]
            if type(check.get("accepted")) is not bool:
                raise ValueError("二审回复缺少是否批准")
            rows[i]["confirmation"] = dict(provenance(second_prompt, verification, check), accepted=check["accepted"],
                                            link_digest=digest(rows[i]["link"]))
            if check["accepted"]:
                rows[i]["status"] = "replaced"
            else:
                rows[i]["rejected_proposal"] = rows[i].pop("link")
                rows[i].update(status="uncertain", reason="独立复核未同意修正：" + check["reason"][:1000])
    for row, candidate in zip(rows, candidates):
        doc = source_by_doc[row["doc_id"]][0]
        if not _valid(row, candidate, claim_lookup[row["claim_id"]], papers[row["doc_id"]]["page_count"]):
            raise ValueError("内部校验拒绝写入决定")
        reader.save(result_path(candidate, doc), dict(key=cache_key(candidate, doc), decision=row))
    return Counter(row["status"] for row in rows)


def publish(frozen):
    papers, claims, links, withheld, _, docs = inventory()
    paper_by_id = {p["doc_id"]:p for p in papers}
    by_claim = {c["claim_id"]:c for c in claims}
    published = []
    statuses, issues = Counter(), Counter()
    for doc in docs:
        accepted = []
        for item in doc["items"]:
            cache = result_path(item, doc)
            if not cache.is_file():
                continue
            previous = reader.load(cache)
            d = previous["decision"]
            if previous.get("key") != cache_key(item, doc) or not _valid(d, item, by_claim[item["claim_id"]], paper_by_id[doc["doc_id"]]["page_count"]):
                continue
            accepted.append(d)
            statuses[d["status"]] += 1
            issues[str(d["issue"])] += 1
        if accepted:
            published.append({**{k:doc[k] for k in ("doc_id","source_sha256","claims_digest")}, "decisions":accepted})
    if not published:
        return dict(published_pairs=0)
    overlay = dict(schema=SCHEMA, version=VERSION, kernel=kernel(), frozen_fingerprint=frozen["fingerprint"],
                   reviewed_at=now(), documents=published)
    if OVERLAY.is_file():
        last = reader.load(OVERLAY)
        reader.save(WORK/"publications"/(digest(last) + ".json"), last)
    reader.save(OVERLAY, overlay)
    _, _, audit = apply_semantic_reviews(PILOT, papers, claims, links, withheld)
    summary = dict(reviewed_at=overlay["reviewed_at"], overlay_sha256=hashlib.sha256(OVERLAY.read_bytes()).hexdigest(),
                   frozen_fingerprint=frozen["fingerprint"], frozen_documents=len(frozen["documents"]),
                   frozen_pairs=sum(len(d["items"]) for d in frozen["documents"]),
                   current_documents=len(docs), published_documents=len(published), states=statuses, issues=issues, audit=audit)
    reader.save(REPORT/"coverage.json", summary)
    return summary


def verify_release():
    """Independently replay the actual published artifact, not only completed jobs."""
    papers, claims, links, withheld, grouped, docs = inventory()
    index = {pair(x):x for d in docs for x in d["items"]}
    paper_lookup = {p["doc_id"]:p for p in papers}
    claim_lookup = {c["claim_id"]:c for c in claims}
    overlay = reader.load(OVERLAY)
    counts = Counter()
    row_moves, endpoint_moves, strength_changes = Counter(), Counter(), Counter()
    errors = []
    seen = set()
    trace = {}
    for f in (WORK/"calls").glob("*/*-input.json"):
        payload = reader.load(f)
        packet = payload["packet"]
        prompt = payload["prompt"]
        cache = f.with_name(f.name.removesuffix("-input.json")+".json")
        if not cache.is_file() or digest([prompt,packet]) != payload["packet_sha256"]:
            continue
        response = reader.load(cache)
        if response.get("key") != reader.digest([reader.VERSION, payload["model"], prompt, packet]):
            continue
        returned = response.get("output", {}).get("decisions")
        if not isinstance(returned, list):
            continue
        items = {i["id"]:i for i in packet["items"]}
        for reply in returned:
            if isinstance(reply,dict) and type(reply.get("id")) is int and reply["id"] in items:
                trace[payload["packet_sha256"],reply["id"]] = (items[reply["id"]],reply)
    for doc in overlay["documents"]:
        doc_id = doc["doc_id"]
        p = paper_lookup.get(doc_id)
        if (not p or p["sha256"] != doc["source_sha256"]
                or claims_digest(grouped[doc_id]) != doc["claims_digest"]):
            errors.append({"doc_id":doc_id,"issue":"current_catalog_signature"})
            continue
        path = str(Path(p["local_path"]).resolve())
        # A newly opened byte stream is checked even when this call follows a
        # batch in the same process and original text is already cached.
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != p["sha256"]:
            errors.append({"doc_id":doc_id,"issue":"changed_physical_pdf"})
            continue
        _, _, native = originals(path, p["sha256"])
        norms = dict(native)
        counts["pdfs_rehashed"] += 1
        for d in doc["decisions"]:
            key = pair(d)
            c = index.get(key)
            if (key in seen or not c or c["doc_id"] != doc_id
                    or not _valid(d,c,claim_lookup[d["claim_id"]],p["page_count"])):
                errors.append({"pair":key,"issue":"old_payload_or_evidence_owner"})
                continue
            seen.add(key)
            verdict = d["status"]
            counts[verdict] += 1
            if verdict == "replaced":
                proposed = d["link"]
                if proposed["row"] != d["row"]:
                    row_moves[d["row"]+" → "+proposed["row"]] += 1
                if proposed["column"] != d["column"]:
                    endpoint_moves[d["column"]+" → "+proposed["column"]] += 1
                old_relation = (d.get("previous") or {}).get("relation","rule_only")
                if old_relation != proposed["relation"]:
                    strength_changes[old_relation+" → "+proposed["relation"]] += 1
            for label in ("assessment","confirmation"):
                if label in d:
                    provenance_record = d[label]
                    reviewed, answer = trace.get((provenance_record["packet_sha256"],provenance_record["candidate_id"]), ({},{}))
                    matches = str(answer.get("reason",""))[:1200] == provenance_record["reason"]
                    if label == "assessment":
                        matches = matches and all(reviewed.get(k) == d[k] for k in ("doc_id","claim_id","row","column","previous"))
                    else:
                        proposed = d.get("link") or d.get("rejected_proposal")
                        matches = (matches and proposed == reviewed.get("link") and proposed is not None
                                   and answer.get("accepted") is provenance_record["accepted"]
                                   and digest(proposed) == provenance_record["link_digest"])
                    if matches:
                        counts[label+"_packets_matched"] += 1
                    else:
                        errors.append({"pair":key,"issue":"unmatched_"+label+"_packet_or_reply"})
            citations = [e for e in (d.get("evidence") or {}).values() if e]
            if d.get("link"):
                citations.extend(d["link"].get("supporting_evidence", []))
            for e in citations:
                n = e.get("pdf_page")
                if n in norms and not at_page(e,norms) and len(norms[n]) == 2:
                    norms[n] = (*norms[n],reader.norm(alternate_text(path,p["sha256"],n)))
                if not at_page(e,norms):
                    errors.append({"pair":key,"issue":"physical_quote","pdf_page":n})
                else:
                    counts["physical_quotes_matched"] += 1
    final_links, hidden, application = apply_semantic_reviews(PILOT, papers, claims, links, withheld)
    final = mapped_candidates(papers, claims, final_links, hidden)
    current = {pair(x) for group in final.values() for x in group}
    before = set(index)
    audit = dict(checked_at=now(), overlay_sha256=hashlib.sha256(OVERLAY.read_bytes()).hexdigest(),
                 snapshot_current_pairs=len(before), applied_pairs=len(seen), pending_pairs=len(before-seen),
                 final_pairs=len(current), original_pairs_removed=len(before-current), new_pairs=len(current-before),
                 final_mapped_claims=len({key[0] for key in current}), final_mapped_documents=len(final),
                 current_upstream_documents=len(docs), counts=counts, application=application, errors=errors,
                 approved_stage_changes=row_moves, approved_endpoint_changes=endpoint_moves,
                 approved_relation_changes=strength_changes)
    if application.get("current_pairs_reviewed") != len(seen):
        errors.append({"issue":"loader_and_publication_disagree"})
    reader.save(REPORT/"verification.json",audit)
    def number(key):
        return str(counts.get(key,0))
    lines = ["# 工艺阶段及端点校勘结果", "",
             f"复核时点（UTC）为 {audit['checked_at']}。发布文件 SHA-256 为 `{audit['overlay_sha256']}`。",
             "", "| 项目 | 数量 |", "|---|---:|",
             f"| 上游当前研究配对 | {len(before)} |", f"| 匹配当前签名的校勘 | {len(seen)} |",
             f"| 当前配对待处理 | {len(before-seen)} |", f"| 保留原边 | {number('kept')} |",
             f"| 经独立复核批准的修正提案 | {number('replaced')} |", f"| 有证据排除 | {number('withheld')} |",
             f"| 无法确证或两次判断未达共识 | {number('uncertain')} |",
             f"| 最终原格移出的配对 | {len(before-current)} |", f"| 独立核准并实际新增的格内配对 | {len(current-before)} |",
             f"| 发布后格内配对 | {len(current)} |", f"| 发布后入图论断 / 资料 | {audit['final_mapped_claims']} / {len(final)} |",
             "", f"从发布文件复验 {number('pdfs_rehashed')} 份原 PDF 的字节哈希，"
             f"核准 {number('physical_quotes_matched')} 次两端及补充引用的原页字面匹配。"
             f"{number('assessment_packets_matched')} 条初核记录、{number('confirmation_packets_matched')} 条二审记录逐条匹配当时的数据包、模型配置与原始答复。"
             f"复验异常 {len(errors)} 项，详见同目录 verification.json。", "",
             "修正提案数包括原格内改写范围、降级对照、新增正式引文及迁格。同一目标格有独立决定或遭屏蔽时，禁止重复迁入。"
             "计数以配对为单位，已核准的配对数量不能用作总体准确率。", "",
             "六张原 PDF 页的开发抽检及完整口径见同目录 method.md。"]
    for title, distribution in (("工艺阶段修正",row_moves),("端点修正",endpoint_moves),("关系强度或证据类型调整",strength_changes)):
        if distribution:
            lines.extend(["","## "+title,"","| 原值 → 核准值 | 提案数 |","|---|---:|"])
            lines.extend(f"| {change} | {n} |" for change,n in distribution.most_common())
    dest = REPORT / "audit.md"
    temp = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text("\n".join(lines)+"\n",encoding="utf-8")
    temp.replace(dest)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("freeze","plan","run","publish","verify"):
        parser.add_argument("--"+flag, action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1,11), default=3)
    parser.add_argument("--slots-file",type=Path,help="Optional local JSON integer (0–10) controlling concurrent work in flight")
    parser.add_argument("--batch-size", type=int, choices=range(1,21), default=10)
    parser.add_argument("--docs-per-batch", type=int, choices=range(1,6), default=3)
    parser.add_argument("--max-jobs", type=int)
    args = parser.parse_args()
    if not (args.freeze or args.plan or args.run or args.publish or args.verify):
        parser.error("需要 --plan、--freeze、--run、--publish 或 --verify")
    papers, claims, _, _, grouped, current = inventory()
    if args.freeze:
        frozen = write_snapshot(current)
    elif args.run or args.publish:
        frozen = load_snapshot()
    else:
        frozen = {"documents":current}
    docs = frozen["documents"]
    by_doc = {p["doc_id"]:p for p in papers}
    if args.run:
        # Frozen candidates may change upstream while readers run. Preserve their
        # provenance, but never process against some newer set of source claims.
        docs = [d for d in docs if d["doc_id"] in by_doc and d["source_sha256"] == by_doc[d["doc_id"]]["sha256"]
                and d["claims_digest"] == claims_digest(grouped[d["doc_id"]])]
    jobs = list(make_jobs(docs, args.batch_size, args.docs_per_batch))
    print(json.dumps(dict(documents=len(docs),pairs=sum(len(d["items"]) for d in docs),pending_batches=len(jobs),
                          pending_pairs=sum(len(items) for job in jobs for _,items in job))), flush=True)
    errors = []
    if args.run:
        if args.max_jobs is not None:
            jobs = jobs[:args.max_jobs]
        ceiling = 10 if args.slots_file else args.workers
        with ThreadPoolExecutor(max_workers=ceiling) as pool:
            futures = {}
            started = completed = 0
            while futures or started < len(jobs):
                slots = args.workers
                if args.slots_file:
                    try:
                        slots = reader.load(args.slots_file)
                    except (OSError,ValueError):
                        slots = 0
                    if type(slots) is not int or not 0 <= slots <= ceiling:
                        slots = 0
                while len(futures) < slots and started < len(jobs):
                    futures[pool.submit(run_job,jobs[started],by_doc,grouped)] = started
                    started += 1
                if not futures:
                    time.sleep(5)
                    continue
                done, _ = wait(futures,timeout=5,return_when=FIRST_COMPLETED)
                for future in done:
                    idx = futures.pop(future)
                    completed += 1
                    try:
                        print(completed, len(jobs), idx, dict(future.result()), flush=True)
                    except Exception as exc:
                        errors.append(idx)
                        print("error", idx, type(exc).__name__, str(exc)[:300], flush=True)
                    if args.publish and completed % 10 == 0:
                        print("publish", json.dumps(publish(frozen), ensure_ascii=False), flush=True)
    if args.publish:
        print("publish", json.dumps(publish(frozen), ensure_ascii=False), flush=True)
    if args.verify:
        result = verify_release()
        print("verify", json.dumps(result, ensure_ascii=False), flush=True)
        errors.extend(result["errors"])
    if errors:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
