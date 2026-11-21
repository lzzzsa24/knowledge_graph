"""Reading copies preserve page provenance and do not invent table structure."""
from copy import deepcopy
from collections import Counter
import json
from pathlib import Path

import pytest

from kg_pilot.reading_markdown import _column_reading_order, relative_pdf_url, render_fulltext_markdown


PAPER = {"doc_id": "fixture", "title": "A [source] *title* <tag>",
         "local_path": "F:/corpus/pilot/collection/batches/测试 (v1) #copy.pdf"}
MD_PATH = "F:/corpus/pilot/parsed/fixture/fulltext.md"
CAPTION = "TABLE I. SOURCE RESULTS"


def _table(**overrides):
    result = dict(kind="table", doc_id="fixture", evidence_id="fixture:p1:t0", pdf_page=1,
                  parser="pymupdf_table", review_status="source_requires_claim_level_review",
                  table_caption=CAPTION, columns=["Original layer", "Yield"],
                  rows=[["Layer 2", "84%"], ["Layer 7", "79%"]],
                  table_bbox=[50, 150, 300, 250], bbox=[50, 130, 300, 250])
    result.update(overrides)
    return result


def _render(pages, evidence=(), **kwargs):
    return render_fulltext_markdown(PAPER, pages, evidence, markdown_path=MD_PATH, **kwargs)


def test_physical_pages_are_sorted_with_encoded_portable_pdf_links_without_mutation():
    pages = [dict(pdf_page=3, text="Page three."), dict(pdf_page=1, text="Page one.")]
    before = deepcopy((PAPER, pages))
    result = _render(pages)
    assert result.index("## PDF 第 1 页") < result.index("## PDF 第 3 页")
    assert "../../collection/batches/%E6%B5%8B%E8%AF%95%20%28v1%29%20%23copy.pdf#page=3" in result
    assert "F:" not in result and "file:" not in result
    assert r"A \[source\] \*title\* &lt;tag&gt;" in result
    assert before == (PAPER, pages)


def test_native_relative_pdf_link_and_layout_default():
    base = Path.cwd() / "tmp/source-link-fixture"
    source = base / "collection/中文/a b.pdf"
    markdown = base / "parsed/fixture/fulltext.md"
    paper = dict(local_path=str(source))
    assert relative_pdf_url(paper, markdown, page=2) == "../../collection/%E4%B8%AD%E6%96%87/a%20b.pdf#page=2"
    assert relative_pdf_url(PAPER, page=1).endswith("%23copy.pdf#page=1")
    assert relative_pdf_url({}) == ""
    with pytest.raises(ValueError):
        relative_pdf_url(PAPER, MD_PATH, page=0)


@pytest.mark.parametrize("pages", [
    [dict(pdf_page=1, text="a"), dict(pdf_page=1, text="b")],
    [dict(pdf_page=0, text="a")],
])
def test_invalid_or_duplicate_physical_page_numbers_are_rejected(pages):
    with pytest.raises(ValueError):
        _render(pages)


def test_conservative_headings_line_reflow_and_untrusted_source_escaping():
    text = "I. INTRODUCTION\nA wrapped source\nparagraph.\n\nA. Test Method\nBody [click](javascript:bad)\n<script>raw</script>\n\n2.1 Results\nA literal value: 1.2 V."
    result = _render([dict(pdf_page=1, text=text)])
    assert "### I\\. INTRODUCTION" not in result  # punctuation remains normal within a heading
    assert "### I. INTRODUCTION" in result
    assert "### A. Test Method" in result and "### 2.1 Results" in result
    assert "A wrapped source paragraph." in result
    assert r"\[click\]\(javascript:bad\)" in result
    assert "<script>" not in result and "&lt;script&gt;raw&lt;/script&gt;" in result
    assert "### 1.0 Bond" not in _render([dict(pdf_page=1, text="1.0 Bond\n0.9 strength")])


