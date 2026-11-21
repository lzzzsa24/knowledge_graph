"""A stale evidence file must never be allowed to create or suppress current map links."""
import hashlib
import json
from pathlib import Path

from kg_pilot.map_relations import claims_digest, load_mapping_reviews, source_readings
from kg_pilot.map_errata import apply_errata
from kg_pilot.map_semantics import AXES_DIGEST, digest, pair
from kg_pilot.research_map import build_research_map
from kg_pilot.research_notes import attach_screened_directions, corpus_digest, research_notes


def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")


def seeded():
    paper={"doc_id":"a","sha256":hashlib.sha256(b"edition1").hexdigest(),"page_count":2,"title":"Study"}
    claim={"doc_id":"a","claim_id":"c","statement":"CMP 后接触电阻为 2 mΩ。",
           "quantity":"contact_resistance","material_system":"Cu/SiO2",
           "claim_type":"reported_experiment","review_status":"independent_agent_reviewed",
           "evidence":[{"pdf_page":2,"quote":"Measured 2 mOhm."}]}
    item={"doc_id":"a","claim_id":"c","row":"surface_planarization","column":"electrical_resistance",
          "relation":"contrast","scope":"Compared matched sites.","limit":"Only this sample.",
          "process_evidence":{"pdf_page":1,"quote":"CMP test condition."},"result_evidence":claim["evidence"][0]}
    binding={"doc_id":"a","source_sha256":paper["sha256"],"claims_digest":claims_digest([claim])}
    return paper,claim,item,binding


def store(tmp,name,doc):
    write(tmp/"research_map"/(name+".json"),{"schema":1,"documents":[doc]})


def test_correction_of_a_previous_decision_binds_both_coordinates(tmp_path):
    paper, claim, old, document = seeded()
    new = {**old, "row":"cleaning_activation"}
    edit = {**old, "action":"replace_cell", "replacement":new, "old_digest":digest(old),
            "target_previously_held":True, "reason":"Replayed actual source", "evidence":claim["evidence"]}
    path = tmp_path / "research_map/source_errata.json"
    store(tmp_path, "source_errata", {**document, "axes_digest":AXES_DIGEST, "edits":[edit]})
    edges, held, changes = apply_errata(path, [paper], [claim], [old], {pair(new)})
    assert edges == [new] and held == {pair(old)} and changes == {"moved":1}
    for current_claim, edges, blocked in (
            (claim, [old], set()), (claim, [old, new], {pair(new)}),
            ({**claim,"unit":"Ω"}, [old], {pair(new)}), (claim, [{**old,"limit":"New reading"}], {pair(new)})):
        assert apply_errata(path, [paper], [current_claim], edges, blocked) == (edges,blocked,{})


def test_passage_note_is_separate_from_counted_relations_and_expires_with_claims(tmp_path):
    p,c,_,binding=seeded()
    note={**binding,"status":"verified","kind":"measurement_method","finding":"Optical calibration.",
          "scope":"Calibration wafer.","review_reason":"Measured on the stated wafer.","evidence":c["evidence"]}
    store(tmp_path,"unmapped_source_readings",note)
    assert source_readings(tmp_path,[p],[c])["a"]["finding"]=="Optical calibration."
    assert not load_mapping_reviews(tmp_path,[p],[c])[0]
    assert not source_readings(tmp_path,[p],[{**c,"statement":"Replaced source claim"}])
    store(tmp_path,"unmapped_source_readings",{**note,"status":"independent_check_withheld"})
    assert not source_readings(tmp_path,[p],[c])


def test_contradictory_readings_are_withheld_and_changed_claim_invalidates_both(tmp_path):
    p,c,link,binding=seeded()
    store(tmp_path,"reviewed_relations",{**binding,"links":[link]})
    row={k:link[k] for k in ("doc_id","claim_id","row","column")}
    row.update(status="unsupported",reason="The measurement is on a different specimen.",evidence=c["evidence"][0])
    store(tmp_path,"adjudicated_relations",{**binding,"decisions":[row]})
    links,held,audit=load_mapping_reviews(tmp_path,[p],[c])
    assert not links and (link["claim_id"],link["row"],link["column"]) in held and audit["conflicting_pairs"]==1
    data=build_research_map([p],[c],reviewed=links,withheld=held)
    assert not data["totals"]["mapped_claims"] and not data["record_details"]
    links,held,_=load_mapping_reviews(tmp_path,[p],[{**c,"statement":"different"}])
    assert not links and not held
    links,held,_=load_mapping_reviews(tmp_path,[{**p,"sha256":"other-edition"}],[c])
    assert not links and not held


