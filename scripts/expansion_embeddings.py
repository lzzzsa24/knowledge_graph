"""Offline, content-addressed BGE-M3 cache for the November corpus expansion.

This script never publishes or writes into the pilot. The only production model
loader requires the configured local snapshot; a cache miss cannot use a proxy,
random vector, remote endpoint, or a model-name download fallback.

Publisher integration::

    cached = CachedEmbedder(pilot=Path("pilot"))
    publisher = functools.partial(publish_batch, embedder=cached)

Run from the project root with the project Python::

    .venv/Scripts/python.exe scripts/expansion_embeddings.py
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from kg_pilot import ingestion
from kg_pilot.ingestion_publish import _vectors
from kg_pilot.pipeline import normalize, read_json

CACHE_SCHEMA = "expansion-bge-m3-npy-v1"
PARSE_SCHEMA = "expansion-parse-v1"
DEFAULT_CACHE = ROOT / ".cache/expansion_embeddings"
DEFAULT_BATCH = ROOT / "pilot/ingestion/batches/b_86f3ce2479b642aebbd4"
DEFAULT_SELECTION = ROOT / "pilot/reports/expansion_20261103/source_selection.json"
NON_TEXT_REVIEW_REGISTRY = ROOT / "pilot/reports/expansion_20261103/nontext_page_reviews.json"


class EmbeddingCacheError(RuntimeError):
    """The real offline embedding contract could not be satisfied."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def cache_key(identity: dict, text: str) -> str:
    """Hash exact UTF-8 embedding input, without any further normalization."""
    if not isinstance(text, str):
        raise TypeError("BGE cache input must be a string")
    return hashlib.sha256(_canonical(identity) + b"\0" + text.encode("utf-8")).hexdigest()


def validate_vector(value) -> np.ndarray:
    vector = np.asarray(value)
    if vector.dtype != np.float32 or vector.shape != (1024,):
        raise EmbeddingCacheError("Cached BGE-M3 vector must be float32 with dimension 1024")
    if not np.isfinite(vector).all():
        raise EmbeddingCacheError("Cached BGE-M3 vector contains non-finite values")
    norm = float(np.linalg.norm(vector))
    if norm == 0 or not np.isclose(norm, 1.0, rtol=1e-4, atol=1e-5):
        raise EmbeddingCacheError("Cached BGE-M3 vector must be nonzero and normalized")
    return vector