@pytest.mark.parametrize("line", [
    "5 \uf06dm diameter at a 35 \uf06dm pitch. The main array in the center",
    "280 \uf06dm. Singulated parts were submitted to Oneida Research",
    "66.03 MPa to make the stress 100 times smaller than before",
    "10 GPa. These results suggest a change in thermal conductance",
    "2024 IEEE 74th Electronic Components and Technology Conference (ECTC)",
    "2 Department of Bioengineering, The University of Tokyo, Japan",
    "14 April 2025",
    "1. S. A. Chew et al., 2023 IEEE International Electron Devices Meeting",
    "31. Uchida K, Tsumura K, Nakamura K, Higashi K, Sugizaki Y,",
    "A. Podpod et al., Novel Temporary Bonding and Debonding Solutions",
])
def test_quantities_years_affiliations_and_authors_stay_in_body(line):
    result = _render([dict(pdf_page=1, text="Original preceding sentence.\n" + line)])
    assert "### " not in result
    assert "Original preceding sentence. " in result


def test_source_paragraph_context_prevents_numbered_body_and_caption_splits():
    text = ("The results are shown in Fig.\n7. The helium leak rate does not show any dependence\n"
            "on the bond line thickness. The second image in\nFig. 8. was used for comparison.\n\n"
            "II. METHODS\n\n2.1. Sample Preparation\nFollowing original body.")
    body, headings = text.split("\n\n", 1)
    elements = [dict(kind="text", pdf_page=1, parser="pymupdf", bbox=[40, 50, 280, 150], text=body),
                dict(kind="text", pdf_page=1, parser="pymupdf", bbox=[40, 200, 280, 300], text=headings)]
    result = _render([dict(pdf_page=1, width=600, height=800, text=text)], elements)
    assert "shown in Fig. 7. The helium leak rate" in result
    assert "image in Fig. 8. was used" in result
    assert "### 7." not in result
    assert "### II. METHODS" in result and "### 2.1. Sample Preparation" in result


def test_text_only_reference_continuation_remains_in_the_same_paragraph():
    result = _render([dict(pdf_page=1, text="The results are shown in Fig.\n7. The helium leak rate does not depend on the width.")])
    assert "shown in Fig. 7. The helium leak rate" in result and "### 7." not in result


def test_full_page_transcription_container_does_not_veto_real_section_headings():
    text = "Prior original paragraph.\n2.1. Preparation of substrates\nFollowing original paragraph."
    elements = [dict(kind="text", pdf_page=1, parser="pymupdf", bbox=[0, 0, 600, 800], text=text)]
    result = _render([dict(pdf_page=1, width=600, height=800, text=text)], elements)
    assert "### 2.1. Preparation of substrates" in result


def test_adjacent_section_and_subsection_in_one_native_block_keep_both_headings():
    headings = "3. Results and Discussion\n3.1. Analysis of Deformation Behavior"
    text = "Prior original paragraph.\n\n" + headings + "\n\nFollowing original paragraph."
    elements = [dict(kind="text", pdf_page=1, parser="pymupdf", bbox=[160, 200, 430, 225], text=headings)]
    result = _render([dict(pdf_page=1, width=600, height=800, text=text)], elements)
    assert r"### 3\. Results and Discussion" in result
    assert "### 3.1. Analysis of Deformation Behavior" in result


def test_reference_section_state_prevents_numbered_bibliography_headings_across_pages():
    result = _render([
        dict(pdf_page=1, text="VI. REFERENCES\n\n1. Hybrid bonding interconnect for advanced\npackaging, 2023."),
        dict(pdf_page=2, text="2. Materials and Methods for Die Bonding\nJournal of Packaging, 2024.\n\nAcknowledgements\nOriginal thanks."),
    ])
    assert "### VI. REFERENCES" in result
    assert "### 1." not in result and "### 2." not in result
    assert "### Acknowledgements" in result and "Original thanks." in result


def test_true_numbered_multilingual_and_bare_section_headings_are_preserved():
    result = _render([dict(pdf_page=1, text=(
        "2 Experimental section\nOriginal body.\n\n3.2 NI 試験における接合強度\nOriginal body.\n\n"
        "4 结果与讨论\nOriginal body.\n\nA. Test Method\nOriginal body.\n\n"
        "4.2. A 4 Gb Memory Test Results\nOriginal body.\n\n1.0 Scope\nOriginal body."))])
    for heading in ["2 Experimental section", "3.2 NI 試験における接合強度", "4 结果与讨论", "A. Test Method",
                    "4.2. A 4 Gb Memory Test Results", "1.0 Scope"]:
        assert "### " + heading in result


