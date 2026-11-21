"""Locate printed labels and repair ambiguous crops with independent PDF geometry."""
from collections import defaultdict
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

import pymupdf as fitz

from kg_pilot.clipped_forms import visible_layout_copy
from kg_pilot.crop_sources import store_bytes
from kg_pilot.native_library import read_json, save


CAPTION = re.compile(r"^\s*(Figure|Fig\.?|Table|图|表)\s*(\d+[a-z]?)(?=\s|[.:：。]|$)", re.I)
BODY_REFERENCE = re.compile(
    r"(?:(?:also|clearly)\s+)?(?:shows?|presents?|illustrates?|depicts?|describes?|displays?|exhibits?|reveals?|is|was|and|to)\b"
    r"|(?:に|において|を|で|から|より)|(?:所示|表明|中)", re.I)


def label_key(text):
    match = CAPTION.match(text)
    if match:
        return ("table" if match[1].lower() in ("table","表") else "image", match[2].lower())


def item_label(item):
    return label_key(" ".join(item.get(item["type"]+"_caption", [])))


def _labels(page):
    labels = defaultdict(list)
    for block in page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]:
        lines = block.get("lines", [])
        words = ["".join(s["text"] for s in line["spans"]).strip() for line in lines]
        for j, first in enumerate(words):
            match = CAPTION.match(first)
            if not match:
                continue
            if j:
                previous=fitz.Rect(lines[j-1]["bbox"])
                current=fitz.Rect(lines[j]["bbox"])
                same_column=max(current.x0,previous.x0)<min(current.x1,previous.x1)
                if same_column and previous.y0-2<=current.y0<=previous.y1+max(5,previous.height):
                    continue
            rest = first[match.end():].lstrip()
            if BODY_REFERENCE.match(rest):
                continue
            parts = [first]
            prev = fitz.Rect(lines[j]["bbox"])
            first_line=fitz.Rect(prev)
            column_start=prev.x0
            for k in range(j+1,len(words)):
                box = fitz.Rect(lines[k]["bbox"])
                same_line=abs(box.y0-prev.y0)<=2 and -1<=box.x0-prev.x1<=2*prev.height
                same_column=box.x0<prev.x1 and column_start<box.x1
                if (label_key(words[k]) or box.y0<prev.y0-2 or box.y0-prev.y1>max(5,prev.height)
                        or not (same_line or same_column)):
                    break
                if same_line and abs(box.y0-first_line.y0)<=2:
                    first_line |= box
                parts.append(words[k])
                prev = box
            value = " ".join(parts).replace("\x01","(").replace("\x02",")")
            labels[label_key(first)].append((value,list(first_line)))
    return labels


def printed_labels(pdf_path):
    with fitz.open(pdf_path) as doc:
        root_forms = {x[0] for page in doc for x in page.get_xobjects() if not x[2]}
        original = [_labels(p) for p in doc]
        for xref in root_forms:
            doc.update_stream(xref, b"q Q")
        output = []
        for idx, page in enumerate(doc):
            # An enclosing page Form can contain the entire paper, including its caption.
            clean = _labels(page) if root_forms and len(page.get_text().strip()) > 100 else original[idx]
            output.append({k:v[0] for k,v in clean.items() if len(v)==1})
        return output


def overlaps(a, b):
    ra, rb = fitz.Rect(a), fitz.Rect(b)
    return (ra & rb).get_area() / max(1, min(ra.get_area(),rb.get_area()))


