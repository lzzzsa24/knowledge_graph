"""Run only the frozen six-document, ten-question controlled development pilot."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
import importlib.metadata
from pathlib import Path
import re
import shutil
import sys
import time
import traceback

from .common import (ANSWER_TOKENS, BENCHMARK, CONTEXT_TOKENS, EXTRACTION_REVISION_TOKENS,
                     GRAPH_TOKENS, INPUTS, PILOT,
                     REQUEST_TIMEOUT, RETRY_DELAYS, ROOT, SYSTEM_PROMPT, TEMPERATURE,
                     BudgetExceeded, Corpus, Runtime, answer_prompt, message_digest, pack_context, read_json,
                     read_jsonl, sha256_file, sha256_text, utc, validate_answer, write_json)
from .framework_adapters import (FRAMEWORK_SETTINGS, QUERY_PARAMETERS, FrameworkAdapter,
                                 merge_native_duplicates, plan_chunks)

sys.path.insert(0, str(ROOT / "src"))

MAX_CALLS = 400
MAX_TOKENS = 2_000_000
SYSTEMS = ("current", "lightrag", "raganything")
VERSIONS = ("lightrag-hku", "raganything", "sentence-transformers", "torch", "transformers",
            "tokenizers", "httpx", "tiktoken", "qdrant-client", "networkx", "psutil")
DEPENDENCIES = (
    "src/kg_pilot/qa.py", "src/kg_pilot/retrieval.py", "src/kg_pilot/pipeline.py",
    "src/kg_pilot/ingestion_publish.py", "src/kg_pilot/effective_evidence.py",
    "src/kg_pilot/embedding_chunks.py", "src/kg_pilot/llm_provider.py",
    "src/kg_pilot/map_relations.py",
)
RECOVERY_SOURCE_FILES = {"scripts/rag_comparison_20261116/harness.py",
                         "scripts/rag_comparison_20261116/framework_adapters.py",
                         "scripts/rag_comparison_20261116/common.py"}
PLAN_REPORT_FIELDS = {"pre_dedup_chunk_slots", "planned_chunks", "planned_primary_extraction_calls"}


def verify_frozen(path):
    manifest = read_json(path / "manifest.json")
    public_digests = {name: sha256_file(path / name) for name in INPUTS}
    for name in INPUTS[1:]:
        declared = {**manifest["shared_hashes"],
                    "questions.dev.json": manifest["question_hashes"]["questions.dev.json"]}[name]
        if public_digests[name] != declared:
            raise ValueError(f"Frozen input content differs from manifest for {name}.")
    focus = set(manifest["pilot_doc_ids"])
    source_hashes = {}
    for document in manifest["documents"]:
        if document["doc_id"] in focus:
            pdf = (path / document["original_pdf"]).resolve()
            if not pdf.is_relative_to(path.resolve()):
                raise ValueError("Frozen input PDF path escapes its snapshot.")
            digest = sha256_file(pdf)
            if digest != document["original_sha256"]:
                raise ValueError(f"Frozen PDF hash mismatch for {document['doc_id']}.")
            source_hashes[document["doc_id"]] = digest
    expected = manifest["pilot_question_ids"]
    rows = read_json(path / "questions.dev.json")
    selected = {row["id"]: row for row in rows if row.get("smoke") and row.get("split") == "dev"}
    if len(expected) != 10 or len(set(expected)) != 10 or set(expected) != set(selected):
        raise ValueError("The smoke subset is inconsistent with the ten frozen development questions.")
    if any(not set(row["doc_ids"]).issubset(focus) for row in selected.values()):
        raise ValueError("A smoke question requires a document outside the six-document subset.")
    questions = [{"id": qid, "question": selected[qid]["question"]} for qid in expected]
    return questions, public_digests, source_hashes


def code_digests():
    sources = [Path(__file__).with_name(name) for name in
               ("harness.py", "common.py", "framework_adapters.py", "baseline_adapter.py")]
    sources.extend(ROOT / name for name in DEPENDENCIES)
    return {path.relative_to(ROOT).as_posix(): sha256_file(path) for path in sources}


def resource_snapshot(directory):
    import psutil

    memory = psutil.Process().memory_info()
    files = [p for p in directory.rglob("*") if p.is_file()]
    return {"process_rss_bytes": memory.rss,
            "process_peak_rss_bytes": getattr(memory, "peak_wset", None),
            "artifact_files": len(files), "artifact_bytes": sum(p.stat().st_size for p in files)}


def production_drift(before):
    changed = []
    for name, value in before.items():
        local = ROOT / name
        if not local.is_file() or sha256_file(local) != value["sha256"]:
            changed.append(name)
    return changed


def usage_summary(output):
    path = output / "events.jsonl"
    usage_fields = ("prompt_tokens", "completion_tokens", "total_tokens",
                    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens")
    grouped = {}
    for event in read_jsonl(path) if path.exists() else []:
        if event["event"] != "llm":
            continue
        group = grouped.setdefault(f"{event['system']}/{event['stage']}",
                                   {"attempts": 0, "failures": 0, "retries": 0,
                                    "usage_reports": 0, "duration_seconds": 0, "usage_tokens": {}})
        group["attempts"] += 1
        group["failures"] += bool(event["error"])
        group["retries"] += event["retry"] > 0
        group["duration_seconds"] += event["seconds"]
        usage = event["usage"]
        if isinstance(usage, dict):
            group["usage_reports"] += 1
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and type(details.get("cached_tokens")) is int:
                usage = {"cached_tokens": details["cached_tokens"], **usage}
            for key in usage_fields:
                if type(usage.get(key)) is int:
                    group["usage_tokens"][key] = group["usage_tokens"].get(key, 0) + usage[key]
    return grouped


async def build_adapter(name, adapter, runtime, state, save):
    out = runtime.out / name
    began = time.perf_counter()
    row = state["systems"][name]
    row.update(status="building", started_utc=utc(), resources_before=resource_snapshot(out))
    save()
    with runtime.stage(name, "checkpoint_clone" if name == "raganything" else "build"):
        await adapter.build()
    row.update(status="ready", setup_seconds=round(time.perf_counter() - began, 3),
               resources_after_setup=resource_snapshot(out), metadata=adapter.metadata)
    if name != "raganything":
        row["build_seconds"] = row["setup_seconds"]
    save()


async def answer_all(name, adapter, corpus, questions, runtime, state, save):
    row = state["systems"][name]
    for question in questions:
        qid = question["id"]
        result = {**question, "system": name, "status": "running"}
        started = time.perf_counter()
        filename = runtime.out / name / "queries" / f"{qid}.json"
        row["queries"].append({"id": qid, "status": "running"})
        save()
        try:
            with runtime.stage(name, "retrieve", qid):
                selected = await adapter.retrieve(question["question"])
            if runtime.stopped:
                raise BudgetExceeded("Pre-run API budget reached during retrieval.")
            result["retrieve_seconds"] = round(time.perf_counter() - started, 3)
            result["retrieved"] = [asdict(corpus.records[x] if isinstance(x, str) else x) for x in selected]
            context, included, token_count = pack_context(corpus, selected, runtime.countable_tokens)
            result.update(context=context, included=included, context_proxy_tokens=token_count)
            write_json(filename, result)
            generation_start = time.perf_counter()
            with runtime.stage(name, "answer", qid):
                content = await runtime.complete(answer_prompt(question["question"], context), SYSTEM_PROMPT)
            result["answer_seconds"] = round(time.perf_counter() - generation_start, 3)
            result["raw_content"] = content
            result["answer"] = validate_answer(content, included)
            result["status"] = "completed"
        except BudgetExceeded:
            result["status"] = "budget_stopped"
            raise
        except Exception as error:
            result.update(status="failed", error_type=type(error).__name__, error=runtime.clean(str(error)))
        finally:
            result["seconds"] = round(time.perf_counter() - started, 3)
            write_json(filename, result)
            row["queries"][-1].update(status=result["status"], seconds=result["seconds"],
                                      context_proxy_tokens=result.get("context_proxy_tokens"))
            save()
            print(f"{name} {qid} {result['status']} {result['seconds']}s", flush=True)
    row["resources_after_queries"] = resource_snapshot(runtime.out / name)
    row["status"] = ("completed" if all(q["status"] == "completed" for q in row["queries"])
                     else "incomplete")
    save()


def check_equivalence(out, questions):
    comparisons = []
    for row in questions:
        qid = row["id"]
        b, c = (out / name / "queries" / f"{qid}.json" for name in ("lightrag", "raganything"))
        if not b.exists() or not c.exists():
            continue
        left, right = read_json(b), read_json(c)
        if "retrieved" in left and "retrieved" in right:
            comparisons.append({"id": qid,
                                "same_ordered_retrieval": left["retrieved"] == right["retrieved"],
                                "same_delivered_context": (left["context"] == right["context"]
                                                           if "context" in left and "context" in right else None)})
    return comparisons


async def close_adapter(adapter):
    close = getattr(adapter, "close", None)
    if close is not None:
        await close()


def carry_forward_current(previous, out, config, questions):
    """Resume the observed framework startup defect without replaying any paid current-arm work."""
    prior = read_json(previous / "run.json")
    prior_config = read_json(previous / "config.json")
    if (prior.get("run_id"), prior.get("status"), prior.get("failed_during"), prior.get("failure_type"),
            prior.get("production_integrity")) != ("smoke-01", "failed", "lightrag", "TypeError", "verified"):
        raise ValueError("The specified original run is not the verified LightRAG startup failure.")
    if "cannot pickle '_thread.RLock' object" not in prior.get("failure", ""):
        raise ValueError("The original failure differs from the framework callback deep-copy defect.")
    allowed_updates = PLAN_REPORT_FIELDS | {"source_sha256"}
    if {k: v for k, v in prior_config.items() if k not in allowed_updates} != {
        k: v for k, v in config.items() if k not in allowed_updates
    }:
        raise ValueError("The recovery configuration differs from the original input/model/prompt contract.")
    if prior_config.get("planned_chunks") != config.get("pre_dedup_chunk_slots"):
        raise ValueError("Pre-deduplication slots differ from the original frozen chunk plan.")
    original_codes = {k: v for k, v in prior_config["source_sha256"].items() if k not in RECOVERY_SOURCE_FILES}
    recovery_codes = {k: v for k, v in config["source_sha256"].items() if k not in RECOVERY_SOURCE_FILES}
    if original_codes != recovery_codes:
        raise ValueError("Source changes outside the framework binding and continuation are not permitted.")
    if read_json(previous / "questions.json") != questions:
        raise ValueError("The recovery questions differ from the original frozen pilot.")
    events = read_jsonl(previous / "events.jsonl")
    started = [e for e in events if e["event"] == "llm_attempt"]
    settled = [e for e in events if e["event"] == "llm"]
    expected_queries = [q["id"] for q in questions]
    current = prior["systems"]["current"]
    if ([row["id"] for row in current["queries"]] != expected_queries
            or any(row["status"] not in {"completed", "failed"} for row in current["queries"])
            or len(started) != len(settled) or len(settled) != prior["llm_attempts"]
            or [e["call_id"] for e in started] != [e["call_id"] for e in settled]
            or [e["query_id"] for e in settled] != expected_queries
            or any((e["system"], e["stage"]) != ("current", "answer") for e in settled)):
        raise ValueError("The original paid request ledger is not a complete isolated current-arm result.")
    charged = 0
    for sent, done in zip(started, settled):
        tokens = (done.get("usage") or {}).get("total_tokens")
        charged += tokens if type(tokens) is int and tokens > 0 else sent["reservation"]
    if charged != prior["token_budget_consumed"]:
        raise ValueError("The original run's budget ledger does not reconcile.")
    hashes = {}
    target = out / "current" / "queries"
    target.mkdir(parents=True)
    for question, query_state in zip(questions, current["queries"]):
        filename = f"{question['id']}.json"
        source = previous / "current" / "queries" / filename
        result = read_json(source)
        if ((result["id"], result["question"], result["system"], result["status"])
                != (question["id"], question["question"], "current", query_state["status"])):
            raise ValueError(f"Original query record does not match its ledger: {filename}.")
        digest = sha256_file(source)
        shutil.copyfile(source, target / filename)
        if sha256_file(target / filename) != digest:
            raise ValueError(f"Copied query result hash mismatch: {filename}.")
        hashes[f"current/queries/{filename}"] = digest
    previous_hashes = {file: sha256_file(previous / file)
                       for file in ("run.json", "config.json", "questions.json", "events.jsonl")}
    return {**current, "reused_from_run": prior["run_id"]}, {
        "original_run": prior["run_id"], "original_directory": str(previous.resolve()),
        "original_provenance_sha256": previous_hashes, "copied_current_queries_sha256": hashes,
        "original_setup_error": "LightRAG asdict deep-copied bound callbacks into the runtime thread lock.",
        "recovery_changes": "Forwarding-function callbacks, identical native chunk coalescing with source aliases retained, and continuation of the current arm.",
        "carryover_attempts": len(settled), "carryover_budget_tokens": charged,
    }


def check_zero_call_run(path, carryover):
    prior = read_json(path / "run.json")
    prior_plan = read_json(path / "config.json")
    continuation = prior.get("continuation", {})
    if ((prior.get("run_id"), prior.get("status"), prior.get("failure_type"), prior.get("production_integrity"))
            != ("smoke-02", "failed", "ValueError", "verified")
            or "Ambiguous preplanned chunk content" not in prior.get("failure", "")
            or prior.get("llm_attempts_in_this_run") != 0 or prior.get("token_budget_consumed_in_this_run") != 0
            or prior.get("model_usage") != {}
            or prior_plan.get("continuation") != continuation):
        raise ValueError("The intermediate run is not the verified zero-call chunk-identity failure.")
    for field in ("original_provenance_sha256", "copied_current_queries_sha256",
                  "carryover_attempts", "carryover_budget_tokens"):
        if continuation.get(field) != carryover[field]:
            raise ValueError(f"Intermediate retry no longer agrees with the original ledger: {field}.")
    events = path / "events.jsonl"
    if ((events.exists() and any(e["event"] in {"llm_attempt", "llm"} for e in read_jsonl(events)))
            or any((path / "model_calls").glob("*.request.json"))):
        raise ValueError("Unexpected provider request artifacts exist in the intermediate run.")
    return {"run_id": prior["run_id"], "run_sha256": sha256_file(path / "run.json"),
            "config_sha256": sha256_file(path / "config.json"), "new_calls": 0, "new_charged_tokens": 0}


def verify_partial_build(path, config, carryover):
    """Validate the real partial graph and map each accepted charge to its native cache row."""
    from lightrag.utils import remove_think_tags

    prior = read_json(path / "run.json")
    prior_config = read_json(path / "config.json")
    store = path / "lightrag/storage/controlled_lightrag"
    immutable = {"source_sha256", "continuation"}
    def unchanged(value):
        return {key: val for key, val in value.items() if key not in immutable}

    if unchanged(prior_config) != unchanged(config):
        raise ValueError("Partial graph metadata does not match the current frozen pre-run contract.")
    def sources(value):
        return {key: val for key, val in value["source_sha256"].items() if key not in RECOVERY_SOURCE_FILES}

    if sources(prior_config) != sources(config):
        raise ValueError("Non-recovery dependencies changed after the original graph build.")
    if ((prior.get("run_id"), prior.get("status"), prior.get("failed_during"), prior.get("production_integrity"))
            != ("smoke-03", "failed", "lightrag", "verified")
            or prior.get("continuation") != prior_config.get("continuation")):
        raise ValueError("The prior run is not the verified partial native build.")
    for name in ("original_provenance_sha256", "copied_current_queries_sha256", "carryover_attempts",
                 "carryover_budget_tokens", "intermediate_zero_call_failure"):
        if prior["continuation"].get(name) != carryover[name]:
            raise ValueError(f"The partial build disagrees with the original paid query ledger: {name}.")
    events = read_jsonl(path / "events.jsonl")
    sent = [row for row in events if row["event"] == "llm_attempt"]
    done = [row for row in events if row["event"] == "llm"]
    if (len(sent) != len(done) or len(done) != prior["llm_attempts_in_this_run"]
            or [row["call_id"] for row in sent] != [row["call_id"] for row in done]
            or any((row["system"], row["stage"]) != ("lightrag", "build") for row in done)
            or any(not isinstance(row.get("usage", {}).get("total_tokens"), int) for row in done)):
        raise ValueError("The partial build ledger is incomplete or includes other stages.")
    charged = sum(row["usage"]["total_tokens"] for row in done)
    if (charged != prior["token_budget_consumed_in_this_run"]
            or prior["llm_attempts"] != carryover["carryover_attempts"] + len(done)
            or prior["token_budget_consumed"] != carryover["carryover_budget_tokens"] + charged):
        raise ValueError("Partial build charges are inconsistent with cumulative totals.")
    errors = [row for row in done if row["error"]]
    if len(errors) != 1 or errors[0]["error"] != "output_truncated" or errors[0] != done[-1]:
        raise ValueError("The partial build did not stop on a single output truncation.")
    failure_call = errors[0]["call_id"]
    cache = read_json(store / "kv_store_llm_response_cache.json")
    accepted = {row["original_prompt"]: row for row in cache.values() if row["cache_type"] == "extract"}
    if len(accepted) != len(cache) or len(cache) != len(done) - 1:
        raise ValueError("The original native extraction cache does not match the successful request count.")
    paid_prompts = set()
    for row in done:
        call_id = row["call_id"]
        request = read_json(path / "model_calls" / f"{call_id}.request.json")
        response = read_json(path / "model_calls" / f"{call_id}.response.txt")
        choice = response["choices"][0]
        messages = request["messages"]
        if (request["max_tokens"] != GRAPH_TOKENS
                or [message["role"] for message in messages] != ["system", "user"]
                or response.get("usage") != row["usage"]):
            raise ValueError(f"Original extraction trace differs from its expected profile: {call_id}.")
        key = messages[1]["content"] + "\n" + messages[0]["content"]
        if row["error"]:
            if choice["finish_reason"] != "length" or key in accepted:
                raise ValueError("The truncated response must never be reused as accepted extraction.")
        else:
            cached = accepted.pop(key, None)
            if not cached or cached["return"] != remove_think_tags(choice["message"]["content"]):
                raise ValueError(f"No exact native cache response for successful extraction {call_id}.")
            paid_prompts.add(message_digest(messages))
    if accepted or len(paid_prompts) != len(cache):
        raise ValueError("Not all native cache entries were attributed to unique paid requests.")
    statuses = read_json(store / "kv_store_doc_status.json")
    status_map = {doc: row["status"] for doc, row in statuses.items()}
    if (list(status_map.values()) != ["processed", "processed", "processed", "failed"]
            or not set(status_map).issubset(config["planned_chunks"])
            or any(len(row["chunks_list"]) != config["planned_chunks"][doc]
                   for doc, row in statuses.items())):
        raise ValueError("The partial graph does not contain the expected processed and failed documents.")
    return store, paid_prompts, {
        "run_id": prior["run_id"], "run_sha256": sha256_file(path / "run.json"),
        "config_sha256": sha256_file(path / "config.json"), "events_sha256": sha256_file(path / "events.jsonl"),
        "new_calls": len(done), "new_charged_tokens": charged,
        "cumulative_calls": prior["llm_attempts"], "cumulative_charged_tokens": prior["token_budget_consumed"],
        "original_extraction_max_output_tokens": GRAPH_TOKENS,
        "truncated_call": failure_call, "truncated_response_sha256": sha256_file(
            path / "model_calls" / f"{failure_call}.response.txt"),
        "validated_cache_entries": len(cache), "document_statuses": status_map,
        "completed_graph_chunks": sum(len(row["chunks_list"]) for row in statuses.values()
                                      if row["status"] == "processed"),
    }


async def run(args, out):
    from .baseline_adapter import CurrentAdapter

    started = time.perf_counter()
    state = {"run_id": args.run_id, "started_utc": utc(), "status": "preparing",
             "systems": {name: {"status": "pending", "queries": []} for name in SYSTEMS}}

    def save():
        write_json(out / "run.json", state)

    save()
    runtime = None
    active_name = "setup"
    jobs = {}
    try:
        frozen = BENCHMARK / "frozen"
        questions, inputs, pdfs = verify_frozen(frozen)
        manifest = read_json(frozen / "manifest.json")
        drift = production_drift(manifest["production_fingerprint"])
        if drift:
            raise ValueError(f"Frozen production fingerprint differs before run: {drift!r}")
        corpus = Corpus.from_frozen(frozen)
        runtime = Runtime(out, execute=args.execute, max_calls=args.max_calls, max_tokens=args.max_tokens)
        chunk_slots = plan_chunks(corpus, runtime.embedding_tokenizer)
        planned = merge_native_duplicates(chunk_slots)
        chunk_counts = {doc: len(chunks) for doc, chunks in planned.items()}
        runtime.countable_tokens("校验")
        dependency_hashes = code_digests()
        config = {
            "scope": "controlled_text_table_development_pre_run",
            "provider": runtime.model_config, "production_fingerprint": manifest["production_fingerprint"],
            "frozen_input_sha256": inputs, "frozen_pilot_pdf_sha256": pdfs,
            "source_sha256": dependency_hashes, "embedding_profile": runtime.embedding_config,
            "package_versions": {package: importlib.metadata.version(package) for package in VERSIONS},
            "temperature": TEMPERATURE, "answer_max_output_tokens": ANSWER_TOKENS,
            "graph_max_output_tokens": GRAPH_TOKENS,
            "answer_response_format": {"type": "json_object"},
            "paratera_v4_thinking": {"type": "disabled"},
            "answer_system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
            "answer_context_proxy_tokenizer": "tiktoken o200k_base",
            "answer_context_proxy_token_max": CONTEXT_TOKENS,
            "retries": {"max_retries": len(RETRY_DELAYS), "delays_seconds": list(RETRY_DELAYS),
                        "request_timeout_seconds": REQUEST_TIMEOUT},
            "limits": {"max_calls": args.max_calls, "max_token_budget": args.max_tokens,
                       "reservation": "UTF-8 request message bytes + 256 + max output; observed usage settles successful reservations"},
            "framework_build": FRAMEWORK_SETTINGS, "framework_query": QUERY_PARAMETERS,
            "framework_equivalence_design": "Native LightRAG build once. RAGAnything independently opens a byte-verified clone from before either framework query.",
            "execution_order": ["current_build", "current_10_questions", "lightrag_native_build",
                                "raganything_checkpoint_clone", "lightrag_10_questions", "raganything_10_questions"],
            "timing_note": "Serial grouped pre-run; order/warm-up may affect latency. Encoder load is logged separately.",
            "evidence_counts": dict(Counter(r.kind for r in corpus.records.values())),
            "pre_dedup_chunk_slots": {doc: len(chunks) for doc, chunks in chunk_slots.items()},
            "planned_chunks": chunk_counts, "planned_primary_extraction_calls": sum(chunk_counts.values()),
            "planned_question_count": len(questions), "question_ids": [q["id"] for q in questions],
        }
        carryover = None
        resume_checkpoint = None
        if args.continue_from:
            current_state, carryover = carry_forward_current(
                BENCHMARK / "runs" / args.continue_from, out, config, questions,
            )
            carryover["intermediate_zero_call_failure"] = check_zero_call_run(
                BENCHMARK / "runs" / "smoke-02", carryover,
            )
            resume_checkpoint, paid_prompts, partial = verify_partial_build(
                BENCHMARK / "runs" / "smoke-03", config, carryover,
            )
            carryover["current_paid_attempts"] = carryover["carryover_attempts"]
            carryover["current_paid_tokens"] = carryover["carryover_budget_tokens"]
            carryover["carryover_attempts"] = partial["cumulative_calls"]
            carryover["carryover_budget_tokens"] = partial["cumulative_charged_tokens"]
            carryover["partial_native_build"] = partial
            config["extraction_capacity_revision"] = {
                "prior_max_output_tokens": GRAPH_TOKENS, "continued_max_output_tokens": EXTRACTION_REVISION_TOKENS,
                "original_truncated_call": partial["truncated_call"], "scope": "native entity/relation extraction only",
            }
            runtime.build_extraction_max_tokens = EXTRACTION_REVISION_TOKENS
            runtime.previously_paid_extraction_prompts = frozenset(paid_prompts)
            config["continuation"] = carryover
            state["continuation"] = carryover
            state["systems"]["current"] = current_state
            runtime.attempts = carryover["carryover_attempts"]
            runtime.budget_charged_tokens = carryover["carryover_budget_tokens"]
        write_json(out / "config.json", config)
        write_json(out / "questions.json", questions)
        (out / "answer_prompt.txt").write_text(SYSTEM_PROMPT + "\n", encoding="utf8")
        total = sum(chunk_counts.values())
        print(f"Prepared 6 documents, {len(questions)} questions, {total} native graph chunks.", flush=True)
        if not args.execute:
            state["status"] = "dry_run"
            return 0
        minimum_calls = total + 5 * len(questions)
        if carryover:
            minimum_calls = (runtime.attempts + total - partial["validated_cache_entries"] + 4 * len(questions))
        if args.max_calls < minimum_calls:
            raise ValueError(f"Call budget {args.max_calls} is less than {minimum_calls} planned build/answer calls.")
        if inputs != {name: sha256_file(frozen / name) for name in INPUTS} or dependency_hashes != code_digests():
            raise ValueError("Code or frozen inputs changed during preflight.")
        runtime.open_api()
        state["status"] = "running"
        save()
        if carryover is None:
            jobs["current"] = CurrentAdapter(corpus, runtime, out / "current")
            active_name = "current"
            await build_adapter(active_name, jobs[active_name], runtime, state, save)
            await answer_all(active_name, jobs[active_name], corpus, questions, runtime, state, save)
            await close_adapter(jobs.pop(active_name))
        else:
            print(f"Reused {len(questions)} current-arm records from {args.continue_from}.",
                  flush=True)

        light = FrameworkAdapter(corpus, runtime, out / "lightrag", planned, resume_from=resume_checkpoint)
        jobs["lightrag"] = light
        active_name = "lightrag"
        await build_adapter(active_name, light, runtime, state, save)
        checkpoint = light.storage / light.workspace

        active_name = "raganything"
        anything = FrameworkAdapter(corpus, runtime, out / "raganything", planned, checkpoint=checkpoint)
        jobs[active_name] = anything
        await build_adapter(active_name, anything, runtime, state, save)
        for active_name in ("lightrag", "raganything"):
            await answer_all(active_name, jobs[active_name], corpus, questions, runtime, state, save)
            await close_adapter(jobs.pop(active_name))
        state["framework_text_equivalence"] = check_equivalence(out, questions)
        state["status"] = ("completed" if all(st["status"] == "completed" for st in state["systems"].values())
                           else "incomplete")
    except (Exception, KeyboardInterrupt) as error:
        reason = traceback.format_exc()
        state.update(status="budget_stopped" if isinstance(error, BudgetExceeded) or (runtime and runtime.stopped)
                     else "failed", failed_during=active_name, failure_type=type(error).__name__,
                     failure=runtime.clean(reason) if runtime else reason)
        print(f"Run {state['status']} during {active_name}. See run.json.", flush=True)
        return 2
    finally:
        try:
            for name, adapter in jobs.items():
                try:
                    await close_adapter(adapter)
                except Exception as error:
                    state.setdefault("close_errors", {})[name] = type(error).__name__
            if runtime:
                await runtime.close()
                state["llm_attempts"] = runtime.attempts
                state["token_budget_consumed"] = runtime.budget_charged_tokens
                if "continuation" in state:
                    state["llm_attempts_in_this_run"] = runtime.attempts - state["continuation"]["carryover_attempts"]
                    state["token_budget_consumed_in_this_run"] = (
                        runtime.budget_charged_tokens - state["continuation"]["carryover_budget_tokens"]
                    )
            outcomes = [query["status"] for system in state["systems"].values() for query in system["queries"]]
            state["all_30_queries_attempted"] = len(outcomes) == 30
            state["all_30_answers_contract_valid"] = len(outcomes) == 30 and all(x == "completed" for x in outcomes)
            state["model_usage"] = usage_summary(out)
            if args.execute and "manifest" in locals():
                state["production_drift"] = production_drift(manifest["production_fingerprint"])
                state["production_integrity"] = "changed" if state["production_drift"] else "verified"
                if state["production_drift"]:
                    state["status_before_integrity_check"] = state["status"]
                    state["status"] = "invalid_integrity"
            state["resources_at_end"] = resource_snapshot(out)
        finally:
            state.update(finished_utc=utc(), seconds=round(time.perf_counter() - started, 3))
            save()
    return 0 if state["status"] == "completed" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    parser.add_argument("--continue-from", choices=("smoke-01",))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.run_id):
        parser.error("Run id must use 1–64 safe ASCII characters.")
    if not 1 <= args.max_calls <= MAX_CALLS or not 1 <= args.max_tokens <= MAX_TOKENS:
        parser.error(f"Limits must be positive and no higher than {MAX_CALLS} calls and {MAX_TOKENS} tokens.")
    if args.continue_from and (not args.execute or args.run_id != "smoke-04"):
        parser.error("The original pilot can be continued only in smoke-04 with --execute.")
    out = BENCHMARK / "runs" / args.run_id
    out.mkdir(parents=True, exist_ok=False)
    print(f"Run artifacts: {out}", flush=True)
    return asyncio.run(run(args, out))


if __name__ == "__main__":
    raise SystemExit(main())