def test_caption_anchors_real_grid_before_following_section_and_deduplicates_inputs():
    table = _table()
    original = "Yield discussion above.\n\nTABLE I.  SOURCE\nRESULTS\n\nII. NEXT SECTION\nFollowing body."
    table["text"] = CAPTION + "\nOriginal layer | Yield\nLayer 2 | 84%\nLayer 7 | 79%"
    page = dict(pdf_page=1, text=original + "\n\n" + table["text"])
    result = _render([page], [table, deepcopy(table)])
    assert result.index("Yield discussion above.") < result.index("| Original layer | Yield |")
    assert result.index("| Layer 7 | 79% |") < result.index("### II. NEXT SECTION")
    assert result.count("SOURCE RESULTS") == 1
    assert result.count("84%") == 1
    assert "正文内位置未能可靠匹配" not in result


def test_new_native_text_avoids_augmented_text_and_keeps_a_table_at_page_end():
    table = _table()
    table["text"] = CAPTION + "\nOriginal layer | Yield\nLayer 2 | 84%\nLayer 7 | 79%"
    original = "Body.\n\n" + CAPTION
    page = dict(pdf_page=1, native_text=original, text=original + "\n\n" + table["text"], parser="pymupdf")
    result = _render([page], [table])
    assert result.count("SOURCE RESULTS") == 1 and result.count("84%") == 1
    assert "按页附列" not in result
    # Historical text can also end in an exact table extraction. Preserve its
    # only caption as an anchor rather than claiming a new source position.
    result = _render([dict(pdf_page=1, text="Body.\n\n" + table["text"])], [table])
    assert result.count("SOURCE RESULTS") == 1 and "按页附列" not in result


def test_table_cells_escape_pipes_markup_html_and_reflow_internal_newlines():
    table = _table(parser="rapidocr_table", rows=[[r"A|B\C", "<img>\n*x* [z]"], ["Layer 7", "79%"]])
    result = _render([dict(pdf_page=1, text=CAPTION)], [table])
    assert "图片表格 OCR 抽取" in result and "数值仍需核对原页" in result
    assert r"A\|B\\C" in result
    assert r"&lt;img&gt; \*x\* \[z\]" in result
    assert "<img>" not in result and "<br>" not in result


def test_explicit_ocr_replacement_is_used_without_native_corruption():
    table = _table()
    ocr = [dict(kind="text", doc_id="fixture", pdf_page=1, parser="docling_rapidocr",
                text="Recognized source.\n\n" + CAPTION)]
    result = _render([dict(pdf_page=1, text="CORRUPTED NATIVE", native_text="CORRUPTED NATIVE")],
                     [table], ocr_elements=ocr)
    assert "Recognized source." in result and "CORRUPTED" not in result
    assert "本页正文使用 OCR" in result and "| Layer 2 | 84% |" in result


def test_page_ocr_parser_keeps_ocr_text_instead_of_native_text():
    result = _render([dict(pdf_page=1, text="OCR body", native_text="bad native", parser="rapidocr")])
    assert "OCR body" in result and "bad native" not in result


def test_native_cell_geometry_prevents_duplicate_flattened_body_rows():
    table = _table()
    original = "Before body.\n\n" + CAPTION + "\nOriginal layer\nYield\nLayer 2\n84%\nLayer 7\n79%\n\nAfter body."
    element = dict(kind="text", pdf_page=1, bbox=[60, 155, 290, 245],
                   text="Original layer\nYield\nLayer 2\n84%\nLayer 7\n79%")
    result = _render([dict(pdf_page=1, text=original)], [element, table])
    assert result.count("84%") == 1 and result.count("79%") == 1
    assert result.index("| Layer 7 |") < result.index("After body.")


def test_native_table_header_reordering_and_baseline_box_tolerance_remove_only_source_table():
    table = _table(columns=["Plasma gas", "Bond strength"], rows=[["Ar", "1.69"]],
                   table_caption="Table 1. Different plasma gas conditions.",
                   bbox=[50, 100, 300, 200], table_bbox=[50, 120, 300, 200])
    # The table candidate's lower edge misses the glyph descender by 2pt.
    elements = [dict(kind="text", pdf_page=1, bbox=[60, 125, 290, 150],
                     text="Plasma gas\nBond strength"),
                dict(kind="text", pdf_page=1, bbox=[60, 165, 290, 202], text="Ar\n1.69"), table]
    page = dict(pdf_page=1, text=table["table_caption"] + "\nBond strength Plasma gas\nAr 1.69\n\nAn original body sentence.")
    result = _render([page], elements)
    assert result.count("1.69") == 1
    assert "Different plasma gas conditions." in result  # do not delete a matching caption fragment
    assert result.count("Bond strength") == 1
    assert "An original body sentence." in result


