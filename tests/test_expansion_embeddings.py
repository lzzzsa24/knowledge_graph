"""Cache mechanics use explicit fake vectors only in these isolated unit tests."""
import json
import threading
import uuid
from pathlib import Path

import numpy as np
import pymupdf
import pytest

from scripts import expansion_embeddings as expansion
from scripts.expansion_embeddings import (CACHE_SCHEMA, CachedEmbedder, EmbeddingCacheError,
                                         cache_key, validate_vector)


IDENTITY = dict(schema=CACHE_SCHEMA, model="BAAI/bge-m3", revision="a" * 40,
                max_seq_length=512, normalize_embeddings=True, dimension=1024, dtype="float32")


@pytest.fixture
def tmp_path():
    # Windows Python's restrictive 0700 pytest temporary-directory ACL excludes
    # the desktop sandbox identity. Use an owned, normally inherited ACL instead.
    directory = Path(__file__).resolve().parents[1] / ".cache/expansion_embeddings/tests" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    return directory


class TestModel:
    __test__ = False

    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append(list(texts))
        result = np.zeros((len(texts), 1024), dtype=np.float32)
        for i, text in enumerate(texts):
            result[i, len(text) % 1024] = 1.0
        return result


def isolated_cache(tmp_path):
    # Bypass production initialization only inside tests. No model injection
    # argument or fake fallback exists in the actual CachedEmbedder API.
    cached = CachedEmbedder.__new__(CachedEmbedder)
    cached.identity = IDENTITY.copy()
    cached.vector_dir = tmp_path
    cached.progress = False
    cached._model = TestModel()
    cached._model_lock = threading.RLock()
    cached._stats_lock = threading.Lock()
    cached._stats = dict(requested=0, cache_hits=0, encoded=0, duplicate_inputs=0)
    return cached


def test_key_uses_exact_text_and_all_encoding_identity():
    key = cache_key(IDENTITY, "test text")
    assert key == cache_key(dict(reversed(list(IDENTITY.items()))), "test text")
    for text in ("test  text", "test text\n", "test Text", "ｔest text"):
        assert cache_key(IDENTITY, text) != key
    for field, value in (("revision", "b" * 40), ("max_seq_length", 511),
                         ("normalize_embeddings", False), ("schema", "other")):
        assert cache_key({**IDENTITY, field: value}, "test text") != key


def test_misses_only_and_order_duplicate_and_single_preserved(tmp_path):
    cached = isolated_cache(tmp_path)
    first = cached.encode(["a", "bb", "a"], batch_size=1)
    assert cached._model.calls == [["a"], ["bb"]]
    assert first.shape == (3, 1024)
    np.testing.assert_array_equal(first[0], first[2])
    second = cached.encode(["bb", "ccc", "a"])
    assert cached._model.calls == [["a"], ["bb"], ["ccc"]]
    np.testing.assert_array_equal(second[0], first[1])
    assert cached.encode("a").shape == (1024,)
    assert cached.encode([]).shape == (0, 1024)
    assert len(list(tmp_path.glob("*.npy"))) == 3
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("bad", [np.zeros(1024, dtype=np.float32),
    np.ones(1024, dtype=np.float32), np.ones(4, dtype=np.float32),
    np.full(1024, np.nan, dtype=np.float32), np.ones(1024, dtype=np.float64)])
def test_invalid_cache_is_rejected(tmp_path, bad):
    cached = isolated_cache(tmp_path)
    np.save(cached.cache_path("bad"), bad)
    with pytest.raises(EmbeddingCacheError):
        cached.encode(["bad"])
    assert not cached._model.calls


