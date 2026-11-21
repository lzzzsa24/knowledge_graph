"""Evidence-backed display metadata for the public paper catalog."""

import json
import re
from pathlib import Path


PAPER_METADATA_OVERRIDES = "catalog/paper_metadata_overrides.json"
_INITIALS = re.compile(r"(?:[A-Z]\.?){1,3}(?:[- ](?:[A-Z]\.?){1,3})*")
_SURNAME_PARTICLES = {"da", "de", "del", "della", "den", "der", "di", "dos", "du", "la", "le", "van", "von"}
_COMPOUND_CHINESE_SURNAMES = {
    "上官", "令狐", "司徒", "司马", "司馬", "夏侯", "欧阳", "歐陽", "皇甫",
    "诸葛", "諸葛", "慕容", "长孙", "長孫", "尉迟", "尉遲",
}
_CJK = re.compile(r"[\u3400-\u9fff]")


def normalize_authors(value: object) -> list[str]:
    """Read list, delimited byline, or single surname-first bibliographic name."""
    if isinstance(value, str):
        names = [name.strip() for name in re.split(r"[;；、\n]", value) if name.strip()]
        if len(names) == 1:
            parts = [part.strip() for part in re.split(r"[,，]", names[0])]
            # A legacy citation byline has the form 'Ong JJ, Chiu WL'.
            # A single inverted name can also contain a comma, as in 'YAN, Meng'.
            if len(parts) > 1 and all(
                len(words) > 1 and _INITIALS.fullmatch(" ".join(words[1:]))
                for words in (part.rstrip(".").split() for part in parts)
            ):
                names = [part.rstrip(".") for part in parts]
    elif isinstance(value, list):
        names = [name.strip() for name in value if isinstance(name, str) and name.strip()]
    else:
        names = []
    return names


def author_family_name(name: str) -> str:
    """Extract a surname when the byline uses an ordinary catalog name format."""
    inverted = re.split(r"[,，]", name, maxsplit=1)
    if len(inverted) == 2:
        family = inverted[0].strip()
    else:
        words = name.split("(", 1)[0].split()
        if not words:
            return ""
        if _CJK.match(words[0]):
            if len(words) > 1:
                return words[0]
            if words[0][:2] in _COMPOUND_CHINESE_SURNAMES:
                return words[0][:2]
            return words[0][0]
        if len(words) > 1 and _INITIALS.fullmatch(" ".join(words[1:])):
            family = words[0]
        else:
            if len(words) > 1 and words[-1].rstrip(".").lower() in {"jr", "sr", "ii", "iii", "iv"}:
                words.pop()
            start = len(words) - 1
            while start and words[start - 1].lower() in _SURNAME_PARTICLES:
                start -= 1
            family = " ".join(words[start:])
    return family.title() if family.isupper() else family


def load_paper_metadata_overrides(pilot: Path) -> dict[str, dict]:
    path = pilot / PAPER_METADATA_OVERRIDES
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return {}
    documents = data.get("documents")
    if not isinstance(documents, dict):
        return {}
    return {doc_id: row for doc_id, row in documents.items() if isinstance(row, dict)}


def apply_paper_metadata(papers: list[dict], pilot: Path) -> list[dict]:
    """Project authors, organization and labels without modifying catalog identity."""
    overrides = load_paper_metadata_overrides(pilot)
    result = []
    for paper in papers:
        override = overrides.get(paper.get("doc_id") or paper.get("id"), {})
        if not override.get("sha256") or override["sha256"] != paper.get("sha256"):
            override = {}
        authors = override.get("authors")
        if not isinstance(authors, list) or any(not isinstance(name, str) or not name.strip() for name in authors):
            authors = paper.get("authors")
        authors = normalize_authors(authors)
        organization = override.get("organization")
        if not isinstance(organization, str) or not organization.strip():
            organization = paper.get("organization")
        family = override.get("family_name")
        if not isinstance(family, str) or not family.strip():
            family = author_family_name(authors[0]) if authors else ""
        label_name = family if authors else organization or "Unknown"
        row = dict(paper, authors=authors)
        if organization:
            row["organization"] = organization
        label_year = override.get("label_year")
        year = paper.get("year") or (label_year if type(label_year) is int else None) or "n.d."
        row["short_label"] = f"{label_name} {year}"
        result.append(row)
    return result