def test_native_table_character_coverage_handles_transposed_columns_without_dropping_prose():
    table = _table(columns=["", "Ref A", "Ref B"],
                   rows=[["Resistance", "0.12", "0.505"], ["Temperature", "400", "350"]])
    elements = [dict(kind="text", pdf_page=1, bbox=[60, 160, 120, 195], text="Ref A\nRef B"),
                dict(kind="text", pdf_page=1, bbox=[60, 200, 290, 230], text="Resistance\n0.12\n0.505"),
                dict(kind="text", pdf_page=1, bbox=[60, 235, 290, 250], text="Temperature\n400\n350"), table]
    page = dict(pdf_page=1, text=CAPTION + "\nRef A Ref B\nResistance Temperature\n0.12 0.505\n400 350\n\nOriginal following prose.")
    result = _render([page], elements)
    assert result.count("0.505") == 1 and result.count("Resistance") == 1
    assert "Original following prose." in result


def test_ligature_caption_matching_preserves_source_offsets_and_avoids_duplicate_table():
    table = _table(table_caption="Table 1. Specific source results.")
    table["text"] = table["table_caption"] + "\nOriginal layer | Yield\nLayer 2 | 84%\nLayer 7 | 79%"
    page = dict(pdf_page=1, text="Unchanged ﬁrst paragraph.\n\nTable 1. Speciﬁc source results.\n\nNext original paragraph.")
    result = _render([page], [table])
    assert result.count("source results.") == 1
    assert "Unchanged ﬁrst paragraph." in result and "Next original paragraph." in result
    assert "按页附列" not in result


def test_source_position_anchor_places_uncaptioned_table_between_nearby_blocks():
    table = _table(table_caption="", table_label="")
    elements = [dict(kind="text", pdf_page=1, bbox=[50, 60, 300, 120], text="Before body."),
                dict(kind="text", pdf_page=1, bbox=[50, 290, 300, 310], text="After body."), table]
    result = _render([dict(pdf_page=1, text="Before body.\n\nAfter body.")], elements)
    assert result.index("Before body.") < result.index("| Layer 2 |") < result.index("After body.")
    assert "按页附列" not in result


def test_no_source_anchor_is_disclosed_instead_of_guessing_position():
    result = _render([dict(pdf_page=1, text="Body only.")], [_table()])
    assert result.index("Body only.") < result.index("| Original layer |")
    assert "正文内位置未能可靠匹配，按页附列" in result


def test_legacy_candidate_does_not_guess_a_header_or_pad_cells():
    table = dict(kind="table", pdf_page=1, evidence_id="fixture:p1:t0",
                 cells=[["Uncertain merged header", None], ["actual label", "12%"], ["ragged"]],
                 review_status="parser_candidate_not_verified")
    result = _render([dict(pdf_page=1, text="Body.")], [table])
    assert "表格候选：行列、表头或合并单元格尚未可靠核验" in result
    assert "```text" in result and "Uncertain merged header | [未识别]" in result
    assert "| --- |" not in result and "actual label | 12%" in result


@pytest.mark.parametrize("overrides", [
    {"rows": [["ragged"]]}, {"rows": [[None, "12%"]]},
    {"merged_cells": [[0, 0, 0, 1]]}, {"review_status": "parser_candidate_not_verified"},
])
def test_uncertain_or_merged_tables_fall_back_to_literal_extraction(overrides):
    result = _render([dict(pdf_page=1, text=CAPTION)], [_table(**overrides)])
    assert "表格候选" in result and "| --- |" not in result


def test_authoritative_reviewed_table_list_does_not_reintroduce_legacy_candidate():
    candidate = dict(kind="table", pdf_page=1, evidence_id="old", cells=[["bad guess"]],
                     review_status="parser_candidate_not_verified")
    reviewed = _table(parser="reviewed_transcription", review_status="source_page_checked",
                      columns=["", "Exact source heading"])
    result = _render([dict(pdf_page=1, text=CAPTION)], [candidate], tables=[reviewed])
    assert "按原页核对的阅读版转录" in result
    assert "|  | Exact source heading |" in result
    assert "bad guess" not in result and "表格候选" not in result
    assert "表格候选" not in _render([dict(pdf_page=1, text="Body")], [candidate], tables=[])


