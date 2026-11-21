"""Locate a revision-verified archive figure on its physical PDF page."""
from pathlib import Path

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image


def _locate(source, page_pixels, zoom):
    # Vote on the alignment using local details, then verify the *entire*
    # figure. A match confined to one micrograph cannot certify the outlines.
    detector = cv2.SIFT_create(nfeatures=1800)
    key_a, desc_a = detector.detectAndCompute(source, None)
    key_b, desc_b = detector.detectAndCompute(page_pixels, None)
    if desc_a is None or desc_b is None or len(desc_b) < 2:
        return None
    neighbors = cv2.BFMatcher().knnMatch(desc_a, desc_b, k=2)
    pairs = [m for m, n in neighbors if m.distance < .72 * n.distance]
    if len(pairs) < 18:
        return None
    a = np.float32([key_a[m.queryIdx].pt for m in pairs])
    b = np.float32([key_b[m.trainIdx].pt for m in pairs])
    transform, mask = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=2.5)
    if transform is None:
        return None
    selected = mask.ravel().astype(bool)
    h, w = source.shape
    if (selected.sum() < 18 or selected.mean() < .52 or transform[0,0] <= 0
            or abs(transform[1,0]) > .007 * transform[0,0]
            or min(np.ptp(a[selected], axis=0) / [w,h]) < .55):
        return None
    corners = cv2.transform(np.float32([[[0,0],[w,0],[w,h],[0,h]]]), transform)[0]
    x, y = corners.min(axis=0)
    right, bottom = corners.max(axis=0)
    if x < 0 or y < 0 or right > page_pixels.shape[1] or bottom > page_pixels.shape[0]:
        return None
    reference = cv2.warpAffine(page_pixels, transform, (w,h),
                               flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=255)
    source = cv2.GaussianBlur(source, (7,7), 1.5)
    reference = cv2.GaussianBlur(reference, (7,7), 1.5)
    scores = []
    for rows in (slice(0,h//2),slice(h//2,h)):
        for cols in (slice(0,w//2),slice(w//2,w)):
            original, aligned = source[rows,cols], reference[rows,cols]
            if min(original.std(),aligned.std()) < 6:
                return None
            scores.append(float(cv2.matchTemplate(original, aligned, cv2.TM_CCOEFF_NORMED)[0,0]))
    if min(scores) < .90:
        return None
    return fitz.Rect(float(x)/zoom,float(y)/zoom,float(right)/zoom,float(bottom)/zoom),round(min(scores),3)


def remove_archive_fragments(page, items):
    candidates = [i for i in items if i["type"]=="image" and i.get("source_bbox")
                  and not i.get("filter_reason") and not i.get("image_caption") and not i.get("image_footnote")]
    originals = [i for i in items if i.get("preview_source")=="pmc_figure"]
    if not candidates or not originals:
        return []
    zoom = 1.6
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom,zoom), colorspace=fitz.csGRAY, alpha=False)
    page_pixels = np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)
    matches = []
    for figure in originals:
        with Image.open(Path(figure["img_path"])) as image:
            gray = image.convert("L")
            gray.thumbnail((1400,1400), Image.Resampling.LANCZOS)
            location = _locate(np.asarray(gray),page_pixels,zoom)
        if location:
            matches.append((figure,*location))
    changes = []
    for item in candidates:
        rect = fitz.Rect(item["source_bbox"])
        covering = [(figure,box,score) for figure,box,score in matches
                    if rect.get_area()>0 and (rect & box).get_area() >= .98*rect.get_area()
                    and (box+(-2,-2,2,2)).contains(rect)]
        if len(covering)==1:
            figure,box,score=covering[0]
            figure.update(source_bbox=list(box),geometry_source="matched_pmc_pixels")
            item["filter_reason"]="同页 PMC 完整原图已覆盖此局部裁图"
            changes.append({"page":page.number+1,"archive_figure":figure["image_sha256"],
                            "covered_fragment":item["image_sha256"],"min_quadrant_correlation":score})
    return changes
