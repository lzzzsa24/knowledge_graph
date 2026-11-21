"""Prepare publication assets, correct PDF crops, and review suspect pages before indexing."""
import asyncio
from collections import Counter, defaultdict
from contextlib import nullcontext
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import pymupdf as fitz
from PIL import Image, ImageChops, ImageDraw, ImageStat

from kg_pilot.crop_audit import AUDIT_POLICY, FOLLOWUP_POLICY, review_page
from kg_pilot.crop_geometry import bind_local_captions, candidate_boxes, item_label, label_key, overlaps, printed_labels, repair_page
from kg_pilot.crop_edges import check_page_edges
from kg_pilot.crop_sources import ArchiveUnavailable, apply_equations, archive_figure, read_archive, same_caption
from kg_pilot.decoration_filter import decoration_reason
from kg_pilot.native_library import file_hash, read_json, save

NONTECHNICAL_MARK = re.compile(
    r"\b(?:logo|masthead|wordmark|navigation icon|publication metadata|license badge|"
    r"publisher(?:['’]s)? (?:mark|badge|button)|footer (?:branding )?mark|journal cover thumbnail|"
    r"open[- ]access (?:padlock|icon|badge)|creative commons (?:license|icon|badge)|crossmark icon|"
    r"(?:envelope|email) icon|check for updates)\b", re.I)


def needs_followup(audit, sha):
    return (audit.get("revision") == sha and audit.get("status") == "needs_review"
            and bool(audit.get("held")) and audit.get("followup_policy") != FOLLOWUP_POLICY)


@lru_cache(maxsize=1)
def _classifier(path):
    from transformers import EfficientNetForImageClassification, EfficientNetImageProcessor
    model = EfficientNetForImageClassification.from_pretrained(path,local_files_only=True).eval()
    return EfficientNetImageProcessor.from_pretrained(path,local_files_only=True), model


def classify(path, tools_dir):
    import torch
    processor, model = _classifier(tools_dir/"classifier")
    with Image.open(path) as image, torch.inference_mode():
        scores = model(**processor(images=image.convert("RGB"),return_tensors="pt")).logits[0].softmax(0)
        idx = int(scores.argmax())
        return {"label":model.config.id2label[idx].lower(),"confidence":round(float(scores[idx]),5)}


def _mark_position(pdf, item, items):
    n = item["page_idx"]
    page = pdf[n]
    box = fitz.Rect(item["source_bbox"])
    if box.height > page.rect.height*.08 or box.width > page.rect.width*.32:
        return False
    if n==0:
        abstracts=page.search_for("Abstract")
        if abstracts and box.y1 < min(r.y0 for r in abstracts) and box.y1 < page.rect.height*.4:
            # Restrict to the masthead or a small author-line badge.
            if box.y1 < page.rect.height*.13 or (box.width < 30 and box.height < 20):
                return True
    if box.y1 < page.rect.height*.15 or box.y0 > page.rect.height*.87:
        return any(other["page_idx"]!=n and other["image_sha256"]==item["image_sha256"]
                   and other.get("source_bbox") and overlaps(box,other["source_bbox"])>.85 for other in items)
    return False


def _page_targets(items, labels):
    found = Counter(item_label(i) for i in items if i.get("caption_source")!="shared_panel")
    targets={k for k in labels if found[k]!=1}
    for item in items:
        key=item_label(item)
        if key not in labels or item["type"]!="image" or not item.get("source_bbox"):
            continue
        area=fitz.Rect(item["source_bbox"])
        # A line's last glyph can extend a fraction beyond the detector box.
        # Count a caption embedded in the interior even with that overhang.
        inside={k for k,(_,b) in labels.items() if k[0]=="image" and area.y0+2 < b[1]
                and b[3] < area.y1-2 and overlaps(area,b)>=.8}
        if inside:
            targets.update(inside | {key})
    return targets


