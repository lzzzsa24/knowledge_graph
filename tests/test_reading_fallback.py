"""Only undecodable display runs may fall back to their own source PDF page."""
import re

import pytest

from kg_pilot.reading_fallback import apply_reading_fallback


def _page(body, number=3):
    return (f"## PDF 第 {number} 页\n\n"
            f"[查看原 PDF 第 {number} 页](../../collection/original.pdf#page={number})\n\n{body}")


def test_imec_formula_fragments_fall_back_without_guessing_or_losing_labels():
    body = ("\ue001 \ue002\ue003\ue004\ue005 \ue006\ue007, \\(1\\)\n\nwhere\n\n"
            "\ue00c\ue00d\ue00e\ue00f\ue010 \ue011\ue012\ue013\ue014 \\(2\\)\n\nand\n\n"
            "B\ue00d\ue00e\ue00f\ue010 \\!\ue01b\ue01e\ue01e\ue01f\ue01e\" . \\(3\\)\n")
    result = apply_reading_fallback(_page(body))
    assert result.count("[该公式排版见原页](../../collection/original.pdf#page=3)") == 3
    assert all(f"\\({n}\\)" in result for n in (1, 2, 3))
    assert "\n\nwhere\n\n" in result and "\n\nand\n\n" in result
    assert "\ue001" not in result and "〔原页符号〕" not in result
    assert "40.415" not in result  # No reconstructed values are introduced.
    assert apply_reading_fallback(result) == result


def test_isolated_unknowns_preserve_prose_numbers_symbols_and_use_one_note_per_page():
    source = _page("The pitch is 5 \ue001m; yield = 89%, σ ≤ 0.2.\n\n"
                   "Chenxi Wang \ue9d9 https://orcid.org/id\n\nMore \ufffd text.\n")
    result = apply_reading_fallback(source)
    assert "The pitch is 5 〔原页符号〕m; yield = 89%, σ ≤ 0.2." in result
    assert "Chenxi Wang 〔原页符号〕 https://orcid.org/id" in result
    assert "More 〔原页符号〕 text." in result
    assert result.count("[未解码符号见本页原文]") == 1


@pytest.mark.parametrize("known", ["1.3771 × 10⁶", "E_d / E_Cu", "α_d + β", "The measured value is", "I "])
def test_dense_unknowns_do_not_erase_readable_quantities_or_prose(known):
    result = apply_reading_fallback(_page(known + " \ue001\ue002\ue003\ue004\ue005\ue006\ue007\ue008\n"))
    assert known in result
    assert "〔原页符号〕" in result
    assert "[该公式排版见原页]" not in result


def test_legitimate_squares_bullets_and_mathematics_are_unchanged():
    source = _page("■ Feature\n\n□ Checkbox; μ µ Ω σ ∑ √ ± − → ≤ ≥ ⎧ ⎪ ⎩; 10⁻¹¹.\n")
    assert apply_reading_fallback(source) == source


def test_headings_tables_and_escaped_body_keep_their_markdown_structure():
    source = _page("### III. \ue001\ue002\ue003\ue004\n\n"
                   "| Label | Value |\n| --- | --- |\n| Cu \\| Si | 5\ue001 |\n\n"
                   "The \\[original\\] \\(value\\) stays at 89%.\n")
    result = apply_reading_fallback(source)
    assert "### III. 〔原页符号〕" in result
    assert "| Label | Value |\n| --- | --- |\n| Cu \\| Si | 5〔原页符号〕 |" in result
    assert "The \\[original\\] \\(value\\) stays at 89%." in result
    assert result.count("[未解码符号见本页原文]") == 1


def test_fenced_source_is_not_mistaken_for_a_new_page_or_collapsed():
    source = _page("```text\n\ue001\ue002\ue003\ue004\n\n## PDF 第 9 页\n\n"
                   "[查看原 PDF 第 9 页](../../collection/original.pdf#page=9)\n```\n\nEnd.\n")
    result = apply_reading_fallback(source)
    assert "```text\n〔原页符号〕\n\n## PDF 第 9 页" in result
    assert "\n```\n\n[未解码符号见本页原文](../../collection/original.pdf#page=3)" in result
    assert "[该公式排版见原页]" not in result
    assert result.endswith("\n\nEnd.\n")


def test_each_page_uses_its_actual_relative_source_link():
    result = apply_reading_fallback("# Title\n\n" + _page("first \ue001\n", 3)
                                    + "\n" + _page("second \ufffd\n", 7))
    assert result.count("[未解码符号见本页原文]") == 2
    assert re.search(r"first 〔原页符号〕\n\n\[未解码符号见本页原文\]\([^\n]*page=3\)", result)
    assert re.search(r"second 〔原页符号〕\n\n\[未解码符号见本页原文\]\([^\n]*page=7\)", result)


@pytest.mark.parametrize("source", [
    "No page provenance \ue001\ue002\ue003\ue004",
    "## PDF 第 3 页\n\nNo source link \ufffd",
    "## PDF 第 3 页\n\n[查看原 PDF 第 7 页](../../x.pdf#page=7)\n\nBad \ue001",
    "## PDF 第 3 页\n\n[查看原 PDF 第 3 页](../../x.pdf#page=7)\n\nBad \ue001",
    "## PDF 第 3 页\n\n[查看原 PDF 第 3 页](https://example.com/x.pdf#page=3)\n\nBad \ue001",
])
def test_no_invented_source_destination_when_page_provenance_is_missing(source):
    assert apply_reading_fallback(source) == source


def test_supplementary_private_use_is_handled_and_line_endings_survive():
    source = _page("Original 5 \U000f0000\U00100000.\n").replace("\n", "\r\n")
    result = apply_reading_fallback(source)
    assert "Original 5 〔原页符号〕." in result
    assert "\U000f0000" not in result and "\U00100000" not in result
    assert "\n" not in result.replace("\r\n", "")


def test_existing_link_destinations_are_not_rewritten():
    source = _page("[Original \ue001 label](https://example.com/\ue002)\n")
    result = apply_reading_fallback(source)
    assert "[Original 〔原页符号〕 label](https://example.com/\ue002)" in result