def test_inconclusive_rule_review_cannot_overturn_explicit_original_sources(tmp_path):
    p,c,link,binding=seeded()
    store(tmp_path,"reviewed_relations",{**binding,"links":[link]})
    row={k:link[k] for k in ("doc_id","claim_id","row","column")}
    store(tmp_path,"adjudicated_relations",{**binding,"decisions":[dict(row,status="uncertain",reason="Unresolved")]})
    links,held,_=load_mapping_reviews(tmp_path,[p],[c])
    assert links==[link] and not held
    data=build_research_map([p],[c],reviewed=links,withheld=held)
    cell=next(x for x in data["cells"] if x["row"]==link["row"] and x["column"]==link["column"])
    assert cell["contrasts"]==cell["experiments"]==1
    c["claim_type"]="reported_simulation"
    sim=build_research_map([p],[c],reviewed=[link])
    cell=next(x for x in sim["cells"] if x["records"])
    assert cell["simulations"] and not cell["experiments"] and not cell["contrasts"]


def test_notes_bind_secondary_pages_and_whole_search_corpus(tmp_path):
    p,c,link,_=seeded()
    anchor={"doc_id":"a","claim_id":"c","pdf_sha256":p["sha256"],**c["evidence"][0]}
    case_row={**anchor,"source_sha256":p["sha256"],"additional_evidence":[{"pdf_page":1,"quote":"same study"}]}
    case={"id":"case","title":"case","conclusion":"boundary","rows":[case_row],"source_fingerprints":{"a":p["sha256"]}}
    studies=tmp_path/"research_map/study_comparisons.json"
    write(studies,{"cases":[case]})
    item={"id":"idea","status":"potential","row":link["row"],"column":link["column"],"title":"Test",
          "question":"Test under matched conditions?","gap":"uncertain","evidence":[anchor],"route_scope":["W2W"]}
    write(tmp_path/"research_map/opportunity_reviews.json",{"reviewed_on":"2026-11-07",
          "corpus":{"source_digest":corpus_digest([p])},"opportunities":[item]})
    notes=research_notes(tmp_path,[p],[c])
    assert len(notes["cases"])==len(notes["opportunities"])==1
    data=attach_screened_directions(build_research_map([p],[c]),notes,"D2W")
    assert not data["directions"] and any("case" in cell["study_ids"] for cell in data["cells"])
    changed=research_notes(tmp_path,[p,{**p,"doc_id":"new"}],[c])
    assert changed["review_status"]=="requires_refresh" and not changed["opportunities"] and changed["cases"]
    case_row["additional_evidence"][0]["pdf_page"]=99
    write(studies,{"cases":[case]})
    assert not research_notes(tmp_path,[p],[c])["cases"]


def test_new_material_direction_requires_matching_dossier_corpus_verdict_and_original_pdf(tmp_path):
    paper, claim, link, _ = seeded()
    folder = tmp_path / "research_map"
    proposal = folder / "material_opportunities.json"
    ledger = folder / "material_release_review.json"
    dataset = corpus_digest([paper])
    write(proposal, {"proposals":[{"id":"materials-test"}], "corpus":{"digest":dataset}})
    card = {**dict.fromkeys(("title","prior_work","question","gap","measurement","success_criterion","counterexample","feasibility"), "Reviewed"),
            "id":"materials-test", "row":link["row"], "column":link["column"], "controls":["Blank witness"],
            "status":"potential", "evidence":[{"doc_id":"a","pdf_page":2,"quote":"Measured.","pdf_sha256":paper["sha256"]}],
            "external_sources":[{"title":"Prior art","url":"https://example.org/test","finding":"Published adjacent experiment."}]}
    decision = {"id":card["id"], "card":card, "decision":"release_refined","conclusion":"Release with a narrower endpoint."}
    signed = {"schema":1, "source_review_sha256":hashlib.sha256(proposal.read_bytes()).hexdigest(), "corpus_digest":dataset,"reviews":[decision]}
    def visible():
        write(ledger, signed)
        return research_notes(tmp_path, [paper], [claim])["opportunities"]
    assert visible()[0]["public_review"]["protocol_integrated"]
    decision["decision"] = "withhold"
    assert not visible()
    decision["decision"] = "release_refined"
    card["evidence"][0]["pdf_sha256"] = "changed"
    assert not visible()
    card["evidence"][0]["pdf_sha256"] = paper["sha256"]
    assert not research_notes(tmp_path, [paper, {**paper, "doc_id":"new"}], [claim])["opportunities"]
    write(proposal, {"proposals":[{"id":"materials-test"}], "corpus":{"digest":"stale"}})
    assert not visible()


def test_reported_overlap_is_limited_to_two_source_editions_and_matching_claim_content(tmp_path):
    p,c,_,_ = seeded()
    q = {**p, "doc_id":"b", "sha256":"b"*64}
    result = {**c,"doc_id":"b","claim_id":"later"}
    def member(paper, record):
        return {"doc_id":paper["doc_id"],"pdf_sha256":paper["sha256"],"evidence":record["evidence"],
                "claim_ids":[record["claim_id"]],"claim_digests":{record["claim_id"]:digest(record)}}
    one = {"id":"paired","kind":"repeated_data","title":"Same results table","finding":"Same five rows.",
           "scope_limit":"Other test vehicles differ.","members":[member(p,c),member(q,result)]}
    write(tmp_path/"research_map/study_connections.json",{"schema":1,"connections":[one]})
    def visible(papers, claims):
        return research_notes(tmp_path,papers,claims)["study_connections"]
    assert [m["claim_ids"] for m in visible([p,q],[c,result])[0]["members"]] == [["c"],["later"]]
    assert visible([p,q],[c,{**result,"unit":"mΩ"}])[0]["members"][1]["claim_ids"] == []
    assert not visible([p,{**q,"sha256":"c"*64}],[c,result])


