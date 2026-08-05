# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities **privately** rather than opening a public issue.

- Email: [contact@agami.ai](mailto:contact@agami.ai)
- Or use GitHub Security Advisories: <https://github.com/AgamiAI/agami-core/security/advisories/new>

We will acknowledge receipt within **3 business days** and aim to provide a fix or mitigation timeline within **14 days** for confirmed vulnerabilities. Please give us a reasonable window to respond before any public disclosure.

When reporting, include where possible:

- A description of the issue and its impact
- Steps to reproduce (proof-of-concept welcome)
- Affected versions / commit SHAs
- Any suggested remediation

## Supported Versions

We support the **latest released version**; older releases will not receive security backports.

## Scope

In scope:

- The agami-core Claude Code plugin (`plugins/agami/`) and the SQL execution pipelines it ships.
- The plugin marketplace manifests (`.claude-plugin/marketplace.json`, `plugins/agami/.claude-plugin/plugin.json`).

Out of scope:

- Third-party databases, drivers, or services agami-core connects to.
- Vulnerabilities in Claude Code itself (report those to Anthropic).
- Issues that require physical access to a user's machine or already-compromised credentials.

## SQL execution model

`execute_sql` is read-only by construction. A single gate (`sql_guard`) runs at the
shared executor, so every path that can run SQL — the stdio server, the hosted HTTP
server, the skills, and cron — is protected identically. It rejects, before any
database connection is opened:

- anything that isn't a single `SELECT` / `WITH...SELECT` (DML, DDL, `SELECT ... INTO`);
- multi-statement SQL, including bypasses hidden in string literals, comments, or
  double-quoted identifiers;
- data-modifying CTEs (`WITH ... DELETE/INSERT/UPDATE ... RETURNING`);
- transaction-control, session-state, and prepared statements, and row-level locks;
- dangerous server-side functions — file I/O (`pg_read_file`, `lo_export`), OS/command
  execution (`copy_program`), remote SQL (`dblink*`), and resource-exhaustion (`pg_sleep`,
  advisory locks).

This is defense in depth at the application layer; you should still connect with a
read-only database role. A guard bypass — SQL that mutates data or reaches a blocked
function yet passes the gate — is in scope for a report.

### The semantic-model pass is separately switchable

A second layer runs after the gate above: the **semantic-model pass**, which confines a
query to the tables and columns your model declares, bans `SELECT *`, and refuses a model
whose declared engine disagrees with its credentials. On a server it is controlled by
`AGAMI_GOVERNANCE_ENFORCED`, and **it is off by default**.

With it off, a query may read anything the connecting role is granted, including columns
you deliberately excluded from the model, and may enumerate your schema through catalog
*relations* (`information_schema.tables`, `pg_catalog.pg_class`, `sqlite_master`). Catalog
*functions* are denied by the gate above in both postures; catalog relations are not, and
the read-only grant recipe we publish does not revoke them. Every answer and every audit
row states that the checks did not run, so a result is never presented as governed when it
was not.

Everything in the list above (read-only, confinement to safe functions, and the resource
bounds) is unaffected by this setting, as is your database role. **A statement that the
read-only or dangerous-function gate would refuse must never become executable because this
setting is off; if you find one, that is a guard bypass and in scope for a report.**

Thank you for helping keep agami-core and its users safe.
