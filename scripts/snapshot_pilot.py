"""Save a compact integrity manifest of the delivered pilot, without model/cache files."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "pilot/reports/delivery_manifest.json"


def main():
    paths = set()
    for relative in ("src", "tests", "scripts", "web", "pilot/annotations", "pilot/catalog", "pilot/reports", "pilot/evaluation"):
        paths.update(p for p in (ROOT / relative).rglob("*")
                     if p.is_file() and p.suffix in {".py", ".ps1", ".md", ".json", ".jsonl", ".csv", ".html", ".js", ".cjs", ".css", ".png", ".txt"} and "__pycache__" not in p.parts)
    paths.update(ROOT / name for name in ("README.md", "pyproject.toml", "requirements.lock.txt", "app.py", "pilot/config.json", "pilot/model_config.json"))
    paths.update(ROOT / "pilot/parsed" / name for name in ("evidence.jsonl", "effective_evidence.jsonl"))
    paths.update(ROOT / "pilot/benchmarks/embedding" / name for name in ("all_vectors.npy", "all_records.json"))
    paths.update((ROOT / "pilot/benchmarks/framework/session_bridge_01").rglob("*.json"))
    for paper in json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig")):
        paths.add(Path(paper["local_path"]))
    paths.discard(OUT)
    rows = [{"path": p.relative_to(ROOT).as_posix(), "bytes": p.stat().st_size,
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(paths) if p.is_file()]
    OUT.write_text(json.dumps({"created_utc": datetime.now(timezone.utc).isoformat(),
                              "scope": "Delivered sources, PDF corpus, annotations, retrieval artifacts and reports; excludes model weights, live databases and caches.",
                              "files": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(OUT), "files": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
