import asyncio
import hashlib
import json
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image
import io

from kg_pilot import crop_sources as assets, native_crop_review as review
from kg_pilot.crop_geometry import repair_page
from kg_pilot.native_library import save


def test_edges_follow_cut_labels_without_touching_closed_or_obstructed_edges(tmp_path):
    from kg_pilot.crop_edges import complete_edges
    with fitz.open() as doc:
        page=doc.new_page()
        page.draw_rect((98,100,102,112),color=None,fill=(0,0,0))
        def crop(box):
            return dict(type="image",source_bbox=box,image_sha256="before")
        item=crop([100,90,140,130])
        assert complete_edges(page,item,[],tmp_path)=="expanded"
        assert item["source_bbox"][0]<98 and item["source_bbox"][1:]==[90,140,130]
        assert complete_edges(page,item,[],tmp_path) is None
        guarded=crop([100,90,140,130])
        assert complete_edges(page,guarded,[fitz.Rect(94,98,99,116)],tmp_path)=="review"
        assert guarded["image_sha256"]=="before"


def test_caption_matching_in_columns_and_japanese_body_reference():
    from kg_pilot.crop_geometry import _labels, bind_local_captions
    from unittest.mock import Mock
    def line(text,coords):
        return {"spans":[{"text":text}],"bbox":coords}
    page=Mock()
    page.get_text.return_value={"blocks":[{"lines":[line("FIG. 7. Left column.",(10,121,100,130)),
        line("FIG. 8. Right column.",(125,121,220,130)),line("Second line.",(125,132,190,140))]},
        {"lines":[line("表1 に示すとおりである。",(10,150,220,162))]},
        {"lines":[line("Figure 2 describes the manufacturing process.",(10,180,220,192))]},
        {"lines":[line("Figure 12 also shows an increase in resistance.",(10,200,250,212))]},
        {"lines":[line("Fig. 3a reveals an interfacial layer.",(10,218,220,230))]}]}
    labels={key:values[0] for key,values in _labels(page).items()}
    items=[{"type":"image","source_bbox":[10,35,100,110],"image_caption":[]},
           {"type":"image","source_bbox":[125,35,220,110],"image_caption":[]}]
    bind_local_captions(items,labels)
    assert set(labels)=={("image","7"),("image","8")}
    assert items[0]["image_caption"]==["FIG. 7. Left column."]
    assert items[1]["image_caption"]==["FIG. 8. Right column. Second line."]
    ambiguous=[dict(type="image",source_bbox=[10,35,80,110]),dict(type="image",source_bbox=[81,35,105,110])]
    bind_local_captions(ambiguous,{("image","7"):labels[("image","7")]})
    assert all(not item.get("image_caption") for item in ambiguous)


def test_formatted_caption_label_continues_on_same_baseline_and_skips_wrapped_reference():
    from kg_pilot.crop_geometry import _labels
    from unittest.mock import Mock
    def line(text,x,y,xf):
        return {"spans":[{"text":text}],"bbox":(x,y,xf,y+9)}
    page=Mock()
    page.get_text.return_value={"blocks":[{"lines":[line("Figure 7.",166,300,205),
        line("(a) Load-displacement curve,",219,300,540),line("with error bars.",166,313,250)]},
        {"lines":[line("The same behavior can be seen in",166,400,540),line("Figure 6d. The data show...",166,413,540)]}]}
    labels=_labels(page)
    assert set(labels)=={("image","7")}
    assert labels[("image","7")][0][0]=="Figure 7. (a) Load-displacement curve, with error bars."
    assert labels[("image","7")][0][1]==[166,300,540,309]


def test_complete_pair_shares_one_caption_but_three_ambiguous_regions_do_not():
    from kg_pilot.crop_geometry import bind_local_captions
    def panel(x):
        return dict(type="image",source_bbox=[x,30,x+70,112])
    key=("image","6")
    labels={key:("Fig. 6. Surface transfer in (a) and (b).",[40,116,192,126])}
    parts=[panel(40),panel(122)]
    bind_local_captions(parts,labels)
    assert [part["caption_source"] for part in parts]==["pdf","shared_panel"]
    assert parts[0]["image_caption"]==parts[1]["image_caption"]
    assert not review._page_targets(parts,labels)
    ambiguous=[panel(40),panel(78),panel(122)]
    bind_local_captions(ambiguous,labels)
    assert all("image_caption" not in part for part in ambiguous)
    assert review._page_targets(ambiguous,labels)=={key}