def _publisher_context(page,item,detail):
    if not item.get("source_bbox") or item["page_idx"]!=0:
        return False
    box=fitz.Rect(item["source_bbox"])
    # An explicit advertisement heading immediately above the artwork is
    # independent evidence even when the classifier labels it as a diagram.
    if re.search(r"advertis|promotional",detail,re.I):
        for heading in page.search_for("advertisement"):
            if 0<=box.y0-heading.y1<=28 and box.x0-18<=heading.x0<box.x1:
                return True
    if not re.search(r"masthead|publisher.{0,20}logo|journal.{0,20}(?:logo|wordmark)",detail,re.I):
        return False
    # Require a publisher link physically inside the masthead. Links to a
    # figure, author profile, dataset, or an unrelated journal are insufficient.
    from urllib.parse import urlsplit
    for link in page.get_links():
        host=(urlsplit(link.get("uri","")).hostname or "").lower()
        if (host in {"mdpi.com","www.mdpi.com","pubs.aip.org","www.jsme.or.jp"}
                and box.y1<=page.rect.height*.14 and overlaps(box,link["from"])>.8):
            return True
    return False


def _confirmed_junk(page, item, issue, pdf, raw, detail=""):
    if item["type"]!="image" or item.get("image_caption") or item.get("image_footnote"):
        return False
    if issue=="decoration":
        pred=item.get("picture_class",{})
        return (_publisher_context(page,item,detail) or
                (pred.get("label") in ("icon","logo") and pred.get("confidence",0)>=.45
                 and item.get("source_bbox") and _mark_position(pdf,item,raw)))
    with Image.open(item["img_path"]) as image:
        ink=ImageChops.invert(image.convert("L"))
    if issue=="blank":
        return ink.getbbox() is None
    if issue!="text_only" or not item.get("source_bbox"):
        return False
    # Verify that (almost) every ink pixel is inside the native glyph boxes
    # of a short prose fragment. White space alone cannot reject a plot.
    area=fitz.Rect(item["source_bbox"])
    if area.is_empty:
        return False
    words=[w for w in page.get_text("words",clip=area) if area.contains(fitz.Rect(w[:4]))]
    if not 3<=len(words)<=22 or not re.search(r"[a-z]{3}.*[a-z]{3}[.,;:]$", " ".join(w[4] for w in words),re.I):
        return False
    outside=ink.copy()
    draw=ImageDraw.Draw(outside)
    sx,sy=ink.width/area.width,ink.height/area.height
    for x,y,r,b,*_ in words:
        draw.rectangle(((x-area.x0)*sx-2,(y-area.y0)*sy-2,(r-area.x0)*sx+2,(b-area.y0)*sy+2), fill=0)
    return ImageStat.Stat(outside).sum[0]<=.002*ImageStat.Stat(ink).sum[0]


def _archive_on_page(archive, job, n, items, labels, uncertain, notes):
    for key, (printed, caption_box) in labels.items():
        if key[0]!="image" or key[1] not in archive["figures"]:
            continue
        if not same_caption(printed,archive["figures"][key[1]]["caption"]):
            notes.append({"page":n,"pdf_fallback":"XML 图注与 PDF 图注无法对应","label":list(key)})
            continue
        matches=[i for i in items if item_label(i)==key]
        if len(matches)>1:
            continue
        old=matches[0] if matches else {}
        try:
            new=archive_figure(archive,key[1],job,n,old,caption_box)
        except ArchiveUnavailable as exc:
            notes.append({"page":n,"pdf_fallback":str(exc)})
            continue
        if key in uncertain:
            new.pop("source_bbox",None)
        if matches:
            items[items.index(old)]=new
        else:
            items.append(new)


def _bind_table_notes(page,item,archive):
    key=item_label(item)
    rect=fitz.Rect(item["source_bbox"])
    if not key or item.get("table_footnote"):
        return
    archived=(archive or {}).get("table_notes",{}).get(key[1],{})
    verified=archived.get("notes",[]) if same_caption(" ".join(item["table_caption"]),archived.get("caption","")) else []
    for entry in page.get_text("blocks"):
        box=fitz.Rect(entry[:4])
        if not -1<=box.y0-rect.y1<=10 or not rect.x0-8<=box.x0<rect.x1:
            continue
        text=" ".join(entry[4].split())
        source=[note for note in verified if same_caption(text,note)]
        if source or re.match(r"^(?:Notes?|Sources?)\s*[:.]\s+\S",text,re.I):
            item.update(table_footnote=source or [text],footnote_bbox=list(box),
                        footnote_source="pmc_xml_and_pdf" if source else "pdf")
            return


