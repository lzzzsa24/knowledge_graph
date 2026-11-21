"""Build page-attributed RAG-Anything input from the reading copy and the original PDF."""
import asyncio
import base64
from collections import Counter, defaultdict
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import time

import pymupdf

from kg_pilot.native_library import file_hash, read_json, save
from kg_pilot.decoration_filter import decoration_reason, visual_templates
from kg_pilot.clipped_forms import visible_layout_copy


POLICY = "legacy-pages-pdf-regions-v1"
REGION_POLICY = "visible-pdf-forms-v3"
MIXED_POLICY = "legacy-pages-reviewed-decorations-v2"
TRANSCRIPTION_LIMIT = 6
STALE_INPUTS = "入库预处理检查点缺失或已过期"
NO_TEXT = "[NO TEXT]"
PAGE_PROMPT = (
    "Transcribe all visible running text on this PDF page verbatim, in column reading order. "
    "Retain the original language, headings, captions, numbers, Greek letters, subscripts and units. "
    "Do not translate, summarize, explain or infer missing characters. Use [illegible] for an "
    "unreadable character. Return plain text only, or exactly [NO TEXT] if there is no text."
)


def validate_indexed_pages(items, chunks, page_count):
    if not chunks:
        raise ValueError("No native chunks for this document")
    text_pages = set()
    visual_pages = Counter()
    for chunk in chunks:
        start = chunk.get("page_idx")
        end = chunk.get("page_idx_end")
        end = start if end is None else end
        if type(start) is not int or type(end) is not int or not 0 <= start <= end < page_count:
            raise ValueError("A source chunk is missing its physical PDF page")
        if chunk.get("is_multimodal"):
            visual_pages[(chunk["original_type"], start)] += 1
        else:
            text_pages.update(range(start, end + 1))
    expected = Counter((i["type"], i["page_idx"]) for i in items if i["type"] != "text")
    if visual_pages != expected:
        raise ValueError(f"Visual item or physical PDF page mismatch: {expected - visual_pages}, {visual_pages - expected}")
    missing = {i["page_idx"] + 1 for i in items if i["type"] == "text" and i["page_idx"] not in text_pages}
    if missing:
        raise ValueError(f"Source text pages without native chunk coverage: {sorted(missing)}")


def page_plan(base, row):
    """Bind legacy text to the PDF revision and its one-based physical page."""
    pages_path, quality_path = base / "pages.json", base / "text_quality.json"
    pages = read_json(pages_path) if pages_path.is_file() else []
    report = read_json(quality_path) if quality_path.is_file() else {}
    by_number = defaultdict(list)
    if report.get("source_sha256") == row["sha256"]:
        for page in pages:
            if type(page.get("pdf_page")) is int:
                by_number[page["pdf_page"]].append(page)
    audit = {p["pdf_page"]: p for p in report.get("pages", [])}
    plan = []
    for n in range(1, row["pages"] + 1):
        matches = by_number[n]
        page = matches[0] if len(matches) == 1 else {}
        quality = page.get("text_quality", {})
        text = page.get("text", "")
        reusable = (text.strip() and quality.get("status") in ("clean", "repaired")
                    and not quality.get("unresolved_critical")
                    and n in audit and not audit[n].get("unresolved_critical", True))
        plan.append(text if reusable else None)
    return plan


def source_layout(pdf_path, job, row, *, reuse=True):
    """Reuse an original-PDF layout if available. New layouts run with whole-page OCR off."""
    from docling_core.types.doc import DoclingDocument
    previous = job / "parse.json"
    if reuse and previous.is_file() and read_json(previous).get("sha256") == row["sha256"]:
        candidates = list((job / "parser").glob(f"*/{pdf_path.stem}/docling/{pdf_path.stem}.json"))
        if len(candidates) == 1:
            return DoclingDocument.load_from_json(candidates[0])
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption
    options = PdfPipelineOptions(do_ocr=False, do_table_structure=True)
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(pdf_path)
    if result.status.value != "success":
        raise ValueError(f"Original-PDF layout incomplete: {result.status}")
    return result.document


