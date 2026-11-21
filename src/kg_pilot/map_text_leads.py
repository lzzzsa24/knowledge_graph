"""Find passages for a reader to check a matrix cell. These never create map edges."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from .research_map import _system
from .retrieval import _fts_expression, _fts_rows
from .effective_evidence import FIELD, load_effective_evidence


# Deliberately different vocabularies for the processing step and the reported
# outcome. A single occurrence of "roughness", for instance, cannot fit both.
FACTORS = {
    "surface_planarization": "CMP|chemical mechanical polishing|polished|polishing|planarization|平坦化|抛光",
    "cleaning_activation": "cleaning|cleaned|activation|activated|plasma treatment|oxide removal|清洗|活化|去氧化",
    "surface_passivation": "passivation|passivated|capping layer|protective layer|钝化|保护层|防氧化",
    "copper_microstructure": "nanotwinned|nanocrystalline copper|grain orientation|grain size|electroplated copper|孪晶铜|晶向|晶粒尺寸|电镀铜",
    "dielectric_interface": "SiCN|SiO2|SiO₂|SiN|Al2O3|polyimide|silicon dioxide|silicon nitride|silicon oxide|polymer dielectric|氧化硅|氮化硅|聚酰亚胺",
    "alignment_contact": "misalignment|overlay error|bonding pressure|contact pressure|thermocompression|thermo-compression|alignment accuracy|bonding force|错位|套刻误差|键合压力|热压键合",
    "annealing": "annealing|annealed|anneal|post-bond heat treatment|退火|键合后热处理",
}
OUTCOMES = {
    "surface_state": "roughness|recess|dishing|contact angle|protrusion|wettability|oxidation|粗糙度|凹陷|润湿|接触角|凸起",
    "voids": "void|voids|delamination|unbonded|cracks|空洞|脱层|未键合|裂纹",
    "bond_strength": "shear strength|bonding strength|bond strength|bonding energy|bond energy|fracture energy|tensile strength|剪切强度|抗剪强度|键合能|键合强度|断裂能",
    "electrical_resistance": "contact resistance|electrical resistance|chain resistance|resistivity|electrical continuity|electrical open|接触电阻|链电阻|电阻率|电连续性|电开路",
    "electrical_reliability": "breakdown|TDDB|leakage current|electromigration|dielectric lifetime|击穿|漏电|电迁移",
    "thermomechanical_reliability": "thermal cycling|temperature cycling|thermal shock|humidity test|HAST|autoclave|温度循环|温循|热冲击|湿热",
    "yield": "yield|yields|pass rate|良率|合格率|通过率",
    "throughput": "throughput|wafers per hour|dies per hour|units per hour|cycle time|吞吐量|每小时晶圆|节拍",
}
REFERENCE_HEADING = re.compile(r"(?im)^[ \t■▪●]*(?:(?:[IVX]+|[0-9]+)[ .\t]+)?(?:references|bibliography|参考文献)[ \t.:：-]*$")
ENTRY = re.compile(r"(?m)^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\s*\.)\s*(?:[A-Z][\w'’-]+,|[A-Z]\.\s*[A-Z])")
CITES_PRIOR = re.compile(r"\b(?:previous(?:ly)? (?:reported|studies|work)|prior (?:work|studies)|et al\.)|已有研究|已有文献|相关工作主要", re.I)
WORD_GAP = r"[\s\-‐‑–]+"
MAX_GAP = 220


@lru_cache(maxsize=32)
def phrase_pattern(group):
    variants = []
    for phrase in sorted(group.split("|"), key=len, reverse=True):
        pattern = WORD_GAP.join(re.escape(word) for word in re.split(r"[ -]", phrase))
        if phrase.isascii():
            pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
        if phrase in ("yield", "yields"):
            pattern += r"(?![-\s]+(?:stress|strength|point|strain|criterion)\b)"
        variants.append(pattern)
    return re.compile("|".join(variants), re.I)


@lru_cache(maxsize=3)
def document_context(path, revision):
    """Cache only catalog identities, bibliography cutoffs and rare CJK blocks."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        signed = {d: json.loads(meta).get("sha256") for d, meta in db.execute("SELECT doc_id,metadata FROM papers")}
        starts = {}
        for doc, page, text in db.execute("SELECT doc_id,pdf_page,text FROM evidence WHERE text LIKE '%references%' OR text LIKE '%bibliography%' OR text LIKE '%参考文献%'"):
            if REFERENCE_HEADING.search(text):
                starts[doc] = min(page, starts.get(doc, page))
        cjk = [dict(zip(("evidence_id","doc_id","pdf_page","text"), row)) for row in db.execute(
            "SELECT evidence_id,doc_id,pdf_page,text FROM evidence WHERE text GLOB '*[一-龥]*'")]
    return signed, starts, cjk


