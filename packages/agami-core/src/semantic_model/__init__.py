"""agami semantic model — provider-portable, standard-concepts hierarchy.

This is **the** agami semantic model: a hierarchy of Datasource → Storage
Connection (physical) + Subject Area (logical) → Table / Entity / Metric /
Relationship, with provider-portable declarative fields (default_filters,
value_transform, caveats, value_pattern, sensitive, cardinality, …) so that any
LLM can progressively traverse it and construct reliable queries against any
backend.

Layout on disk (the canonical profile format, rooted at `<artifacts_dir>/<profile>/`):

    datasource.yaml                                 # org desc + storage_connections + subject_areas
    datasources/<connection>/storage.yaml    # physical: storage_type, storage_config
    subject_areas/<name>/
      subject_area.yaml                      # desc, default_time_window, tables (TableRefs)
      tables/<t>.yaml                        # canonical Table definitions
      entities/<e>.yaml
      metrics/<m>.yaml
      relationships.yaml                     # intra-area FK graph
    cross_subject_area_relationships.yaml    # optional, org-level
    prompt_examples/<subject_area>/examples.yaml

Modules:
  models.py     — Pydantic v2 models (structural validation).
  validator.py  — cross-cutting invariants (sizing, orphans, type-compat, …).
  loader.py     — read the on-disk tree; context assembly.
  runtime.py    — examples-first traversal, entity ID, fan/chasm pre-flight.
  cli.py        — one dispatch surface.

Depends on Pydantic v2 + sqlglot (see requirements.txt).
"""

from __future__ import annotations

__all__: list[str] = []
