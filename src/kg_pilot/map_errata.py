"""Apply a source-checked correction only while both old and target coordinates match."""
from collections import Counter

from .map_relations import _current_documents
from .map_semantics import AXES_DIGEST, _citation, _link_valid, digest, pair


def apply_errata(path, papers, claims, links, withheld):
    indexed = {pair(link): link for link in links}
    held = set(withheld)
    stats = Counter()
    for document, ids, pages in _current_documents(path, papers, claims):
        if document.get("axes_digest") != AXES_DIGEST:
            continue
        by_claim = {c["claim_id"]: c for c in claims if c["claim_id"] in ids}
        for edit in document.get("edits", []):
            ident = edit.get("claim_id")
            old = (ident, edit.get("row"), edit.get("column"))
            original = indexed.get(old)
            if not original or ident not in by_claim or old in held or digest(original) != edit.get("old_digest"):
                continue
            if (edit.get("doc_id") != document["doc_id"] or not edit.get("reason") or not edit.get("evidence")
                    or not all(_citation(e, pages) for e in edit["evidence"])):
                continue
            replacement = edit.get("replacement")
            if edit.get("action") == "withdraw_from_cell" and replacement is None:
                held.add(old)
                del indexed[old]
                stats["withdrawn"] += 1
                continue
            if not _link_valid(replacement, original, by_claim[ident], pages):
                continue
            target = pair(replacement)
            if edit.get("action") == "reclassify_method_only" and target == old and replacement["relation"] == "method":
                indexed[old] = replacement
                stats["method"] += 1
            elif (edit.get("action") == "replace_cell" and target != old and target not in indexed
                    and (target in held) == edit.get("target_previously_held")):
                del indexed[old]
                held.add(old)
                held.discard(target)
                indexed[target] = replacement
                stats["moved"] += 1
    return list(indexed.values()), held, dict(stats)
