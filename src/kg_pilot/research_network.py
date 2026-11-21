"""A paper-level projection for the spatial map, with inspectable reasons for every line."""
from collections import Counter, defaultdict
from itertools import combinations


_CONNECTION_BASIS_NOTES = {
    "repeated_data": "对照两篇原文的结果表，核对重复报告的数据。",
    "reused_figure": "对照两份原文的图像，核对再次使用的代表图。",
    "documented_extension": "由后续论文对前作的回引和新增试验说明确认研究延伸。",
}


def source_role(paper):
    cls = paper.get("classification") or {}
    nature, form = cls.get("content_nature"), cls.get("document_form")
    if form in {"patent","standard","test_method"}:
        return "guidance"
    if paper.get("is_review") or nature in {"review","tutorial","commentary"}:
        return "review"
    if nature == "original_research":
        return "original"
    return "guidance" if nature in {"technical_guidance","product_specification","normative_document","industry_overview"} else "unknown"


def build_network(papers, categories, matrix, citations, notes):
    catalog = {p["doc_id"]:p for p in papers}
    clusters = {c["id"]:{**{k:c[k] for k in ("id","label","description") if k in c},"doc_ids":[]} for c in categories}
    documents, links = {}, []
    def anchor(doc_id, source, cid=None):
        p = catalog.get(doc_id)
        if not p or not isinstance(source,dict) or type(source.get("pdf_page")) is not int:
            return None
        page, quote = source["pdf_page"], source.get("quote")
        if not 1 <= page <= (p.get("page_count") or 0) or not isinstance(quote,str) or not quote.strip():
            return None
        value = {"doc_id":doc_id,"pdf_page":page,"quote":quote}
        if cid:
            value["claim_id"] = cid
        return value

    for p in papers:
        doc_id, cls = p["doc_id"], p.get("classification") or {}
        group = cls.get("primary_category") or "unclassified"
        clusters.setdefault(group,{"id":group,"label":"待分类","doc_ids":[]})["doc_ids"].append(doc_id)
        documents[doc_id] = {"doc_id":doc_id,"title":p.get("title") or doc_id,"year":p.get("year"),
            "authors":p.get("authors",[]),"primary_cluster":group,"topics":cls.get("topics",[]),
            "related_stages":cls.get("related_stages",[]),"role":source_role(p),"has_measured":False,
            "primary_evidence":next((a for e in cls.get("evidence",[]) if (a:=anchor(doc_id,e))),None),"cells":[]}

    for rel in citations.get("edges",[]):
        a, b = rel["citing"], rel["cited"]
        proof = anchor(a,rel)
        if proof and b in documents:
            links.append({"id":f"citation-{a}-{b}","source":a,"target":b,"kind":"citation","label":"参考文献引用",
                          "basis":"source_text","basis_note":"原文书目列出被引文献。",
                          "scope":"施引论文的书目列出了另一篇文献。","evidence":[proof]})
    for record in notes.get("study_connections",[]):
        first, second = record["members"]
        proofs = [anchor(m["doc_id"],e) for m in (first,second) for e in m["evidence"]]
        if all(proofs):
            links.append({"id":record["id"], "source":first["doc_id"], "target":second["doc_id"],"kind":record["kind"],
                          "basis":"source_text","basis_note":_CONNECTION_BASIS_NOTES[record["kind"]],
                          "label":record["title"],"scope":record["finding"]+" "+record["scope_limit"],"evidence":proofs})
    for case in notes.get("cases",[]):
        members = {}
        for row in case["rows"]:
            a = anchor(row["doc_id"],row,row.get("claim_id"))
            if a:
                members.setdefault(row["doc_id"],a)
        for a,b in combinations(members,2):
            links.append({"id":f'study-{case["id"]}-{a}-{b}',"source":a,"target":b,"kind":"study_comparison",
                          "basis":"agent_review","basis_note":"Agent 复核原页后编入同一专题。",
                          "label":case["title"],"scope":case["comparison_limit"],"study_id":case["id"],"evidence":[members[a],members[b]]})

    rows = {r["id"]:r["label"] for r in matrix["rows"]}
    columns = {r["id"]:r["label"] for r in matrix["columns"]}
    groups = defaultdict(dict)
    for cell in matrix["cells"]:
        row, column = cell["row"],cell["column"]
        local = {}
        for cid in cell["records"]:
            d = matrix["record_details"][cid]
            doc = documents.get(d["doc_id"])
            if doc is None:
                continue
            rel = cell.get("reviewed_records",{}).get(cid) or {}
            measured = d["source_kind"] == "experiment" and rel.get("relation") not in {"method","interpretation"}
            doc["has_measured"] |= measured
            item = local.setdefault(d["doc_id"],{"row":row,"column":column,"row_label":rows[row],"column_label":columns[column],
                                                "records":0,"experiments":0,"contrasts":0,"claim_id":cid,"pdf_page":None})
            item["records"] += 1
            item["experiments"] += int(measured)
            item["contrasts"] += int(measured and rel.get("relation") == "contrast")
            evidence = anchor(d["doc_id"],rel.get("result_evidence"),cid)
            if evidence and item["pdf_page"] is None:
                item.update(claim_id=cid,pdf_page=evidence["pdf_page"])
            if (evidence and measured and rel.get("relation") in {"contrast","condition"} and
                    d.get("system") not in {None,"","mixed_or_uncertain","unspecified","solder_hybrid"}):
                groups[(row,column,d["system"])].setdefault(d["doc_id"],evidence)
        for doc_id, item in local.items():
            documents[doc_id]["cells"].append(item)

    # Represent each paper in a few specific same-process comparisons; a
    # complete clique would obscure their original measurements on screen.
    degree, connected = Counter(), set()
    for (row,column,system), members in sorted(groups.items(),key=lambda g:(len(g[1]),g[0])):
        for a,b in combinations(sorted(members),2):
            if degree[a] >= 3 or degree[b] >= 3 or (a,b) in connected:
                continue
            connected.add((a,b)); degree.update([a,b])
            links.append({"id":f"shared-{a}-{b}","source":a,"target":b,"kind":"shared_process","row":row,"column":column,"system":system,
                          "basis":"rule","basis_note":"按材料体系和矩阵格配对实测。",
                          "label":rows[row]+" × "+columns[column],"scope":"两篇在这一材料体系和交叉下均有实测原页。比较前还需核对指标、样品和分母。",
                          "evidence":[members[a],members[b]]})
    for record in documents.values():
        record["cells"].sort(key=lambda c:(-c["contrasts"],-c["experiments"],-c["records"],c["row"],c["column"]))
    opportunities = [{k:c[k] for k in ("id","title","question","row","column","system","status","gap","evidence") if k in c}
                     for c in notes["opportunities"] if c["status"] == "potential"]
    for cluster in clusters.values():
        ids = set(cluster["doc_ids"])
        members = [documents[ident] for ident in ids]
        cluster["coverage"] = {"documents":len(members),"mapped_documents":sum(bool(p["cells"]) for p in members),
            "measured_documents":sum(p["has_measured"] for p in members),
            "contrast_documents":sum(any(c["contrasts"] for c in p["cells"]) for p in members)}
        coverage = {}
        for member in members:
            for c in member["cells"]:
                key = c["row"],c["column"]
                item = coverage.setdefault(key, {**{k:c[k] for k in ("row","column","row_label","column_label")},
                    "documents":0,"measured_documents":0,"contrast_documents":0})
                item["documents"] += 1
                item["measured_documents"] += int(bool(c["experiments"]))
                item["contrast_documents"] += int(bool(c["contrasts"]))
        thin = [c for c in coverage.values() if c["measured_documents"] >= 2 and c["contrast_documents"] <= 1]
        cluster["comparison_gap"] = min(thin,key=lambda c:(-c["measured_documents"],c["contrast_documents"],c["row"],c["column"]),default=None)
        cluster["opportunity_ids"] = [o["id"] for o in opportunities if any(e["doc_id"] in ids for e in o["evidence"])]
    return {"papers":list(documents.values()),"clusters":[c for c in clusters.values() if c["doc_ids"]],
            "links":links,"opportunities":opportunities,"totals":{"papers":len(documents),"links":len(links),"by_kind":dict(Counter(e["kind"] for e in links))},
            "method":"位置按文献的主要工艺分类排列。同工艺实测每篇最多展示三条，全部记录可回到矩阵。空间距离和空白不能判定研究质量或全球空白。"}
