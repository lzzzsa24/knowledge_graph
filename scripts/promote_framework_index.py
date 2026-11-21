"""Promote the existing two-document retrieval index without invoking any model."""
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "pilot"
RUN = PILOT / "benchmarks/gao_adeia_raw_20261118/native_vision"
TARGET = PILOT / "framework/gao_adeia"
FILES = ("graph_chunk_entity_relation.graphml", "vdb_chunks.json", "vdb_entities.json",
         "vdb_relationships.json", "kv_store_text_chunks.json", "kv_store_entity_chunks.json",
         "kv_store_relation_chunks.json")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    contract = read(RUN / "run_contract.json")
    catalog = {p["doc_id"]: p for p in read(PILOT / "catalog/papers.json")}
    status = read(RUN / "doc_status.json")
    documents = {}
    tables = {}
    for doc in contract["documents"]:
        doc_id = doc["doc_id"]
        paper = catalog[doc_id]
        digest = hashlib.sha256(Path(paper["local_path"]).read_bytes()).hexdigest()
        if digest != doc["sha256"] or digest != paper["sha256"] or status[doc_id]["status"] != "processed":
            raise ValueError(f"Index and current PDF disagree: {doc_id}")
        documents[doc_id] = digest
        parser_file, = (RUN / "parser").rglob(doc_id + ".json")
        grids = {}
        for table in read(parser_file)["tables"]:
            page = table["prov"][0]["page_no"]
            rows = [" | ".join(cell["text"] for cell in row) for row in table["data"]["grid"]]
            if rows:
                grids.setdefault(page, []).append("\n".join(rows))
        tables[doc_id] = grids
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TARGET.parent) as temporary:
        stage = Path(temporary) / "index"
        stage.mkdir()
        for name in FILES:
            shutil.copyfile(RUN / "storage" / name, stage / name)
        (stage / "tables.json").write_text(json.dumps(tables, ensure_ascii=False), encoding="utf-8")
        hashes = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in stage.iterdir()}
        manifest = {"documents": documents, "files": hashes, "versions": contract["versions"]}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if TARGET.exists():
            if read(TARGET / "manifest.json") != manifest or any(
                    hashlib.sha256((TARGET / n).read_bytes()).hexdigest() != h for n, h in hashes.items()):
                raise ValueError("Existing promoted index differs; refusing to overwrite it.")
        else:
            stage.rename(TARGET)
    print(f"Verified {len(documents)} original PDFs and {len(hashes)} retrieval assets.")


if __name__ == "__main__":
    main()
