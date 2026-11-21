"""Restore read-only previews for published PDFs ingested before mixed-parse.json."""
import base64
from collections import Counter
import hashlib
from pathlib import Path
import sys

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot import native_library as lib


def archived_input(pilot, row, job):
    doc = row["id"]
    if row["prebuilt"] == "sol":
        run = pilot / lib.SOL_RUN
        if lib.read_json(run / "contract.json")["documents"][doc] != row["sha256"]:
            raise ValueError("Seed revision differs")
        items = lib.read_json(run / (doc + ".parse.json"))["content"]
    elif row["prebuilt"] == "gao_adeia":
        run = pilot / lib.GAO_RUN
        contract = lib.read_json(run / "run_contract.json")["documents"]
        if not any(r["doc_id"] == doc and r["sha256"] == row["sha256"] for r in contract):
            raise ValueError("Seed revision differs")
        matches = []
        for path in run.glob("*.parsed_content.json"):
            content = lib.read_json(path)["items"]
            if any(doc in Path(i["img_path"]).parts for i in content if i.get("img_path")):
                matches.append(content)
        items, = matches
    else:
        run = job
        parse = lib.read_json(job / "parse.json")
        if parse["sha256"] != row["sha256"]:
            raise ValueError("Parse revision differs")
        items = parse["items"]
    layout_file, = (run / "parser").glob(f"*/{doc}/docling/{doc}.json")
    return items, layout_file


def block_bounds(block, layout, page_nr, page):
    size = layout["pages"][str(page_nr)]["size"]
    sx, sy = page.rect.width / size["width"], page.rect.height / size["height"]
    rect = pymupdf.Rect()
    for prov in block["prov"]:
        if prov["page_no"] != page_nr:
            continue
        box = prov["bbox"]
        top, bottom = box["t"], box["b"]
        if box["coord_origin"] == "BOTTOMLEFT":
            top, bottom = size["height"] - top, size["height"] - bottom
        elif box["coord_origin"] != "TOPLEFT":
            raise ValueError("Unknown coordinate origin")
        rect |= pymupdf.Rect(box["l"] * sx, top * sy, box["r"] * sx, bottom * sy) & page.rect
    if rect.is_empty:
        raise ValueError("Empty preview crop")
    return rect


def restore(pilot, row):
    root = lib.directory(pilot)
    pdf = lib.verify_source(pilot, row)
    items, layout_file = archived_input(pilot, row, root / "documents" / row["id"])
    layout = lib.read_json(layout_file)
    dest = root / "legacy_gallery" / row["id"] / row["sha256"]
    images = dest / "original-regions"
    images.mkdir(parents=True, exist_ok=True)
    regions = []
    with pymupdf.open(pdf) as original:
        if len(original) != len(layout["pages"]) or len(original) != row["pages"]:
            raise ValueError("Layout page count differs")
        for item in items:
            kind = item["type"]
            if kind not in {"image", "table", "equation"}:
                continue
            page_nr = item["page_idx"] + 1
            caption = item.get(kind + "_caption", [])
            if kind == "image":
                path = Path(item["img_path"]).resolve(strict=True)
                if path.parent != (layout_file.parent / "images").resolve():
                    raise ValueError("Image belongs to another parse")
                data = path.read_bytes()
                reference = layout["pictures"][int(path.stem.removeprefix("image_"))]
                if reference["prov"][0]["page_no"] != page_nr or base64.b64decode(reference["image"]["uri"].split(",")[-1]) != data:
                    raise ValueError("Archived image or physical page differs")
                source = "archived_image"
            else:
                blocks = layout["tables"] if kind == "table" else layout["texts"]
                # Bind by exact original table data/formula and page, never by proximity.
                candidates = [b for b in blocks if b.get("prov") and b["prov"][0]["page_no"] == page_nr
                              and (b.get("data") == item.get("table_body") if kind == "table" else
                                   b.get("label") == "formula" and b.get("orig") == item.get("text"))]
                block, = candidates
                page = original[page_nr - 1]
                rect = block_bounds(block, layout, page_nr, page)
                data = page.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), clip=rect, alpha=False).tobytes("png")
                source = "pdf_crop"
                if not caption:
                    caption = [layout["texts"][int(c["$ref"].split("/")[-1])]["text"]
                               for c in block.get("captions", []) if c["$ref"].startswith("#/texts/")]
            digest = hashlib.sha256(data).hexdigest()
            image = images / (digest + ".png")
            if not image.exists():
                image.write_bytes(data)
            if lib.file_hash(image) != digest:
                raise ValueError("Restored image differs")
            regions.append({"type":kind, "page_idx":page_nr-1, "img_path":str(image), "image_sha256":digest,
                            kind+"_caption":caption, kind+"_footnote":item.get(kind+"_footnote", []),
                            "preview_source":source})
    lib.save(dest / "regions.json", {"identity":{"sha256":row["sha256"]}, "items":regions})
    return Counter(item["type"] for item in regions)


if __name__ == "__main__":
    pilot = ROOT / "pilot"
    root = lib.directory(pilot)
    published = lib.read_json(root / "manifest.json")["documents"]
    for row in lib.read_json(root / "queue.json")["documents"]:
        if row["id"] in published and not (root / "documents" / row["id"] / "mixed-parse.json").exists():
            if row["sha256"] != published[row["id"]]["sha256"]:
                raise ValueError("Published revision differs")
            print(row["id"], dict(restore(pilot, row)), flush=True)
