"""Tables must survive intake with their source labels and row geometry."""
from functools import partial
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from kg_pilot import ingestion as ing
from kg_pilot.ingestion_publish import publish_batch
from kg_pilot.table_evidence import extract_table_evidence, validate_table_element


CAPTION = "TABLE II. SYNTHETIC LAYER RESULTS"
COLUMNS = ["Original label", "Void free yield"]
ROWS = [["Stratum C", "84%"], ["Stratum C (CAP)", "79%"], ["Whole stack", "61%"]]


def _table_pdf(raster=False, rows=ROWS):
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=600)
    page.insert_text((50, 60), "Synthetic table intake fixture")
    page.insert_text((50, 85), "This fixture verifies row evidence through the ordinary batch entry.")
    page.insert_text((50, 120), CAPTION, fontsize=10)
    matrix = [COLUMNS, *rows]
    if raster:
        image = Image.new("RGB", (900, 320), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 26)
        for r, row in enumerate(matrix):
            for c, text in enumerate(row):
                draw.rectangle((c * 450, r * 80, (c + 1) * 450, (r + 1) * 80),
                               fill="#e7ebf4" if r % 2 else "#c6d1e8", outline="black", width=2)
                draw.text((c * 450 + 12, r * 80 + 20), text, fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        page.insert_image(pymupdf.Rect(50, 130, 500, 290), stream=buffer.getvalue())
    else:
        page.draw_rect(pymupdf.Rect(50, 130, 500, 170), color=None, fill=(.75, .82, .9))
        for r in range(5):
            page.draw_line((50, 130 + r * 40), (500, 130 + r * 40))
        for x in (50, 275, 500):
            page.draw_line((x, 130), (x, 290))
        for r, row in enumerate(matrix):
            for c, text in enumerate(row):
                page.insert_text((60 + c * 225, 155 + r * 40), text)
    data = pdf.tobytes()
    pdf.close()
    return data


def _extract(data):
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        return extract_table_evidence(pdf[0], doc_id="synthetic_table", document_sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("raster", [False, True])
def test_native_and_embedded_raster_tables_preserve_cells_and_boundaries(raster):
    data = _table_pdf(raster)
    tables = _extract(data)
    assert len(tables) == 1
    table = tables[0]
    assert table["columns"] == COLUMNS
    assert table["rows"] == ROWS
    assert table["evidence_id"] == "synthetic_table:p1:table0"
    assert table["parser"] == ("rapidocr_table" if raster else "pymupdf_table")
    assert len(table["cell_bboxes"]) == len(ROWS) + 1
    assert table["text"].startswith(CAPTION + "\n")
    assert "Stratum C (CAP) | 79%" in table["text"]
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        page = dict(pdf_page=1, width=600, height=600, native_text=pdf[0].get_text())
    validate_table_element(table, page)
    for mutate in (
        lambda value: value["rows"][0].__setitem__(1, "99%"),
        lambda value: value.__setitem__("pdf_page", 2),
        lambda value: value["cell_bboxes"][1][0].__setitem__(0, -1),
        lambda value: value.__setitem__("table_caption", "TABLE IV. INVENTED"),
    ):
        tampered = json.loads(json.dumps(table))
        mutate(tampered)
        with pytest.raises(ValueError):
            validate_table_element(tampered, page)


def test_raster_table_result_changes_with_pixels_and_does_not_infer_layer_sequence():
    changed = [["Layer 2", "91%"], ["Layer 7", "83%"], ["Entire pile", "77%"]]
    table = _extract(_table_pdf(True, changed))[0]
    assert table["rows"] == changed
    assert "Layer 3" not in table["text"]


@pytest.mark.parametrize("external,broken", [(True, False), (False, True)])
def test_ambiguous_native_header_and_detached_decimal_are_rejected(monkeypatch, external, broken):
    from types import SimpleNamespace
    data = _table_pdf(False)
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        page = pdf[0]
        original = page.find_tables(use_layout=False).tables[0]
        grid = original.extract()
        if broken:
            grid[1][1] = "084\n."
        unsafe = SimpleNamespace(header=SimpleNamespace(external=external), extract=lambda: grid)
        monkeypatch.setattr(pymupdf.Page, "find_tables", lambda *args, **kwargs: SimpleNamespace(tables=[unsafe]))
        assert extract_table_evidence(page, doc_id="synthetic_table", document_sha256=hashlib.sha256(data).hexdigest()) == []


class _Embedder:
    def encode(self, texts, **kwargs):
        return np.full((len(texts), 1024), 1 / 32, dtype=np.float32)


def test_normal_batch_publishes_raster_table_and_binds_its_claim(tmp_path):
    pilot = tmp_path / "isolated"
    (pilot / "catalog").mkdir(parents=True)
    (pilot / "catalog/papers.json").write_text("[]", encoding="utf-8")
    seen = []
    quote = "Stratum C (CAP) | 79%"
    def runner(mode, task, packet, workdir):
        seen.append((task, packet))
        if task == "review":
            return dict(classification_approved=True, classification_notes="Synthetic table checked.",
                        claims=[dict(claim_id=c["claim_id"], status="accepted", reason="Matches table cells.")
                                for c in packet["proposal"]["claims"]], summary="Synthetic fixture.")
        assert any(quote in page["text"] for page in packet["pages"])
        return dict(metadata=dict(title="Synthetic raster table batch fixture", doi=None, year=2026, authors=[]),
            classification=dict(primary_category="characterization_evaluation", related_stages=[], relevance="core",
                document_form="technical_report", content_nature="original_research", publication_status="unknown",
                access_route="other", routes=[], materials=["Cu/SiO2"], topics=["良率"], methods=["experimental"],
                keywords=["void free yield"], classification_reason="合成图片表格用于验证逐行数据及原始层标签的正常批量入库。",
                evidence=[dict(pdf_page=1, quote=quote)]),
            claims=[dict(claim_id="one", statement="合成表格的Stratum C (CAP)层无空洞良率为79%。",
                material_system="Cu/SiO2", sample_id="synthetic_cap", stage="measurement",
                quantity="bonding_yield", value_text="79", unit="%", conditions={},
                evidence=[dict(pdf_page=1, quote=quote)], claim_type="reported_experiment")], coverage_note="Synthetic fixture.")
    batch = ing.create_batch([("image-table.pdf", _table_pdf(True))], pilot=pilot)
    result = ing.run_batch(batch["id"], pilot, runner=runner,
                           publisher=partial(publish_batch, embedder=_Embedder()))
    assert result["status"] == "completed", result
    prepared = json.loads((ing.batch_path(batch["id"], pilot) / "prepared.json").read_text(encoding="utf-8"))["documents"][0]
    assert prepared["pages"][0]["parser"] == "pymupdf"  # prose did not trigger whole-page OCR
    tables = [item for item in prepared["elements"] if item["kind"] == "table"]
    assert tables[0]["rows"] == ROWS
    evidence = prepared["claims"][0]["evidence"][0]
    assert evidence["evidence_id"] == tables[0]["evidence_id"]
    assert evidence["parser"] == "rapidocr_table"
    published = [json.loads(line) for line in (pilot / "parsed/effective_evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(item for item in published if item["kind"] == "table")["rows"] == ROWS
    markdown_path = pilot / "parsed" / prepared["paper"]["doc_id"] / "fulltext.md"
    reading = markdown_path.read_text(encoding="utf-8")
    assert r"| Stratum C \(CAP\) | 79% |" in reading
    assert not markdown_path.with_suffix(".txt").exists()
    assert [task for task, _ in seen] == ["extract", "review"]


def test_actual_gao_image_table_keeps_original_repeated_layer_label():
    source = Path(__file__).parents[1] / "pilot/collection/reliability/gao_d2w_2021.pdf"
    if not source.exists():
        pytest.skip("Local Gao source is not bundled in this checkout")
    data = source.read_bytes()
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        tables = extract_table_evidence(pdf[2], doc_id="reliability_gao_d2w_2021",
                                        document_sha256=hashlib.sha256(data).hexdigest())
    assert len(tables) == 1
    assert tables[0]["pdf_page"] == 3
    assert tables[0]["table_caption"] == "TABLE I. VOID FREE BONDING YEILD PER LAYER IN A 5-LAYER STACK"
    assert tables[0]["rows"] == [["Layer 1", "90%"], ["Layer 2", "87%"], ["Layer 3", "92%"],
                                 ["Layer 5", "93%"], ["Layer 5 (CAP)", "89%"],
                                 ["Total Yield (5-layer stacks without void)", "60%"]]
