from kg_pilot.embedding_chunks import source_chunks


def token_offsets(text, **options):
    assert options["return_offsets_mapping"]
    return {"offset_mapping": [(i, i + 1) for i, char in enumerate(text) if char.isalnum()]}


def test_preserve_scientific_units_and_ignored_symbols_at_source_boundaries():
    passage = "∠" + "a" * 415 + "5 μm, Ω " + "b" * 480 + " ±"
    first, second, third = source_chunks(token_offsets, passage)
    assert first.startswith("∠") and third.endswith(" ±")
    assert "5 μm, Ω " in first and "μm, Ω " in second
    assert all(part in passage for part in (first, second, third))
    assert source_chunks(token_offsets, "77300 Ω ± 1785 Ω") == ["77300 Ω ± 1785 Ω"]
