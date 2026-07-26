-- The **guardrail audit trail** — one row per execute_sql call, keyed by the response Envelope's
-- `audit_id`. Written best-effort at the shared executor chokepoint (`tools.tool_execute_sql`) so
-- every ok/refused result is recorded on BOTH surfaces (stdio + HTTP), not only the HTTP path that
-- writes `tool_calls`. This is the trail the Envelope's `audit_id` points at.
--
-- Deliberately thin for now — status + refusal kind + the query context; a richer verdict/action
-- list is a follow-on. Portable CREATE TABLE (runs on SQLite + Postgres unchanged).

CREATE TABLE guardrail_audit (
    audit_id       TEXT PRIMARY KEY,        -- == the response Envelope's audit_id
    ts             TEXT NOT NULL,           -- UTC ISO8601
    datasource     TEXT,
    status         TEXT NOT NULL,           -- 'ok' | 'refused'
    refusal_kind   TEXT,                    -- the refusal kind when status = 'refused'
    sql            TEXT,                    -- the query the caller sent (may be NULL on a bad request)
    row_count      INTEGER,                 -- on an ok result
    execution_ms   INTEGER,
    correlation_id TEXT,                    -- the turn (one user question), self-reported; may be NULL
    source         TEXT                     -- 'mcp_server'
);

-- The admin views read newest-first; index the time access path.
CREATE INDEX idx_guardrail_audit_ts ON guardrail_audit (ts);