def _layout_regions(pdf_path, layout, row, selected):
    items = []
    with pymupdf.open(pdf_path) as pdf:
        if set(layout.pages) != set(range(1, len(pdf) + 1)) or len(pdf) != row["pages"]:
            raise ValueError("Layout and original PDF disagree on physical pages")
        for item, _ in layout.iterate_items():
            kind = {"picture": "image", "table": "table", "formula": "equation"}.get(item.label.value)
            if not kind:
                continue
            if not item.prov:
                raise ValueError(f"Missing PDF provenance for {item.self_ref}")
            boxes = {}
            for prov in item.prov:
                n = prov.page_no
                if selected is not None and n not in selected:
                    continue
                page = pdf[n - 1]
                size = layout.pages[n].size
                box = prov.bbox.to_top_left_origin(page_height=size.height)
                sx, sy = page.rect.width / size.width, page.rect.height / size.height
                rect = pymupdf.Rect(box.l * sx, box.t * sy, box.r * sx, box.b * sy) & page.rect
                if rect.is_empty:
                    raise ValueError(f"Empty {kind} region on physical page {n}")
                boxes[n] = boxes[n] | rect if n in boxes else rect
            for n, rect in boxes.items():
                entry = dict(type=kind, page_idx=n-1, source_bbox=list(rect))
                if kind in ("image", "table"):
                    caption = item.caption_text(doc=layout)
                    entry[kind + "_caption"] = [caption] if caption else []
                    if item.footnotes:
                        entry[kind + "_footnote"] = [ref.resolve(layout)._get_plain_text(layout) for ref in item.footnotes]
                if kind == "table":
                    has_cell_text = any(cell.text.strip() for cell in item.data.table_cells)
                    one_page = len({p.page_no for p in item.prov}) == 1
                    entry["table_body"] = item.export_to_markdown(doc=layout) if one_page and has_cell_text else ""
                items.append(entry)
    return items


def _align_figures(items, anchors):
    for n, figures in anchors.items():
        for box, caption in figures:
            matches = []
            ambiguous = False
            for item in items:
                if item["type"] != "image" or item["page_idx"] != n - 1:
                    continue
                rect = pymupdf.Rect(item["source_bbox"])
                overlap = (rect & box).get_area()
                if overlap >= 0.90 * rect.get_area():
                    matches.append(item)
                elif overlap > 0.25 * min(rect.get_area(), box.get_area()):
                    ambiguous = True
            if ambiguous:
                continue
            # One physical Form can contain multiple panels, partially detected
            # by the parser. Its printed caption identifies the complete image.
            new = dict(type="image", page_idx=n-1, source_bbox=list(box), image_caption=[caption])
            footnotes = list(dict.fromkeys(note for m in matches for note in m.get("image_footnote", [])))
            if footnotes:
                new["image_footnote"] = footnotes
            pos = min((items.index(m) for m in matches), default=len(items))
            if not matches:
                pos = next((j for j,i in enumerate(items) if (i["page_idx"], i["source_bbox"][1])
                            > (n - 1, box.y0)), len(items))
            for item in matches:
                items.remove(item)
            items.insert(pos, new)


