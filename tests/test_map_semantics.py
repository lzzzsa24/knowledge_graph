"""Current-snapshot overlays cannot turn quotes from a different claim into evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pymupdf
import pytest

from kg_pilot.map_relations import claims_digest
from kg_pilot.map_semantics import SCHEMA, VERSION, apply_semantic_reviews, digest, mapped_candidates, pair
from kg_pilot.research_map import build_research_map

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import review_map_semantics as sem


FACTOR = {"pdf_page": 1, "quote": "Contact pressure and heat were applied together for ten minutes."}
END = {"pdf_page": 2, "quote": "Four bonded joints had a tensile strength of ten MPa."}
ROW = "annealing"
TARGET = "alignment_contact"
COL = "bond_strength"


def fixture():
    paper = {"doc_id":"d", "title":"Synthetic pressure hold", "sha256":"a"*64, "page_count":2}
    claim = {"doc_id":"d", "claim_id":"c", "statement":"退火后测得接头拉伸强度。", "claim_type":"reported_experiment",
             "quantity":"tensile strength", "review_status":"independent_agent_reviewed", "conditions":{},
             "evidence":[dict(END, evidence_id="endpoint")]}
    old = dict(doc_id="d", claim_id="c", row=ROW, column=COL, relation="contrast", scope="接头", limit="原有比较",
               process_evidence=FACTOR, result_evidence=END)
    item = mapped_candidates([paper], [claim], [old], set())["d"][0]
    return paper, claim, old, item


def proposal(old, item, target=TARGET):
    link = dict(old, row=target, relation="condition", scope="同步加热加压的接头。", limit="固定制程，未做分离变量对照。")
    return dict(item, status="replaced", reason="带压加热属于同一步工艺。",
                assessment={"model":"A", "packet_sha256":"a"*64, "candidate_id":0, "reason":"核对操作次序。"},
                confirmation={"model":"B", "packet_sha256":"b"*64, "candidate_id":0, "link_digest":digest(link),
                              "reason":"独立复核同意。", "accepted":True},
                link=link, evidence={"factor":FACTOR, "endpoint":END})


def write(root, paper, claim, rows):
    f = root / "research_map/semantic_reviews.json"
    f.parent.mkdir(parents=True,exist_ok=True)
    f.write_text(json.dumps(dict(schema=SCHEMA, version=VERSION, documents=[dict(doc_id="d",
                 source_sha256=paper["sha256"], claims_digest=claims_digest([claim]), decisions=rows)])), "utf-8")


def test_wrong_stage_relocated_without_resurrecting_lexical_edge(tmp_path):
    p,c,old,item = fixture()
    write(tmp_path,p,c,[proposal(old,item)])
    links, hidden, stats = apply_semantic_reviews(tmp_path,[p],[c],[old],set())
    assert hidden == {("c",ROW,COL)}
    assert [(l["row"],l["relation"]) for l in links] == [(TARGET,"condition")]
    result = build_research_map([p],[c],reviewed=links,withheld=hidden)
    assert [(r["row"],r["contrasts"]) for r in result["cells"] if r["claims"]] == [(TARGET,0)]
    assert stats["replacement_links_applied"] == 1
    assert old["relation"] == "contrast"


def test_same_pair_can_be_downgraded_and_existing_blocked_target_stays_blocked(tmp_path):
    p,c,old,item = fixture()
    write(tmp_path,p,c,[proposal(old,item,ROW)])
    links, hidden, _ = apply_semantic_reviews(tmp_path,[p],[c],[old],set())
    assert not hidden and links[0]["relation"] == "condition"
    write(tmp_path,p,c,[proposal(old,item)])
    links, hidden, stats = apply_semantic_reviews(tmp_path,[p],[c],[old],{("c",TARGET,COL)})
    assert not links and len(hidden) == 2 and stats["target_already_mapped_or_withheld"] == 1


@pytest.mark.parametrize("fault", ["paper", "claims", "old_edge", "ownership", "refused", "same_packet",
                                   "duplicate", "wrong_page", "mismatched_evidence", "no_stage"])
def test_malformed_or_outdated_decision_leaves_upstream_state(tmp_path, fault):
    p,c,old,item = fixture()
    row = deepcopy(proposal(old,item))
    if fault == "ownership":
        row["link"]["claim_id"] = "another_claim"
    if fault == "refused":
        row["confirmation"]["accepted"] = False
    if fault == "same_packet":
        row["confirmation"]["packet_sha256"] = row["assessment"]["packet_sha256"]
    if fault == "wrong_page":
        row["link"]["result_evidence"] = row["evidence"]["endpoint"] = dict(END,pdf_page=1)
    if fault == "mismatched_evidence":
        row["evidence"]["endpoint"] = dict(END,quote="An unrelated physical endpoint.")
    if fault == "no_stage":
        row["link"]["row"] = "thermal"
    write(tmp_path,p,c,[row,row] if fault=="duplicate" else [row])
    if fault == "paper":
        p["sha256"] = "b"*64
    if fault == "claims":
        c["evidence"] = [dict(END,quote="Newly corrected source evidence.")]
    if fault == "old_edge":
        old["limit"] = "新上游解释"
    links, hidden, stats = apply_semantic_reviews(tmp_path,[p],[c],[old],set())
    assert links == [old] and not hidden and stats["current_pairs_reviewed"] == 0


def test_withholding_rule_only_pair_and_no_empty_experiment_count(tmp_path):
    p,c,_,_ = fixture()
    item = mapped_candidates([p],[c],[],set())["d"][0]
    assert item["previous"] is None
    row = dict(item, status="withheld", reason="等待属于测量步骤。", evidence={"factor":FACTOR,"endpoint":END},
               assessment={"model":"A","packet_sha256":"c"*64,"candidate_id":0,"reason":"追查时间定义。"})
    write(tmp_path,p,c,[row])
    links, hidden, stats = apply_semantic_reviews(tmp_path,[p],[c],[],set())
    assert not links and hidden == {pair(item)} and stats["withheld"] == 1
    assert not build_research_map([p],[c],reviewed=links,withheld=hidden)["totals"]["mapped_documents"]


def test_uncertainty_and_conflicting_replacements_never_create_edge():
    p,c,old,item = fixture()
    a = proposal(old,item)
    b = deepcopy(a)
    b["row"] = "copper_microstructure"
    b["link"]["scope"] = "另一组试样，归属尚有争议。"
    from kg_pilot.map_semantics import _enact
    links, hidden, stats = _enact({pair(x) for x in (a,b)},[a,b],[old],set())
    assert not links and len(hidden) == 2 and stats["conflicting_replacements"] == 2


def test_native_evidence_owner_and_witness_or_selection_boundaries():
    p,c,old,item = fixture()
    norm = {1: (sem.reader.norm(FACTOR["quote"]),),2:(sem.reader.norm(END["quote"]),)}
    options = {"f":FACTOR,"e":END}
    audit={"model":"test","packet_sha256":"a"*64,"candidate_id":0,"reason":"read"}
    base={"action":"exclude","reason":"筛选步骤不提供相应测量结果。","issue":"endpoint","factor":"f","endpoint":"e"}
    assert sem.assess_one(item,base,c,2,norm,options,audit)["status"] == "withheld"
    for override in ({"endpoint":"f"}, {"factor":{"pdf_page":1,"quote":"A fabricated statement appears on this page."}},
                     {"action":"unsure", "reason":"No test structure given."}):
        row = sem.assess_one(item,dict(base,**override),c,2,norm,options,audit)
        assert row["status"] == "uncertain" and "link" not in row


def test_two_isolated_assessments_physical_pdf_and_checkpoint(tmp_path, monkeypatch):
    p,c,old,item = fixture()
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as source:
        for evidence in (FACTOR, END):
            source.new_page().insert_text((48, 48),evidence["quote"],fontsize=10)
        source.save(path)
    import hashlib
    p.update(local_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    doc = dict(doc_id="d",source_sha256=p["sha256"],claims_digest=claims_digest([c]),items=[item])
    monkeypatch.setattr(sem,"WORK",tmp_path / "work")
    monkeypatch.setattr(sem,"PILOT",tmp_path / "pilot")
    monkeypatch.setattr(sem,"REPORT",tmp_path / "reports")
    monkeypatch.setattr(sem,"OVERLAY",sem.PILOT / "research_map/semantic_reviews.json")
    monkeypatch.setattr(sem,"inventory",lambda:([p],[c],[old],set(),{"d":[c]},[doc]))
    received = []
    def fake(directory, name, prompt, packet):
        received.append(name)
        if name == "assess":
            output = {"decisions":[dict(id=0,action="repair",reason="带载热压后测接头。",issue="process_stage",
                                        factor=FACTOR,endpoint=END,row=TARGET,column=COL,relation="condition",
                                        scope="同批热压接头。",limit="未单独改变温度。")]}
        else:
            assert "previous" not in packet["items"][0] and "reason" not in packet["items"][0]
            output = {"decisions":[dict(id=0,accepted=True,reason="物理页支持工艺与强度的同批归属。")]}
        sem.reader.save(directory/(name+".json"),dict(key=sem.reader.digest([sem.reader.VERSION,sem.reader.MODEL,prompt,packet]),output=output))
        return output
    monkeypatch.setattr(sem.reader,"call_reader",fake)
    assert sem.run_job([(doc,[item])], {"d":p},{"d":[c]}) == {"replaced":1}
    assert received == ["assess","independent"]
    assert not list(sem.make_jobs([doc],10,3))
    cached = json.loads(sem.result_path(item,doc).read_text("utf-8"))
    assert cached["key"] == sem.cache_key(item,doc) and cached["decision"]["link"]["row"] == TARGET
    assert len(list((sem.WORK/"calls").rglob("*-input.json"))) == 2
    sem.publish(dict(documents=[doc],fingerprint="f"*64))
    verified = sem.verify_release()
    assert not verified["errors"] and verified["counts"]["confirmation_packets_matched"] == 1
    assert verified["new_pairs"] == 1 and verified["pending_pairs"] == 0
    assert (sem.REPORT/"audit.md").is_file()
    confirmation = next((sem.WORK/"calls").rglob("independent.json"))
    corrupt = sem.reader.load(confirmation)
    corrupt["output"]["decisions"][0]["accepted"] = False
    sem.reader.save(confirmation,corrupt)
    assert sem.verify_release()["errors"][0]["issue"] == "unmatched_confirmation_packet_or_reply"
    with pytest.raises(ValueError, match="哈希"):
        sem.originals(str(path),"bad")