def test_full_scan_around_table_holds_both_candidates(tmp_path,monkeypatch):
    path=tmp_path/"annex.pdf"
    with fitz.open() as pdf:
        page=pdf.new_page()
        page.insert_text((50,500),"Source note from the original annex.")
        pdf.save(path)
    monkeypatch.setattr(review,"classify",lambda *_: {"label":"table","confidence":.3})
    rows=[dict(type="table",page_idx=0,source_bbox=[55,52,395,300],image_sha256="inner"),
          dict(type="image",page_idx=0,source_bbox=[50,50,400,670],image_sha256="outer",img_path="scan.png")]
    pages,_,_,held,*_=review._prepare(path,tmp_path,{"sha256":"a"*64},rows,tmp_path,{},[])
    assert [i["image_sha256"] for i in pages[1]]==["inner","outer"]
    assert len(held)==1 and held[0]["image"]=="outer" and held[0]["table"]=="inner"
    record=dict(page=1,image="outer",table="inner",notes=["Verified source note"])
    changed=dict(record,table="another table")
    assert review._check_table_scans(1,rows,[changed]) and "filter_reason" not in rows[1]
    assert review._check_table_scans(2,rows,[record]) and "table_footnote" not in rows[0]
    assert review._check_table_scans(1,rows,[record])==[]
    assert rows[0]["table_footnote"]==["Verified source note"]
    assert rows[0]["footnote_source_sha256"]=="outer" and rows[1]["filter_reason"]


@pytest.mark.parametrize("extra", [None, "caption", "data", "artwork"])
def test_first_page_table_filters_only_verified_author_metadata(tmp_path, extra):
    path = tmp_path / "article.pdf"
    names = ("Dana Smith Device Lab dana.smith@example.org", "Kim Lee Device Lab kim.lee@example.org")
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((30, 80), names[0])
        page.insert_text((290, 80), names[1])
        page.insert_text((30, 210), "Abstract - Findings of the experiment.")
        if extra == "caption":
            page.insert_text((30, 62), "Table 1. Contributor details.")
        if extra == "data":
            page.insert_text((30, 100), "Contact resistance was 0.5 ohm.")
        if extra == "artwork":
            page.draw_line((32, 90), (190, 110))
        pdf.save(path)

    caption = ["Table 1. Contributor details."] if extra == "caption" else []
    table = dict(type="table", page_idx=0, source_bbox=[25, 67, 565, 112], image_sha256="table",
                 table_caption=caption, table_body=f"| {names[0]} | {names[1]} |")
    pages, _, risks, *_ = review._prepare(path, tmp_path, {"sha256": "a" * 64}, [table], tmp_path, {}, [])
    assert pages[1][0].get("filter_reason") == (review.AUTHOR_TABLE_REASON if extra is None else None)
    if extra in ("data", "artwork"):
        assert 1 in risks


def test_table_note_uses_revision_matched_text_near_this_table():
    from kg_pilot.native_crop_review import _bind_table_notes
    with fitz.open() as doc:
        page=doc.new_page()
        page.insert_text((40,149),"E is the modulus of the film.")
        item=dict(type="table",table_caption=["Table 3. Mechanical properties tested at room temperature."],
                  source_bbox=[40,40,300,137])
        archive={"table_notes":{"3":{"caption":"Mechanical properties tested at room temperature.",
                                    "notes":["E is the modulus of the film."]}}}
        _bind_table_notes(page,item,archive)
        assert item["table_footnote"]==archive["table_notes"]["3"]["notes"]
        assert item["footnote_source"]=="pmc_xml_and_pdf"


def test_advertisement_requires_corroboration_and_an_uncaptioned_image():
    with fitz.open() as pdf:
        page=pdf.new_page()
        page.insert_text((40,490),"ADVERTISEMENT")
        item=dict(type="image",page_idx=0,source_bbox=[40,500,440,620],image_sha256="a")
        assert review._confirmed_junk(page,item,"decoration",pdf,[item],"An advertisement.")
        assert not review._confirmed_junk(page,item,"decoration",pdf,[item],"A chart.")
        assert not review._confirmed_junk(page,dict(item,image_caption=["Figure 1."]), "decoration",pdf,[item],"An advertisement.")