def original_regions(pdf_path, job, row, *, cached_only=False):
    """Run layout on visible PDF content and render crops from the original physical pages."""
    cache = job / "pdf-regions.json"
    old = read_json(cache) if cache.is_file() else {}
    cached = old.get("items") if old.get("sha256") == row["sha256"] else None
    if cached is not None and not all(Path(i["img_path"]).is_file() and
            file_hash(Path(i["img_path"])) == i["image_sha256"] for i in cached):
        cached = None
    if cached is not None and old.get("policy") == REGION_POLICY:
        return cached
    if cached_only:
        raise ValueError(STALE_INPUTS)
    if old.get("policy") not in (POLICY, "visible-pdf-forms-v2"):
        cached = None
    with tempfile.TemporaryDirectory(prefix="layout-", dir=job) as temp:
        analysis, affected, anchors = visible_layout_copy(pdf_path, temp)
        if analysis or cached is None:
            layout = source_layout(analysis or pdf_path, job, row, reuse=analysis is None)
            items = _layout_regions(pdf_path, layout, row, affected if cached is not None else None)
            if cached:
                items.extend(i for i in cached if i["page_idx"] + 1 not in affected)
                items.sort(key=lambda i: i["page_idx"])
        else:
            items = list(cached)
        _align_figures(items, anchors)
    dest = job / "original-regions"
    dest.mkdir(parents=True, exist_ok=True)
    old_paths = {i["image_sha256"]: i["img_path"] for i in (cached or [])}
    with pymupdf.open(pdf_path) as pdf:
        for item in items:
            if "img_path" in item:
                continue
            n = item["page_idx"]
            pix = pdf[n].get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), clip=item["source_bbox"], alpha=False)
            png = pix.tobytes("png")
            sha = hashlib.sha256(png).hexdigest()
            path = Path(old_paths[sha]) if sha in old_paths else dest / f"p{n+1:03}-{sha}.png"
            if not path.is_file() or file_hash(path) != sha:
                path.write_bytes(png)
            item.update(img_path=str(path.resolve()), image_sha256=sha)
    save(cache, {"policy": REGION_POLICY, "sha256": row["sha256"], "items": items})
    return items


async def crop_regions(pilot, row, pdf_path, job, hooks, *, review_crops=True, layout_slot=None, cached_only=False):
    async with layout_slot if layout_slot is not None else nullcontext():
        reuse = {"cached_only": True} if cached_only else {}
        regions = await asyncio.to_thread(original_regions, pdf_path, job, row, **reuse)
    reviewed = read_json(Path(__file__).with_name("publication_decorations.json"))
    templates = visual_templates(reviewed)
    if review_crops:
        from kg_pilot.native_crop_review import prepare_reviewed_regions
        options = {} if layout_slot is None else {"layout_slot": layout_slot}
        regions = await prepare_reviewed_regions(pilot,row,pdf_path,job,regions,hooks,reviewed,templates,**options,**reuse)
    return regions, reviewed, templates


