"""A candidate cannot enter the measured map through any one- or two-anchor path."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pymupdf
import pytest

from kg_pilot.map_relations import claims_digest
from kg_pilot.map_sample_bindings import SCHEMA, VERSION, apply_sample_bindings, base_files, candidate_index, sha
from kg_pilot.map_semantics import AXES_DIGEST, digest, pair
from kg_pilot.research_map import build_research_map

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import review_map_sample_bindings as review


FACTOR = {"pdf_page":1,"quote":"Group B received the nitrogen plasma before joining."}
END = {"pdf_page":2,"quote":"Electron microscopy showed three voids in the joined B coupons."}
JOIN = {"pdf_page":2,"quote":"The joined B coupons from the plasma-treated batch were cross-sectioned.","role":"sample"}
LINK = dict(doc_id="d",claim_id="c",row="cleaning_activation",column="voids",relation="condition",
            scope="清洗后的B组接头截面观察到三个空洞。",limit="观察范围仅限B组已取样的接头截面。",
            sample_group="B coupons",sample_link="相同B组样品从制备进入截面观察。",
            process_evidence=FACTOR,result_evidence=END,supporting_evidence=[JOIN],origin="sample_methods")


def make_fixture(root):
    f = root/"paper.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50,50),FACTOR["quote"])
        p = pdf.new_page()
        p.insert_text((50,50),END["quote"]); p.insert_text((50,80),JOIN["quote"])
        pdf.save(f)
    p = {"doc_id":"d","title":"Synthetic B coupons","sha256":sha(f.read_bytes()),"page_count":2,"local_path":str(f)}
    c = {"claim_id":"c","doc_id":"d","claim_type":"reported_experiment","review_status":"independent_agent_reviewed",
         "statement":"已接合结构的截面观察到三个空洞。","quantity":"void_count","evidence":[END],"conditions":{}}
    doc = dict(doc_id="d",source_sha256=p["sha256"],claims_digest=claims_digest([c]),links=[deepcopy(LINK)])
    candidate = dict(schema=1,comparison_snapshot="d"*64,documents=[doc])
    return p,c,candidate


def release(root,candidate):
    # Include the file's exact CRLF representation as generated on Windows.
    data = json.dumps(candidate,ensure_ascii=False,indent=2).replace("\n","\r\n")
    doc = candidate["documents"][0];link=doc["links"][0]
    notes = {k:{"pdf_page":ev["pdf_page"],"basis":"准确追溯该项证据。"}
             for k,ev in review.anchors(link).items() if ev}
    row = dict(doc_id="d",claim_id="c",row=link["row"],column=link["column"],input_digest=digest(link),
               candidate_sha256=sha(data.encode()),source_sha256=doc["source_sha256"],claims_digest=doc["claims_digest"],
               verdict="accept",reason="三项均有同组原论文证据。",anchor_checks=notes,
               review={"model":"test","packet_sha256":"a"*64,"reply_sha256":"b"*64,"candidate_id":0})
    result = dict(schema=SCHEMA,version=VERSION,axes_digest=AXES_DIGEST,base_files=base_files(root),
                  comparison_snapshot=candidate["comparison_snapshot"],candidate_sha256=row["candidate_sha256"],
                  candidate_utf8=data,decisions=[row])
    return result


def save(root,obj):
    obj["release_digest"] = digest({k:v for k,v in obj.items() if k!="release_digest"})
    f=root/"research_map/sample_bindings.json"
    f.parent.mkdir(parents=True,exist_ok=True); f.write_text(json.dumps(obj,ensure_ascii=False),"utf-8")
    return f


def test_complete_scientific_triplet_reaches_filtered_map(tmp_path):
    p,c,candidate=make_fixture(tmp_path)
    save(tmp_path,release(tmp_path,candidate))
    links,a=apply_sample_bindings(tmp_path,[p],[c],[],set())
    assert a["accepted"]==1
    m=build_research_map([p],[c],reviewed=links)
    evidence=next(x for x in m["cells"] if x["records"])["reviewed_records"]["c"]
    assert evidence["sample_group"]=="B coupons"
    assert evidence["supporting_evidence"]==[JOIN] and evidence["sample_review"]["anchor_checks"]["sample"]["pdf_page"]==2


def test_http_cache_revision_changes_on_physical_pdf_change(tmp_path):
    from web.research_cache import digest_files
    p,c,candidate = make_fixture(tmp_path)
    cat=tmp_path/"catalog/papers.json"; cat.parent.mkdir()
    cat.write_text(json.dumps([p]),"utf-8")
    save(tmp_path,release(tmp_path,candidate))
    inputs=("catalog/papers.json","research_map/sample_bindings.json")
    previous=digest_files(tmp_path,inputs)
    assert apply_sample_bindings(tmp_path,[p],[c],[],set())[1]["accepted"]==1
    Path(p["local_path"]).write_bytes(b"Wrong physical source replaced outside catalog.")
    assert digest_files(tmp_path,inputs)!=previous
    assert apply_sample_bindings(tmp_path,[p],[c],[],set())[1]["accepted"]==0


@pytest.mark.parametrize("fault",["sample","owner","pdf","claims","file","candidate_sha","baseline","verdict","incomplete",
                                  "ambiguity","holdout","existing","model","wrong_anchor","reply","physical","wrong_kind"])
def test_unsigned_unowned_or_blocked_evidence_never_adds_pair(tmp_path,fault):
    p,c,candidate=make_fixture(tmp_path)
    prior=[]; withheld=set()
    if fault=="sample":
        candidate["documents"][0]["links"][0]["supporting_evidence"]=[]
    if fault=="owner":
        candidate["documents"][0]["links"][0]["result_evidence"]=dict(END,pdf_page=1)
    if fault=="model":
        c["claim_type"]="simulation_result"
        candidate["documents"][0]["claims_digest"]=claims_digest([c])
    if fault=="ambiguity":
        candidate["documents"][0]["links"].append(deepcopy(LINK))
    r=release(tmp_path,candidate)
    if fault=="claims": c["statement"]="new paper assertion"
    if fault=="pdf": p["sha256"]="e"*64
    if fault=="physical": Path(p["local_path"]).write_bytes(b"Changed source document.")
    if fault=="candidate_sha": r["candidate_sha256"]="e"*64
    if fault=="baseline": r["comparison_snapshot"]="e"*64
    if fault=="verdict": r["decisions"][0]["verdict"]="uncertain"
    if fault=="incomplete": r["decisions"]=[]
    if fault=="wrong_anchor": r["decisions"][0]["anchor_checks"]["sample"]["pdf_page"]=1
    if fault=="reply": r["decisions"][0]["review"].pop("reply_sha256")
    if fault=="holdout": withheld.add(pair(LINK))
    if fault=="existing": prior.append(deepcopy(LINK))
    if fault=="wrong_kind": p["source_type"]="datasheet"
    f=save(tmp_path,r)
    if fault=="file": f.write_text(f.read_text("utf-8").replace("准确追溯","没有追溯"),"utf-8")
    links,a=apply_sample_bindings(tmp_path,[p],[c],prior,withheld)
    assert links==prior and a["accepted"]==0


def test_source_pages_and_model_reply_can_be_replayed_without_real_cli(tmp_path,monkeypatch):
    p,c,can=make_fixture(tmp_path)
    docs, entries=candidate_index(can); link=next(iter(entries.values()))
    original = json.dumps(can,ensure_ascii=False).encode()
    state=dict(raw=original,hash=sha(original),candidate=can,documents=docs,entries=entries,by_paper={"d":p},
               by_claim={"c":c},claims={"d":[c]},kinds={},locked=set(),base={},base_files=base_files(tmp_path),
               protocol_sha256="e"*64,cohort_documents=1,cohort_claims=1)
    monkeypatch.setattr(review,"WORK",tmp_path/"work")
    monkeypatch.setattr(review,"PILOT",tmp_path)
    frozen=tmp_path/"frozen";frozen.mkdir()
    monkeypatch.setattr(review,"FROZEN",frozen)
    def fake(directory,name,prompt,packet):
        assert "claims" in packet["sources"][0] and "previous" not in packet["items"][0]
        answer=dict(id=0,verdict="accept",issue="none",reason="三段原文明确同一B组实际接合结构。",
                    anchor_checks={k:dict(pdf_page=v["pdf_page"],basis="该原段清楚标出B组试样及该读数。")
                                   for k,v in review.anchors(link).items()})
        raw={"decisions":[answer]}
        review.reader.save(directory/(name+".json"),dict(key=review.reader.digest([review.reader.VERSION,review.reader.MODEL,prompt,packet]),output=raw))
        return raw
    monkeypatch.setattr(review.reader,"call_reader",fake)
    assert review.assess_job(state,[[link]],True)==({"accept":1},1)
    decision=review.reader.load(review.row_path(state,link))
    review.validate_replies(state,[decision])
    from web import server
    def project(*,sample_release_path):
        additions,audit=apply_sample_bindings(tmp_path,[p],[c],[],set(),release_path=sample_release_path)
        assert audit["accepted"]==1
        return build_research_map([p],[c],reviewed=additions)
    monkeypatch.setattr(server,"research_map",project)
    manifest=review.stage(state)
    assert manifest["base_pairs"]==0 and manifest["final_pairs"]==1 and manifest["verified_positive_anchors"]==3
    assert manifest["projection_sha256"]==sha((frozen/"sample_methods_projected_map.json").read_bytes())
    assert not (tmp_path/"research_map/sample_bindings.json").exists()
    assert review.stage(state)==manifest
    decision["anchor_checks"]["sample"]["basis"]="没有原文支撑的另一条理由。"
    with pytest.raises(ValueError): review.validate_replies(state,[decision])
    changed=deepcopy(link);changed["supporting_evidence"][0]["quote"]="A different trial with a different specimen."
    assert review.assess_job(state,[[changed]],False)==({"ineligible":1},0)
