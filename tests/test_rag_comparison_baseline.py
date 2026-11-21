"""Offline isolation checks for the current system comparison adapter."""
import asyncio
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace

import numpy as np
import pytest

from kg_pilot import qa, retrieval
from kg_pilot.effective_evidence import REGISTRY, load_effective_evidence
from kg_pilot.map_relations import claims_digest
from kg_pilot.pipeline import read_json, write_json, write_jsonl
from scripts.rag_comparison_20261116.baseline_adapter import CurrentAdapter


class FixtureRuntime:
    def __init__(self):
        self.calls = []
        self.fail = False

    def embedding_tokenizer(self, text, **kwargs):
        return {"offset_mapping": [token.span() for token in re.finditer(r"\S+", text)]}

    def embed_sync(self, texts):
        self.calls.append(texts)
        if self.fail:
            raise ValueError("test embedding failure")
        vectors = np.zeros((len(texts), 1024), dtype=np.float32)
        vectors[:, 0] = 1
        return vectors


@pytest.fixture
def current_adapter(tmp_path):
    frozen = tmp_path / "frozen"
    pdf = frozen / "pdfs/example.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"synthetic PDF identity")
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    paper = {"doc_id": "example", "title": "Synthetic stacking results", "sha256": sha, "local_path": str(pdf)}
    quote = "Table IV lists void free yield. The cumulative void free yield of the stack was 58%."
    table = "TABLE IV. Void free yield per layer\nLayer 1 | 96%\nLayer 2 | 92%"
    separate = "Review detail on a separate physical page."
    evidence = [
        {"evidence_id": eid, "doc_id": "example", "pdf_page": page, "text": text,
         "normalized_text": " ".join(text.split()), "bbox": []}
        for eid, page, text in [
            ("example:p2:b0", 2, quote),
            ("example:p2:table0", 2, table),
            ("example:p5:b0", 5, separate),
        ]
    ]
    unreadable = {"evidence_id": "example:p6:b0", "doc_id": "example", "pdf_page": 6,
                  "text": "\x01", "normalized_text": "\x01", "bbox": []}
    claim = {
        "doc_id": "example", "claim_id": "c1", "statement": "Stack void free yield", "material_system": "Cu",
        "stage": "bonding", "sample_id": "stack A", "quantity": "void_free_yield",
        "conditions": {"method": "U-SAM"}, "value_text": "58%",
        "evidence": [{"evidence_id": "example:p2:b0", "pdf_page": 2, "quote": quote}],
    }
    qualification = {
        "id": "scope-review", "kind": "source_limit", "pdf_pages": [5], "claim_ids": [],
        "summary": "Physical page qualification", "usage_limit": "Use only for the reviewed result.",
        "evidence": [{"pdf_page": 5, "quote": separate}], "query_aliases": [["cumulative", "void free"]],
    }
    write_json(frozen / "papers.json", [paper])
    write_jsonl(frozen / "evidence.jsonl", [*evidence, unreadable])
    write_jsonl(frozen / "claims.jsonl", [claim])
    write_json(frozen / "qualifications.json", {"schema": 1, "documents": [
        {"doc_id": "example", "source_sha256": sha, "claims_digest": claims_digest([claim]),
         "qualifications": [qualification]},
    ]})
    write_json(frozen / "manifest.json", {"pilot_doc_ids": ["example"], "documents": [
        {"doc_id": "example", "original_pdf": "pdfs/example.pdf", "original_sha256": sha},
    ]})
    corpus = SimpleNamespace(path=frozen, doc_ids={"example"}, records={
        row["evidence_id"]: SimpleNamespace(doc_id=row["doc_id"], page=row["pdf_page"])
        for row in evidence
    })
    corpus.records["claim:c1:p2"] = SimpleNamespace(doc_id="example", page=2)
    corpus.records["qualification:example:scope-review:p5"] = SimpleNamespace(doc_id="example", page=5)
    runtime = FixtureRuntime()
    adapter = CurrentAdapter(corpus, runtime, tmp_path / "runs")
    asyncio.run(adapter.build())
    return adapter, runtime


def test_isolated_production_index_and_source_selection(current_adapter):
    adapter, runtime = current_adapter
    staged = Path(adapter.metadata["index_directory"])
    assert staged == adapter._qa.PILOT
    assert qa.PILOT != staged and retrieval.PILOT != staged
    assert adapter.metadata["qualification_status"] == {"scope-review": "active"}
    assert adapter.metadata["vectors"] == 3
    assert adapter.metadata["production_source_sha256"]["qa.py"] == hashlib.sha256(Path(qa.__file__).read_bytes()).hexdigest()
    assert len(runtime.calls) == 1 and len(runtime.calls[0]) == 3

    question = "What was the cumulative void free yield of the stack?"
    assert load_effective_evidence(staged).matches(question) == []
    assert "query_aliases" not in read_json(staged / REGISTRY)["documents"][0]["qualifications"][0]
    ranked = asyncio.run(adapter.retrieve(question))
    assert ranked[:3] == ["example:p2:b0", "claim:c1:p2", "example:p2:table0"]
    assert ranked[-2:] == ["example:p5:b0", "qualification:example:scope-review:p5"]
    assert runtime.calls[-1] == [question]
    sources, warnings = adapter._qa.retrieve_sources(question)
    assert not warnings
    assert any(reason["method"] == "same_page_result_table" for reason in sources[1]["retrieval_reasons"])


def test_dense_errors_do_not_report_a_lexical_only_result(current_adapter):
    adapter, runtime = current_adapter
    runtime.fail = True
    with pytest.raises(RuntimeError, match="could not complete dense retrieval") as error:
        asyncio.run(adapter.retrieve("What was the stacking yield?"))
    assert str(error.value.__context__) == "test embedding failure"
