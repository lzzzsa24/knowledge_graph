"""Run six real application questions, preserving answers without declaring accuracy."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = [
    "AEC-Q104适用于什么对象？能直接作为混合键合裸界面的验收标准吗？",
    "Bruker的混合键合表面计量资料中，晶圆测量吞吐量的数据适用于哪些条件？",
    "NPL指南为什么说Ra不能完整描述表面形状？",
    "《Chemical Mechanical Polishing of Plasma-Modified Cu/Polymer Interfaces for Advanced Hybrid Bonding》实际键合试样是什么尺寸，属于整片晶圆还是切割后的芯片？",
    "TEL资料中140 nm pitch键合结果和14.6 nm对准误差分别是实测还是仿真？是否已经完成140 nm间距的电学验证？",
    "DuPont的《Tailored Cleaning Solutions for Advanced Packaging Needs》里画出了EDTA结构，是否意味着其ACST清洗剂实际使用EDTA？",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-total", type=int, default=100)
    parser.add_argument("--retest", action="store_true", help="Preserve the initial run and save a separate retest report.")
    parser.add_argument("--retest-round", type=int, choices=range(1, 10), default=1)
    args = parser.parse_args()
    suffix = ("_retest" + (str(args.retest_round) if args.retest_round > 1 else "")) if args.retest else ""
    output = ROOT / f"pilot/reports/expansion_20261103/qa_{args.expected_total}{suffix}.json"
    rows = []
    with httpx.Client(base_url="http://127.0.0.1:8600", trust_env=False, timeout=300) as client:
        summary = client.get("/api/summary")
        summary.raise_for_status()
        if summary.json()["papers"] != args.expected_total:
            raise ValueError("App has not loaded the expected published corpus.")
        for question in QUESTIONS:
            started = time.monotonic()
            response = client.post("/api/chat", json={"question": question, "history": []})
            body = response.json()
            row = {"question": question, "status_code": response.status_code,
                   "elapsed_seconds": round(time.monotonic() - started, 2), "response": body}
            rows.append(row)
            output.write_text(json.dumps({"published_documents": args.expected_total,
                "evaluation": "Actual application responses; semantic judgments are recorded separately.", "results": rows},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"question": question, "status": response.status_code, "answer": body.get("answer"),
                "generated": body.get("generated"), "warnings": body.get("warnings"),
                "sources": [(s["id"], s["title"], s["pdf_page"]) for s in body.get("sources", [])[:4]]},
                ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
