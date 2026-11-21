"""Exercise real local innovation generation; use the app's existing provider config.

Only synthetic research prompts and public corpus evidence are written to the report.
This script never reads or prints the credential configuration.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "random": {"prompt": "", "count": 1},
    "guided": {"prompt": "围绕低温Cu/介质混合键合中的CMP表面形貌与退火条件，提出可通过小规模对照实验验证的研究方向。", "count": 1},
    "regression": {"prompt": "围绕《High-speed ultra-accurate direct C2W bonding》的Si/SiO2直接C2W键合吞吐、双键合头与工艺节拍，提出进一步可验证的研究点。", "count": 1},
    "citation_regression": {"prompt": "围绕《Constitutive Model of Secondary Annealing Behavior of Cu-Cu Joints in Cu/SiO2 Hybrid Bonding》中的二次退火与机械性能，提出一个可验证的研究方向。", "count": 1},
    "conversation": {"prompt": "", "count": 1, "history": [
        {"role": "user", "content": "我更关注混合键合前的表面清洗和活化。实验条件以表面粗糙度、接触角测量为主。"},
        {"role": "assistant", "content": "可以讨论活化后的等待时间与表面状态之间的关系，这只是待验证的设想。"},
        {"role": "user", "content": "希望先做低成本实验，不依赖完整晶圆产线。"},
    ]},
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "pilot/reports/innovation_20261105" / f"live_{args.case}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with httpx.Client(base_url="http://127.0.0.1:8600", timeout=420, trust_env=False) as client:
        response = client.post("/api/ideas", json=CASES[args.case])
    try:
        result = response.json()
    except ValueError:
        result = {"error": "Local server returned non-JSON content"}
    report = {"case": args.case, "at": datetime.now(timezone.utc).isoformat(),
              "request": CASES[args.case], "http_status": response.status_code,
              "elapsed_seconds": round(time.monotonic() - started, 2), "response": result,
              "scope": "Real provider integration check; does not establish novelty or scientific validity."}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"case": args.case, "http_status": response.status_code,
                      "mode": result.get("mode"), "ideas": len(result.get("ideas", [])),
                      "sources": len(result.get("sources", [])), "generated": result.get("generated"),
                      "elapsed_seconds": report["elapsed_seconds"], "report": str(output)}, ensure_ascii=False))
    if response.status_code != 200 or not result.get("generated"):
        raise SystemExit(1)
    assert result["mode"] == ("random" if args.case == "random" else "guided")
    assert len(result["ideas"]) == 1
    source_ids = {source["id"] for source in result["sources"]}
    for idea in result["ideas"]:
        assert idea["source_ids"] and set(idea["source_ids"]).issubset(source_ids)
        assert all(idea.get(field) for field in ("title", "question", "hypothesis", "basis", "novelty", "validation", "risks", "search_queries"))


if __name__ == "__main__":
    main()