def test_archive_pixel_registration_requires_agreement_across_the_whole_figure():
    import numpy as np
    import cv2
    from kg_pilot.crop_registration import _locate
    rng=np.random.default_rng(51)
    original=np.full((260,360),255,np.uint8)
    for x,y in rng.integers([7,7],[352,250],size=(420,2)):
        cv2.circle(original,(int(x),int(y)),int(rng.integers(2,7)),int(rng.integers(0,160)),-1)
    page=np.full((450,600),255,np.uint8)
    page[60:320,80:440]=original
    rect,score=_locate(original,page,1)
    assert list(rect)==pytest.approx([80,60,440,320],abs=.5) and score>.99
    page[190:320,260:440]=255
    assert _locate(original,page,1) is None


def test_side_caption_recovers_whole_tree_of_panels(tmp_path):
    from kg_pilot.crop_geometry import _whole_image_box
    image=io.BytesIO()
    Image.new("RGB",(300,476),"navy").save(image,format="PNG")
    with fitz.open() as doc:
        page=doc.new_page(width=594,height=792)
        rect=fitz.Rect(44,216,344,692)
        page.insert_image(rect,stream=image.getvalue())
        prediction=fitz.Rect(291,216,551,397)
        assert _whole_image_box(page,prediction,fitz.Rect(375,395,547,404))==rect
        assert _whole_image_box(page,prediction,fitz.Rect(210,706,500,722)) is None


def test_secondary_geometry_separates_two_figures_and_preserves_equation(tmp_path):
    with fitz.open() as pdf:
        page=pdf.new_page()
        page.draw_rect((30,40,190,130),fill=(.2,.3,.5))
        page.draw_rect((30,300,190,380),fill=(.5,.3,.2))
        keys=[("image","9"),("image","10")]
        captions=[[30,140,190,152],[30,390,190,402]]
        labels={k:(f"Figure {k[1]}. Caption",c) for k,c in zip(keys,captions)}
        boxes=[dict(page=1,kind=k[0],number=k[1],box=b,caption_box=c,source="pdffigures2")
               for k,b,c in zip(keys,[[30,40,190,130],[30,300,190,380]],captions)]
        old=dict(type="image",page_idx=0,image_caption=["Figure 10. Caption"],source_bbox=[28,38,194,382])
        overhang={**labels,keys[0]:("Figure 9. Panel", [30,140,194.3,152])}
        assert review._page_targets([old],overhang)==set(keys)
        equation=dict(type="equation",page_idx=0,source_bbox=[50,200,170,216])
        items,res=repair_page(pdf,1,[old,equation],labels,set(keys),boxes,tmp_path)
        assert res==set(keys) and old not in items and equation in items and len(items)==3
        assert items[1] is equation
        for i in items:
            if i["type"]=="image":
                data=Path(i["img_path"]).read_bytes()
                assert hashlib.sha256(data).hexdigest()==i["image_sha256"]
                assert i["preview_source"]=="pdffigures2"
        assert repair_page(pdf,1,[old],labels,set(keys),boxes[:1],tmp_path)==([old],set())


def test_unresolved_audit_is_checkpointed_before_indexing(tmp_path,monkeypatch):
    source=tmp_path/"test.pdf"
    with fitz.open() as pdf:
        page=pdf.new_page()
        page.insert_text((25,55),"Detected text and an unnumbered plot.")
        pdf.save(source)
    async def flag(page,n,items,hooks):
        return {"page":n,"regions":[],"issues":[dict(kind="missing",regions=[],labels=[],detail="Visible plot")]}
    monkeypatch.setattr(review,"review_page",flag)
    row={"sha256":"1"*64}
    with pytest.raises(ValueError,match="裁图待复核"):
        asyncio.run(review.prepare_reviewed_regions(tmp_path,row,source,tmp_path,[],None,{},[]))
    report=json.loads((tmp_path/"crop-review.json").read_text("utf8"))
    assert report["status"]=="needs_review" and report["held"][0]["page"]==1