def test_missing_real_weights_fail_before_tokenizer_or_model_import(tmp_path):
    pilot = tmp_path / "pilot"
    snapshot = pilot / "model_cache/models--BAAI--bge-m3/snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    config = dict(embedding_model="BAAI/bge-m3", revision="a" * 40, dimension=1024,
                  max_sequence_length=512, chunk_tokens=480, chunk_stride_tokens=416,
                  normalize_embeddings=True)
    (pilot / "model_config.json").write_text(json.dumps(config))
    for name in ("config.json", "modules.json", "tokenizer.json", "tokenizer_config.json",
                 "sentence_bert_config.json", "1_Pooling/config.json"):
        file = snapshot / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("{}")
    with pytest.raises(EmbeddingCacheError, match="weights.*proxy"):
        CachedEmbedder(pilot, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_cache_miss_cannot_substitute_a_vector_when_model_unavailable(tmp_path):
    cached = isolated_cache(tmp_path)
    def unavailable():
        raise EmbeddingCacheError("actual model unavailable")
    cached._load_model = unavailable
    with pytest.raises(EmbeddingCacheError, match="actual model unavailable"):
        cached.encode(["not cached"])
    assert not list(tmp_path.glob("*.npy"))


def test_incompatible_encoding_options_are_rejected(tmp_path):
    cached = isolated_cache(tmp_path)
    with pytest.raises(EmbeddingCacheError):
        cached.encode(["a"], normalize_embeddings=False)
    with pytest.raises(EmbeddingCacheError):
        cached.encode(["a"], prompt="different input")
    assert not cached._model.calls


def review_fixture(tmp_path):
    source = tmp_path / "source.pdf"
    with pymupdf.open() as pdf:
        for text in ("Thank you!", "Section overview includes only a heading and a subtitle.",
                     "This body page contains substantial readable technical text about copper bonding. "
                     "The measured specimens were prepared and inspected with an optical microscope."):
            page = pdf.new_page()
            page.insert_textbox(pymupdf.Rect(36, 160, 550, 400), text)
        pdf.save(source)
    sha = expansion.file_sha256(source)
    document = dict(source_path=str(source), sha256=sha, doc_id="doc_" + sha[:20],
                    filename=source.name, page_count=3)
    reviews = []
    with pymupdf.open(source) as pdf:
        for number in (1, 2):
            path = tmp_path / f"review_page_{number}.png"
            pdf[number - 1].get_pixmap().save(path)
            reviews.append(dict(pdf_page=number, actor="/root/unit_test", judgment="Isolated test divider",
                                render_path=str(path), render_sha256=expansion.file_sha256(path)))
    registry = dict(schema="nontext-page-reviews-v1", documents=[dict(
        source_sha256=sha, doc_id=document["doc_id"], reviews=reviews)])
    registry_path = tmp_path / "registry.json"
    expansion._atomic_json(registry_path, registry)
    return document, registry, registry_path


def test_review_source_hash_binding_is_strict(tmp_path, monkeypatch):
    document, registry, registry_path = review_fixture(tmp_path)
    monkeypatch.setattr(expansion, "NON_TEXT_REVIEW_REGISTRY", registry_path)
    registry["documents"][0]["source_sha256"] = "f" * 64
    expansion._atomic_json(registry_path, registry)
    with pytest.raises(EmbeddingCacheError, match="different source hash"):
        expansion.cached_parse(document, tmp_path / "cache")
    with Path(document["source_path"]).open("ab") as stream:
        stream.write(b"\n% changed fixture\n")
    with pytest.raises(EmbeddingCacheError, match="source SHA-256 mismatch"):
        expansion.cached_parse(document, tmp_path / "cache")


def test_review_change_invalidates_parse_cache_without_mutating_caller(tmp_path, monkeypatch):
    document, registry, registry_path = review_fixture(tmp_path)
    monkeypatch.setattr(expansion, "NON_TEXT_REVIEW_REGISTRY", registry_path)
    cache = tmp_path / "cache"
    parsed, path, origin = expansion.cached_parse(document, cache)
    assert origin == "ingestion.parse_document"
    assert [p["pdf_page"] for p in parsed["pages"] if p.get("nontechnical_page_reviewed")] == [1, 2]
    old_meta = expansion.read_json(path.with_name("metadata.json"))
    assert expansion.cached_parse(document, cache)[2] == "verified_parse_cache"
    # Removing approval for this >=45-character subtitle must reparse it as
    # ordinary source text, without needing OCR or an embedding model in tests.
    registry["documents"][0]["reviews"].pop()
    expansion._atomic_json(registry_path, registry)
    updated, path, origin = expansion.cached_parse(document, cache)
    new_meta = expansion.read_json(path.with_name("metadata.json"))
    assert origin == "ingestion.parse_document"
    assert old_meta["nontext_review"]["review_sha256"] != new_meta["nontext_review"]["review_sha256"]
    assert new_meta["nontext_review"]["approved_pages"] == [1]
    assert any(element["pdf_page"] == 2 for element in updated["elements"])
    assert "verified_nontext_pages" not in document and "nontext_review_source_sha256" not in document
