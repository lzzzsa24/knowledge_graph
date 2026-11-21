"""Run the current QA retrieval and source selection over the frozen corpus."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import shutil
import time
import uuid

import numpy as np

from kg_pilot import qa, retrieval
from kg_pilot.effective_evidence import FIELD, REGISTRY, load_effective_evidence
from kg_pilot.ingestion_publish import _build_indexes, _vectors
from kg_pilot.pipeline import jsonl, read_json, write_json, write_jsonl

from .common import sanitized_text


class _SharedEncoder:
    """Adapt the comparison runtime to the production encoder interface."""

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def tokenizer(self):
        return self.runtime.embedding_tokenizer

    def encode(self, texts, *, normalize_embeddings, batch_size=8, show_progress_bar=False):
        if not normalize_embeddings or batch_size != 8:
            raise ValueError("The current comparison requires normalized embeddings and batches of eight.")
        encoded = np.asarray(self.runtime.embed_sync(list(texts)), dtype=np.float32)
        if (encoded.shape != (len(texts), 1024) or not np.isfinite(encoded).all()
                or not np.allclose(np.linalg.norm(encoded, axis=1), 1, atol=1e-4)):
            raise ValueError("Shared BGE-M3 returned an invalid normalized embedding matrix.")
        return encoded


def _isolated_reader(original, pilot: Path):
    # QA and semantic retrieval currently use module-level PILOT paths. Each
    # adapter gets its own globals, without patching any imported module.
    short_name = original.__name__.rsplit(".", 1)[-1]
    spec = importlib.util.spec_from_file_location(f"kg_pilot._comparison_{short_name}", original.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.PILOT = pilot
    return isolated


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _stage_inputs(corpus, pilot: Path):
    frozen = Path(corpus.path)
    docs = set(corpus.doc_ids)
    manifest = read_json(frozen / "manifest.json")
    if not docs or set(manifest["pilot_doc_ids"]) != docs:
        raise ValueError("Frozen manifest does not identify the comparison documents.")
    papers = [row for row in read_json(frozen / "papers.json") if row["doc_id"] in docs]
    claims = [row for row in jsonl(frozen / "claims.jsonl") if row["doc_id"] in docs]
    registry = read_json(frozen / "qualifications.json")
    registry["documents"] = [row for row in registry["documents"] if row["doc_id"] in docs]
    qualification_docs = {row["doc_id"] for row in registry["documents"]}
    if {paper["doc_id"] for paper in papers} != docs or len(docs) != len(papers):
        raise ValueError("Frozen paper catalog differs from the comparison corpus.")
    evidence = []
    for row in jsonl(frozen / "evidence.jsonl"):
        if row["doc_id"] not in docs:
            continue
        clean = sanitized_text(row["text"] or row["normalized_text"])
        if not clean:
            continue
        source = corpus.records.get(row["evidence_id"])
        if source is None or (source.doc_id, source.page) != (row["doc_id"], row["pdf_page"]):
            raise ValueError(f"Frozen evidence is absent from the shared corpus: {row['evidence_id']}")
        evidence.append({**row, "text": clean, "normalized_text": sanitized_text(row["normalized_text"])})
    if {row["doc_id"] for row in evidence} != docs:
        raise ValueError("Some pilot documents have no usable frozen evidence.")

    manifest_docs = {row["doc_id"]: row for row in manifest["documents"]}
    for paper in papers:
        doc_id = paper["doc_id"]
        paper.pop("local_path", None)
        if doc_id not in qualification_docs:
            continue
        document = manifest_docs[doc_id]
        original = (frozen / document["original_pdf"]).resolve()
        if not original.is_relative_to(frozen.resolve()):
            raise ValueError(f"Qualified PDF is outside the frozen corpus: {doc_id}")
        filename = hashlib.sha256(doc_id.encode("utf-8")).hexdigest() + ".pdf"
        copy = pilot / "sources" / filename
        copy.parent.mkdir(exist_ok=True)
        shutil.copyfile(original, copy)
        if _sha(copy) != document["original_sha256"]:
            raise ValueError(f"PDF revision differs from the frozen manifest: {doc_id}")
        # The production version checker resolves this relative to its PILOT.
        paper["local_path"] = f"pilot/sources/{filename}"
    for entry in registry["documents"]:
        for qualification in entry["qualifications"]:
            qualification.pop("query_aliases", None)

    write_json(pilot / "catalog/papers.json", papers)
    write_json(pilot / REGISTRY, registry)
    write_jsonl(pilot / "parsed/effective_evidence.jsonl", evidence)
    write_jsonl(pilot / "annotations/claims.jsonl", claims)
    return evidence, claims, docs


class CurrentAdapter:
    """Use production indexing, dense/FTS retrieval and QA source ordering."""

    def __init__(self, corpus, runtime, output_dir: Path):
        self.corpus = corpus
        self.encoder = _SharedEncoder(runtime)
        self.output_dir = Path(output_dir).resolve()
        self.metadata = {}
        self._qa = None

    async def build(self):
        started = time.perf_counter()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        pilot = self.output_dir / f"current-pilot-{uuid.uuid4().hex}"
        pilot.mkdir()
        evidence, claims, docs = _stage_inputs(self.corpus, pilot)
        view = load_effective_evidence(pilot)
        before_indexes = time.perf_counter()
        vector_count = _vectors(pilot, pilot, evidence, docs, self.encoder)
        _build_indexes(pilot)

        isolated_retrieval = _isolated_reader(retrieval, pilot)
        isolated_qa = _isolated_reader(qa, pilot)
        isolated_qa.CONFIG_PATH = pilot / "llm_config.json"
        isolated_qa.SECRET_PATH = pilot / "secrets/paratera.key.dpapi"
        isolated_qa.retrieval_model = lambda: self.encoder

        def retrieve(query, model, limit):
            if model is None:
                # The comparison requires every dense search to run. A
                # retrieval error is a failed case, never a lexical-only run.
                raise RuntimeError("Current adapter could not complete dense retrieval.")
            return isolated_retrieval.retrieve(query, model, limit, pilot=pilot)

        isolated_qa.retrieve = retrieve
        self._qa = isolated_qa
        source_dir = Path(qa.__file__).parent
        self.metadata = {
            "index_directory": str(pilot),
            "documents": len(docs),
            "evidence": len(evidence),
            "claims": len(claims),
            "vectors": vector_count,
            "qualification_status": {
                entry["id"]: entry["status"] for doc_id in sorted(docs) for entry in view.qualifications(doc_id)
            },
            "qualification_query_aliases": False,
            "stage_inputs_seconds": round(before_indexes - started, 3),
            "build_seconds": round(time.perf_counter() - started, 3),
            "production_source_sha256": {
                name: _sha(source_dir / name) for name in (
                    "qa.py", "retrieval.py", "bilingual_retrieval.py", "effective_evidence.py",
                    "embedding_chunks.py", "ingestion_publish.py", "pipeline.py",
                )
            },
        }

    async def retrieve(self, question: str) -> list[str]:
        if self._qa is None:
            raise RuntimeError("Build the current adapter before retrieval.")
        sources, _ = self._qa.retrieve_sources(question)
        selected = []
        for source in sources:
            selected.append(source["evidence_id"])
            doc_id, page = source["doc_id"], source["pdf_page"]
            # Preserve the reviewed evidence attached by production QA in the
            # common representation, adjacent to its physical source page.
            companions = [f"qualification:{doc_id}:{item['id']}:p{page}" for item in source.get(FIELD, ())]
            companions.extend(f"claim:{item['claim_id']}:p{page}" for item in source["reviewed_claims"])
            selected.extend(eid for eid in companions if eid in self.corpus.records)
        return list(dict.fromkeys(selected))
