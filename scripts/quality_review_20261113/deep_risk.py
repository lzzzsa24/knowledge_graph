"""Validate the risk review and reproduce its two image comparisons."""

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw
import pymupdf
from scipy import ndimage
from scipy.optimize import minimize


ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "pilot"
REPORT = PILOT / "reports/quality_review_20261113"
RENDERS = REPORT / "renders/risk"

SOURCES = {
    "ectc": "doc_266f68275dc3780aa334",
    "hvm": "doc_f1bed78ead989436f89d",
    "yap": "reliability_yap_yield_2025",
    "cmp": "doc_af06ee5856591462e2c3",
    "nc": "doc_f5292e6b6d0e24d12f59",
    "adeia": "doc_0086822b49e2d5df8658",
    "theil": "reliability_die_edge_2022",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_papers():
    rows = read_json(PILOT / "catalog/papers.json")
    return {row.get("doc_id") or row.get("id"): row for row in rows}


def image_from_page(path, page, xref):
    with pymupdf.open(path) as document:
        assert xref in {item[0] for item in document[page - 1].get_images()}
        return Image.open(BytesIO(document.extract_image(xref)["image"])).convert("RGB")


def register_images(first, second, initial_transform):
    source = np.asarray(first.convert("L"), dtype=float)
    reference = np.asarray(second.convert("L"), dtype=float)
    rows, columns = np.indices(source.shape)

    def compare(transform, *, remove_low_frequency=False):
        scale_x, scale_y, translate_x, translate_y = transform
        y = rows * scale_y + translate_y
        x = columns * scale_x + translate_x
        inside = (y >= 0) & (y < reference.shape[0] - 1) & (x >= 0) & (x < reference.shape[1] - 1)
        aligned = ndimage.map_coordinates(reference, [y, x], order=1, prefilter=False)
        if remove_low_frequency:
            def detail(array):
                return ndimage.gaussian_filter(array, 1) - ndimage.gaussian_filter(array, 5)

            inside = ndimage.binary_erosion(inside, iterations=10)
            return np.corrcoef(detail(source)[inside], detail(aligned)[inside])[0, 1]
        return np.corrcoef(source[inside], aligned[inside])[0, 1], inside.mean()

    fit = minimize(lambda transform: -compare(transform)[0], initial_transform,
                   method="Nelder-Mead", options={"maxiter": 300})
    correlation, overlap = compare(fit.x)
    return {
        "transform": fit.x.tolist(),
        "transform_order": ["scale_x", "scale_y", "translate_x", "translate_y"],
        "reference_overlap": float(overlap),
        "intensity_correlation": float(correlation),
        "detail_correlation": float(compare(fit.x, remove_low_frequency=True)),
    }


def compare_pair(papers, members, initial_transform, *, render=False):
    images = [
        image_from_page(Path(papers[SOURCES[name]]["local_path"]), page, xref)
        for name, page, xref in members
    ]
    if render:
        canvas = Image.new("RGB", (920, 450), "white")
        labels = ImageDraw.Draw(canvas)
        for index, (photo, (name, page, xref)) in enumerate(zip(images, members, strict=True)):
            photo = photo.copy()
            photo.thumbnail((448, 410))
            x = index * 460
            canvas.paste(photo, (x, 30))
            labels.text((x, 5), f"{name} p{page} xref {xref}", fill="black")
        canvas.save(RENDERS / ("-".join(name for name, _, _ in members) + "-comparison.png"))
    return {
        "members": [
            {"doc_id": SOURCES[name], "pdf_page": page, "image_xref": xref, "image_px": list(photo.size)}
            for photo, (name, page, xref) in zip(images, members, strict=True)
        ],
        **register_images(*images, initial_transform),
    }


def verify_records(papers, records, *, render=False):
    claims = {}
    for line in (PILOT / "annotations/claims.jsonl").read_text(encoding="utf-8").splitlines():
        claim = json.loads(line)
        claims[claim["claim_id"]] = claim["doc_id"]
    for record in records:
        document_id = record["doc_id"]
        name = next(name for name, source in SOURCES.items() if source == document_id)
        paper = papers[document_id]
        pdf = Path(paper["local_path"])
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == paper["sha256"]
        with pymupdf.open(pdf) as document:
            assert all(0 < page <= len(document) for page in record["reviewed_pages"])
        assert set(record["visual_pages"]) <= set(record["reviewed_pages"])
        assert len(record["claims"]) >= 2
        for item in record["claims"]:
            if item["claim_id"] is not None:
                assert claims[item["claim_id"]] == document_id
            for evidence in item["source_evidence"]:
                source_id = evidence.get("doc_id", document_id)
                assert 0 < evidence["pdf_page"] <= papers[source_id]["page_count"]
                assert evidence["quote"].strip()
        for page in record["visual_pages"]:
            prefix = RENDERS / f"{name}-p{page:02}"
            if render:
                subprocess.run(
                    ["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                     "-png", "-scale-to", "1700", str(pdf), str(prefix)],
                    check=True,
                )
            assert prefix.with_suffix(".png").is_file()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true", help="regenerate reviewed pages and image pairs")
    args = parser.parse_args()
    records = read_json(REPORT / "deep/risk.json")
    papers = load_papers()
    verify_records(papers, records, render=args.render)
    pairs = {
        "gao_sem": compare_pair(papers, [("ectc", 5, 112), ("hvm", 5, 18)],
                                (0.82, 0.82, 32, 33), render=args.render),
        "adeia_theil_photo": compare_pair(papers, [("adeia", 16, 72), ("theil", 2, 39)],
                                         (3.71, 3.72, 0, 0), render=args.render),
    }
    print(json.dumps({
        "documents": len(records),
        "reviewed_claims_or_source_results": sum(len(record["claims"]) for record in records),
        "registered_images": pairs,
    }, indent=2))


if __name__ == "__main__":
    main()