def test_pending_crop_is_rechecked_automatically_with_prior_findings(tmp_path,monkeypatch):
    source=tmp_path/"test.pdf"
    with fitz.open() as pdf:
        page=pdf.new_page()
        page.insert_text((25,55),"Text referencing a figure on another page.")
        pdf.save(source)
    issue={"kind":"missing", "regions":[], "labels":[], "detail":"Possible missing plot"}
    async def check(pdf,n,items,hooks,*,previous=None):
        if previous:
            assert previous == [{"page":1, **issue}]
        return {"page":n, "regions":[], "issues":[] if previous else [issue]}
    monkeypatch.setattr(review,"review_page",check)
    row={"sha256":"1"*64}
    args=(tmp_path,row,source,tmp_path,[],None,{},[])
    with pytest.raises(ValueError,match="裁图待复核"):
        asyncio.run(review.prepare_reviewed_regions(*args))
    report=tmp_path/"crop-review.json"
    assert review.needs_followup(json.loads(report.read_text("utf8")),row["sha256"])
    assert asyncio.run(review.prepare_reviewed_regions(*args))==[]
    followup=json.loads(report.read_text("utf8"))
    assert followup["status"]=="accepted" and followup["followup_policy"]==review.FOLLOWUP_POLICY


@pytest.mark.parametrize("decision", ["confirmed", "unconfirmed", "invalid_box", "followup_repair", "invalid_review"])
def test_llm_crop_repair_reuses_review_checkpoint_and_requires_independent_acceptance(tmp_path, monkeypatch, decision):
    source = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=400, height=600)
        page.insert_text((35, 55), "The display equation is printed separately below.")
        page.insert_text((80, 180), "C = a / b     (1)")
        if decision == "followup_repair":
            page.insert_text((80, 310), "R = a + b     (2)")
        pdf.save(source)

    issue = {"kind":"missing", "regions":[], "labels":["Equation 1"], "detail":"Uncropped display equation"}
    async def initial_review(pdf, n, items, hooks):
        return {"page":n, "regions":[], "issues":[issue]}

    monkeypatch.setattr(review, "review_page", initial_review)
    row = {"sha256":"a" * 64}
    report_path = tmp_path / "crop-review.json"
    calls = []

    class Hooks:
        async def invoke(self, messages):
            text, original, overlay = messages[0]["content"]
            assert original["type"] == overlay["type"] == "image_url"
            calls.append(text["text"])
            if len(calls) == 1:
                bbox = [185, 275, 470, 315] if decision != "invalid_box" else [185, 275, 1100, 315]
                return json.dumps({"fixes":[{"finding":1,"outputs":[{"kind":"equation","bbox":bbox}]}]})
            if decision == "followup_repair" and len(calls) == 2:
                return json.dumps({"confirmed":False, "issues":[{**issue, "labels":["Equation 2"]}]})
            if decision == "followup_repair" and len(calls) == 3:
                assert "Equation 2" in text["text"]
                return json.dumps({"fixes":[{"finding":1,"outputs":[{"kind":"equation","bbox":[185, 485, 470, 535]}]}]})
            assert "Independent verification" in text["text"]
            if decision == "invalid_review":
                return "invalid JSON"
            return json.dumps({"confirmed":decision in {"confirmed", "followup_repair"}, "issues":[]})

    inputs = (tmp_path, row, source, tmp_path, [], Hooks(), {}, [])
    with pytest.raises(ValueError, match="裁图待复核"):
        asyncio.run(review.prepare_reviewed_regions(*inputs))
    old = json.loads(report_path.read_text("utf8"))
    save(report_path, {**old, "followup_policy":review.FOLLOWUP_POLICY})

    def unexpected_review(*_):
        pytest.fail("Only the flagged-page repair may run against the stored checkpoint")

    monkeypatch.setattr(review, "_prepare", unexpected_review)
    if decision in {"confirmed", "followup_repair"}:
        items = asyncio.run(review.prepare_reviewed_regions(*inputs))
        assert len(items) == (2 if decision == "followup_repair" else 1)
        item = items[0]
        assert item["type"] == "equation" and item["page_idx"] == 0
        assert item["source_bbox"] == [74, 165, 188, 189]
        assert hashlib.sha256(Path(item["img_path"]).read_bytes()).hexdigest() == item["image_sha256"]
        assert asyncio.run(review.prepare_reviewed_regions(*inputs, cached_only=True)) == items
    else:
        for _ in range(2):
            with pytest.raises(ValueError, match="裁图待复核"):
                asyncio.run(review.prepare_reviewed_regions(*inputs))
        result = json.loads(report_path.read_text("utf8"))
        assert result["held"] == old["held"] and result["items"] == []
        assert not review.needs_repair(result, row["sha256"])
    assert len(calls) == {"invalid_box":1,"confirmed":2,"unconfirmed":2,"followup_repair":4,"invalid_review":3}[decision]


