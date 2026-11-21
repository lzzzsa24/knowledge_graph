"""Compact, lossless overview projection of the existing research responses."""
from __future__ import annotations


CELL_FIELDS = ("row", "column", "documents", "claims", "experiments", "simulations", "contrasts", "study_ids")
LINK_FIELDS = ("id", "source", "target", "kind", "label", "study_id", "basis", "basis_note")
DIRECTION_FIELDS = ("id", "title", "row", "column", "status", "system_label", "question", "gap")


def build_research_graph(matrix: dict, network: dict, papers: list[dict], *, route: str | None = None) -> dict:
    """Select existing links, preserving IDs and the matrix's exact counts.

    Rebuilding a route-specific network could change its degree-limited links,
    creating IDs absent from the full evidence endpoint. Filter that endpoint's
    existing links instead. Document IDs also let clients filter unmapped dots.
    """
    document_ids = sorted(p["doc_id"] for p in papers
                          if not route or route in (p.get("classification") or {}).get("routes", []))
    selected = set(document_ids)
    claim_cells = {}
    for cell in matrix["cells"]:
        for cid in cell["records"]:
            claim_cells.setdefault(cid, []).append([cell["row"], cell["column"]])
    return {
        "rows": matrix["rows"], "columns": matrix["columns"],
        "claim_cells": claim_cells,
        "cells": [{key: cell[key] for key in CELL_FIELDS} for cell in matrix["cells"]],
        "links": [{key: link[key] for key in LINK_FIELDS if key in link}
                  for link in network["links"] if link["source"] in selected and link["target"] in selected],
        "directions": [{key: item[key] for key in DIRECTION_FIELDS if key in item} for item in matrix["directions"]],
        "studies": [{key: item[key] for key in ("id", "title", "conclusion")} for item in matrix["studies"]],
        "totals": dict(matrix["totals"]), "route": route, "document_ids": document_ids,
        "network_snapshot_id": network.get("snapshot_id"),
    }
