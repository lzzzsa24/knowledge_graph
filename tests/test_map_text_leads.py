import json
import sqlite3

from kg_pilot.map_text_leads import FACTORS, OUTCOMES, passage, phrase_pattern, text_leads
from kg_pilot.research_map import ROWS, COLUMNS


def test_distinct_terms_and_short_passages():
    assert set(FACTORS) == {r["id"] for r in ROWS}
    assert set(OUTCOMES) == {c["id"] for c in COLUMNS}
    clean = phrase_pattern(FACTORS["surface_planarization"])
    surface = phrase_pattern(OUTCOMES["surface_state"])
    assert not passage("We report only the roughness of the blanket copper specimen.", clean, surface)
    assert not passage("CMP " + "spacing " * 40 + " surface roughness.", clean, surface)
    block = "After CMP the measured surface roughness on these four pads was 0.3 nm."
    _, excerpt = passage(block, clean, surface)
    assert excerpt["quote"] == block == "".join(p["text"] for p in excerpt["parts"])
    assert [p["text"] for p in excerpt["parts"] if p.get("role")] == ["CMP", "roughness"]
    assert not passage("After annealing, the yield strength of copper decreased.", phrase_pattern(FACTORS["annealing"]), phrase_pattern(OUTCOMES["yield"]))


def test_signature_scope_and_references(tmp_path):
    folder = tmp_path / "indexes"
    folder.mkdir()
    papers = [{"doc_id": ident, "sha256": ident, "page_count": 4, "classification": {"routes": [route]}}
              for ident, route in [("a","D2W"), ("b","W2W"), ("stale","D2W"), ("blank","D2W")]]
    claims = [{"doc_id": "a", "material_system": "Cu/SiO2"}, {"doc_id": "b", "material_system": "Cu/SiO2"}]
    with sqlite3.connect(folder/"pilot.sqlite") as db:
        db.executescript("CREATE TABLE papers(doc_id TEXT,metadata TEXT); CREATE TABLE evidence(evidence_id TEXT,doc_id TEXT,pdf_page INTEGER,text TEXT); CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED,doc_id UNINDEXED,text,tokenize='porter unicode61');")
        for p in papers:
            ident = p["doc_id"]
            db.execute("INSERT INTO papers VALUES(?,?)",(ident,json.dumps({"sha256":"different" if ident == "stale" else ident})))
            blocks = ["We performed chemical mechanical polishing and then measured surface roughness.",
                      "■REFERENCES", "CMP roughness: This later bibliography entry must not be shown."]
            if ident == "blank":
                blocks[0] = "The structure has a reported roughness but the process was undisclosed."
            for page, block in enumerate(blocks, 1):
                eid = f"{ident}:p{page}:b1"
                db.execute("INSERT INTO evidence VALUES(?,?,?,?)",(eid,ident,page,block))
                db.execute("INSERT INTO evidence_fts VALUES(?,?,?)",(eid,ident,block))
    args = (tmp_path, papers, claims, "surface_planarization", "surface_state")
    result = text_leads(*args,route="D2W")
    assert result["eligible_documents"] == 2
    assert [(r["doc_id"],r["pdf_page"]) for r in result["results"]] == [("a",1)]
    assert text_leads(*args,route="D2W",system="cu_oxide")["eligible_documents"] == 1
    assert not text_leads(*args,route="D2W",system="dielectric_sicn")["results"]
