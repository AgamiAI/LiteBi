"""Multi-area table membership: a table DEFINED in one area, REFERENCED from another."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _two_area_org():
    shared = m.Table(name="sys_user", schema="s", storage_connection="c", grain=["id"],
                     description="users", columns=[
                         m.Column(name="id", type="integer", primary_key=True),
                         m.Column(name="email", type="string")])
    own = m.Table(name="incident", schema="s", storage_connection="c", grain=["id"],
                  description="incidents",
                  columns=[m.Column(name="id", type="integer", primary_key=True)])
    area_a = m.SubjectArea(name="users", tables_defined=[shared],
                           tables=[m.TableRef(storage_connection="c", schema="s", table="sys_user")])
    # 'incidents' DEFINES incident and REFERENCES the shared sys_user defined in 'users'
    area_b = m.SubjectArea(name="incidents", tables_defined=[own], tables=[
        m.TableRef(storage_connection="c", schema="s", table="incident"),
        m.TableRef(storage_connection="c", schema="s", table="sys_user")])
    return m.Datasource(datasource="o", subject_areas=[area_a, area_b])


def test_find_table_resolves_referenced_table_from_other_area():
    org = _two_area_org()
    t = L._find_table(org, "sys_user", area="incidents")    # defined in 'users', referenced here
    assert t is not None and t.name == "sys_user"
    # a table an area neither defines nor references still doesn't resolve there
    assert L._find_table(org, "incident", area="users") is None


def test_bundle_includes_referenced_table_from_other_area():
    org = _two_area_org()
    bundle = L.get_subject_area_bundle(org, "incidents")
    assert "incident" in bundle["tables"]
    assert "sys_user" in bundle["tables"]   # the shared referenced table is in B's bundle


def test_single_membership_unchanged():
    org = _two_area_org()
    # 'users' bundle has only its own table (no spurious cross-area tables)
    assert set(L.get_subject_area_bundle(org, "users")["tables"]) == {"sys_user"}
