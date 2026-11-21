"""Build page-addressable citations from bibliographies in signed catalog PDFs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
import unicodedata

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot.map_citations import identity_hash

PILOT = ROOT / "pilot"
HEADING = re.compile(
    r"^[\s■□▪●◆•]*(?:(?:[IVX]+|\d+)[ .\t]+)?"
    r"(?:references(?:\s+and\s+notes)?|bibliography|(?:参\s*考\s*)?文\s*献)[\s.:：-]*$", re.I
)
NUMBER = re.compile(r"^\s*(?:[\[［](\d{1,3})[\]］]|[（(](\d{1,3})[)）]|(\d{1,3})\s*[.．)）])"
                    r"(?=\s|$|[A-Za-z\u3040-\u30ff\u3400-\u9fff])")
AUTHOR_START = re.compile(
    r"^[^\W\d_][\w’'’-]*(?:[ -][^\W\d_][\w’'’-]*){0,3},\s*"
    r"(?:[A-ZÀ-ÖØ-Þ][.-]|[A-Z]{1,3}(?=[, .]))"
)
STOP = re.compile(
    r"^(?:disclaimer\s*/?\s*publisher[’']s note\b|publisher[’']s note\b|"
    r"corresponding author\s*[:：]|"
    r"(?:author biographies|biographies|author information|supporting information|"
    r"supplementary information|appendix(?:\s+[A-Z0-9])?|abstract|目录|作者简介)\s*[:：.]?\s*$)", re.I
)
YEAR = re.compile(r"(?<![\w.])(?:18|19|20)\d{2}(?!\d)")
PAGE_NUMBER = re.compile(r"^(?:\d+\s+of\s+\d+|page\s+\d+(?:\s+of\s+\d+)?)$", re.I)
DOI_BODY = re.compile(r"10\.\s*\d{4,9}\s*/\s*[-._;()/:A-Z0-9]+", re.I)
FORMAT_CHARS = str.maketrans("", "", "\u200b\ufeff\u00ad")
CONTACT_FOOTER = re.compile(r"(?:^|\s)(?:phone|tel(?:ephone)?)\s*:|all rights reserved", re.I)


def fold_offsets(text):
    chars, offsets = [], []
    for i, ch in enumerate(text):
        for char in unicodedata.normalize("NFKD", ch.casefold()):
            if char.isalnum():
                chars.append(char)
                offsets.append(i)
    return "".join(chars), offsets


@dataclass(frozen=True)
class Line:
    page: int
    text: str


@dataclass(frozen=True)
class Reference:
    text: str
    pages: tuple[int, ...]   # One physical page per character, including joins
    number: int | None

    @classmethod
    def from_lines(cls, lines, number):
        text_parts, pages = [], []
        for line in lines:
            value = " ".join(line.text.translate(FORMAT_CHARS).split())
            if not value:
                continue
            if text_parts:
                pages.append(line.page)
            text_parts.append(value)
            pages.extend([line.page] * len(value))
        return cls(" ".join(text_parts), tuple(pages), number)


def split_references(lines):
    """Read one reference list. Numbered and author-sorted lists may span pages."""
    usable = []
    markers = []
    for line in lines:
        text = line.text.strip()
        if STOP.match(text):
            break
        if text:
            match = NUMBER.match(text)
            if match:
                markers.append((len(usable), int(next(s for s in match.groups() if s is not None))))
            usable.append(line)
    # A page range (e.g. "615. https://doi.org/...") or "a (111) surface"
    # wrapped at the start of a line may look exactly like a reference number.
    # List neighbours disambiguate it without assuming the PDF reading order
    # is always monotonic across two columns.
    starts = {}
    for i, (line_index, item) in enumerate(markers):
        neighbors = markers[max(0, i - 3):i] + markers[i + 1:i + 4]
        if len(markers) <= 2 or item == 1 or any(abs(item - other) == 1 for _, other in neighbors):
            starts[line_index] = item
    current, number, numbered = [], None, False
    for index, line in enumerate(usable):
        text = line.text.strip()
        marker = starts.get(index)
        author = not numbered and AUTHOR_START.match(text)
        if marker is not None or author:
            if current:
                yield Reference.from_lines(current, number)
            current = []
            if marker is not None:
                number, numbered = marker, True
        elif not current:
            # A heading alone (e.g. a table-of-contents entry) does not make
            # preceding prose the first item of its reference list.
            continue
        current.append(line)
    if current:
        yield Reference.from_lines(current, number)


def name_tokens(authors):
    if isinstance(authors, str):
        # Compact publisher field, e.g. "Kim YS, Nguyen TH, Choa SH."
        authors = [
            m.group(1) + ","
            for part in authors.rstrip(".").split(",")
            if (m := re.fullmatch(r"\s*(.+?)\s+[A-Z]{1,5}\.?\s*", part))
        ]
    candidates = []
    for name in authors if isinstance(authors, list) else []:
        name = name.strip()
        if name:
            family = name.split(",")[0] if "," in name else name.split()[-1]
            token = fold_offsets(family)[0]
            if len(token) >= 2:
                candidates.append(token)
    return candidates


def normalize_doi(value):
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value or "").strip().casefold())


def doi_locations(text, dois):
    """Allow PDF line wrapping, but never match a catalog DOI as an identifier prefix."""
    offsets = [i for i, ch in enumerate(text) if not ch.isspace()]
    compact = "".join(text[i] for i in offsets).lower()
    for match in re.finditer(r"10\.\d{4,9}/", compact):
        start = match.start()
        candidates = [(doi, ids) for doi, ids in dois.get(match.group(), ())
                      if compact.startswith(doi, start)]
        if candidates:
            doi, ids = max(candidates, key=lambda record: len(record[0]))
            end = offsets[start + len(doi) - 1] + 1
            tail = text[end:]
            if len(ids) != 1 or re.match(r"(?:\s+[./_-]\s*[A-Za-z0-9]|\.\s+\d)", tail):
                continue
            if tail and not (tail[0].isspace() or tail[0] in "),;]}>" or
                             (tail[0] in ".:" and (len(tail) == 1 or tail[1].isspace() or tail[1] in ")]}"))):
                continue
            yield ids[0], doi, offsets[start], end


def index_dois(dois):
    groups = defaultdict(list)
    for doi, ids in dois.items():
        if "/" in doi:
            groups[doi.split("/", 1)[0] + "/"].append((doi, ids))
    return groups


def identity_details(ref, year, names, title_start):
    cleaned = DOI_BODY.sub("", ref.text)
    years = {int(v.group()) for v in YEAR.finditer(cleaned)}
    authors = ref.text[:title_start].casefold()
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", authors) if not unicodedata.combining(ch))
    tokens = set(re.findall(r"[^\W_]+", plain))
    author = next((name for name in names if name in tokens), None)
    supported_years = year if isinstance(year, tuple) else (year,)
    year_match = next((int(y) for y in supported_years if y and int(y) in years), None)
    conflict = bool(year and years and not year_match)
    return author, year_match, conflict


def anchor(ref, begin, end, **fields):
    quote_end = len(ref.text)
    if fields["match"] == "doi" and CONTACT_FOOTER.search(ref.text[end:]):
        quote_end = end
    result = {"quote": ref.text[:quote_end], "pdf_page": ref.pages[begin],
              "reference_start_page": ref.pages[0], "reference_end_page": ref.pages[quote_end - 1], **fields}
    if ref.pages[end - 1] != ref.pages[begin]:
        result["anchor_end_page"] = ref.pages[end - 1]
    if ref.number is not None:
        result["reference_number"] = ref.number
    return result


def prefer(current, new):
    def rank(edge):
        # DOI proves an edition. For title evidence prefer matching year and author.
        return (edge["match"] == "doi", bool(edge.get("matched_year")), bool(edge.get("matched_author")),
                not edge.get("anchor_basis"),
                bool(edge.get("reference_number")), -len(edge["quote"]))
    if current is None or rank(new) > rank(current):
        return new
    return current


def near_title(flat, title):
    """Find a single substitution in a long otherwise identical title (corroboration only)."""
    if len(title) < 48:
        return None
    middle = len(title) // 2
    candidates = set()
    for offset, part in ((0, title[:middle]), (middle, title[middle:])):
        pos = flat.find(part)
        while pos >= 0:
            start = pos - offset
            if 0 <= start <= len(flat) - len(title):
                candidates.add(start)
            pos = flat.find(part, pos + 1)
    for start in sorted(candidates):
        if sum(a != b for a, b in zip(title, flat[start:start + len(title)])) == 1:
            return start
    return None


def venue_codes(text):
    provenance = re.sub(r'["“][^"”]*["”]', '', text)
    return set(re.findall(r"\b[A-Z][A-Z0-9]{2,}\b", provenance)) - {"USA", "IEEE", "DOI", "ISBN"}


def match_references(references, targets, doi_index, self_id):
    found, conflicts, indexed = {}, defaultdict(list), []
    for ref in references:
        if not ref.text:
            continue
        flat, offsets = fold_offsets(ref.text)
        indexed.append((ref, flat, offsets))
        for target, doi, begin, end in doi_locations(ref.text, doi_index):
            if target != self_id:
                found[target] = prefer(found.get(target), anchor(ref, begin, end, match="doi", matched_doi=doi))
        for target, title, year, names in targets:
            if target == self_id:
                continue
            pos = flat.find(title)
            if pos < 0:
                continue
            author, matched_year, conflict = identity_details(ref, year, names, offsets[pos])
            if not (matched_year or author):
                continue
            proof = anchor(ref, offsets[pos], offsets[pos + len(title) - 1] + 1,
                           match="full_title", matched_author=author, matched_year=matched_year)
            if conflict:
                conflicts[target].append(proof)
            else:
                found[target] = prefer(found.get(target), proof)

    # A near title alone never creates an edge. A conflicting exact-title entry
    # needs independent title/author/year corroboration from the same bibliography.
    for target, title, year, names in targets:
        if target in found or target not in conflicts:
            continue
        for ref, flat, offsets in indexed:
            pos = near_title(flat, title)
            if pos is None:
                continue
            author, matched_year, _ = identity_details(ref, year, names, offsets[pos])
            if not (author and matched_year):
                continue
            supporting = next((proof for proof in conflicts[target]
                               if venue_codes(ref.text) & venue_codes(proof["quote"])), None)
            if supporting is None:
                continue
            corroborating = anchor(ref, offsets[pos], offsets[pos + len(title) - 1] + 1,
                                   match="full_title", matched_author=author, matched_year=matched_year,
                                   anchor_basis="corroborated_variant")
            corroborating["exact_title_evidence"] = supporting
            found[target] = corroborating
            break
    return found


def find_targets(text, targets, dois, self_id):
    """Compatibility entry point for checking a bibliography string."""
    sources = (Line(1, line) for line in text.splitlines())
    return match_references(split_references(sources), targets, index_dois(dois), self_id)


def page_lines(page):
    # Native block order preserves columns in most journals. Geometry also permits
    # an independent column-order read when a producer interleaves left and right.
    width, height = page.rect.width, page.rect.height
    native = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                native.append((text, x0 / width, y0 / height, x1 / width, y1 / height))
    return native


def order_columns(lines):
    left = [l for l in lines if l[3] <= .53 and l[3] - l[1] > .16]
    right = [l for l in lines if l[1] >= .47 and l[3] - l[1] > .16]
    if len(left) < 4 or len(right) < 4:
        return lines
    split = (max(l[3] for l in left) + min(l[1] for l in right)) / 2
    if any(l[1] < split - .03 and l[3] > split + .03 for l in lines if .12 < l[2] < .90):
        return lines
    return sorted(lines, key=lambda l: (l[1] >= split - .015, round(l[2], 3), l[1]))


def section_lines(pages, running, columns):
    active = False
    for page_num, lines in pages:
        for text, x0, y0, x1, y1 in order_columns(lines) if columns else lines:
            if HEADING.fullmatch(text):
                active = True
                continue
            if not active:
                continue
            if text.lower().startswith(("downloaded from http", "visit www.")):
                continue
            margin_number = text.isdecimal() and (int(text) == page_num or y1 > .95)
            if (y0 < .11 or y1 > .90) and (text in running or margin_number or PAGE_NUMBER.fullmatch(text)):
                continue
            if STOP.match(text):
                return
            yield Line(page_num, text)


def scan_pdf(paper, targets, doi_index):
    path = Path(paper["local_path"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != paper["sha256"]:
        raise ValueError("The source PDF differs from its catalog fingerprint: " + paper["doc_id"])
    pages = []
    with pymupdf.open(path) as pdf:
        if len(pdf) != paper["page_count"]:
            raise ValueError("Page count differs")
        for num, page in enumerate(pdf, 1):
            if not pages and not any(HEADING.fullmatch(s.strip()) for s in page.get_text().splitlines()):
                continue
            pages.append((num, page_lines(page)))
    if not pages:
        return False, {}
    # Repeat-only page furniture is safe to remove without admitting body text.
    counts = Counter()
    for _, lines in pages:
        counts.update({s for s, _, y0, _, y1 in lines if y0 < .11 or y1 > .90})
    running = {line for line, n in counts.items() if n >= 2 and not NUMBER.match(line)}
    found = {}
    for columns in (False, True):
        lines = section_lines(pages, running, columns)
        for target, edge in match_references(split_references(lines), targets, doi_index, paper["doc_id"]).items():
            found[target] = prefer(found.get(target), edge)
    return True, found


def publication_years(paper):
    """A proceedings deposit date may differ from the meeting printed on the PDF."""
    catalog_year = paper.get("year")
    with pymupdf.open(paper["local_path"]) as pdf:
        front = pdf[0].get_text() if len(pdf) else ""
    event = re.findall(
        r"(?im)^\s*(?:presented at|proceedings of (?:the )?)"
        r"(?=[^\n]{0,180}\b(?:conference|symposium|workshop|congress|meeting)\b)"
        r"[^\n]{0,175}?(?<!\d)((?:19|20)\d{2})(?!\d)", front
    )
    return tuple(dict.fromkeys(int(y) for y in (catalog_year, *event) if y)) or None


def build(papers, year_evidence=()):
    dois, by_title = defaultdict(list), defaultdict(list)
    for paper in papers:
        doi = normalize_doi(paper.get("doi"))
        if doi:
            dois[doi].append(paper["doc_id"])
        title = fold_offsets(paper["title"])[0]
        if len(title) >= 28 and len(paper["title"].split()) >= 5:
            by_title[title].append(paper)
    # Separately verified publication years, e.g. e-newsletter ahead of print.
    # Bind each claim to the same catalog/PDF edition served by the map.
    by_edition = defaultdict(dict)
    for item in year_evidence:
        if (type(item.get("year")) is not int or not item.get("source_url", "").startswith("https://") or
                not item.get("finding") or not item.get("kind")):
            raise ValueError("Year evidence lacks year, source or review finding")
        by_edition[item["target_signature"]][item["year"]] = item
    targets, alternate_years = [], {}
    for title, editions in by_title.items():
        if len(editions) != 1:
            continue
        paper = editions[0]
        years = publication_years(paper) or ()
        alternate = dict(by_edition.get(identity_hash(paper), {}))
        for y in years:
            if y != paper.get("year"):
                alternate.setdefault(y, {"year": y, "kind": "meeting_printed_on_first_page",
                                          "pdf_page": 1, "target_signature": identity_hash(paper)})
        alternate_years[paper["doc_id"]] = alternate
        targets.append((paper["doc_id"], title, tuple(dict.fromkeys((*years, *alternate))),
                        name_tokens(paper.get("authors"))))
    doi_index = index_dois(dois)
    docs, edges = [], []
    for paper in papers:
        heading, found = scan_pdf(paper, targets, doi_index)
        docs.append({"doc_id": paper["doc_id"], "signature": identity_hash(paper), "references_heading": heading})
        for target, edge in found.items():
            evidence = alternate_years.get(target, {}).get(edge.get("matched_year"))
            if evidence:
                edge["matched_year_source"] = evidence
        edges.extend({"citing": paper["doc_id"], "cited": target, **e} for target, e in sorted(found.items()))
    return {"schema": 1, "method": "signed_pdf_bibliography_doi_or_full_title_and_identifier",
            "documents": docs, "edges": edges}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--publish", action="store_true")
    destination.add_argument("--output", type=Path, help="Write a candidate snapshot to a separate path.")
    parser.add_argument("--year-evidence", type=Path,
                        help="Optional reviewed evidence of other release years for fingerprinted editions.")
    args = parser.parse_args()
    production = PILOT / "research_map/citation_edges.json"
    if args.output and args.output.resolve() == production.resolve():
        parser.error("Writing the production snapshot requires --publish.")
    papers = json.loads((PILOT / "catalog/papers.json").read_text("utf-8-sig"))
    evidence_path = args.year_evidence or (PILOT / "research_map/citation_year_evidence.json")
    evidence = json.loads(evidence_path.read_text("utf-8-sig"))["records"] if args.year_evidence or evidence_path.is_file() else ()
    data = build(papers, evidence)
    out = production if args.publish else args.output
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=out.parent, suffix=".tmp", mode="w", encoding="utf8", delete=False) as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        Path(file.name).replace(out)
    print(len(data["documents"]), "sources,", len(data["edges"]), "within-library references;",
          sum(d["references_heading"] for d in data["documents"]), "recognized reference sections")


if __name__ == "__main__":
    main()