def test_new_logos_require_context_and_scientific_evidence_survives(tmp_path,monkeypatch):
    path=tmp_path/"page.pdf"
    with fitz.open() as pdf:
        page=pdf.new_page()
        page.insert_text((70,150),"Abstract. Results from measurements on copper.")
        pdf.save(path)
    rows=[]
    for name,box in [("logo",[50,35,90,55]),("plot",[50,255,210,410])]:
        crop=tmp_path/(name+".png")
        Image.new("RGB",(70,35),"navy").save(crop)
        rows.append(dict(type="image",page_idx=0,img_path=str(crop),image_sha256=name,source_bbox=box))
    monkeypatch.setattr(review,"classify",lambda *_: {"label":"logo","confidence":.97})
    pages,*_=review._prepare(path,tmp_path,{"sha256":"a"*64},rows,tmp_path,{},[])
    assert "filter_reason" in pages[1][0] and "filter_reason" not in pages[1][1]


def test_wrong_archive_revision_never_downloads(tmp_path,monkeypatch):
    f=tmp_path/"x.pdf"
    f.write_bytes(b"another revision")
    url="s3://pmc-oa-opendata/PMC123.1/PMC123.1"
    save(f.with_suffix(".pmc.json"),dict(is_pmc_openaccess=True,pdf_url=url+".pdf?md5="+"0"*32,
                                        xml_url=url+".xml?md5="+"1"*32))
    monkeypatch.setattr(assets,"_fetch",lambda *_:pytest.fail("Fetched another revision"))
    assert assets.read_archive(f,tmp_path) is None


@pytest.mark.parametrize("still_flagged", [False, True])
def test_corrected_crop_must_pass_a_second_review(tmp_path,monkeypatch,still_flagged):
    f=tmp_path/"source.pdf"
    with fitz.open() as pdf:
        pg=pdf.new_page()
        pg.draw_rect((30,40,190,125),fill=(.2,.5,.1))
        pg.insert_text((30,147),"Figure 1. Diagram of the joint.")
        pg.insert_text((30,210),"The complete experimental setup is shown above.")
        pdf.save(f)
        path=tmp_path/"partial.png"
        pg.get_pixmap(clip=(30,40,95,110)).save(path)
    input_item=dict(type="image",page_idx=0,img_path=str(path),source_bbox=[30,40,95,110],
                    image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    image_caption=["Figure 1. Diagram of the joint."])
    calls=[]
    async def flag(pdf,n,items,hooks):
        calls.append(items[0]["source_bbox"])
        issues=[dict(kind="cut",regions=[1],labels=["Figure 1"],detail="Missing right half")]
        return dict(page=n,issues=issues if len(calls)==1 or still_flagged else [])
    monkeypatch.setattr(review,"review_page",flag)
    monkeypatch.setattr(review,"candidate_boxes",lambda *_:[dict(page=1,kind="image",number="1",
                         box=[30,40,190,125],source="pdffigures2")])
    def run():
        return asyncio.run(review.prepare_reviewed_regions(tmp_path,{"sha256":"d"*64},f,tmp_path,[input_item],None,{},[]))
    if still_flagged:
        with pytest.raises(ValueError,match="裁图待复核"):
            run()
        assert json.loads((tmp_path/"crop-review.json").read_text("utf8"))["held"][0]["round"]=="after_repair"
    else:
        first=run()
        assert first[0]["source_bbox"]==[30,40,190,125] and run()==first
    assert calls==[[30,40,95,110],[30,40,190,125]]


def test_held_paper_allows_next_rolling_lane(tmp_path,monkeypatch):
    from scripts import run_native_library as runner
    rows=[{"id":k,"sha256":k} for k in ("one","two")]
    started=[]
    async def lane(root,row,phase,call):
        started.append(row["id"])
        if row["id"]=="one":
            return {"doc_id":"one","message":"裁图待复核：1 项"}
    monkeypatch.setattr(runner,"_rolling_lane",lane)
    monkeypatch.setattr(runner,"document_concurrency",lambda root:1)
    held=asyncio.run(runner._rolling_pass(tmp_path,rows,None))
    assert started==["one","two"] and len(held)==1 and held[0]["doc_id"]=="one"