async def mixed_pages(pilot, row, pdf_path, job, hooks, *, review_crops=False, layout_slot=None, cached_only=False):
    plan = page_plan(pilot / "parsed" / row["id"], row)
    plan_hash = hashlib.sha256(json.dumps(plan, ensure_ascii=False).encode()).hexdigest()
    options = {} if layout_slot is None else {"layout_slot": layout_slot}
    if cached_only:
        options["cached_only"] = True
    regions, reviewed, templates = await crop_regions(pilot, row, pdf_path, job, hooks, review_crops=review_crops, **options)
    region_hash = hashlib.sha256(json.dumps(regions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    skipped = []
    by_page = defaultdict(list)
    for item in regions:
        label = item.get("filter_reason") or decoration_reason(item, reviewed, templates, pdf_sha=row["sha256"])
        if label:
            skipped.append({**item, "pdf_page": item["page_idx"] + 1, "reason": label})
        else:
            by_page[item["page_idx"]].append(item.copy())
    inputs = dict(sha256=row["sha256"], legacy_sha256=plan_hash, regions_sha256=region_hash)
    def page_crop(item):
        return f"p{item['page_idx'] + 1}:{item.get('image_sha256')}"

    decorations = {page_crop(s): s["reason"] for s in skipped}
    identity = dict(policy=MIXED_POLICY, **inputs, decorations=decorations)
    cache = job / "mixed-parse.json"
    if cache.exists():
        previous = read_json(cache)
        if previous.get("identity") == identity:
            return previous
        if cached_only:
            raise ValueError(STALE_INPUTS)
        old = previous.get("identity", {})
        original_parse = old == dict(policy=POLICY, **inputs)
        additive_filter = (old.get("policy") == MIXED_POLICY and all(old.get(k) == v for k,v in inputs.items())
                           and "skipped_decorations" in previous
                           and all(page_crop(s) in decorations for s in previous["skipped_decorations"]))
        if original_parse or additive_filter:
            # All retained items were present before. Reuse their original text,
            # tables and formula transcriptions, even when more marks are skipped.
            excluded={(i["type"],i["page_idx"],i["image_sha256"]) for i in skipped}
            retained = [i for i in previous["items"] if (i["type"],i["page_idx"],i.get("image_sha256")) not in excluded
                        or i.get("image_caption") or i.get("image_footnote")]
            result = dict(identity=identity, pages=previous["pages"], items=retained, skipped_decorations=skipped,
                          transcription={"tasks": 0, "limit": TRANSCRIPTION_LIMIT, "peak": 0, "seconds": 0,
                                         "reused_previous_parse": True})
            save(cache, result)
            return result

    if cached_only:
        raise ValueError(STALE_INPUTS)
    slots = asyncio.Semaphore(TRANSCRIPTION_LIMIT)
    active = peak = 0
    failed = False
    started = time.monotonic()
    page_jobs = {}
    region_jobs = []
    with pymupdf.open(pdf_path) as pdf:
        async def transcribe(prompt, *, page_idx=None, image_path=None):
            nonlocal active, peak, failed
            async with slots:
                if failed:
                    return
                active += 1
                peak = max(peak, active)
                try:
                    if page_idx is not None:
                        image = pdf[page_idx].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")
                    else:
                        image = Path(image_path).read_bytes()
                    answer = (await hooks.vision(prompt, image_data=base64.b64encode(image).decode("ascii"))).strip()
                    if not answer:
                        raise ValueError("Empty visual transcription for " + (f"page {page_idx+1}" if page_idx is not None else str(image_path)))
                    return answer
                except Exception:
                    failed = True
                    raise
                finally:
                    active -= 1

        for idx, text in enumerate(plan):
            if text is None:
                page_jobs[idx] = asyncio.create_task(transcribe(PAGE_PROMPT, page_idx=idx))
            for item in by_page[idx]:
                if item["type"] == "equation":
                    if item.get("formula_source") == "pmc_xml" and item.get("latex"):
                        continue
                    key = "latex"
                    prompt = ("Transcribe exactly the displayed equation into LaTeX. "
                              "Preserve symbols, fractions and exponents. Return only LaTeX.")
                elif item["type"] == "table" and not any(c.isalnum() for c in item["table_body"]):
                    key = "table_body"
                    prompt = ("Transcribe this table into Markdown. Preserve all row and "
                              "column labels, values, signs and units. Do not guess unreadable cells.")
                else:
                    continue
                task = asyncio.create_task(transcribe(prompt, image_path=item["img_path"]))
                region_jobs.append((item, key, task))
        tasks = [*page_jobs.values(), *(task for _, _, task in region_jobs)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # Keep the provider's original quota/pause error and drain users of the open PDF.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    for item, key, task in region_jobs:
        item[key] = task.result()
    items, sources = [], []
    for idx, text in enumerate(plan):
        source = "legacy"
        if idx in page_jobs:
            text, source = page_jobs[idx].result(), "pdf_vision"
            if text == NO_TEXT:
                text, source = "", "pdf_no_text"
        sources.append({"pdf_page": idx+1, "source": source, "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
        if text:
            items.append({"type": "text", "text": text, "page_idx": idx})
        items.extend(by_page[idx])
    result = {"identity": identity, "pages": sources, "items": items, "skipped_decorations": skipped,
              "transcription": {"tasks": len(tasks), "limit": TRANSCRIPTION_LIMIT, "peak": peak,
                                "seconds": round(time.monotonic() - started, 3)}}
    save(cache, result)
    return result
