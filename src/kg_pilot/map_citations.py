"""Serve bibliographic links only while both document identities match."""
import hashlib
import json
from pathlib import Path


def identity_hash(paper: dict):
    fields=("doc_id","sha256","title","year","authors","doi","page_count")
    raw=json.dumps([paper.get(k) for k in fields],ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _valid_anchor(item, total):
    if not isinstance(item, dict) or not item.get("quote"):
        return False
    page = item.get("pdf_page")
    first = item.get("reference_start_page", page)
    last = item.get("reference_end_page", page)
    end = item.get("anchor_end_page", page)
    return (all(type(n) is int for n in (page, first, last, end))
            and 1 <= first <= page <= end <= last <= total)


def citations(pilot: Path,papers: list[dict]):
    path=pilot/"research_map/citation_edges.json"
    snapshot=json.loads(path.read_text("utf-8-sig")) if path.is_file() else {}
    rows={p["doc_id"]:p for p in papers}
    year_path=pilot/"research_map/citation_year_evidence.json"
    years=json.loads(year_path.read_text("utf-8-sig")).get("records",[]) if year_path.is_file() else []
    current={d["doc_id"]:d for d in snapshot.get("documents",[]) if snapshot.get("schema")==1 and
             d.get("doc_id") in rows and d.get("signature")==identity_hash(rows[d["doc_id"]])}
    edges=[]
    seen=set()
    for e in snapshot.get("edges",[]):
        a,b=e.get("citing"),e.get("cited")
        if (a not in current or b not in current or a==b or (a,b) in seen or not _valid_anchor(e,rows[a]["page_count"])
            or e.get("match") not in {"doi","full_title"}):
            continue
        if e.get("anchor_basis") == "corroborated_variant" and not _valid_anchor(e.get("exact_title_evidence"), rows[a]["page_count"]):
            continue
        yr = e.get("matched_year_source")
        if yr:
            if not isinstance(yr,dict) or yr.get("target_signature") != current[b]["signature"] or yr.get("year") != e.get("matched_year"):
                continue
            if yr.get("kind") == "publisher_electronic_release":
                if yr not in years or yr.get("doc_id") != b or not str(yr.get("source_url", "")).startswith("https://"):
                    continue
            elif (yr.get("kind") != "meeting_printed_on_first_page" or type(yr.get("pdf_page")) is not int
                    or not 1 <= yr["pdf_page"] <= rows[b]["page_count"]):
                continue
        seen.add((a,b));edges.append(e)
    return {"edges":edges,"documents":len(rows),"scanned_documents":len(current),
            "reference_sections":sum(bool(d.get("references_heading")) for d in current.values()),
            "linked_documents":len({p for edge in seen for p in edge}),
            "method":"引文来自施引 PDF 的参考文献区，逐条匹配唯一 DOI 或完整题名与作者、年份。遇到跨页、同题异拼或会议年与出版年不一致，可逐页查看匹配依据。范围限于当前库内可定位的来源。"}