def _two_columns():
    blocks = []
    for label, box in [("Left first", [40, 60, 280, 170]), ("Right first", [320, 60, 560, 170]),
                       ("Left second", [40, 220, 280, 350]), ("Right second", [320, 220, 560, 350])]:
        blocks.append(dict(kind="text", pdf_page=1, parser="pymupdf", bbox=box,
                           text=label + ": complete original paragraph with sufficient source text to establish this column."))
    return blocks, dict(pdf_page=1, width=600, height=600, text="\n\n".join(b["text"] for b in blocks))


def test_clear_complete_columns_reorder_without_losing_any_nonwhitespace_character():
    blocks, page = _two_columns()
    result, reordered = _column_reading_order(page["text"], page, blocks)
    assert reordered
    assert result.index("Left second") < result.index("Right first")
    assert Counter(c for c in result if not c.isspace()) == Counter(c for c in page["text"] if not c.isspace())
    assert "本页按原页双栏顺序整理" in _render([page], blocks)


@pytest.mark.parametrize("problem", ["incomplete", "crossing", "overlapping", "altered", "ocr_coordinates"])
def test_unsafe_columns_preserve_original_page_order(problem):
    blocks, page = _two_columns()
    if problem == "incomplete":
        blocks.pop()
    elif problem == "crossing":
        blocks[0]["bbox"] = [40, 60, 560, 170]
    elif problem == "overlapping":
        blocks[2]["bbox"] = [40, 150, 280, 350]
    elif problem == "altered":
        blocks[0]["text"] += " an added word"
    else:
        blocks[0]["parser"] = "docling_rapidocr"
    result, reordered = _column_reading_order(page["text"], page, blocks)
    assert not reordered and result == page["text"]
    assert "本页按原页双栏顺序整理" not in _render([page], blocks)


def test_actual_gao_table_keeps_repeated_layer_label_and_original_caption_position():
    parsed = Path(__file__).parents[1] / "pilot/parsed/reliability_gao_d2w_2021"
    if not (parsed / "tables.json").exists():
        pytest.skip("Local source-bound Gao extraction is not bundled")
    tables = json.loads((parsed / "tables.json").read_text(encoding="utf-8"))
    pages = json.loads((parsed / "pages.json").read_text(encoding="utf-8"))
    elements = [json.loads(line) for line in (parsed / "elements.jsonl").read_text(encoding="utf-8").splitlines()]
    paper = dict(doc_id="reliability_gao_d2w_2021", title="Gao source fixture",
                 local_path="F:/corpus/pilot/collection/reliability/gao_d2w_2021.pdf")
    result = render_fulltext_markdown(paper, [pages[2]], elements, tables=tables,
                                     markdown_path="F:/corpus/pilot/parsed/reliability_gao_d2w_2021/fulltext.md")
    assert result.count("VOID FREE BONDING YEILD PER LAYER IN A 5-LAYER STACK") == 1
    assert "| Layer 5 | 93% |" in result and r"| Layer 5 \(CAP\) | 89% |" in result
    assert "Layer 4" not in result
    assert result.index("B. D2W Stacking") < result.index("After all 5 layers were bonded")
    assert result.index("C. Assembly Yield") < result.index("| Layer 5 |") < result.index("Fig. 4a shows")
    assert "本页按原页双栏顺序整理" in result
    assert "gao_d2w_2021.pdf#page=3" in result and "按页附列" not in result


def test_actual_gao_measurement_and_figure_reference_stay_inside_source_paragraphs():
    parsed = Path(__file__).parents[1] / "pilot/parsed/reliability_gao_d2w_2021"
    if not (parsed / "pages.json").exists():
        pytest.skip("Local source-bound Gao extraction is not bundled")
    pages = json.loads((parsed / "pages.json").read_text(encoding="utf-8"))
    elements = [json.loads(line) for line in (parsed / "elements.jsonl").read_text(encoding="utf-8").splitlines()]
    result = _render([pages[1], pages[5]], [dict(item, doc_id="fixture") for item in elements], tables=[])
    assert "### 5 " not in result and "### 280 " not in result and "### 7." not in result
    assert "and the TSVs are 5 " in result
    assert "results are shown in Fig. 7. The helium leak rate" in result
    assert "### II. TEST VEHICLE DESIGN AND FABRICATION" in result
    assert "### V. DISCUSSION" in result
    assert result.count("本页按原页双栏顺序整理") == 2