class CachedEmbedder:
    """SentenceTransformer-compatible encoder backed by real local BGE-M3.

    The tokenizer loads immediately from the local snapshot. Model weights load
    lazily only for cache misses, so a publisher with complete cache coverage
    does not need to instantiate a second 2.3 GB model. Multiple processes can
    safely read or atomically replace the same completed vector files.
    """

    def __init__(self, pilot: Path = ROOT / "pilot", cache_dir: Path = DEFAULT_CACHE,
                 *, progress: bool = False):
        self.pilot = Path(pilot).resolve()
        self.cache_dir = Path(cache_dir).resolve()
        self.progress = progress
        self._model = None
        self._model_lock = threading.RLock()
        self._stats_lock = threading.Lock()
        self._stats = dict(requested=0, cache_hits=0, encoded=0, duplicate_inputs=0)
        self._last_progress = 0.0
        config = read_json(self.pilot / "model_config.json")
        expected = dict(embedding_model="BAAI/bge-m3", dimension=1024,
                        max_sequence_length=512, chunk_tokens=480,
                        chunk_stride_tokens=416, normalize_embeddings=True)
        if any(config.get(key) != value for key, value in expected.items()):
            raise EmbeddingCacheError("pilot/model_config.json does not match the BGE-M3 512/480/416 contract")
        revision = config.get("revision", "")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise EmbeddingCacheError("A pinned local BGE-M3 revision is required")
        self.snapshot = self.pilot / "model_cache/models--BAAI--bge-m3/snapshots" / revision
        required = ("config.json", "modules.json", "tokenizer.json", "tokenizer_config.json",
                    "sentence_bert_config.json", "1_Pooling/config.json")
        if not all((self.snapshot / name).is_file() for name in required):
            raise EmbeddingCacheError("Configured local BGE-M3 snapshot is incomplete; downloads are forbidden")
        if not any((self.snapshot / name).is_file() for name in ("pytorch_model.bin", "model.safetensors")):
            raise EmbeddingCacheError("Real BGE-M3 model weights are unavailable; proxy vectors are forbidden")
        if read_json(self.snapshot / "config.json").get("hidden_size") != 1024:
            raise EmbeddingCacheError("Configured local model has an unexpected hidden dimension")
        self.max_seq_length = 512
        self.identity = dict(schema=CACHE_SCHEMA, model="BAAI/bge-m3", revision=revision,
                             max_seq_length=512, normalize_embeddings=True,
                             dimension=1024, dtype="float32")
        self.identity_hash = hashlib.sha256(_canonical(self.identity)).hexdigest()
        self.vector_dir = self.cache_dir / "vectors" / self.identity_hash
        manifest = self.vector_dir / "identity.json"
        if manifest.exists() and read_json(manifest) != self.identity:
            raise EmbeddingCacheError("Cache identity/hash version does not match this encoder")
        _atomic_json(manifest, self.identity)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        import torch
        torch.set_num_threads(min(8, os.cpu_count() or 1))
        self.torch_threads = torch.get_num_threads()
        from transformers import AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(str(self.snapshot), local_files_only=True)
        if self.tokenizer is None:
            raise EmbeddingCacheError("The actual local BGE-M3 tokenizer is required")

    def cache_path(self, text: str) -> Path:
        return self.vector_dir / (cache_key(self.identity, text) + ".npy")

    def _read(self, text: str):
        path = self.cache_path(text)
        if not path.exists():
            return None
        try:
            return validate_vector(np.load(path, allow_pickle=False))
        except (OSError, ValueError) as exc:
            raise EmbeddingCacheError(f"Invalid cache file: {path.name}") from exc

    def _write(self, text: str, vector):
        vector = validate_vector(vector)
        path = self.cache_path(text)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("wb") as stream:
                np.save(stream, vector, allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(str(self.snapshot), device="cpu", local_files_only=True,
                                        cache_folder=str(self.cache_dir / "model_loader"))
            model.max_seq_length = 512
            if model.get_sentence_embedding_dimension() != 1024 or model.tokenizer is None:
                raise EmbeddingCacheError("Real local BGE-M3 dimension/tokenizer validation failed")
            self._model = model
        return self._model

    def stats(self):
        with self._stats_lock:
            return dict(self._stats)

    def encode(self, sentences, batch_size=8, show_progress_bar=False,
               normalize_embeddings=True, convert_to_numpy=True, convert_to_tensor=False, **kwargs):
        if normalize_embeddings is not True or convert_to_numpy is not True or convert_to_tensor:
            raise EmbeddingCacheError("This cache supports normalized float32 NumPy embeddings only")
        # Changing prompts, precision, dimensions, or output modes would change
        # the meaning of an otherwise identical cache key; reject such requests.
        if kwargs:
            raise EmbeddingCacheError("Unsupported encode options: " + ", ".join(sorted(kwargs)))
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        single = isinstance(sentences, str)
        texts = [sentences] if single else list(sentences)
        if not all(isinstance(text, str) for text in texts):
            raise TypeError("BGE cache inputs must be strings")
        if not texts:
            return np.empty((0, 1024), dtype=np.float32)
        unique = list(dict.fromkeys(texts))
        found, missing = {}, []
        for text in unique:
            vector = self._read(text)
            if vector is None:
                missing.append(text)
            else:
                found[text] = vector
        with self._stats_lock:
            self._stats["requested"] += len(texts)
            self._stats["cache_hits"] += len(found)
            self._stats["duplicate_inputs"] += len(texts) - len(unique)
        if missing:
            with self._model_lock:
                model = None
                for offset in range(0, len(missing), batch_size):
                    # Another process may have filled the cache in the meantime.
                    current = missing[offset:offset + batch_size]
                    todo = []
                    for text in current:
                        vector = self._read(text)
                        if vector is None:
                            todo.append(text)
                        else:
                            found[text] = vector
                            with self._stats_lock:
                                self._stats["cache_hits"] += 1
                    if not todo:
                        continue
                    model = model or self._load_model()
                    encoded = np.asarray(model.encode(todo, batch_size=batch_size,
                        normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True), dtype=np.float32)
                    if encoded.shape != (len(todo), 1024):
                        raise EmbeddingCacheError("Actual BGE-M3 returned an unexpected matrix shape")
                    for text, vector in zip(todo, encoded, strict=True):
                        self._write(text, vector)
                        found[text] = vector
                    with self._stats_lock:
                        self._stats["encoded"] += len(todo)
                    if self.progress and time.monotonic() - self._last_progress >= 15:
                        print(json.dumps(dict(event="embedding_progress", time=utc_now(), **self.stats())), flush=True)
                        self._last_progress = time.monotonic()
        result = np.stack([found[text] for text in texts]).astype(np.float32, copy=False)
        return result[0] if single else result


def parser_fingerprint():
    # Changes in paragraphing, OCR selection, normalization, or strict limits
    # invalidate the reusable parsed document cache.
    from kg_pilot import table_evidence
    identity = dict(schema=PARSE_SCHEMA, limits=ingestion.LIMITS,
                    parse_source=inspect.getsource(ingestion.parse_document),
                    ocr_source=inspect.getsource(ingestion._local_ocr),
                    table_source=inspect.getsource(table_evidence),
                    normalize_source=inspect.getsource(normalize),
                    pymupdf_version=ingestion.pymupdf.VersionBind)
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _verify_parsed(parsed, document):
    pages, elements = parsed["pages"], parsed["elements"]
    if not pages or not elements or [p["pdf_page"] for p in pages] != list(range(1, len(pages) + 1)):
        raise EmbeddingCacheError("Parsed document has missing pages/elements")
    if document.get("page_count") and len(pages) != document["page_count"]:
        raise EmbeddingCacheError("Parsed document page count differs from selected source")
    approved = set(document.get("verified_nontext_pages", []))
    marked = {page["pdf_page"] for page in pages if page.get("nontechnical_page_reviewed")}
    if marked != approved:
        raise EmbeddingCacheError("Parsed nontechnical-page annotations differ from the current review registry")
    for page in pages:
        if page["pdf_page"] in approved and (page["text"] != page["native_text"]
                                            or len(page["native_text"].strip()) >= 80):
            raise EmbeddingCacheError("Reviewed nontechnical pages must retain their exact sparse native text")
    seen = set()
    by_page = {p["pdf_page"]: p for p in pages}
    for element in elements:
        if (element["doc_id"] != document["doc_id"]
                or element["document_sha256"] != document["sha256"]
                or element["pdf_page"] not in by_page
                or element["pdf_page"] in approved
                or element["evidence_id"] in seen):
            raise EmbeddingCacheError("Parsed evidence identity does not match its PDF")
        seen.add(element["evidence_id"])
        if element["text"] not in by_page[element["pdf_page"]]["text"]:
            raise EmbeddingCacheError("Parsed evidence is not an exact source-page substring")
        if element["normalized_text"] != normalize(element["text"]):
            raise EmbeddingCacheError("Parsed evidence differs from pipeline.normalize")
    return parsed


def _document_with_nontext_reviews(document):
    """Apply only the fixed, hash-bound registry to a fresh document copy.

    Caller-supplied review flags never authorize this shortcut. A review digest
    is per source document so an unrelated source's approval does not invalidate
    every parsed cache. The exact visual-review records participate in it.
    """
    local = dict(document)
    local.pop("verified_nontext_pages", None)
    local.pop("nontext_review_source_sha256", None)
    if not NON_TEXT_REVIEW_REGISTRY.is_file():
        return local, None
    registry = read_json(NON_TEXT_REVIEW_REGISTRY)
    if registry.get("schema") != "nontext-page-reviews-v1" or not isinstance(registry.get("documents"), list):
        raise EmbeddingCacheError("Nontechnical-page review registry has an unsupported schema")
    candidates = [row for row in registry["documents"]
                  if row.get("source_sha256") == local["sha256"] or row.get("doc_id") == local["doc_id"]]
    if not candidates:
        return local, None
    if len(candidates) != 1:
        raise EmbeddingCacheError("Nontechnical-page review registry has duplicate source identities")
    entry = candidates[0]
    if entry.get("source_sha256") != local["sha256"] or entry.get("doc_id") != local["doc_id"]:
        raise EmbeddingCacheError("Nontechnical-page review is bound to a different source hash")
    reviews = entry.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise EmbeddingCacheError("Nontechnical-page source entry has no explicit reviewed pages")
    numbers = []
    for review in reviews:
        number = review.get("pdf_page")
        if (type(number) is not int or number < 1
                or not isinstance(review.get("actor"), str) or not review["actor"].strip()
                or not isinstance(review.get("judgment"), str) or not review["judgment"].strip()):
            raise EmbeddingCacheError("Nontechnical-page review lacks an actor, page number or judgment")
        image_path = Path(review.get("render_path", ""))
        if not image_path.is_file() or file_sha256(image_path) != review.get("render_sha256"):
            raise EmbeddingCacheError("Nontechnical-page review render is missing or has changed")
        numbers.append(number)
    if len(numbers) != len(set(numbers)):
        raise EmbeddingCacheError("Nontechnical-page review contains duplicate page numbers")
    if local.get("page_count") and any(n > local["page_count"] for n in numbers):
        raise EmbeddingCacheError("Nontechnical-page review has an out-of-range source page")
    digest = hashlib.sha256(_canonical(dict(schema=registry["schema"],
        source_sha256=local["sha256"], reviews=sorted(reviews, key=lambda item: item["pdf_page"])))).hexdigest()
    local["verified_nontext_pages"] = sorted(numbers)
    local["nontext_review_source_sha256"] = local["sha256"]
    return local, dict(source_sha256=local["sha256"], approved_pages=sorted(numbers), review_sha256=digest)


def cached_parse(document, cache_dir: Path, *, existing_parsed: Path | None = None):
    source = Path(document["source_path"])
    if file_sha256(source) != document["sha256"]:
        raise EmbeddingCacheError("Selected source SHA-256 mismatch")
    document, review_summary = _document_with_nontext_reviews(document)
    directory = cache_dir / "parsed" / document["sha256"]
    parsed_path, meta_path = directory / "parsed.json", directory / "metadata.json"
    expected = dict(schema=PARSE_SCHEMA, source_sha256=document["sha256"],
                    doc_id=document["doc_id"], parser_fingerprint=parser_fingerprint(),
                    nontext_review=review_summary)
    parsed = None
    if parsed_path.exists() and meta_path.exists() and read_json(meta_path) == expected:
        parsed, origin = read_json(parsed_path), "verified_parse_cache"
    elif existing_parsed is not None and review_summary is None:
        parsed, origin = read_json(existing_parsed), "existing_batch_parsed"
    if parsed is None or parsed.get("parser_version") != ingestion.PARSER_VERSION:
        # Preserve the application's exact parsing/OCR/limit behavior. Failure
        # is reported as failure; no blank-page, character, or OCR relaxation.
        parsed, origin = ingestion.parse_document(document), "ingestion.parse_document"
    _verify_parsed(parsed, document)
    if file_sha256(source) != document["sha256"]:
        raise EmbeddingCacheError("Source PDF changed during parsing")
    _atomic_json(parsed_path, parsed)
    _atomic_json(meta_path, expected)
    return parsed, parsed_path, origin


def precompute_document(document, cached: CachedEmbedder, *, existing_parsed=None):
    started = time.monotonic()
    parsed, parsed_path, origin = cached_parse(document, cached.cache_dir, existing_parsed=existing_parsed)
    stage = cached.cache_dir / "precompute" / document["sha256"]
    before = cached.stats()
    # An intentionally empty vector source prevents any dependence on or writes
    # to formal vector matrices. The real publisher's function creates every
    # embedding_text using its exact tokenizer 480-token/416-stride algorithm.
    count = _vectors(stage, cached.pilot, parsed["elements"], {document["doc_id"]}, cached,
                     vector_source=stage / "empty_vector_source")
    records = read_json(stage / "benchmarks/embedding/all_records.json")
    # The matrix/records above are disposable precomputation artifacts. Cache
    # coverage is verified independently before a document is declared ready.
    for record in records:
        if cached._read(record["embedding_text"]) is None:
            raise EmbeddingCacheError("An exact publisher embedding input is missing from cache")
    after = cached.stats()
    return dict(status="ready", doc_id=document["doc_id"], sha256=document["sha256"],
                source_path=document["source_path"], pages=len(parsed["pages"]),
                elements=len(parsed["elements"]), vectors=count,
                unique_embedding_texts=len({r["embedding_text"] for r in records}),
                encoded=after["encoded"] - before["encoded"],
                cache_hits=after["cache_hits"] - before["cache_hits"],
                parsed_path=str(parsed_path), parsed_origin=origin,
                warnings=parsed.get("warnings", []), elapsed_seconds=round(time.monotonic() - started, 3),
                ready_at=utc_now())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=ROOT / "pilot")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--first-batch", type=Path, default=DEFAULT_BATCH)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--first-only", action="store_true")
    args = parser.parse_args(argv)
    cached = CachedEmbedder(args.pilot, args.cache_dir, progress=True)
    selected = read_json(args.selection)["selected"]
    selected_by_sha = {row["sha256"]: row for row in selected}
    if len(selected_by_sha) != len(selected):
        raise EmbeddingCacheError("Selected sources contain duplicate SHA-256 values")
    batch = read_json(args.first_batch / "batch.json")
    first_documents = batch["documents"]
    if len(first_documents) != 10 or any(d["sha256"] not in selected_by_sha for d in first_documents):
        raise EmbeddingCacheError("The first batch must contain ten selected sources")
    report_path = cached.cache_dir / "precompute_report.json"
    report = dict(schema=CACHE_SCHEMA, status="running", started_at=utc_now(),
                  pid=os.getpid(), cache_dir=str(cached.cache_dir), identity=cached.identity,
                  torch_threads=cached.torch_threads, selection_sha256=file_sha256(args.selection),
                  first_batch=str(args.first_batch.resolve()), selected_documents=len(selected),
                  selected_pages=sum(row["page_count"] for row in selected),
                  first_ten=[], remaining=[], formal_publication=False)
    _atomic_json(report_path, report)
    (cached.cache_dir / "first10_ready.json").unlink(missing_ok=True)
    _atomic_json(cached.cache_dir / "first10_status.json", dict(status="running", started_at=report["started_at"]))

    def process(document, group, existing=None):
        print(json.dumps(dict(event="document_start", group=group, doc_id=document["doc_id"], time=utc_now())), flush=True)
        try:
            result = precompute_document(document, cached, existing_parsed=existing)
        except Exception as exc:
            result = dict(status="failed", doc_id=document["doc_id"], sha256=document["sha256"],
                          source_path=document["source_path"], error_type=type(exc).__name__, error=str(exc),
                          failed_at=utc_now())
        report[group].append(result)
        report["updated_at"] = utc_now()
        report["stats"] = cached.stats()
        _atomic_json(report_path, report)
        print(json.dumps(dict(event="document_result", **result), ensure_ascii=False), flush=True)

    for original in first_documents:
        document = {**original, "page_count": selected_by_sha[original["sha256"]]["page_count"]}
        existing = args.first_batch / "documents" / document["id"] / "parsed.json"
        if not existing.is_file():
            raise EmbeddingCacheError("The first batch requires an existing parsed.json for every document")
        process(document, "first_ten", existing)
    first_ready = all(row["status"] == "ready" for row in report["first_ten"])
    first_status = dict(status="ready" if first_ready else "failed", completed_at=utc_now(),
                        identity=cached.identity, documents=report["first_ten"],
                        selection_sha256=report["selection_sha256"], first_batch=report["first_batch"],
                        pages=sum(row.get("pages", 0) for row in report["first_ten"]),
                        vectors=sum(row.get("vectors", 0) for row in report["first_ten"]),
                        formal_publication=False)
    _atomic_json(cached.cache_dir / "first10_status.json", first_status)
    if first_ready:
        _atomic_json(cached.cache_dir / "first10_ready.json", first_status)
    print(json.dumps(dict(event="first10_complete", status=first_status["status"], time=utc_now())), flush=True)
    if not args.first_only:
        first_hashes = {d["sha256"] for d in first_documents}
        for row in selected:
            if row["sha256"] in first_hashes:
                continue
            source = Path(row["local_path"]).resolve()
            document = dict(doc_id="doc_" + row["sha256"][:20], sha256=row["sha256"],
                            source_path=str(source), filename=source.name, page_count=row["page_count"])
            process(document, "remaining")
    results = report["first_ten"] + report["remaining"]
    report.update(status="complete" if all(r["status"] == "ready" for r in results) else "completed_with_failures",
                  completed_at=utc_now(), ready_documents=sum(r["status"] == "ready" for r in results),
                  failed_documents=sum(r["status"] == "failed" for r in results),
                  ready_pages=sum(r.get("pages", 0) for r in results),
                  vectors=sum(r.get("vectors", 0) for r in results), stats=cached.stats())
    _atomic_json(report_path, report)
    return 0 if report["failed_documents"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