def bind_local_captions(items, labels):
    """Only bind a free caption when both it and its adjacent crop have one match."""
    assigned = {item_label(i) for i in items}
    matches = defaultdict(list)
    for key, (text, coords) in labels.items():
        if key in assigned:
            continue
        caption = fitz.Rect(coords)
        for index, item in enumerate(items):
            if item["type"] != key[0] or item.get(key[0]+"_caption") or not item.get("source_bbox"):
                continue
            box = fitz.Rect(item["source_bbox"])
            width = min(box.width, caption.width)
            overlap = min(box.x1,caption.x1)-max(box.x0,caption.x0)
            gap = caption.y0-box.y1
            if key[0]=="table" and caption.y1 <= box.y0+1:
                gap = box.y0-caption.y1
            if width>0 and overlap>=.65*width and -1<=gap<=30:
                matches[key].append(index)
    for key, candidates in matches.items():
        if len(candidates)==1 and sum(candidates[0] in ids for ids in matches.values())==1:
            item=items[candidates[0]]
            text, coords=labels[key]
            item.update(caption_bbox=coords,caption_source="pdf")
            item[key[0]+"_caption"]=[text]

    # Keep separate panel crops while sharing the one printed multipart caption.
    for key,(text,coords) in labels.items():
        if key[0]!="image" or set(re.findall(r"\(([a-z])\)",text.lower()))!={"a","b"}:
            continue
        cap=fitz.Rect(coords)
        panels=[]
        for index,item in enumerate(items):
            if item["type"]!="image" or not item.get("source_bbox") or item.get("filter_reason"):
                continue
            if item.get("image_caption") and item_label(item)!=key:
                continue
            if any(index in candidates for other,candidates in matches.items() if other!=key):
                continue
            box=fitz.Rect(item["source_bbox"])
            if -1<=cap.y0-box.y1<=24 and cap.x0<=box.x0+3 and cap.x1>=box.x1-3:
                panels.append((box,item))
        if len(panels)!=2:
            continue
        (left,first),(right,second)=sorted(panels,key=lambda pair:pair[0].x0)
        if (0<=right.x0-left.x1<=24 and abs(left.y0-right.y0)<=4 and abs(left.y1-right.y1)<=4
                and not any(item_label(i)==key for i in items if i is not first and i is not second)):
            first.update(image_caption=[text],caption_bbox=coords,caption_source="pdf")
            second.update(image_caption=[text],caption_bbox=coords,caption_source="shared_panel")


def _whole_image_box(page, predicted, caption):
    """Reject a detector box slicing through a placed image; recover a unique side-caption figure."""
    partial=[]
    for info in page.get_image_info():
        asset=fitz.Rect(info["bbox"])
        if (asset.is_empty or not page.rect.contains(asset) or min(asset.width,asset.height)<24
                or asset.get_area()>.75*page.rect.get_area()):
            continue
        overlap=(asset & predicted).get_area()
        if overlap>=.035*min(asset.get_area(),predicted.get_area()) and overlap<.9*asset.get_area():
            if asset not in partial:
                partial.append(asset)
    if not partial:
        return predicted
    if len(partial)==1:
        asset=partial[0]
        side_gap=max(caption.x0-asset.x1,asset.x0-caption.x1)
        if (0<=side_gap<=.10*page.rect.width and asset.y0<=caption.y0<=asset.y1
                and abs(asset.y0-predicted.y0)<=8):
            return asset
    return None


