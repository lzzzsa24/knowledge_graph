import hashlib
import json
from pathlib import Path
import sys

import pymupdf
from kg_pilot.map_citations import citations, identity_hash

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "scripts"))
from build_research_citations import HEADING, build, find_targets, fold_offsets, name_tokens


def test_match_requires_identity_from_the_same_reference():
    title="Accurate evaluation of wafer bonding strength under controlled conditions"
    target=("b",fold_offsets(title)[0],2020,["smith"])
    wrong="[1] J. Doe, "+title+", Institute Report.\n2.\nSmith. Different study, 2020."
    assert find_targets(wrong,[target],{},"a")=={}
    right="[1] J. Smith, "+title+", Institute Report, 2020.\n[2] J. Doe, Other Work."
    e=find_targets(right,[target],{},"a")["b"]
    assert e["match"]=="full_title" and e["matched_author"]=="smith" and e["matched_year"]==2020
    assert "Other Work" not in e["quote"] and not find_targets(right,[target],{},"b")
    # An author's surname in the cited title or venue cannot replace a missing author/year.
    near = "[1] Unknown. " + title + ". Smith Institute."
    assert not find_targets(near, [target], {}, "a")


def test_doi_rejects_ambiguous_catalog_identity():
    text="[2] A. Author. Example, 2020. doi:10.1234/xyz.1."
    assert find_targets(text,[],{"10.1234/xyz.1":["b","c"]},"a")=={}
    assert find_targets(text,[],{"10.1234/xyz.1":["b"]},"a")["b"]["match"]=="doi"


def test_corroborating_page_and_external_edition_are_checked_before_publication(tmp_path):
    papers=[{"doc_id":s,"sha256":s*64,"page_count":3,"title":s} for s in ("a","b")]
    root=tmp_path/"research_map";root.mkdir()
    signatures=[{"doc_id":p["doc_id"],"signature":identity_hash(p)} for p in papers]
    year={"kind":"publisher_electronic_release","doc_id":"b","year":2024,
          "target_signature":signatures[1]["signature"],"source_url":"https://example.org/release"}
    edge={"citing":"a","cited":"b","quote":"Corroborating entry","pdf_page":1,"match":"full_title",
          "reference_start_page":1,"reference_end_page":2,"anchor_end_page":2,
          "anchor_basis":"corroborated_variant","exact_title_evidence":{"pdf_page":3,"quote":"Exact title"},
          "matched_year":2024,"matched_year_source":year}
    def write_edge(item):
        (root/"citation_edges.json").write_text(json.dumps({"schema":1,"documents":signatures,"edges":[item]}),"utf8")
    write_edge(edge)
    assert not citations(tmp_path,papers)["edges"]
    (root/"citation_year_evidence.json").write_text(json.dumps({"records":[year]}),"utf8")
    assert citations(tmp_path,papers)["edges"]==[edge]
    write_edge({**edge,"exact_title_evidence":{"pdf_page":4,"quote":"outside the citing PDF"}})
    assert not citations(tmp_path,papers)["edges"]


def test_both_editions_must_match_for_a_citation(tmp_path):
    papers=[{"doc_id":i,"title":"Research "+i,"sha256":i*64,"page_count":2} for i in ["a","b"]]
    root=tmp_path/"research_map";root.mkdir()
    edge={"citing":"a","cited":"b","pdf_page":2,"match":"doi","quote":"Example reference"}
    (root/"citation_edges.json").write_text(json.dumps({"schema":1,"documents":[
        {"doc_id":p["doc_id"],"signature":identity_hash(p),"references_heading":True} for p in papers],
        "edges":[edge,edge,dict(edge,pdf_page=20)]}),encoding="utf8")
    assert citations(tmp_path,papers)["edges"]==[edge]
    assert not citations(tmp_path,[papers[0],dict(papers[1],title="different edition")])["edges"]


def test_decorated_headings_and_unambiguous_numbered_entries():
    assert HEADING.fullmatch("■REFERENCES")
    assert HEADING.fullmatch("参 考 文 献")
    assert HEADING.fullmatch("文　　　献")
    assert not HEADING.fullmatch("References to other materials follow.")
    title = "Electrical and optical measurements under controlled conditions"
    target = ("b", fold_offsets(title)[0], 2023, ["kim"])
    bibliography = (
        "(9) M. Doe. Other work. pp. 590–\n654. https://example.org.\n"
        f"(10) Y. Kim. {title}, 2023. pp. 590–\n654. https://example.org.\n"
        "(11) N. Doe. Other article.\nCorresponding Author: Y. Kim."
    )
    edge = find_targets(bibliography, [target], {}, "a")["b"]
    assert edge["reference_number"] == 10
    assert edge["quote"] == f"(10) Y. Kim. {title}, 2023. pp. 590– 654. https://example.org."
    assert not find_targets(f"We compare {title} to earlier work by Kim in 2023.\n(1) Other research.",
                            [target], {}, "a")
    jp = find_targets(f"1 ）青木. Other.\n2 ）Y. Kim. {title}, 2023.\n3 ）作者. Otro.", [target], {}, "a")
    assert jp["b"]["reference_number"] == 2 and "青木" not in jp["b"]["quote"]