def _check_table_scans(page_no,items,confirmed):
    findings=[]
    tables=[item for item in items if item["type"]=="table" and item.get("source_bbox")]
    for image in items:
        if (image["type"]!="image" or image.get("image_caption") or image.get("filter_reason")
                or not image.get("source_bbox")):
            continue
        outer=fitz.Rect(image["source_bbox"])
        enclosed=[]
        for table in tables:
            inner=fitz.Rect(table["source_bbox"])
            if outer.get_area()>2*inner.get_area()>0 and overlaps(outer,inner)>.97:
                enclosed.append(table)
        for table in enclosed:
            pair={"page":page_no,"image":image["image_sha256"],"table":table["image_sha256"]}
            match=next((entry for entry in confirmed if all(entry[k]==v for k,v in pair.items())),None)
            if len(enclosed)==1 and match and match["notes"]:
                table["table_footnote"]=list(dict.fromkeys([*table.get("table_footnote",[]),*match["notes"]]))
                table["footnote_source"]="verified_scan"
                table["footnote_source_sha256"]=pair["image"]
                image["filter_reason"]="重复扫描表格，表外附注已随同页表格保存"
            else:
                findings.append({**pair,"reason":"未编号大图覆盖已裁表格，需核对表外内容"})
    return findings


def _prepare(pdf_path, job, row, regions, tools_dir, reviewed, templates, confirmed_scans=()):
    notes=[]
    try:
        archive=read_archive(pdf_path,job)
    except ArchiveUnavailable as exc:
        archive=None
        notes.append({"pdf_fallback":str(exc)})
    labels=printed_labels(pdf_path)
    pages=defaultdict(list)
    for i in regions:
        pages[i["page_idx"]+1].append(dict(i))
    raw = [r for items in pages.values() for r in items]
    boxes=None
    risks=set()
    held=[]
    with fitz.open(pdf_path) as pdf:
        if archive:
            apply_equations(pdf,raw,archive["formulas"])
        missing_archive=set(archive["figures"]) if archive else set()
        archive_pages=defaultdict(set)
        for n,page_labels in enumerate(labels,1):
            for kind,num in page_labels:
                if kind=="image":
                    archive_pages[num].add(n)
        for n in range(1,len(pdf)+1):
            items=pages[n]
            expected=labels[n-1]
            bind_local_captions(items,expected)
            targets=_page_targets(items,expected)
            if targets:
                risks.add(n)
                if boxes is None:
                    boxes=candidate_boxes(pdf_path,job,row["sha256"],tools_dir)
                items, fixed=repair_page(pdf,n,items,expected,targets,boxes,job)
                notes.extend({"page":n,"repair":kind+" "+num} for kind,num in sorted(fixed))
                unresolved=targets-fixed
            else:
                unresolved=set()
            if archive:
                bound={k:v for k,v in expected.items() if archive_pages[k[1]]=={n}}
                _archive_on_page(archive,job,n,items,bound,unresolved,notes)
                for i in items:
                    key=item_label(i)
                    if key in bound and key[0]=="image" and (i.get("preview_source")=="pmc_figure" or
                                                             i.get("source_bbox") and key not in unresolved):
                        missing_archive.discard(key[1])
                        if i.get("preview_source")=="pmc_figure":
                            unresolved.discard(key)
            held.extend({"page":n,"reason":"缺少可靠裁框","label":list(k)} for k in sorted(unresolved))
            for i in items:
                reason=decoration_reason(i,reviewed,templates,pdf_sha=row["sha256"])
                if reason:
                    i["filter_reason"]=reason
                elif i["type"]=="image" and not i.get("image_caption") and not i.get("image_footnote"):
                    risks.add(n)
                    prediction=classify(i["img_path"],tools_dir)
                    i["picture_class"]=prediction
                    if (prediction["label"] in ("logo","icon") and prediction["confidence"]>=.9
                            and i.get("source_bbox") and _mark_position(pdf,i,raw)):
                        i["filter_reason"]="Docling "+prediction["label"]+" · 作者栏或重复页眉页脚"
            for item in items:
                if item["type"]=="table" and item.get("source_bbox"):
                    _bind_table_notes(pdf[n-1],item,archive)
            if archive:
                from kg_pilot.crop_registration import remove_archive_fragments
                notes.extend(remove_archive_fragments(pdf[n-1],items))
            edges=check_page_edges(pdf[n-1],items,expected,job)
            if edges:
                notes.extend(edges)
                risks.add(n)
            # A scan can contain both a separately detected table and additional
            # notes outside that table. Do not discard either automatically.
            conflicts=_check_table_scans(n,items,confirmed_scans)
            if conflicts:
                held.extend(conflicts)
                risks.add(n)
            if len(pdf[n-1].get_text().strip())<60:
                risks.add(n)
            pages[n]=items
        held.extend({"reason":"原版插图未定位到唯一的 PDF 页","figure":num} for num in sorted(missing_archive))
    return pages, labels, risks, held, notes, archive


