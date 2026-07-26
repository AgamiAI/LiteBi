-- The organization record (F15 / ACE-068): the deployment's ONE company-level row, derived from the
-- disk record <artifacts_dir>/organization.yaml (ACE-067). This is the registry `001_serving.sql`
-- admitted it pointed at but never had ("org_id is just the scoping key here; any registry it points
-- at lives outside this table"). It holds the company-wide facts (name, description, fiscal year,
-- display conventions, glossary) that used to be duplicated into every per-datasource datasource_model
-- row and drift.
--
-- Keyed on `org_id` ALONE — deliberately unlike every (org_id, datasource) serving table: there is one
-- company per deployment, many datasources under it. Portable DDL (TEXT only, app-minted key, no JSONB)
-- so the same file runs on SQLite (tests / small self-host) and Postgres (prod), same rules as
-- 001_serving.sql. `doc` carries the OrgRecord's structured tail as JSON (fiscal_year_start_month +
-- display_conventions + glossary); name/description are promoted so a reader can filter/show them
-- without decoding the blob.
--
-- COLUMN SUPERSET (coordination with agami-hosted): the hosted product's tenant registry is ALSO a
-- table named `organization` (org_id, org_name, created_at) and other hosted tables FK-reference it.
-- To let one physical table serve both, this DDL is a superset of hosted's columns: `org_name` (not a
-- bare `name`) so hosted's INSERT finds its column, `created_at` present (hosted-owned; core never
-- writes it), and `doc` carries a DEFAULT so hosted's INSERT — which omits `doc` — still satisfies NOT
-- NULL. Core writes this row with an FK-safe UPSERT (model_store.write_organization_record), never a
-- DELETE, so a redeploy can't violate the hosted FKs. Core migrations apply BEFORE the hosted overlay,
-- so this definition wins and hosted's `CREATE TABLE IF NOT EXISTS organization` becomes a no-op.

CREATE TABLE organization (
    org_id      TEXT PRIMARY KEY,           -- the deployment's org_id (F14); the one table keyed on org alone
    org_name    TEXT,                       -- company display name (OrgRecord.name); shared with hosted's column
    description TEXT,                        -- company description
    doc         TEXT NOT NULL DEFAULT '{}', -- JSON: fiscal_year_start_month + display_conventions + glossary
    created_at  TEXT                         -- hosted-owned tenant-creation stamp; core leaves it NULL
);
