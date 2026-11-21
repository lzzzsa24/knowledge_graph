"""Recover ink cut at a figure boundary using the visible physical PDF page."""
import hashlib
from pathlib import Path

import numpy as np
import pymupdf as fitz

from kg_pilot.crop_sources import store_bytes


def _ink_extent(edge, band):
    if not len(band) or not (edge & band[0]).any():
        return 0
    ink = band.any(axis=1)
    for end in range(len(ink)-3):
        if not ink[end:end+4].any():
            return end+1
    return None


def complete_edges(page, item, obstacles, job):
    """Return 'review' for an open or obstructed boundary, 'expanded' for a repair."""
    if item["type"]!="image" or not item.get("source_bbox") or item.get("preview_source")=="pmc_figure":
        return None
    rect=fitz.Rect(item["source_bbox"])
    matrix=fitz.Matrix(2.5,2.5)
    area=(rect+(-14,-14,14,14)) & page.rect
    if rect.is_empty or area.is_empty:
        return "review"
    pix=page.get_pixmap(matrix=matrix,clip=area,colorspace=fitz.csGRAY,alpha=False)
    ink=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)<160
    original=(rect*matrix).irect & pix.irect
    if original.is_empty:
        return "review"
    x,y=original.x0-pix.x,original.y0-pix.y
    right,bottom=x+original.width,y+original.height
    sides=[(ink[y:bottom,x],ink[y:bottom,:x].T[::-1]),
           (ink[y,x:right],ink[:y,x:right][::-1]),
           (ink[y:bottom,right-1],ink[y:bottom,right:].T),
           (ink[bottom-1,x:right],ink[bottom:,x:right])]
    extents=[_ink_extent(edge,band) for edge,band in sides]
    if None in extents:
        return "review"
    if not any(extents):
        return None
    left,top,r,b=extents
    enlarged=fitz.Rect((original.x0-left)/2.5 if left else rect.x0,
                       (original.y0-top)/2.5 if top else rect.y0,
                       (original.x1+r)/2.5 if r else rect.x1,
                       (original.y1+b)/2.5 if b else rect.y1) & page.rect
    # Do not borrow a neighbouring figure, a printed caption or an independent
    # prose line. Labels already intersecting the old box can grow with it.
    for other in obstacles:
        if (enlarged & other).get_area() > (rect & other).get_area()+.05:
            return "review"
    png=page.get_pixmap(matrix=matrix,clip=enlarged,alpha=False).tobytes("png")
    digest=hashlib.sha256(png).hexdigest()
    path=Path(job)/"original-regions"/f"p{page.number+1:03}-{digest}.png"
    store_bytes(path,png)
    item.update(source_bbox=list(enlarged),img_path=str(path.resolve()),image_sha256=digest)
    return "expanded"


def check_page_edges(page, items, labels, job):
    caption_boxes=[fitz.Rect(coords) for _,coords in labels.values()]
    prose=[]
    for block in page.get_text("dict",flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]:
        for line in block.get("lines",[]):
            text="".join(s["text"] for s in line["spans"]).strip()
            if len(text.split())>=5 or len(text)>=35:
                prose.append(fitz.Rect(line["bbox"]))
    changes=[]
    for item in items:
        if item.get("filter_reason") or item["type"]!="image" or not item.get("source_bbox"):
            continue
        rect=fitz.Rect(item["source_bbox"])
        obstacles=[fitz.Rect(i["source_bbox"]) for i in items if i is not item and i.get("source_bbox")]
        obstacles.extend(caption_boxes)
        obstacles.extend(box for box in prose if not (rect & box).get_area())
        before=item["image_sha256"]
        action=complete_edges(page,item,obstacles,job)
        if action:
            changes.append({"page":page.number+1,"edge":action,"before":before,"after":item["image_sha256"]})
    return changes
