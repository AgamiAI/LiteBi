-- agami-core serving schema — the semantic model, served from the DB instead of local YAML files.
--
-- Shape mirrors the in-memory Datasource tree (semantic_model/models.py) under an
-- org -> datasource -> subject-area hierarchy. Each object row carries the structural/key columns
-- a tool queries on PLUS a `doc` (the object's JSON) so the loader rebuilds the EXACT pydantic
-- object losslessly — no per-field column sprawl, and YAML stays the source of truth.
-- Portable DDL: TEXT/INTEGER only, app-minted keys (no SERIAL/JSONB) so the same file runs on
-- SQLite (tests / small self-host) and Postgres (prod).
--
-- table -> tool backing map:
--   datasource_model, subject_area, model_table, metric, entity, relationship, memory
--                              -> get_datasource_schema (subject_area.table_count + model_table.est_row_count
--                                 drive the full/summary/index sizing + large_tables; relationship rows feed
--                                 execute_sql's internal pre_flight_check; entity.value_pattern feeds the
--                                 folded identify_entity)
--   prompt_example             -> get_prompt_examples (scoped by org+datasource+area, ranked app-side)
--   model_version              -> the execute_sql trust receipt's version pin
--   (list_datasources reads the set of `datasource` rows; execute_sql/log_feedback write runtime rows — 002)

-- The root of a datasource's served model (the pydantic Datasource: version, glossary, fiscal year,
-- storage_connections, cross-subject-area objects). Named `datasource_model`, not `organization` —
-- `org_id` is just the scoping key here; any registry it points at lives outside this table. This is
-- the MODEL, one row per (org, datasource). model_store.load_datasource / write_datasource read/
-- write it (their names track the pydantic type, not the table).
CREATE TABLE datasource_model (
    org_id      TEXT NOT NULL DEFAULT 'local',
    datasource  TEXT NOT NULL,      -- the profile/datasource id
    description TEXT,
    doc         TEXT NOT NULL,      -- org-level JSON: version, fiscal_year_start_month, glossary,
                                    -- storage_connections, cross_subject_area_{relationships,metrics,entities}
                                    -- (the model's own name lives in here — no separate column)
    PRIMARY KEY (org_id, datasource)
);

CREATE TABLE subject_area (
    org_id              TEXT NOT NULL DEFAULT 'local',
    datasource          TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT,
    default_time_window TEXT,
    table_count         INTEGER NOT NULL DEFAULT 0,  -- sizing metadata (full <=12 / summary <=50 / index)
    doc                 TEXT NOT NULL,
    PRIMARY KEY (org_id, datasource, name)
);

CREATE TABLE model_table (
    org_id        TEXT NOT NULL DEFAULT 'local',
    datasource    TEXT NOT NULL,
    area          TEXT NOT NULL,
    name          TEXT NOT NULL,
    est_row_count INTEGER,          -- sizing metadata: >= 1e6 surfaces in large_tables
    doc           TEXT NOT NULL,
    PRIMARY KEY (org_id, datasource, area, name)
);

-- NOTE: `area` is part of the PK and is NOT NULL. Postgres forbids NULL in any PK column (SQLite
-- would allow it), so org-level (cross-subject-area) metrics/entities/relationships are NOT stored
-- as area-NULL rows here — they ride inside datasource_model.doc and are rebuilt from there. These
-- tables hold per-subject-area objects only (area = the subject-area name).
CREATE TABLE metric (
    org_id     TEXT NOT NULL DEFAULT 'local',
    datasource TEXT NOT NULL,
    area       TEXT NOT NULL,
    name       TEXT NOT NULL,
    doc        TEXT NOT NULL,
    PRIMARY KEY (org_id, datasource, area, name)
);

CREATE TABLE entity (
    org_id        TEXT NOT NULL DEFAULT 'local',
    datasource    TEXT NOT NULL,
    area          TEXT NOT NULL,
    name          TEXT NOT NULL,
    value_pattern TEXT,            -- feeds the folded identify_entity matching
    doc           TEXT NOT NULL,
    PRIMARY KEY (org_id, datasource, area, name)
);

-- WITHIN-subject-area relationships only. Cross-subject-area / cross-datasource relationships are
-- org-level (Datasource.cross_subject_area_relationships) and ride inside datasource_model.doc — they
-- are rebuilt from there, not stored as rows here (which would need a null area, see the NOTE above).
CREATE TABLE relationship (
    org_id     TEXT NOT NULL DEFAULT 'local',
    datasource TEXT NOT NULL,
    area       TEXT NOT NULL,
    name       TEXT NOT NULL,      -- "<position>:<from>-><to>" (model_store._relationship_key)
    doc        TEXT NOT NULL,
    PRIMARY KEY (org_id, datasource, area, name)
);

CREATE TABLE prompt_example (
    org_id     TEXT NOT NULL DEFAULT 'local',
    datasource TEXT NOT NULL,
    area       TEXT,               -- NULL = org-level cross-datasource example bucket
    id         TEXT NOT NULL,
    question   TEXT NOT NULL,
    doc        TEXT NOT NULL,
    embedding  TEXT,               -- optional, off by default: deploy-time vector as JSON text (no pgvector)
    PRIMARY KEY (org_id, datasource, id)
);

-- Domain-context docs. kind='datasource' (datasource.md) is per-datasource; kind='user'
-- (USER_MEMORY.md) is cross-datasource, stored once under datasource='' (the empty sentinel) — but
-- still PER ORG, so one tenant's user memory can't reach another's. Mirrors the file layout
-- (<artifacts_dir>/<profile>/datasource.md vs <artifacts_dir>/USER_MEMORY.md).
CREATE TABLE memory (
    org_id     TEXT NOT NULL DEFAULT 'local',
    datasource TEXT NOT NULL,      -- '' for the per-org user-memory row
    kind       TEXT NOT NULL,      -- 'datasource' (per-datasource) | 'user' (per-org, cross-datasource)
    content    TEXT,
    PRIMARY KEY (org_id, datasource, kind)
);

CREATE TABLE model_version (
    org_id     TEXT NOT NULL DEFAULT 'local',
    datasource TEXT NOT NULL,
    version    TEXT NOT NULL,      -- the snapshot content hash the receipt pins
    created_at TEXT,
    PRIMARY KEY (org_id, datasource, version)
);