def test_wrapped_doi_prefix_and_author_sorted_bibliography():
    assert name_tokens("Kim YS, Nguyen TH, Choa SH.") == ["kim", "nguyen", "choa"]
    reference = "25 . Earlier work.\n26 . Y. Kim. New work. doi:10.1234/abc.\n123\n27 . Another work."
    found = find_targets(reference, [], {"10.1234/abc.123": ["b"], "10.1234/abc": ["c"]}, "a")
    assert set(found) == {"b"} and found["b"]["reference_number"] == 26
    assert not find_targets(reference, [], {"10.1234/abc": ["b"]}, "a")
    title = "Measurements of electrical and optical properties in wafer bonding"
    target = ("b", fold_offsets(title)[0], 2024, ["kim"])
    ref = f"Kim, Y., T. Nguyen.\n2024. {title}.\nSmith, J. New study, 2024."
    edge = find_targets(ref, [target], {}, "a")["b"]
    assert edge["quote"].startswith("Kim, Y.") and "Smith" not in edge["quote"]


def test_conflicting_year_requires_separate_bibliographic_corroboration():
    title = "Assessment of electrical continuity in a wafer level bonding process"
    target = ("b", fold_offsets(title)[0], 2018, ["kim"])
    correct = "[8] Y. Kim. S" + title[1:] + ". In MYCONF 2018."
    conflict = "[9] Y. Kim. " + title + ". In MYCONF 2020."
    assert not find_targets(conflict, [target], {}, "a")
    assert not find_targets(correct, [target], {}, "a")
    assert not find_targets(correct.replace("MYCONF", "OTHER") + "\n" + conflict, [target], {}, "a")
    edge = find_targets(correct + "\n" + conflict, [target], {}, "a")["b"]
    assert edge["anchor_basis"] == "corroborated_variant" and edge["reference_number"] == 8
    assert edge["exact_title_evidence"]["reference_number"] == 9


def test_signed_pdf_cross_page_title_and_doi(tmp_path):
    def paper(key, pages, title, year=None, doi=None):
        path = tmp_path / f"{key}.pdf"
        with pymupdf.open() as pdf:
            for lines in pages:
                page = pdf.new_page()
                for index, line in enumerate(lines):
                    page.insert_text((60, 80 + index * 20), line)
            pdf.save(path)
        return {"doc_id": key, "title": title, "year": year, "doi": doi,
                "authors": ["Mei Lin"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "local_path": str(path), "page_count": len(pages)}

    source = paper("a", [
        ["Experimental investigation", "References", "[1] M. Lin. Optical validation of metal"],
        ["dishing on wafers, ECTC 2022.", "[2] M. Li. Another work. doi:10.1234/complex."],
        ["1234", "[3] J. Doe. Unrelated work.", "Corresponding Author: M. Lin."],
    ], "Sample document")
    target = paper("b", [["Presented at the Test Conference (ECTC2022)"]],
                   "Optical validation of metal dishing on wafers", 2023)
    doi_target = paper("c", [["Another work"]], "Another work", doi="10.1234/complex.1234")
    found = {edge["cited"]: edge for edge in build([source, target, doi_target])["edges"]}
    assert (found["b"]["pdf_page"], found["b"]["anchor_end_page"], found["b"]["matched_year"]) == (1, 2, 2022)
    assert found["b"]["matched_year_source"]["kind"] == "meeting_printed_on_first_page"
    assert (found["c"]["pdf_page"], found["c"]["anchor_end_page"], found["c"]["reference_end_page"]) == (2, 3, 3)
    assert all("Unrelated" not in edge["quote"] and "Corresponding" not in edge["quote"] for edge in found.values())

    # A publisher's independently reviewed release year is scoped to one edition.
    print_edition = paper("b", [["A later collected edition"]], target["title"], 2025)
    evidence = {"target_signature": identity_hash(print_edition), "year": 2022,
                "source_url": "https://example.org/2022", "kind": "publisher_electronic_release",
                "finding": "Dated publisher archive."}
    assert not build([source, print_edition])["edges"]
    edge = build([source, print_edition], [evidence])["edges"][0]
    assert edge["matched_year_source"] == evidence
    assert not build([source, dict(print_edition, year=2026)], [evidence])["edges"]

