import asyncio
from dataclasses import replace
import json
import math
import threading
from uuid import uuid4

import httpx
import numpy as np
import pytest

from scripts.rag_comparison_20261116.common import (
    BENCHMARK, BudgetExceeded, Corpus, Evidence, ModelError, Runtime, append_jsonl, message_digest,
    pack_context, read_jsonl, sha256_file, validate_answer, write_json,
)
from scripts.rag_comparison_20261116.framework_adapters import (
    Chunk, FrameworkAdapter, merge_native_duplicates, native_structured_chunks, native_text_chunks, plan_chunks,
)
from scripts.rag_comparison_20261116.harness import carry_forward_current


class CharacterTokenizer:
    def encode(self, value, **kwargs):
        return list(value)


@pytest.fixture
def tmp_path():
    # Standard pytest-created private directories fail ACL checks in this
    # Windows workspace. Use a separate run-artifact area with inherited ACLs.
    path = BENCHMARK / "runs" / "harness_offline_tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def tiny_corpus(path):
    first = Evidence("source:p1:b0", "source", 1, "Cu and SiCN form a hybrid bond.", "text")
    return Corpus(path, {"source": {"title": "Adjacent Cu and SiCN"}},
                  {first.evidence_id: first}, [], {})


def test_context_preserves_citation_while_truncating(tmp_path):
    corpus = tiny_corpus(tmp_path)
    source = next(iter(corpus.records.values()))
    corpus.records[source.evidence_id] = replace(source, text="接合条件。" * 300)
    context, included, count = pack_context(corpus, [source.evidence_id], list, 300)
    assert count == len(context) <= 300
    record = json.loads(context)
    assert record["evidence_id"] == source.evidence_id
    assert record["doc_id"] == source.doc_id
    assert record["pdf_page"] == source.page
    assert included[0]["text"].endswith(" […]")
    with pytest.raises(ValueError, match="excerpt mismatch"):
        pack_context(corpus, [replace(source, text="unseen")], list)


def test_answer_cites_exact_delivered_evidence_and_allows_crystal_direction(tmp_path):
    corpus = tiny_corpus(tmp_path)
    _, included, _ = pack_context(corpus, corpus.records, list)
    parsed = {"answerability": "supported", "answer": "在 [111] 方向观测。[source:p1:b0]",
              "citations": [{"evidence_id": "source:p1:b0", "doc_id": "source", "pdf_page": 1}]}
    assert validate_answer(json.dumps(parsed), included) == parsed
    with pytest.raises(ValueError, match="array differ"):
        validate_answer(json.dumps({**parsed, "answer": parsed["answer"] + "[source:p9:b8]"}), included)
    with pytest.raises(ValueError, match="requires"):
        validate_answer(json.dumps({**parsed, "citations": [], "answer": "无引用"}), included)


def test_native_parsers_distinguish_missing_metadata_from_no_results():
    assert native_structured_chunks({"status": "failure", "metadata": {"failure_reason": "no_results"}}) == []
    with pytest.raises(ValueError, match="empty structured metadata"):
        native_structured_chunks({})
    with pytest.raises(ValueError, match="recognizable"):
        native_text_chunks("unstructured fallback")
    retrieved = 'Document Chunks (citations):\n\n```json\n{"content":"original evidence"}\n```\n'
    assert native_text_chunks(retrieved) == ["original evidence"]


def test_identical_native_content_retains_distinct_evidence_locators(tmp_path):
    corpus = tiny_corpus(tmp_path)
    original = next(iter(corpus.records.values()))
    alias = replace(original, evidence_id="source:p1:full")
    identical = [Chunk("heading\nCu and SiCN", (original,)), Chunk("heading\nCu and SiCN", (alias,))]
    assert merge_native_duplicates({"source": identical}) == {
        "source": [Chunk("heading\nCu and SiCN", (original, alias))]
    }


def test_runtime_preserves_usage_and_stops_at_request_ceiling(tmp_path, monkeypatch):
    import kg_pilot.qa

    key = "test-credential-keep-out-of-artifacts"
    monkeypatch.setattr(kg_pilot.qa, "_api_key", lambda config: key)
    requests = []

    def handle(request):
        assert request.headers["authorization"] == f"Bearer {key}"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": f"response {key}"},
                                                       "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12,
                                                   "prompt_tokens_details": {"cached_tokens": 4}}})

    async def run():
        runtime = Runtime(tmp_path, execute=True, max_calls=1, max_tokens=10000,
                          transport=httpx.MockTransport(handle))
        with runtime.stage("current", "answer", "dev-01"):
            assert "credential redacted" in await runtime.complete("question")
            with pytest.raises(BudgetExceeded):
                await runtime.complete("second question")
        await runtime.close()
        assert runtime.stopped and runtime.attempts == 1 and runtime.budget_charged_tokens == 12

    asyncio.run(run())
    assert len(requests) == 1
    events = read_jsonl(tmp_path / "events.jsonl")
    finished = next(row for row in events if row["event"] == "llm")
    assert finished["stage"] == "answer"
    assert finished["usage"]["prompt_tokens_details"]["cached_tokens"] == 4
    assert events[-1]["event"] == "budget_stop"
    assert all(key not in file.read_text(encoding="utf8") for file in tmp_path.rglob("*") if file.is_file())


