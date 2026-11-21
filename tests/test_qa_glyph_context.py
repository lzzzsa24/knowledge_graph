from kg_pilot.qa import _source_context


def test_model_sees_unresolved_markers_but_keeps_confirmed_units_and_exact_source():
    text = "50 μm thickness, 5 \uf06d\uf06dm diameter and 35 (cid:27)m pitch."
    source = {"id": "S1", "text": text, "reviewed_claims": [
        {"statement": "TSV dimensions", "evidence_quotes": ["5 \uf06d\uf06dm diameter"]}]}
    context = _source_context(source)
    assert context["text"] == "50 μm thickness, 5 〔原页符号待核〕m diameter and 35 〔原页符号待核〕m pitch."
    assert context["reviewed_claims"][0]["evidence_quotes"] == ["5 〔原页符号待核〕m diameter"]
    assert source["text"] == text
    assert source["reviewed_claims"][0]["evidence_quotes"] == ["5 \uf06d\uf06dm diameter"]
