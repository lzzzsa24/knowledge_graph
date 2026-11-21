"""Revision-bound PMC figures and display math for native PDF ingestion."""
import hashlib
import io
import os
from pathlib import Path
import re
import unicodedata
import uuid
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError
from urllib.request import urlopen

from lxml import etree
from PIL import Image

from kg_pilot.native_library import read_json


class ArchiveUnavailable(RuntimeError):
    pass


def store_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return
    temp = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _identity(url, revision):
    parts = urlsplit(url)
    checksum = parse_qs(parts.query).get("md5", [""])[0]
    if (parts.scheme != "s3" or parts.netloc != "pmc-oa-opendata" or
            not re.fullmatch("/"+re.escape(revision)+r"/[A-Za-z0-9_.-]+", parts.path)
            or not re.fullmatch("[0-9a-f]{32}", checksum)):
        raise ValueError("PMC asset does not match the archive revision")
    return "https://pmc-oa-opendata.s3.amazonaws.com"+parts.path, checksum


def _fetch(url, revision, job):
    source, digest = _identity(url, revision)
    path = job / "pmc-assets" / digest
    if path.is_file():
        data = path.read_bytes()
        if hashlib.md5(data).hexdigest() == digest:
            return data
    try:
        with urlopen(source, timeout=40) as response:
            data = response.read()
    except (URLError, TimeoutError) as exc:
        raise ArchiveUnavailable(f"无法获取同版 PMC 文件 {revision}/{Path(urlsplit(url).path).name}") from exc
    if hashlib.md5(data).hexdigest() != digest:
        raise ArchiveUnavailable(f"PMC 文件校验失败 {revision}/{Path(urlsplit(url).path).name}")
    store_bytes(path, data)
    return data


def read_archive(pdf, job):
    sidecar = pdf.with_suffix(".pmc.json")
    if not sidecar.is_file():
        return None
    meta = read_json(sidecar)
    if not (meta.get("pdf_url") and meta.get("xml_url") and meta.get("is_pmc_openaccess")):
        return None
    revision = urlsplit(meta["pdf_url"]).path.strip("/").split("/")[0]
    if not re.fullmatch(r"PMC\d+\.\d+", revision):
        return None
    _, expected = _identity(meta["pdf_url"], revision)
    with pdf.open("rb") as f:
        if hashlib.file_digest(f, "md5").hexdigest() != expected:
            return None
    root = etree.fromstring(_fetch(meta["xml_url"], revision, job),
                            parser=etree.XMLParser(resolve_entities=False, no_network=True))
    media = {Path(urlsplit(url).path).name:url for url in meta.get("media_urls", [])}
    figures = {}
    for fig in root.xpath("./body//fig|./floats-group//fig"):
        label = fig.findtext("label") or ""
        match = re.fullmatch(r"Fig(?:ure)?\.?\s*(\d+[A-Za-z]?)\.?", label.strip(), re.I)
        graphics = fig.xpath("./graphic|./alternatives/graphic")
        if not match or len(graphics) != 1:
            continue
        ref = graphics[0].get("{http://www.w3.org/1999/xlink}href")
        if ref in media:
            number = match[1].lower()
            caption = " ".join("".join(fig.xpath("./caption//text()")).split())
            figures[number] = {"caption":f"{label}. {caption}", "url":media[ref], "id":fig.get("id")}
    table_notes = {}
    for table in root.xpath("./body//table-wrap|./floats-group//table-wrap"):
        label=(table.findtext("label") or "").strip()
        match=re.fullmatch(r"Table\s+(\d+[a-z]?)\.?",label,re.I)
        foot=table.find("table-wrap-foot")
        if match and foot is not None:
            notes=[" ".join("".join(p.itertext()).split()) for p in foot.iter("p")]
            caption=" ".join("".join(table.xpath("./caption//text()")).split())
            table_notes[match[1].lower()]={"caption":caption,"notes":[n for n in notes if n]}
    formulas = {}
    inline = []
    math_ns = "{http://www.w3.org/1998/Math/MathML}"
    for elem in root.xpath("./body//disp-formula|./body//inline-formula"):
        math = elem.find(".//"+math_ns+"math")
        tex = elem.findtext(".//tex-math")
        if math is None and not tex:
            continue
        record = {"id":elem.get("id"), "mathml":etree.tostring(math, encoding="unicode",with_tail=False)
                  if math is not None else "", "tex":tex or ""}
        if elem.tag == "inline-formula":
            parent = next(elem.iterancestors("p"), None)
            record["paragraph"] = " ".join("".join(parent.itertext()).split()) if parent is not None else ""
            inline.append(record)
        else:
            label = (elem.findtext("label") or "").strip()
            if re.fullmatch(r"\(\d+[a-z]?\)", label, re.I):
                formulas[label] = record
    return {"revision":revision, "figures":figures, "formulas":formulas, "inline":inline,"table_notes":table_notes}


def archive_figure(archive, number, job, page, old, caption_bbox):
    ref = archive["figures"][number]
    data = _fetch(ref["url"], archive["revision"], job)
    with Image.open(io.BytesIO(data)) as source:
        out = io.BytesIO()
        source.convert("RGB").save(out, format="PNG")
    png = out.getvalue()
    sha = hashlib.sha256(png).hexdigest()
    path = job/"original-regions"/f"p{page:03}-pmc-{sha}.png"
    store_bytes(path, png)
    return {**old,"type":"image","page_idx":page-1,"img_path":str(path.resolve()),"image_sha256":sha,
            "image_caption":[ref["caption"]],"caption_bbox":caption_bbox,
            "preview_source":"pmc_figure","archive_id":ref["id"],"archive_url":ref["url"]}


def same_caption(printed, archive):
    """Confirm a shared caption phrase after its label, ignoring line wrapping and ligatures."""
    def words(value):
        text=unicodedata.normalize("NFKC",value).casefold()
        text=re.sub(r"^\s*(?:figure|fig\.?)\s*\d+[a-z]?[.:]?\s*","",text)
        return re.findall(r"[^\W_]+",text)
    a,b=words(printed),words(archive)
    size=min(4,len(a),len(b))
    if size<2:
        return bool(a) and a==b
    phrases={tuple(a[i:i+size]) for i in range(len(a)-size+1)}
    return any(tuple(b[i:i+size]) in phrases for i in range(len(b)-size+1))


def apply_equations(pdf, items, formulas):
    if not formulas:
        return
    from importlib.resources import files
    xsl = etree.XSLT(etree.parse(str(files("sympy.utilities.mathml")/"data"/"mmltex.xsl")))
    bindings = {}
    for item in items:
        if item["type"] != "equation" or not item.get("source_bbox"):
            continue
        import pymupdf
        box = pymupdf.Rect(item["source_bbox"]) + (-2,-3,3,3)
        text = "".join(pdf[item["page_idx"]].get_textbox(box).split())
        match = re.search(r"(\(\d+[a-z]?\))$", text, re.I)
        if match and match[1] in formulas:
            bindings.setdefault(match[1], []).append(item)
    for label, matches in bindings.items():
        if len(matches) != 1:
            continue
        formula = formulas[label]
        latex = formula["tex"]
        if not latex:
            math = etree.fromstring(formula["mathml"].encode("utf8"))
            latex = str(xsl(math)).strip().strip("$").strip()
        if latex:
            matches[0].update(latex=latex, mathml=formula["mathml"], formula_label=label,
                              formula_source="pmc_xml")