def test_resumed_capacity_and_paid_prompt_guard_are_extraction_only(tmp_path, monkeypatch):
    import kg_pilot.qa

    monkeypatch.setattr(kg_pilot.qa, "_api_key", lambda config: "offline-recovery-credential")
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"total_tokens": 10}})

    async def run():
        runtime = Runtime(tmp_path, execute=True, max_calls=10, max_tokens=50000,
                          transport=httpx.MockTransport(handle))
        runtime.build_extraction_max_tokens = 8192
        prompt = "responsible for extracting entities and relationships"
        runtime.previously_paid_extraction_prompts = {message_digest([
            {"role": "system", "content": prompt}, {"role": "user", "content": "old"},
        ])}
        with runtime.stage("lightrag", "build"):
            with pytest.raises(ModelError, match="verified paid"):
                await runtime.complete("old", system_prompt=prompt)
            await runtime.complete("new", system_prompt=prompt)
            await runtime.complete("summary", system_prompt="summarize")
        with runtime.stage("lightrag", "retrieve"):
            await runtime.complete("old", system_prompt=prompt)
        with runtime.stage("raganything", "answer"):
            await runtime.complete("old", system_prompt=prompt)
        await runtime.close()

    asyncio.run(run())
    assert [x["max_tokens"] for x in payloads] == [8192, 4096, 4096, 4096]
    assert read_jsonl(tmp_path / "events.jsonl")[0]["event"] == "previously_paid_prompt_blocked"


def test_native_graph_checkpoint_can_run_both_text_query_entrypoints(tmp_path):
    """Synthetic data tests API wiring without using any paid model requests."""
    class TestRuntime:
        model_config = {"model": "offline_contract_test"}
        stopped = False

        def __init__(self):
            self._encode_lock = threading.RLock()

        async def embed(self, texts, **kwargs):
            return np.full((len(texts), 1024), 1 / math.sqrt(1024), dtype=np.float32)

        async def complete(self, prompt, **kwargs):
            if kwargs.get("keyword_extraction"):
                return '{"high_level_keywords":["hybrid bond"],"low_level_keywords":["Cu","SiCN"]}'
            return ("entity<|#|>Cu<|#|>material<|#|>Cu in hybrid bonds\n"
                    "entity<|#|>SiCN<|#|>material<|#|>SiCN in hybrid bonds\n"
                    "relation<|#|>Cu<|#|>SiCN<|#|>hybrid bond<|#|>Cu and SiCN form a hybrid bond.\n<|COMPLETE|>")

        def event(self, *args, **kwargs):
            pass

    async def run():
        corpus = tiny_corpus(tmp_path)
        planned = plan_chunks(corpus, CharacterTokenizer())
        runtime = TestRuntime()
        light = FrameworkAdapter(corpus, runtime, tmp_path / "light", planned)
        anything = FrameworkAdapter(corpus, runtime, tmp_path / "anything", planned,
                                    checkpoint=light.storage / light.workspace)
        try:
            await light.build()
            await anything.build()
            q = "What do Cu and SiCN form?"
            expected = list(corpus.records.values())
            assert await light.retrieve(q) == expected
            assert await anything.retrieve(q) == expected
            assert light.metadata["graph"] == anything.metadata["graph"] == {"nodes": 2, "edges": 1}
        finally:
            await anything.close()
            await light.close()

    asyncio.run(run())


def test_continuation_copies_original_records_and_charges_the_previous_ledger(tmp_path):
    prior = tmp_path / "original"
    target = tmp_path / "continuation"
    questions = [{"id": "dev-01", "question": "question"}]
    carried_result = {**questions[0], "system": "current", "status": "failed", "error": "fixed schema failure"}
    original = prior / "current" / "queries" / "dev-01.json"
    write_json(original, carried_result)
    original_code = {"scripts/rag_comparison_20261116/harness.py": "old", "unchanged.py": "stable"}
    config = {"source_sha256": original_code, "provider": {"model": "unchanged"}}
    write_json(prior / "config.json", config)
    write_json(prior / "questions.json", questions)
    write_json(prior / "run.json", {"run_id": "smoke-01", "status": "failed", "failed_during": "lightrag",
                                    "failure_type": "TypeError", "production_integrity": "verified",
                                    "failure": "cannot pickle '_thread.RLock' object",
                                    "systems": {"current": {"queries": [carried_result]}},
                                    "llm_attempts": 1, "token_budget_consumed": 4})
    attributed = {"system": "current", "stage": "answer", "query_id": "dev-01", "call_id": "0001"}
    append_jsonl(prior / "events.jsonl", {**attributed, "event": "llm_attempt", "reservation": 42})
    append_jsonl(prior / "events.jsonl", {**attributed, "event": "llm", "usage": {"total_tokens": 4}})
    current, carryover = carry_forward_current(
        prior, target, {**config, "source_sha256": {**original_code,
                                                   "scripts/rag_comparison_20261116/harness.py": "new"}},
        questions)
    assert current["reused_from_run"] == "smoke-01"
    assert current["queries"][0]["status"] == "failed"
    assert carryover["carryover_attempts"] == 1 and carryover["carryover_budget_tokens"] == 4
    assert carryover["copied_current_queries_sha256"]["current/queries/dev-01.json"] == sha256_file(original)
    assert (target / "current" / "queries" / "dev-01.json").read_bytes() == original.read_bytes()
    with pytest.raises(ValueError, match="input/model/prompt"):
        carry_forward_current(prior, tmp_path / "bad", {**config, "provider": {"model": "new"}}, questions)