def passage(text, factors, outcomes):
    if ENTRY.search(text) or REFERENCE_HEADING.search(text):
        return None
    words = re.sub(r"\s+", " ", text).strip()
    if len(words) < 40:
        return None
    matched = [(max(a.start(), b.start()) - min(a.end(), b.end()), a, b)
               for a in factors.finditer(words) for b in outcomes.finditer(words)
               if a.end() <= b.start() or b.end() <= a.start()]
    if not matched:
        return None
    gap, factor, outcome = min(matched, key=lambda match: match[0])
    if gap > MAX_GAP:
        return None
    start = max(0, min(factor.start(), outcome.start()) - 85)
    end = min(len(words), max(factor.end(), outcome.end()) + 100)
    quote = words[start:end]
    parts = []
    pos = start
    for mark, role in sorted(((factor, "工艺词"), (outcome, "结果词")), key=lambda pair: pair[0].start()):
        parts.extend([{"text": words[pos:mark.start()]}, {"text": mark.group(), "role": role}])
        pos = mark.end()
    parts.append({"text": words[pos:end]})
    return gap, {"quote": quote, "parts": parts, "before": bool(start), "after": end < len(words)}


def text_leads(pilot: Path, papers, claims, row, column, route=None, system=None, limit=6):
    path = (pilot / "indexes/pilot.sqlite").resolve()
    stat = path.stat()
    signed, starts, cjk = document_context(path, (stat.st_size, stat.st_mtime_ns))
    eligible = {p["doc_id"]: p for p in papers if p.get("sha256") and signed.get(p["doc_id"]) == p["sha256"]
                and (not route or route in p.get("classification", {}).get("routes", []))}
    if system:
        in_system = {c["doc_id"] for c in claims if c["doc_id"] in eligible and _system(c)[0] == system}
        eligible = {d: p for d, p in eligible.items() if d in in_system}
    result = {"row": row, "column": column, "route": route, "system": system, "eligible_documents": len(eligible),
              "results": [], "factor_terms": FACTORS[row].split("|"), "outcome_terms": OUTCOMES[column].split("|")}
    if not eligible:
        return result
    factors, outcomes = phrase_pattern(FACTORS[row]), phrase_pattern(OUTCOMES[column])
    query = " AND ".join("(" + _fts_expression(result[key]) + ")" for key in ("factor_terms", "outcome_terms"))
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        hits = _fts_rows(db, query, 400, tuple(eligible))
    view = load_effective_evidence(pilot, papers=papers, claims=claims)
    best = {}
    for hit in [*hits, *cjk]:
        doc, page = hit["doc_id"], hit["pdf_page"]
        if doc not in eligible or not 1 <= page <= (eligible[doc].get("page_count") or 0) or page >= starts.get(doc, 10**6):
            continue
        found = passage(hit["text"], factors, outcomes)
        if not found:
            continue
        gap, excerpt = found
        mentions_prior = bool(CITES_PRIOR.search(excerpt["quote"]))
        score = (mentions_prior, gap, hit.get("score", 0), page, hit["evidence_id"])
        if doc not in best or score < best[doc][0]:
            source = view.source({"doc_id": doc, "pdf_page": page, "evidence_id": hit["evidence_id"],
                                  "mentions_prior_work": mentions_prior, **excerpt})
            if FIELD in source and "parts" not in source:
                source.update(parts=[{"text": source["quote"]}], before=False, after=False)
            best[doc] = (score, source)
    result["results"] = [item for _, item in sorted(best.values(), key=lambda pair: pair[0])[:limit]]
    return result