def test_withheld_design_note_requires_case_anchor_and_matching_source_pack(tmp_path):
    paper, claim, _, _ = seeded()
    root = tmp_path / "research_map"
    anchor = {"doc_id": "a", "pdf_page": 2, "quote": "Measured 2 mOhm.", "pdf_sha256": paper["sha256"]}
    write(root / "study_comparisons.json", {"cases": [{"id": "case", "rows": [anchor],
          "source_fingerprints": {"a": paper["sha256"]}}]})
    pack = root / "stack_opportunities.json"
    write(pack, {"opportunities": [{"id": "not-published"}]})
    review = {"decision": "withhold", "study_id": "case", "study_note": "First measure the full chain.",
              "study_anchor": {"doc_id": "a", "pdf_page": 2}, "local_evidence": [anchor]}
    signed = {"reviews": [review], "source_review_sha256": hashlib.sha256(pack.read_bytes()).hexdigest()}
    write(root / "stack_release_review.json", signed)
    notes = research_notes(tmp_path, [paper], [claim])
    assert notes["cases"][0]["design_review"]["source"] == anchor
    assert not notes["opportunities"]
    write(pack, {"opportunities": []})
    assert "design_review" not in research_notes(tmp_path, [paper], [claim])["cases"][0]
    signed["source_review_sha256"] = hashlib.sha256(pack.read_bytes()).hexdigest()
    review["local_evidence"] = [{**anchor, "pdf_sha256": "different-edition"}]
    write(root / "stack_release_review.json", signed)
    assert "design_review" not in research_notes(tmp_path, [paper], [claim])["cases"][0]


def test_cache_revision_changes_even_with_same_file_size_and_timestamp(tmp_path):
    import os
    from web.research_cache import digest_files
    src=tmp_path/"claims"
    src.write_bytes(b"first")
    st=src.stat()
    observed=digest_files(tmp_path,["claims","optional"])
    src.write_bytes(b"other")
    os.utime(src,ns=(st.st_atime_ns,st.st_mtime_ns))
    assert digest_files(tmp_path,["claims","optional"])!=observed


def test_old_opportunity_screen_cannot_rewrite_an_edited_idea(tmp_path):
    from kg_pilot.research_notes import apply_public_reviews
    root=tmp_path/"research_map";root.mkdir()
    original=root/"opportunity_reviews.json";original.write_text("{}",encoding="utf8")
    idea={"id":"x","title":"initial","external_sources":[]}
    check={"id":"x","verdict":"refine","revised_question":"q","revised_title":"refined","revised_gap":"g",
           "revised_prior_work":"existing disclosure","external_sources":[{"title":"prior","finding":"stated","url":"https://example.org/a"}]}
    write(root/"external_prior_art.json",{"source_review_sha256":hashlib.sha256(original.read_bytes()).hexdigest(),
                                        "reviews":[check],"review_date":"2026-11-07"})
    assert apply_public_reviews(root,[idea],[])[0][0]["title"]=="refined"
    protocol={"id":"x","controls":["Train across both process states."],"measurement":"Keep the held-out lot locked."}
    write(root/"opportunity_protocols.json",{"schema":1,"revisions":[protocol],"prior_art_sha256":"other"})
    assert "controls" not in apply_public_reviews(root,[idea],[])[0][0]
    write(root/"opportunity_protocols.json",{"schema":1,"revisions":[protocol],
          "prior_art_sha256":hashlib.sha256((root/"external_prior_art.json").read_bytes()).hexdigest()})
    integrated=apply_public_reviews(root,[idea],[])[0][0]
    assert integrated["measurement"]==protocol["measurement"] and integrated["public_review"]["protocol_integrated"]
    original.write_text('{"changed":true}',encoding="utf8")
    assert apply_public_reviews(root,[idea],[])[0]==[idea]


def test_material_and_contrast_layers_keep_selected_doc_and_endpoint_separate():
    p,c,link,_=seeded()
    other={**p,"doc_id":"b","sha256":"b"*64}
    d={**c,"claim_id":"d","doc_id":"b","material_system":"pure Cu-Cu","statement":"纯Cu-Cu的CMP后接触电阻为2mΩ。"}
    source={**link,"claim_id":"d","doc_id":"b","relation":"condition"}
    out=build_research_map([p,other],[c,d],reviewed=[link,source],system="cu_only")
    cs=[x for x in out["cells"] if x["records"]]
    assert out["totals"]["documents"]==2
    assert all(x["experiments"]==1 and x["contrasts"]==0 for x in cs)
    assert out["totals"]["mapped_documents"]==1 and set(out["record_details"])=={"d"}
    assert {"cu_only","cu_oxide"}=={s["id"] for s in out["system_options"]}

