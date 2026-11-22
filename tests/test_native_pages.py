"""Revision identity, per-page selection, native visual evidence and retry behavior."""
import asyncio
import hashlib
from types import SimpleNamespace

import pymupdf
import pytest

from kg_pilot import native_pages as mix
from kg_pilot.native_library import file_hash, save


def test_windows_layout_retries_a_crashed_worker_with_pdfium(tmp_path, monkeypatch):
    pdf = tmp_path / "sample.pdf"
    layout = object()
    calls = []

    def convert(path, *, pdfium=False):
        calls.append((path, pdfium))
        if not pdfium:
            raise mix.BrokenProcessPool("native PDF parser exited")
        return layout

    monkeypatch.setattr(mix, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(mix, "_isolated_source_layout", convert)
    assert mix.source_layout(pdf, tmp_path, {"id": "sample", "sha256": "bound"}) is layout
    assert calls == [(pdf, False), (pdf, True)]


def test_index_rejects_shifted_visual_and_missing_text_page():
    items = [{"type": "text", "page_idx": 0}, {"type": "text", "page_idx": 1},
             {"type": "image", "page_idx": 1}]
    text = {"page_idx": 0, "page_idx_end": 1}
    image = {"page_idx": 1, "is_multimodal": True, "original_type": "image"}
    mix.validate_indexed_pages(items, [text, image], 2)
    with pytest.raises(ValueError, match="item or physical PDF page"):
        mix.validate_indexed_pages(items, [text, dict(image, page_idx=0)], 2)
    with pytest.raises(ValueError, match="without native chunk"):
        mix.validate_indexed_pages(items, [dict(text, page_idx_end=0), image], 2)


def write_legacy(base, sha, pages):
    save(base / "pages.json", pages)
    save(base / "text_quality.json", {"source_sha256": sha, "pages": [
        {"pdf_page": p["pdf_page"], "unresolved_critical": False} for p in pages]})


def page(n, text, status="clean"):
    return {"pdf_page": n, "text": text, "text_quality": {"status": status}}


def test_page_selection_keeps_good_neighbors_and_checks_revision(tmp_path):
    row = {"sha256": "bound", "pages": 6}
    pages = [page(6, "repaired", "repaired"), page(1, "old"), page(2, " "), page(3, "bad", "needs_review"),
             page(4, "first"), page(4, "duplicate")]
    write_legacy(tmp_path, "bound", pages)
    assert mix.page_plan(tmp_path, row) == ["old", None, None, None, None, "repaired"]
    assert mix.page_plan(tmp_path, dict(row, sha256="changed")) == [None] * 6


def test_only_bad_page_and_original_formula_use_vision(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    with pymupdf.open() as pdf:
        for line in ("Page one", "Page two", "Page three"):
            pdf.new_page().insert_text((50, 50), line)
        pdf.save(pdf_path)
    row = {"id": "paper", "sha256": file_hash(pdf_path), "pages": 3}
    base = tmp_path / "parsed" / row["id"]
    legacy = [page(1, "exact legacy text"), page(2, "broken", "needs_review"), page(3, "third")]
    write_legacy(base, row["sha256"], legacy)
    target = tmp_path / "job"
    target.mkdir()
    clip = target / "formula.png"
    with pymupdf.open(pdf_path) as pdf:
        pdf[2].get_pixmap().save(clip)
    regions = [{"type": "equation", "page_idx": 2, "img_path": str(clip), "image_sha256": file_hash(clip)}]
    monkeypatch.setattr(mix, "original_regions", lambda *_: regions)
    prompts = []
    class Hooks:
        async def vision(self, prompt, image_data):
            assert image_data
            prompts.append(prompt)
            return "reread Ω" if prompt == mix.PAGE_PROMPT else "x^2"
    def run():
        return asyncio.run(mix.mixed_pages(tmp_path, row, pdf_path, target, Hooks()))
    data = run()
    assert [p["source"] for p in data["pages"]] == ["legacy", "pdf_vision", "legacy"]
    assert data["pages"][0]["text_sha256"] == hashlib.sha256(b"exact legacy text").hexdigest()
    assert [p["page_idx"] for p in data["items"]] == [0, 1, 2, 2]
    assert data["items"][1]["text"] == "reread Ω"
    assert data["items"][-1]["latex"] == "x^2"
    assert run() == data and len(prompts) == 2
    legacy[0]["text"] = "new edition of reading copy"
    write_legacy(base, row["sha256"], legacy)
    assert run()["items"][0]["text"] == legacy[0]["text"]


def test_reviewed_crop_filter_is_exact_caption_safe_and_revision_aware(tmp_path, monkeypatch):
    pdf_path = tmp_path / "paper.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(pdf_path)
    row = {"id": "doc", "sha256": file_hash(pdf_path), "pages": 1}
    write_legacy(tmp_path/"parsed/doc", row["sha256"], [page(1, "source text")])
    known, other = "d" * 64, "e" * 64
    registry = {known: "journal masthead"}
    real_read = mix.read_json
    monkeypatch.setattr(mix, "read_json", lambda p: registry if p.name == "publication_decorations.json" else real_read(p))
    crop = dict(type="image", page_idx=0, image_sha256=known, img_path="region.png", source_bbox=[0, 1, 2, 3])
    regions = [crop, dict(crop, image_sha256=other), dict(crop, image_caption=["Figure 1"]),
               dict(crop, image_footnote=["Scientific note"]), dict(crop, type="table", table_body="123")]
    monkeypatch.setattr(mix, "original_regions", lambda *_: regions)
    def run():
        return asyncio.run(mix.mixed_pages(tmp_path, row, pdf_path, tmp_path, None))
    result = run()
    assert result["skipped_decorations"] == [{**crop, "pdf_page": 1, "reason": "journal masthead"}]
    assert result["items"][1:] == regions[1:]
    legacy_identity = {k: v for k, v in result["identity"].items() if k != "decorations"}
    legacy_identity["policy"] = mix.POLICY
    save(tmp_path/"mixed-parse.json", {"identity": legacy_identity, "pages": result["pages"],
                                      "items": result["items"][:1] + regions})
    assert run()["items"] == result["items"]
    registry.clear()
    assert run()["items"][1:] == regions


def test_cover_rule_preserves_same_artwork_in_body_and_other_pdfs(tmp_path, monkeypatch):
    from kg_pilot.decoration_filter import decoration_reason
    pdf_path = tmp_path / "paper.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        pdf.save(pdf_path)
    digest = "b" * 64
    row = {"id":"doc", "sha256":file_hash(pdf_path), "pages":2}
    write_legacy(tmp_path/"parsed/doc", row["sha256"], [page(1,"Cover"), page(2,"Article")])
    cover = dict(type="image", page_idx=0, image_sha256=digest)
    body = dict(cover, page_idx=1)
    captioned = dict(cover, image_caption=["Figure 1"])
    monkeypatch.setattr(mix, "original_regions", lambda *_: [cover, body, captioned])
    registry = {}
    real_read = mix.read_json
    monkeypatch.setattr(mix, "read_json", lambda p: registry if p.name == "publication_decorations.json" else real_read(p))
    def run():
        return asyncio.run(mix.mixed_pages(tmp_path, row, pdf_path, tmp_path, None))
    before = run()
    registry[f"{row['sha256']}:p1:{digest}"] = "cover illustration"
    after = run()
    assert after["transcription"]["reused_previous_parse"]
    assert after["pages"] == before["pages"]
    assert [i for i in after["items"] if i["type"] == "image"] == [captioned, body]
    assert [i["pdf_page"] for i in after["skipped_decorations"]] == [1]
    assert decoration_reason(cover, registry, [], pdf_sha="other-revision") is None
    registry.clear()
    registry[f"{row['sha256']}:p2:{digest}"] = "second page only"
    changed = run()
    assert [i for i in changed["items"] if i["type"] == "image"] == [cover, captioned]


def test_false_equation_removal_is_scoped_by_type_page_and_pdf(tmp_path,monkeypatch):
    from kg_pilot.decoration_filter import decoration_reason
    pdf_path=tmp_path/"paper.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        pdf.save(pdf_path)
    row=dict(id="doc",sha256=file_hash(pdf_path),pages=2)
    write_legacy(tmp_path/"parsed/doc",row["sha256"],[page(1,"Authors and affiliations"),page(2,"Equation")])
    crop=tmp_path/"region.png"
    crop.write_bytes(b"fixture")
    eq=dict(type="equation",page_idx=0,image_sha256="x",img_path=str(crop))
    image=dict(type="image",page_idx=0,image_sha256="x")
    eq2=dict(eq,page_idx=1)
    registry={}
    async def crop_regions(*args,**kwargs):
        return [eq,image,eq2],registry,[]
    monkeypatch.setattr(mix,"crop_regions",crop_regions)
    class Hooks:
        async def vision(self,prompt,image_data):
            return "x"
    def run():
        return asyncio.run(mix.mixed_pages(tmp_path,row,pdf_path,tmp_path,Hooks()))
    before=run()
    registry[f"{row['sha256']}:p1:equation:x"]="author line"
    assert decoration_reason(eq,registry,[],pdf_sha="other") is None
    after=run()
    assert after["transcription"]["reused_previous_parse"] and after["pages"]==before["pages"]
    assert [(i["type"],i["page_idx"]) for i in after["items"]]==[("text",0),("image",0),("text",1),("equation",1)]
    assert after["skipped_decorations"]==[dict(eq,pdf_page=1,reason="author line")]


def test_preparation_limits_all_modalities_together_and_keeps_order(tmp_path, monkeypatch):
    pdf_path = tmp_path / "mixed.pdf"
    with pymupdf.open() as pdf:
        for _ in range(2):
            pdf.new_page()
        pdf.save(pdf_path)
    regions = []
    for n, kind in enumerate(["equation", "table", "equation", "equation", "table", "equation"]):
        path = tmp_path / f"crop-{n}"
        path.write_bytes(f"crop-{n}".encode())
        regions.append(dict(type=kind, img_path=str(path), image_sha256=file_hash(path),
                            page_idx=n//3, table_body=""))
    row = {"id": "doc", "sha256": file_hash(pdf_path), "pages": 2}
    monkeypatch.setattr(mix, "original_regions", lambda *_: regions)
    class Hooks:
        calls = active = peak = 0
        fail = True
        async def vision(self, prompt, image_data):
            self.calls += 1
            idx = self.calls
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(.002 if idx % 2 == 0 else .025)
                if self.fail and idx == 2:
                    raise RuntimeError("quota reached")
                return mix.NO_TEXT if idx == 5 else str(idx)
            finally:
                self.active -= 1
    hook = Hooks()
    def run():
        return asyncio.run(mix.mixed_pages(tmp_path, row, pdf_path, tmp_path, hook))
    with pytest.raises(RuntimeError, match="quota reached"):
        run()
    assert hook.active == 0 and hook.calls == 6 and not (tmp_path/"mixed-parse.json").exists()
    hook.fail, hook.calls = False, 0
    result = run()
    assert hook.peak == result["transcription"]["peak"] == 6
    assert result["transcription"]["tasks"] == hook.calls == 8
    assert [s["source"] for s in result["pages"]] == ["pdf_vision", "pdf_no_text"]
    assert [i["page_idx"] for i in result["items"]] == [0, 0, 0, 0, 1, 1, 1]
    assert [(i.get("latex") or i.get("table_body") or i.get("text")) for i in result["items"]] == ["1","2","3","4","6","7","8"]
    assert run() == result and hook.calls == 8
    # Existing v1 checkpoints survive even if provider response files have since been archived.
    old_identity = {k: v for k, v in result["identity"].items() if k != "decorations"}
    old_identity["policy"] = mix.POLICY
    save(tmp_path/"mixed-parse.json", {"identity": old_identity, "items": result["items"], "pages": result["pages"]})
    upgraded = run()
    assert upgraded["transcription"]["reused_previous_parse"] and hook.calls == 8
    assert upgraded["items"] == result["items"] and upgraded["pages"] == result["pages"]


def test_decoration_template_checks_whole_image_and_preserves_evidence(tmp_path):
    from PIL import Image, ImageDraw, ImageOps
    from kg_pilot.decoration_filter import decoration_reason, signature
    logo = Image.new("RGB", (120, 40), "white")
    pen = ImageDraw.Draw(logo)
    pen.rectangle((8, 6, 35, 32), fill="black")
    pen.ellipse((16, 13, 26, 23), fill="white")
    pen.text((41, 11), "Journal", fill="black")
    ref = tmp_path / "original.png"
    logo.save(ref)
    sha = file_hash(ref)
    ratio, masks = signature(ref)
    refs = [(sha, ratio, masks)]
    reviewed = {sha:"masthead"}
    path = tmp_path / "candidate.png"
    ImageOps.expand(logo.resize((240,80)), border=(10,5,15,20), fill="white").save(path)
    row = dict(type="image", img_path=str(path), image_sha256=file_hash(path))
    assert row["image_sha256"] != sha
    assert decoration_reason(row, reviewed, refs) == "masthead"
    for extra in [{"image_caption":["Fig 1"]}, {"image_footnote":["Scale 1 nm"]}, {"type":"table"}, {"type":"equation"}]:
        assert decoration_reason(dict(row, **extra), reviewed, refs) is None
    assert decoration_reason(row, {}, refs) is None
    chart = ImageOps.expand(logo, border=(0,0,0,80), fill="white")
    ImageDraw.Draw(chart).line([(4,84),(49,99),(79,70),(100,63)], fill="black", width=2)
    chart.save(path)
    assert decoration_reason(dict(row, image_sha256=file_hash(path)), reviewed, refs) is None


def test_white_crop_filter_keeps_sparse_measurements(tmp_path):
    from PIL import Image
    from kg_pilot.decoration_filter import decoration_reason
    path = tmp_path / "sparse.png"
    crop = {"type": "image", "img_path": path}
    im = Image.new("RGB", (300, 200), "white")
    im.save(path)
    assert decoration_reason(crop, {}, []) == "纯白误裁"
    im.putpixel((70, 80), (230, 230, 230))
    im.save(path)
    assert decoration_reason(crop, {}, []) is None


def test_clipped_form_repairs_missing_panels_and_keeps_printed_caption(tmp_path):
    from kg_pilot.clipped_forms import visible_layout_copy
    pdf = tmp_path / "forms.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=300)
        page.insert_text((40, 165), "Figure 1. Two measured panels.", fontsize=10)
        form = doc.get_new_xref()
        doc.update_object(form, "<< /Type /XObject /Subtype /Form /BBox [0 0 100 55] "
                          "/Matrix [1 0 0 1 0 0] /Resources << /Font << /F1 "
                          f"{page.get_fonts()[0][0]} 0 R >> >> >>")
        # A previously embedded page contains real ink on both sides of the
        # diagram and a hidden caption entirely outside the Form clipping box.
        doc.update_stream(form, b"q 0 g 5 5 25 40 re f 70 5 25 40 re f "
                          b"BT /F1 10 Tf 120 10 Td (Figure 99. Hidden.) Tj ET Q")
        resources = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
        doc.xref_set_key(resources, "XObject", f"<< /Plot {form} 0 R >>")
        stream = page.get_contents()[0]
        doc.update_stream(stream, doc.xref_stream(stream) + b" q 1 0 0 1 40 150 cm /Plot Do Q")
        doc.save(pdf)
    original_hash = file_hash(pdf)
    analysis, affected, anchors = visible_layout_copy(pdf, tmp_path)
    assert affected == {1}
    assert analysis and file_hash(pdf) == original_hash
    assert anchors == {1: [(pymupdf.Rect(40, 95, 140, 150), "Figure 1. Two measured panels.")]}
    with pymupdf.open(analysis) as checked:
        page = checked[0]
        assert "Figure 99" not in page.get_text()
        assert "Figure 1." in page.get_text()
        pix = page.get_pixmap()
        assert pix.pixel(50, 120) == pix.pixel(120, 120) == (0, 0, 0)
        assert pix.pixel(90, 120) == (255, 255, 255)
    items = [dict(type="image", page_idx=0, source_bbox=[45, 100, 68, 144]),
             dict(type="equation", page_idx=0, source_bbox=[30, 200, 80, 215])]
    mix._align_figures(items, anchors)
    assert items[0]["source_bbox"] == [40, 95, 140, 150]
    assert items[0]["image_caption"] == ["Figure 1. Two measured panels."]
    assert items[1]["type"] == "equation"
