-- The read-only role, made runnable.
--
-- `plugins/agami/shared/readonly-grants.md` carries the PostgreSQL recipe as prose with `<…>`
-- placeholders, which is the right shape for an operator to copy and the wrong shape for a test to
-- execute. This file is that block with the placeholders filled in for the throwaway container in
-- `docker-compose.yml`: `<password>` is a fixture value, `<db>` is the database created below,
-- `<schema>` is `public`, and `<owner>` is the container's own `POSTGRES_USER`.
--
-- Why it matters that it is a COPY rather than an approximation: the role is the primary,
-- non-bypassable integrity control — the one that holds when the app-layer guard does not — and a
-- test that invented its own grants would be proving something about a role no operator has. Keep
-- the two in step: a change to the recipe belongs here too.
--
-- The corpus gets its own database rather than sharing `shop`. The safety corpus declares `orders`
-- and `customers` with its own columns, `shop` already seeds tables under both names for the CLI
-- smoke scripts, and one of the two would have to give. A separate database keeps each fixture
-- describing exactly what it means to.
--
-- WHAT IS DELIBERATELY NOT COPIED, and the residual that leaves. The recipe carries two further
-- lines under "Optional hardening (PostgreSQL <= 14 / Redshift)":
--
--     REVOKE CREATE ON SCHEMA public FROM PUBLIC;
--     REVOKE TEMP ON DATABASE <db> FROM PUBLIC;
--
-- They are absent here on purpose, and the purpose is the same one the paragraph above states: this
-- file is the REQUIRED baseline with the placeholders filled in, so the role-floor test proves the
-- floor an operator who follows the recipe actually has. Running the optional lines here would
-- prove a floor that is stronger than the one shipped, which is the "proving something about a role
-- no operator has" failure in its politest form.
--
-- The residual, measured on this container rather than reasoned about, so it is a recorded fact and
-- not an omission:
--
--   * `CREATE TABLE floor_probe (x int)` is refused with SQLSTATE 42501. Not because of anything
--     here — PostgreSQL 15 dropped the public-schema `CREATE` default, so on the `postgres:16`
--     image the first optional line is already in force. `test_role_floor_pg.py::WRITES` asserts
--     it, which is what turns a server default into evidence. On PostgreSQL <= 14 or Redshift the
--     same recipe grants it, and that assertion would fail there rather than pass silently.
--   * `CREATE TEMP TABLE tmp_probe AS SELECT id FROM orders` SUCCEEDS. `TEMP ON DATABASE` is still
--     granted to `PUBLIC` on 16, so the role can materialize anything it can already read into a
--     session-local table. It reaches no data the role could not already SELECT and the table dies
--     with the connection, so it is a resource-consumption residual rather than a disclosure one —
--     and it is the reason the second optional line exists. An operator who wants it closed runs
--     that line; this fixture records that the baseline does not.

-- Cluster-wide, so it is created once and works in every database below.
CREATE USER agami_ro WITH PASSWORD 'agami_ro_pw';

CREATE DATABASE corpus OWNER agami_test;

GRANT CONNECT ON DATABASE corpus TO agami_ro;

\connect corpus

GRANT USAGE ON SCHEMA public TO agami_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agami_ro;
-- Load-bearing for this fixture, not an afterthought: the corpus tables are created by the test
-- session (from `safety.corpus.SCHEMA`, so the model and the warehouse cannot drift), which means
-- every table `agami_ro` reads is created AFTER this script runs. `GRANT … ON ALL TABLES` above
-- covers the tables that exist right now — none — so without this line the role would be unable to
-- read anything and the role-floor test would "pass" for the wrong reason entirely.
ALTER DEFAULT PRIVILEGES FOR ROLE agami_test IN SCHEMA public GRANT SELECT ON TABLES TO agami_ro;
