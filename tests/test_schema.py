"""The serving + runtime schema migrations create cleanly on an empty DB (Slice B).

Runs the real migrations/core/*.sql against SQLite (the portable backend the tests use) and asserts
every per-object serving table + the runtime tables exist — the schema that backs the 5 tools.
"""

from __future__ import annotations

from store import Store

SERVING_TABLES = {
    "datasource_model",
    "subject_area",
    "model_table",
    "metric",
    "entity",
    "relationship",
    "prompt_example",
    "memory",
    "model_version",
}
RUNTIME_TABLES = {"query_executions", "feedback"}


def _tables(s: Store) -> set[str]:
    return {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}


def test_real_migrations_create_all_tables_on_empty_db():
    s = Store.connect("sqlite://")
    ran = s.run_migrations()  # the real migrations/core dir
    assert "001_serving.sql" in ran and "002_runtime.sql" in ran
    tables = _tables(s)
    assert SERVING_TABLES <= tables
    assert RUNTIME_TABLES <= tables
    s.close()


def test_organization_registry_table_exists():
    # F15 / ACE-068: the one company-level row, keyed on org_id ALONE (not (org_id, datasource)). Its
    # columns are a superset of agami-hosted's tenant `organization` table so one physical table serves
    # both; `doc` carries a DEFAULT so hosted's INSERT (which omits it) still satisfies NOT NULL.
    s = Store.connect("sqlite://")
    ran = s.run_migrations()
    assert "013_organization.sql" in ran
    assert "organization" in _tables(s)
    cols = {r["name"] for r in s.query("PRAGMA table_info(organization)")}
    assert {"org_id", "org_name", "description", "doc", "created_at"} == cols
    # keyed on org_id alone
    pk = [r["name"] for r in s.query("PRAGMA table_info(organization)") if r["pk"]]
    assert pk == ["org_id"]
    s.close()


def test_oauth_tables_exist():
    # The OAuth provider owns these: oauth_client (registered clients) + oauth_state (authorization
    # codes bound to their PKCE challenge + redirect + the authenticated username).
    s = Store.connect("sqlite://")
    s.run_migrations()
    assert {"oauth_client", "oauth_state"} <= _tables(s)
    state_cols = {r["name"] for r in s.query("PRAGMA table_info(oauth_state)")}
    assert {
        "code",
        "code_challenge",
        "redirect_uri",
        "username",
        "expires_at",
        "used",
    } <= state_cols
    s.close()


def test_users_table_is_flat_no_role_column():
    # The users table exists with the identity columns and, crucially, NO role/permission column —
    # flat access is the open-core contract (roles are paid).
    s = Store.connect("sqlite://")
    s.run_migrations()
    assert "users" in _tables(s)
    cols = {r["name"] for r in s.query("PRAGMA table_info(users)")}
    assert {"id", "username", "password_hash", "email", "status", "created"} <= cols
    # OIDC identity binding columns (provider + subject) exist; still NO role/permission column.
    assert {"oidc_provider", "oidc_subject"} <= cols
    assert not (cols & {"role", "roles", "permission", "permissions"}), (
        "flat access — no role column"
    )
    s.close()


def test_users_has_org_id_column():
    # F14 / ACE-057: the users roster is tenant-scoped like the serving/runtime tables (migration 012),
    # so an authorized-user set rides along when a deployment is lifted into hosted. org_id is an indexed
    # NON-PK column here (users PK is `id`, login is by UNIQUE username) — deliberately unlike the
    # serving tables where org_id leads the PK.
    s = Store.connect("sqlite://")
    ran = s.run_migrations()
    assert "012_users_org_id.sql" in ran
    info = {r["name"]: r for r in s.query("PRAGMA table_info(users)")}
    assert "org_id" in info
    assert info["org_id"]["notnull"] == 1
    assert info["org_id"]["dflt_value"] in ("'local'", "local")  # SQLite quotes the default literal
    assert info["org_id"]["pk"] == 0  # NOT part of the primary key
    indexes = {r["name"] for r in s.query("PRAGMA index_list(users)")}
    assert "idx_users_org" in indexes
    s.close()


def test_users_password_hash_nullable_and_email_indexed():
    # OIDC users have no password, and we look them up by email — so password_hash is nullable and
    # email is indexed after the passwordless migration.
    s = Store.connect("sqlite://")
    s.run_migrations()
    pw = next(r for r in s.query("PRAGMA table_info(users)") if r["name"] == "password_hash")
    assert pw["notnull"] == 0, "password_hash must be nullable for OIDC users"
    # username must STILL be unique (the rebuild preserves the constraint)
    uname = next(r for r in s.query("PRAGMA table_info(users)") if r["name"] == "username")
    assert uname["notnull"] == 1
    email_idx = next(
        (r for r in s.query("PRAGMA index_list(users)") if r["name"] == "idx_users_email"), None
    )
    assert email_idx is not None and email_idx["unique"] == 1, "email must be uniquely indexed"
    s.close()


def test_migrations_are_idempotent_on_real_dir():
    s = Store.connect("sqlite://")
    s.run_migrations()
    assert s.run_migrations() == []  # nothing new the second time
    s.close()


def test_sizing_metadata_columns_present():
    # The smart get_datasource_schema sizing reads these; assert they exist so a schema change
    # can't silently drop them.
    s = Store.connect("sqlite://")
    s.run_migrations()
    sa_cols = {r["name"] for r in s.query("PRAGMA table_info(subject_area)")}
    assert "table_count" in sa_cols
    tbl_cols = {r["name"] for r in s.query("PRAGMA table_info(model_table)")}
    assert "est_row_count" in tbl_cols
    s.close()


def test_pk_area_columns_are_not_null_for_postgres_portability():
    # Postgres forbids NULL in a PRIMARY KEY column; SQLite would allow it. `area` is in the PK of
    # metric/entity/relationship, so it must be NOT NULL or the schema isn't Postgres-portable.
    s = Store.connect("sqlite://")
    s.run_migrations()
    for table in ("metric", "entity", "relationship"):
        area = next(r for r in s.query(f"PRAGMA table_info({table})") if r["name"] == "area")
        assert area["notnull"] == 1, f"{table}.area must be NOT NULL (it's in the PK)"
    s.close()
