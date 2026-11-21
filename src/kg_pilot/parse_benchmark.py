"""Run a bounded, recorded Docling comparison on actual corpus pages."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import time
import traceback
from pathlib import Path

from kg_pilot.pipeline import PILOT, read_json, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-all", action="store_true")
    parser.add_argument("--ocr-tables", action="store_true")
    args = parser.parse_args()
    from huggingface_hub import file_download
    file_download.are_symlinks_supported = lambda cache_dir=None: False
    import torch
    torch.set_num_threads(4)
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions

    papers = {p["doc_id"]: p for p in read_json(PILOT / "catalog" / "papers.json")}
    cases = [("journals_ma15051888", 7, 7, False), ("journals_mi13040537", 7, 8, False),
             ("journals_ma18225152", 5, 5, False),
             ("reliability_small_die_2022", 1, 7 if args.ocr_all else 1, True)]
    metrics = []
    for doc_id, first, last, ocr in cases:
        if args.ocr_tables and "small_die" in doc_id:
            continue
        ocr = ocr or args.ocr_tables
        suffix = "_forced_ocr" if args.ocr_tables else ""
        out = PILOT / "benchmarks" / "docling" / f"{doc_id}_p{first}-{last}{suffix}"
        out.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        info = {"doc_id": doc_id, "first_pdf_page": first, "last_pdf_page": last,
                "force_full_page_ocr": ocr, "version": importlib.metadata.version("docling"), "device": "cpu", "threads": 4}
        try:
            options = PdfPipelineOptions(do_ocr=ocr, do_table_structure=True,
                                         document_timeout=240,
                                         accelerator_options=AcceleratorOptions(num_threads=4, device="cpu"))
            if ocr:
                options.ocr_options = RapidOcrOptions(force_full_page_ocr=True)
            converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
            result = converter.convert(Path(papers[doc_id]["local_path"]), page_range=(first, last))
            content = result.document.export_to_markdown()
            (out / "document.md").write_text(content, encoding="utf-8")
            write_json(out / "document.json", result.document.export_to_dict())
            info.update(status=str(result.status), markdown_characters=len(content),
                        tables=len(result.document.tables), pictures=len(result.document.pictures))
        except Exception as exc:
            info.update(status="error", error=f"{type(exc).__name__}: {exc}")
            (out / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        info["wall_seconds_including_first_download"] = round(time.perf_counter() - started, 3)
        metrics.append(info)
        write_json(PILOT / "reports" / ("docling_ocr_tables.json" if args.ocr_tables else "docling_benchmark.json"), metrics)
        print(json.dumps(info, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