async def prepare_reviewed_regions(pilot,row,pdf_path,job,regions,hooks,reviewed,templates,*,layout_slot=None,cached_only=False):
    tools_dir=pilot/"model_cache"/"crop-tools"
    report_path=job/"crop-review.json"
    sidecar=pdf_path.with_suffix(".pmc.json")
    confirmed_scans=read_json(Path(__file__).with_name("reviewed_table_scans.json")).get(row["sha256"],[])
    identity=hashlib.sha256(json.dumps(["crop-edges-and-captions-v8",AUDIT_POLICY,row["sha256"],regions,reviewed,templates,
                                        file_hash(sidecar) if sidecar.is_file() else None,*confirmed_scans],
                                      ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    previous = defaultdict(list)
    prior_findings = []
    if report_path.exists():
        old=read_json(report_path)
        if old.get("identity")==identity and old.get("status")=="accepted" and all(
                Path(i["img_path"]).is_file() and file_hash(Path(i["img_path"]))==i["image_sha256"] for i in old["items"]):
            return old["items"]
        if needs_followup(old, row["sha256"]):
            prior_findings = old["held"]
            for issue in prior_findings:
                if issue.get("page"):
                    previous[issue["page"]].append(issue)
    if cached_only:
        from kg_pilot.native_pages import STALE_INPUTS
        raise ValueError(STALE_INPUTS)
    # Both layout and crop classification initialize local Torch models.
    async with layout_slot if layout_slot is not None else nullcontext():
        pages, labels, risks, held, notes, archive=await asyncio.to_thread(
            _prepare,pdf_path,job,row,regions,tools_dir,reviewed,templates,confirmed_scans)
    # Cover at least one otherwise clean page, including documents without detected figures.
    normal=[n for n in pages if n not in risks and any(not i.get("filter_reason") for i in pages[n])]
    normal=normal or [n for n in pages if n not in risks]
    selected=sorted(risks | previous.keys() | ({normal[int(row["sha256"][:8],16)%len(normal)]} if normal else set()))
    kept={n:[i for i in pages[n] if not i.get("filter_reason")] for n in selected}
    with fitz.open(pdf_path) as pdf:
        slots=asyncio.Semaphore(4)

        async def run(n):
            async with slots:
                options = {"previous": previous[n]} if previous.get(n) else {}
                return await review_page(pdf,n,kept[n],hooks,**options)

        async def check(page_numbers):
            # Drain tasks before closing the document if a request fails.
            answers=await asyncio.gather(*(run(n) for n in page_numbers),return_exceptions=True)
            for answer in answers:
                if isinstance(answer,BaseException):
                    raise answer
            return answers

        def filter_confirmed(n, issue, refs):
            kind=issue["kind"]
            def agrees(item):
                if _confirmed_junk(pdf[n-1],item,kind,pdf,regions,issue["detail"]):
                    return True
                if (kind!="decoration" or not previous.get(n) or item["type"]!="image"
                        or item.get("image_caption") or item.get("image_footnote") or not item.get("source_bbox")):
                    return False
                # The independent review must identify a specific nontechnical mark, and
                # the same region must have been flagged as decoration in the first pass.
                identifier = [item["page_idx"], item["type"], item["image_sha256"]]
                for prior in previous[n]:
                    if prior.get("kind") != kind:
                        continue
                    reports = old.get("repair_reviews" if prior.get("round")=="after_repair" else "reviews", [])
                    keys = next((r["regions"] for r in reports if r["page"]==n), [])
                    if any(keys[j-1]==identifier for j in prior["regions"] if 0 < j <= len(keys)):
                        return NONTECHNICAL_MARK.search(issue["detail"]) is not None
                return False
            if kind not in ("decoration","blank","text_only") or not refs or not all(
                    agrees(i) for i in refs):
                return False
            for item in refs:
                item["filter_reason"]={"decoration":"装饰图标","blank":"纯白误裁","text_only":"仅含正文残行"}[kind]+" · 裁图复核确认"
            notes.append({"page":n,"audit_filter":issue})
            return True

        answers=await check(selected)
        boxes=None
        repaired_pages=set()
        for result in answers:
            n=result["page"]
            changed=set()
            for issue in result["issues"]:
                targets={label_key(s) for s in issue["labels"]}
                refs=[kept[n][j-1] for j in issue["regions"]]
                if filter_confirmed(n,issue,refs):
                    continue
                if issue["kind"] in ("missing","merged","wrong_caption","cut") and targets and targets<=labels[n-1].keys():
                    # Archive figures already contain the complete source asset. A cropped
                    # or merged PDF overlay still needs a corrected physical boundary.
                    if boxes is None:
                        boxes=await asyncio.to_thread(candidate_boxes,pdf_path,job,row["sha256"],tools_dir)
                    before=pages[n]
                    updated, fixed=repair_page(pdf,n,before,labels[n-1],targets,boxes,job)
                    before_geometry={(item_label(i),tuple(i.get("source_bbox",[])),tuple(i.get(i["type"]+"_caption",[])))
                                     for i in before if item_label(i) in targets or i in refs}
                    after_geometry={(item_label(i),tuple(i.get("source_bbox",[])),tuple(i.get(i["type"]+"_caption",[])))
                                    for i in updated if item_label(i) in targets or i in refs}
                    corrected=before_geometry!=after_geometry and all(i not in updated for i in refs)
                    if issue["kind"]=="cut":
                        corrected=corrected and bool(refs) and any(
                            old.get("source_bbox")!=new.get("source_bbox") for old in refs
                            for new in updated if item_label(new) in targets)
                    if issue["kind"]=="merged":
                        corrected=corrected and len(targets)>1
                    if fixed and corrected:
                        pages[n]=updated
                        if archive:
                            unique={k:v for k,v in labels[n-1].items() if sum(k in l for l in labels)==1}
                            _archive_on_page(archive,job,n,pages[n],unique,set(),notes)
                        changed.update(fixed)
                        repaired_pages.add(n)
                        notes.append({"page":n,"audit_repair":issue})
                        continue
                    if targets<=changed:
                        continue
                held.append({"page":n,**issue})
        kept={n:[i for i in pages[n] if not i.get("filter_reason")] for n in repaired_pages}
        repair_reviews=await check(sorted(repaired_pages))
        for result in repair_reviews:
            n=result["page"]
            for issue in result["issues"]:
                refs=[kept[n][j-1] for j in issue["regions"]]
                if not filter_confirmed(n,issue,refs):
                    held.append({"page":n,"round":"after_repair",**issue})
    items=[i for n in sorted(pages) for i in pages[n]]
    followup = {"followup_policy":FOLLOWUP_POLICY, "prior_findings":prior_findings} if prior_findings else {}
    save(report_path,{"identity":identity,"policy":AUDIT_POLICY,"status":"needs_review" if held else "accepted",
                      "revision":row["sha256"],"selected_pages":selected,"reviews":answers,
                      "repair_reviews":repair_reviews,"held":held,
                      "repairs":notes,"items":items,"inline_formulas":archive["inline"] if archive else [], **followup})
    if held:
        raise ValueError(f"裁图待复核：{len(held)} 项，见 {report_path}")
    return items
