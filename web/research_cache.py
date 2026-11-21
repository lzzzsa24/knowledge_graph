"""Version the read-only matrix response by its source files, including review overlays."""
from __future__ import annotations

import hashlib
from pathlib import Path
from kg_pilot.effective_evidence import REGISTRY, physical_revision as qualification_revision

DATA_FILES = ("catalog/papers.json","catalog/classifications.json","catalog/taxonomy.json",
              "annotations/claims.jsonl","research_map/reviewed_relations.json",
              "research_map/adjudicated_relations.json","research_map/recovered_relations.json","research_map/semantic_reviews.json",
              "research_map/source_kind_reviews.json","research_map/study_comparisons.json",
              "research_map/additional_studies.json","research_map/opportunity_reviews.json","research_map/external_prior_art.json",
              "research_map/stack_opportunities.json","research_map/stack_release_review.json","research_map/unmapped_source_readings.json",
              "research_map/opportunity_protocols.json","research_map/sample_bindings.json",
              "research_map/material_opportunities.json","research_map/material_release_review.json",
              "research_map/direction_starting_points.json","research_map/source_errata.json",
              "research_map/study_connections.json", "research_map/coverage_reviews.json",
              "catalog/paper_metadata_overrides.json", REGISTRY)


def digest_files(root: Path, files, *, overrides=None):
    stamp = hashlib.sha256()
    for name in files:
        stamp.update(name.encode())
        try:
            path = (overrides or {}).get(name,root/name)
            with path.open("rb") as f:
                stamp.update(hashlib.file_digest(f,"sha256").digest())
            if name=="research_map/sample_bindings.json":
                from kg_pilot.map_sample_bindings import physical_revision
                stamp.update(physical_revision(root,path).encode())
            if name == REGISTRY:
                stamp.update(qualification_revision(root, path).encode())
        except FileNotFoundError:
            stamp.update(b"absent")
    return stamp.hexdigest()


_ROOT = Path(__file__).resolve().parents[1]
ALGORITHM = digest_files(_ROOT, ("web/server.py","web/research_cache.py","src/kg_pilot/research_map.py",
    "src/kg_pilot/map_source_kinds.py","src/kg_pilot/map_relations.py","src/kg_pilot/map_semantics.py","src/kg_pilot/map_sample_bindings.py",
    "src/kg_pilot/research_notes.py","src/kg_pilot/map_errata.py","src/kg_pilot/classification.py",
    "src/kg_pilot/map_coverage.py","src/kg_pilot/paper_metadata.py","src/kg_pilot/effective_evidence.py"))

