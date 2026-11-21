"""Execute this collection campaign through the normal Codex ingestion pipeline.

Downloaded files and source manifests are inputs, never commands. Publication is
explicit so the first mixed-source batch can be inspected before expanding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot import ingestion as intake
from kg_pilot import ingestion_runners
from kg_pilot.ingestion_publish import publish_batch
from kg_pilot.pipeline import PILOT

COLLECTION = PILOT / "collection/expansion_20261103"
REPORTS = PILOT / "reports/expansion_20261103"


def sources():
    rows = []
    for group in ("papers", "industry", "standards"):
        manifest = COLLECTION / group / "manifest.json"
        if manifest.exists():
            for row in json.loads(manifest.read_text(encoding="utf-8-sig"))["documents"]:
                rows.append({**row, "collection_group": group})
    return rows


def create(ids, name, max_claims=12):
    records = {row["id"]: row for row in sources()}
    selected = [records[key] for key in ids]
    files, provenance = [], []
    for row in selected:
        path = Path(row["local_path"]).resolve()
        if not path.is_relative_to(COLLECTION.resolve()):
            raise ValueError("Collection file must stay within this campaign.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("Source hash changed: " + row["id"])
        files.append((path.name, data))
        route = row.get("access_route") or ("company_shared" if row["collection_group"] == "industry" else "other")
        provenance.append({**row, "access_route": route})
    batch = intake.create_batch(files, mode="codex", max_claims=max_claims,
                               auto_publish=False, provenance=provenance)
    REPORTS.mkdir(parents=True, exist_ok=True)
    intake.write_atomic(REPORTS / (name + "_selection.json"), {
        "name": name, "batch_id": batch["id"], "source_ids": ids,
        "mode": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "ultra",
        "sources": selected})
    return batch


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    selection = commands.add_parser("create")
    selection.add_argument("name")
    selection.add_argument("ids", nargs="+")
    selection.add_argument("--max-claims", type=int, default=12)
    for name in ("run", "publish", "status"):
        command = commands.add_parser(name)
        command.add_argument("batch_id")
        if name == "run":
            command.add_argument("--reuse-echo-drafts", action="store_true")
    commands.add_parser("inventory")
    args = parser.parse_args()
    if args.action == "create":
        result = create(args.ids, args.name, args.max_claims)
    elif args.action == "run":
        def campaign_runner(mode, task, packet, workdir):
            # The initial trial exposed a harmless exact echo of input source
            # metadata. Revalidate those actual saved CLI drafts, preserving the
            # original bytes/hash. Semantic rejections always get a fresh draft.
            if args.reuse_echo_drafts and task == "extract" and not (workdir / "review_feedback.json").exists():
                for path in sorted(workdir.glob("extract-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    try:
                        raw = path.read_text(encoding="utf-8-sig")
                        metadata = json.loads(raw).get("metadata", {})
                        if "provenance" not in metadata:
                            continue
                        value = ingestion_runners._parse_result(raw, task, packet)
                    except (ValueError, ingestion_runners.RunnerError):
                        continue
                    value["_result_meta"] = {"mode": "codex", "task": task, "model": "gpt-6.1-sol",
                        "reasoning_effort": "ultra", "reused_cli_draft": str(path),
                        "raw_output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "normalization": "Removed only exact echo of input provenance; all evidence and fields revalidated."}
                    return value
            return ingestion_runners.run_task(mode, task, packet, workdir)
        result = intake.run_batch(args.batch_id, workers=3, runner=campaign_runner)
    elif args.action == "publish":
        state = intake.get_batch(args.batch_id)
        if state["status"] != "ready":
            raise ValueError("Only a reviewed, ready batch can be published.")
        result = intake.run_batch(args.batch_id, publish_only=True)
    elif args.action == "status":
        result = intake.get_batch(args.batch_id)
    else:
        catalog = json.loads((PILOT / "catalog/papers.json").read_text(encoding="utf-8-sig"))
        known = {row["sha256"] for row in catalog}
        result = {"published": len(catalog), "collected": len(sources()),
                  "not_yet_published": [{"id": row["id"], "title": row["title"], "pages": row["page_count"], "group": row["collection_group"]}
                                        for row in sources() if row["sha256"] not in known]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
