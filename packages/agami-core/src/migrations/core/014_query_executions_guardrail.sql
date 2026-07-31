-- Record the guardrail VERDICT on the query row (ACE-035). `execute_sql` now writes one
-- `query_executions` row for every outcome — `ok`, `refused` and `failed` alike — so the row has to
-- say which of the three it was, and, when it was a refusal, which rule fired and under which reason.
-- Without these columns a refusal is indistinguishable from a successful query in the audit trail,
-- which is the same as not auditing refusals at all: the outcomes most worth reviewing are exactly
-- the ones a reviewer could not find.
--
-- COLUMNS, NOT A SECOND TABLE. The audit record is 1:1 with the query — one execution, one verdict —
-- so a `query_audit` table would be a join that can only ever match a single row, and it would need a
-- key to join on. There is none to invent: the envelope's `audit_id` IS `query_executions.id` (the
-- app-minted key this table has carried since 002_runtime.sql), so the answer and its trail are
-- already joined by construction.
--
-- NULLABLE on purpose, all three. Every row written before this migration ran has no verdict, and a
-- back-fill would be inventing history — NULL reads as "written before the guardrail contract", which
-- is not the same claim as `ok`. `reason` and `rule` are additionally NULL on every non-refusal row,
-- because only a refusal has them; a failure carries the database's error, not a rule of ours.
--
-- Forward-only and portable (runs on SQLite + Postgres unchanged) — same shape as
-- 009_tool_calls_correlation.sql. No `IF NOT EXISTS`: SQLite's ALTER TABLE does not accept it, and
-- re-run safety comes from the runner's `schema_migrations` ledger, which skips an applied file.

ALTER TABLE query_executions ADD COLUMN status TEXT;
ALTER TABLE query_executions ADD COLUMN reason TEXT;
ALTER TABLE query_executions ADD COLUMN rule TEXT;