def candidate_boxes(pdf_path, job, sha, tools_dir):
    cache = job/"crop-geometry.json"
    if cache.is_file() and (data:=read_json(cache)).get("identity")==[sha,"pdffigures2-visible-v1"]:
        return data["boxes"]
    path = tools_dir/"pdffigures"/"lib"
    if not (path/"pdffigures2.jar").is_file():
        raise RuntimeError("PDFFigures 2 未安装，请运行 scripts/setup_native_crop_tools.py pdffigures")
    with tempfile.TemporaryDirectory(dir=job, prefix="figure-layout-") as tmp:
        analysis, _, anchors = visible_layout_copy(pdf_path,tmp)
        source = analysis or pdf_path
        prefix = str(Path(tmp)/"boxes-")
        result = subprocess.run(["java","-Djava.awt.headless=true","-Xmx1200m","-cp",str(path.resolve()/"*"),
                    "org.allenai.pdffigures2.FigureExtractorBatchCli","-d",prefix,str(source.resolve())],
                   capture_output=True,timeout=240, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        target = Path(prefix+source.stem+".json")
        if result.returncode or not target.is_file():
            raise RuntimeError("PDFFigures 2 未生成版面坐标：" + result.stderr.decode(errors="replace")[-320:])
        boxes = []
        with fitz.open(pdf_path) as pdf:
            for record in read_json(target):
                idx = record["page"]
                if not 0<=idx<len(pdf) or pdf[idx].rotation:
                    continue
                b = record["regionBoundary"]
                rect = fitz.Rect(*(b[k] for k in ("x1","y1","x2","y2")))
                if rect.is_empty or not pdf[idx].rect.contains(rect):
                    continue
                cb = record["captionBoundary"]
                boxes.append(dict(page=idx+1,kind="table" if record["figType"]=="Table" else "image",
                                  number=str(record["name"]).lower(),box=list(rect),
                                  caption_box=[cb[k] for k in ("x1","y1","x2","y2")],source="pdffigures2"))
        # A verified placement clip with the same printed caption includes the
        # source's full panel and axes, even when the secondary detector trims them.
        for n, values in anchors.items():
            for box, caption in values:
                key=label_key(caption)
                if key:
                    boxes=[b for b in boxes if (b["page"],b["kind"],b["number"])!=(n,*key)]
                    boxes.append(dict(page=n,kind=key[0],number=key[1],box=list(box),source="pdf_form"))
        save(cache,{"identity":[sha,"pdffigures2-visible-v1"],"boxes":boxes})
        return boxes


def repair_page(pdf, page_no, items, labels, targets, boxes, job):
    resolved = set()
    replacements = []
    for key in sorted(targets):
        candidates = [b for b in boxes if (b["page"],b["kind"],b["number"])==(page_no,*key)]
        if key not in labels or len(candidates)!=1:
            continue
        b=candidates[0]
        caption, cap_box=labels[key]
        if "caption_box" in b and not overlaps(b["caption_box"],cap_box):
            continue
        rect=fitz.Rect(b["box"])
        source=b["source"]
        if key[0]=="image" and source=="pdffigures2":
            full=_whole_image_box(pdf[page_no-1],rect,fitz.Rect(cap_box))
            if full is None:
                continue
            if full!=rect:
                rect=full
                source="pdffigures2_pdf_image"
        if any(other!=key and rect.contains(fitz.Rect(coords)) for other,(_,coords) in labels.items()):
            continue
        if any(overlaps(rect, r["source_bbox"])>.3 for r in replacements):
            continue
        pix=pdf[page_no-1].get_pixmap(matrix=fitz.Matrix(2.5,2.5),clip=rect,alpha=False)
        if pix.is_monochrome and pix.samples.count(255)==len(pix.samples):
            continue
        png=pix.tobytes("png")
        sha=hashlib.sha256(png).hexdigest()
        path=job/"original-regions"/f"p{page_no:03}-{sha}.png"
        store_bytes(path,png)
        matches=[i for i in items if item_label(i)==key]
        new=dict(matches[0]) if len(matches)==1 else {}
        new.update(type=key[0],page_idx=page_no-1,source_bbox=list(rect),image_sha256=sha,
                   img_path=str(path.resolve()),preview_source=source)
        new[key[0]+"_caption"]=[caption]
        if key[0]=="table" and (not matches or matches[0].get("image_sha256")!=sha):
            new["table_body"]=""
        replacements.append(new)
        resolved.add(key)
    if resolved != targets:
        return items, set()
    keep=[]
    for i in items:
        if i["type"] not in ("image","table"):
            keep.append(i)
            continue
        same=[r for r in replacements if i["type"]==r["type"]]
        label=item_label(i)
        intersecting=[r for r in same if i.get("source_bbox") and overlaps(r["source_bbox"],i["source_bbox"])>=.75]
        if label in resolved or (intersecting and (not label or label not in labels)):
            continue
        keep.append(i)
    for new in sorted(replacements,key=lambda i:(i["source_bbox"][1],i["source_bbox"][0])):
        box=fitz.Rect(new["source_bbox"])
        pos=len(keep)
        for j,other in enumerate(keep):
            if other.get("source_bbox"):
                later=fitz.Rect(other["source_bbox"])
                if later.y0>box.y0 and min(later.x1,box.x1)>max(later.x0,box.x0):
                    pos=j
                    break
        keep.insert(pos,new)
    return keep, resolved
