"""Identify an entire, uncaptioned crop of a previously reviewed publication mark."""
from pathlib import Path

from PIL import Image, ImageOps
from kg_pilot.pipeline import read_json


AUTHOR_TABLE_REASON = "首页作者、单位与联系方式"


def visual_templates(reviewed):
    samples = read_json(Path(__file__).with_name("publication_decoration_templates.json"))
    return [(sha, sample["aspect"], [int(mask, 16) for mask in sample["masks"]])
            for sha, sample in samples.items() if sha in reviewed]


def signature(path):
    # Native PDF crops have an opaque white background. Preserve all ink in the
    # crop, including neighbouring labels, when normalising white margins.
    with Image.open(path) as original:
        ink = ImageOps.invert(original.convert("L"))
    peak = ink.getextrema()[1]
    if peak < 32:
        return None
    box = ink.point(lambda p: 255 if p >= max(16, 0.1 * peak) else 0).getbbox()
    if box is None:
        return None
    ink = ink.crop(box)
    if min(ink.size) < 7:
        return None
    reduced = ink.resize((48, 32), Image.Resampling.LANCZOS)
    masks = []
    for threshold in (0.30 * peak, 0.60 * peak):
        mask = reduced.point(lambda p: 255 if p >= threshold else 0).convert("1")
        masks.append(int.from_bytes(mask.tobytes(), "big"))
    if any(mask.bit_count() < 30 for mask in masks):
        return None
    return ink.width / ink.height, masks


def decoration_reason(item, reviewed, templates, *, pdf_sha=None):
    if item["type"]=="equation" and pdf_sha and type(item.get("page_idx")) is int:
        return reviewed.get(f"{pdf_sha}:p{item['page_idx']+1}:equation:{item.get('image_sha256')}")
    if item["type"] != "image" or item.get("image_caption") or item.get("image_footnote"):
        return None
    digest = item.get("image_sha256")
    if pdf_sha and type(item.get("page_idx")) is int:
        # A journal cover can reuse artwork from the article. A review of that
        # cover applies only to the verified crop on that physical PDF page.
        scoped = reviewed.get(f"{pdf_sha}:p{item['page_idx'] + 1}:{digest}")
        if scoped:
            return scoped
    exact = reviewed.get(digest)
    if exact:
        return exact
    if not item.get("img_path"):
        return None
    try:
        path = Path(item["img_path"])
        with Image.open(path) as original:
            if original.convert("RGB").getextrema() == ((255, 255),) * 3:
                return "纯白误裁"
        if not templates:
            return None
        crop = signature(path)
    except (OSError, ValueError):
        return None
    if crop is None:
        return None
    ratio, masks = crop
    for reference, aspect, reference_masks in templates:
        if reference not in reviewed or max(ratio, aspect) > 1.08 * min(ratio, aspect):
            continue
        # Compare ink only: two almost-white crops do not match merely because
        # their backgrounds agree. Both light and dark strokes must match.
        if all((a ^ b).bit_count() <= 0.12 * (a | b).bit_count() for a, b in zip(masks, reference_masks)):
            return reviewed[reference]
    return None
