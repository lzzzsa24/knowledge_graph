import json
from copy import deepcopy
from pathlib import Path

import pytest

from kg_pilot.paper_metadata import apply_paper_metadata, author_family_name, normalize_authors


@pytest.mark.parametrize(("author", "family"), [
    ("Lin Hou", "Hou"),
    ("YAN, Meng", "Yan"),
    ("Ong JJ", "Ong"),
    ("DE AMICIS, Giovanni", "De Amicis"),
    ("Giovanni de Amicis", "de Amicis"),
    ("D. van den Berg", "van den Berg"),
    ("José María García-López", "García-López"),
    ("John Smith Jr.", "Smith"),
    ("歐陽修", "歐陽"),
    ("欧阳明", "欧阳"),
    ("内海 淳", "内海"),
])
def test_first_author_family_name(author, family):
    assert author_family_name(author) == family


def test_normalize_author_delimiters_without_splitting_inverted_name():
    assert normalize_authors("Ong JJ, Chiu WL, Lee OH.") == ["Ong JJ", "Chiu WL", "Lee OH"]
    assert normalize_authors("YAN, Meng") == ["YAN, Meng"]
    assert normalize_authors("DE AMICIS, Giovanni; DEL MONTE, Andrea") == ["DE AMICIS, Giovanni", "DEL MONTE, Andrea"]
    assert normalize_authors(["DE AMICIS, Giovanni", " DEL MONTE, Andrea "]) == ["DE AMICIS, Giovanni", "DEL MONTE, Andrea"]
    assert normalize_authors(None) == []


def test_overrides_require_matching_source_and_preserve_input(monkeypatch):
    papers = [{"doc_id": "a", "sha256": "version-a", "authors": ["Lin Hou"], "year": 2022},
              {"doc_id": "b", "sha256": "version-b", "authors": ["Lin Hou"], "year": 2022}]
    original = deepcopy(papers)
    entry = {"sha256": "version-a", "authors": [], "organization": "IPC"}
    data = json.dumps({"schema_version": 1, "documents": {"a": entry, "b": entry}})
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: data)
    matched, stale = apply_paper_metadata(papers, Path("pilot"))
    assert matched["authors"] == [] and matched["short_label"] == "IPC 2022"
    assert stale["authors"] == ["Lin Hou"] and stale["short_label"] == "Hou 2022"
    assert papers == original


@pytest.mark.parametrize("data", [
    "incomplete-json",
    "[]",
    '{"schema_version": 1, "documents": []}',
    '{"schema_version": 1, "documents": {"a": "invalid entry"}}',
    '{"schema_version": 1, "documents": {"a": {"sha256": "a", "authors": 1, "family_name": [], "organization": []}}}',
])
def test_invalid_overrides_keep_catalog_authors(monkeypatch, data):
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: data)
    row, = apply_paper_metadata([{"doc_id": "a", "sha256": "a", "authors": "Ong JJ, Chiu WL", "year": 2022}], Path("pilot"))
    assert row["authors"] == ["Ong JJ", "Chiu WL"]
    assert row["short_label"] == "Ong 2022"


def test_active_catalog_metadata_complete_and_traceable():
    pilot = Path(__file__).resolve().parents[1] / "pilot"
    papers = json.loads((pilot / "catalog/papers.json").read_text(encoding="utf-8-sig"))
    data = apply_paper_metadata(papers, pilot)
    assert all(isinstance(row["authors"], list) and row["short_label"].strip() for row in data)
    assert all(row["authors"] or row.get("organization") for row in data)
    rows = {row["doc_id"]: row for row in data}
    assert rows["journals_ma15051888"]["authors"][:2] == ["Jia-Juen Ong", "Wei-Lan Chiu"]
    assert rows["journals_s44172-026-00649-w"]["authors"][4] == "Chandra Rao B.S.S"
    assert rows["reliability_gao_d2w_2021"]["short_label"] == "Gao 2021"
    assert rows["doc_e32cb835d14010241a5a"]["short_label"] == "IPC 2020"
    assert rows["doc_0f671a22d6f00dcc3195"]["short_label"] == "Kawai Pétillot 2023"
    assert rows["doc_fe5ef6af1545ccfe67bb"]["short_label"] == "小山 2022"
    assert rows["doc_84e375f077d18f647b0e"]["year"] is None
    assert rows["doc_84e375f077d18f647b0e"]["short_label"] == "Tsau 2022"
