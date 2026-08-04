"""ACE-071 — a statement whose reach cannot be determined is refused on the entry point that cannot
determine it, and the two entry points agree about everything else.

`python3 -m execute_sql` is the invocation the plugin README documents, and it resolves the vendored
stdlib-only slice, which ships `semantic_model/__init__.py` and `units.py` and no runtime at all.
Table scope, the `SELECT *` ban and column scope therefore cannot run there. `_receipt_for` already
conceded exactly that, returning `undetermined_receipt(RECEIPT_NO_RUNTIME)` — so the executor
published "I could not determine what this statement reaches" and then executed it anyway. Principle
4c makes undetermined a refusal, so that path now refuses whenever a model is DECLARED for the
profile, and stays inert only when none is (a bare install before its first `agami-connect` has no
declared surface, so nothing about it is undetermined).

This file is the entry-point parity test: the same statement against the same on-disk model, run
from the supported virtualenv interpreter (in process) and from the package-less one (a child
started with `-S`, which disables site.py so the installed package is invisible). The two must agree
that a write is refused and that neither ever runs a statement it has not checked. Where they
legitimately differ is which of those two things happens: the supported interpreter ENFORCES the
scope gates, and the package-less one REFUSES because it cannot.

The `-S` device is the one `tests/test_ace088_receipt_placement.py` uses to pin the degraded
receipt, and the layout it depends on is pinned by `tests/test_plugin_lib_resolution.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")  # `_write_model` builds the fixture model with it

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402

VENDORED_LIB = REPO_ROOT / "plugins" / "agami" / "lib"

# One profile and one statement for the whole file, so "the same statement against the same on-disk
# model" is a property of the file rather than something each test has to restate.
PROFILE = "acme"
IN_SCOPE_SQL = "SELECT id FROM orders"
OUT_OF_SCOPE_SQL = "SELECT id FROM invoices"
WRITE_SQL = "DROP TABLE orders"

# `-S` disables site.py, so the installed agami-core is invisible and only the vendored `lib/` slice
# is importable — the state a marketplace user's plain `python3` is in.
_NOPKG = [sys.executable, "-S"]

# Reports BOTH what the safety pass RETURNS and what the chokepoint's Envelope CARRIES, because
# those are the two questions this file asks of the package-less interpreter and one process start
# answers both. It carries its own executor stub rather than importing the module-level one below:
# a child that cannot see the installed package cannot see this test module either.
_CHILD_PROBE = """
import json
import sys

sys.path.insert(0, sys.argv[1])

import execute_sql


class _NeverRuns:
    def execute(self, vetted_sql, creds, *, profile):
        raise AssertionError("the executor must not be reached on a statement that was refused")


sql, verdict = execute_sql._model_safety(sys.argv[2], sys.argv[3], None)
envelope = execute_sql.execute_guarded(sys.argv[2], sys.argv[3], None, executor=_NeverRuns())
print(json.dumps({
    "safety": {
        "refused": verdict is not None,
        "sql_unchanged": sql == sys.argv[2],
        "reason": getattr(verdict, "reason", None),
        "rule": getattr(verdict, "rule", None),
        "detail": getattr(verdict, "detail", None),
        "remediation": getattr(verdict, "remediation", None),
    },
    "envelope": {
        "status": envelope.status,
        "reason": getattr(envelope.refusal, "reason", None),
        "rule": getattr(envelope.refusal, "rule", None),
        "receipt_undetermined": envelope.receipt.tables.undetermined,
    },
}))
"""


class _NeverRuns:
    """An executor that fails the test if the chokepoint ever reaches it.

    Every statement this file sends is refused above the executor, so being called at all is the
    defect under test rather than an incidental setup detail.
    """

    def execute(self, vetted_sql: str, creds: dict[str, str], *, profile: str) -> None:
        raise AssertionError("the executor must not be reached on a statement that was refused")


def _silent(capsys: pytest.CaptureFixture[str]) -> None:
    """Assert the in-process branch wrote nothing.

    A gate returns its `Refusal`; the ONE writer is `main`. Anything on stderr from here would be a
    diagnostic that precedes the refusal on the wire — exactly what makes the stream unparseable to
    the parent that reads it.
    """
    captured = capsys.readouterr()
    assert captured.err == "", captured.err
    assert captured.out == "", captured.out


def _refusal(verdict: Any) -> guardrail.Refusal:
    """The verdict, asserted to be a real contract `Refusal` rather than a bare sentinel."""
    assert isinstance(verdict, guardrail.Refusal), verdict
    return verdict


def _write_model(root: Path) -> None:
    """Write a loadable two-table model for `AcmeCorp` at `root`.

    A real, parseable model rather than a stub file, even though the branch under test only asks
    whether `datasource.yaml` EXISTS (it refuses before `_resolve_guard_model` is ever reached, and
    an empty file fires it just as well). The supported interpreter has to LOAD this same directory
    to enforce the scope gates against it, and the parity claim is about one on-disk model seen by
    two interpreters — a model only one of them could read would be two models.
    """
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "AcmeCorp", "version": 1,
                        "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
                        "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "customers"}]})
    )
    for table in ("orders", "customers"):
        (root / "subject_areas" / "sales" / "tables" / f"{table}.yaml").write_text(
            yaml.safe_dump({"name": table, "schema": "public", "storage_connection": "c",
                            "grain": ["id"], "description": table,
                            "columns": [{"name": "id", "type": "integer", "primary_key": True}]})
        )


def _child_env(artifacts_dir: Path, *, pythonpath: str = "") -> dict[str, str]:
    """The environment every child in this file runs with.

    `AGAMI_ARTIFACTS_DIR` is set EXPLICITLY rather than inherited. `_disk_model_root` falls back to
    `~/agami-artifacts`, so leaving it unset would make the answer depend on the developer's home
    directory instead of on the fixture, in both directions: a developer with a real `acme` profile
    would see the refusal fire in the test that asserts inertness.

    Both database URLs are removed so `_hosted()` is false and the branch reached is the LOCAL one.
    The hosted twin of this refusal is ACE-051's and is pinned there.

    `PYTHONPATH` is emptied by default so an inherited entry cannot put the installed package back.
    """
    env = {**os.environ, "PYTHONPATH": pythonpath, "AGAMI_ARTIFACTS_DIR": str(artifacts_dir)}
    env.pop("AGAMI_DB_URL", None)
    env.pop("APP_DATABASE_URL", None)
    return env


def _require_a_package_less_interpreter() -> None:
    """Skip unless `-S` really does hide the installed package on this machine.

    If the probe SUCCEEDS, the child can import agami-core after all, and every assertion below
    would be measuring the supported interpreter under a package-less name — passing for the wrong
    reason, which on a fail-closed property is worse than not running. It probes with an empty
    `PYTHONPATH` rather than the vendored dir because what it confirms is that the INSTALLED package
    is hidden, which is independent of where the vendored slice sits.
    """
    hidden = subprocess.run([*_NOPKG, "-c", "import agami_paths"],
                            env={**os.environ, "PYTHONPATH": ""}, capture_output=True)
    if hidden.returncode == 0:
        pytest.skip("cannot simulate a package-less interpreter here (-S does not hide agami-core)")


def _package_less(artifacts_dir: Path, sql: str) -> dict[str, Any]:
    """Run `_CHILD_PROBE` on a package-less interpreter and return its parsed report.

    The vendored dir arrives as `argv[1]` and the probe puts it on `sys.path` itself, which keeps
    `PYTHONPATH` empty and therefore keeps the skip guard above measuring the same thing this run
    relies on.
    """
    _require_a_package_less_interpreter()
    proc = subprocess.run([*_NOPKG, "-c", _CHILD_PROBE, str(VENDORED_LIB), sql, PROFILE],
                          env=_child_env(artifacts_dir), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _package_less_cli(artifacts_dir: Path, sql: str) -> subprocess.CompletedProcess[str]:
    """Run the documented `python3 -m execute_sql` on a package-less interpreter.

    `PYTHONPATH` carries the vendored dir here, unlike everywhere else in this file: `-m` has no
    `argv[1]` for the probe to insert, so the slice has to be importable before the interpreter
    starts. `-S` still hides the installed package, which is the part that makes this package-less.
    """
    _require_a_package_less_interpreter()
    return subprocess.run([*_NOPKG, "-m", "execute_sql", "--profile", PROFILE, "--sql", sql],
                          env=_child_env(artifacts_dir, pythonpath=str(VENDORED_LIB)),
                          capture_output=True, text=True)


def test_the_documented_entry_point_refuses_when_a_model_is_declared(tmp_path: Path) -> None:
    """A1 — the invocation the plugin README documents, on the interpreter it actually resolves,
    with a model declared for the profile: it refuses rather than running a statement whose reach
    nothing on that interpreter could check."""
    artifacts = tmp_path / "art"
    _write_model(artifacts / PROFILE)

    proc = _package_less_cli(artifacts, IN_SCOPE_SQL)

    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == "", proc.stdout  # nothing ran, so there is no result to emit
    refusal = json.loads(proc.stderr)["refusal"]
    assert refusal["rule"] == guardrail.RULE_MODEL_UNAVAILABLE
    # The reason comes from the contract's own table rather than a literal, so a gate cannot pick
    # its own and a change to the mapping is caught here rather than agreed with.
    assert refusal["reason"] == guardrail.REASON_FOR_RULE[guardrail.RULE_MODEL_UNAVAILABLE]
    # The whole actionable content: the model is fine and the interpreter is the problem, so the
    # remediation has to name the invocation that works.
    assert "interpreter" in refusal["remediation"]
    assert "virtualenv" in refusal["remediation"]


def test_the_refusal_names_no_path_it_probed(tmp_path: Path) -> None:
    """A1 — value-free, like its hosted siblings. The branch resolves an artifacts directory to
    decide whether a model is declared, and the refusal it returns must not carry that directory
    back to the caller: a refusal is answered by an operator, and a resolved filesystem path is
    disclosure rather than advice."""
    artifacts = tmp_path / "art"
    _write_model(artifacts / PROFILE)

    proc = _package_less_cli(artifacts, IN_SCOPE_SQL)

    assert proc.returncode == 1, proc.stderr
    refusal = json.loads(proc.stderr)["refusal"]
    # The WHOLE refusal, not just the detail, so the authored remediation is covered too.
    text = json.dumps({key: refusal[key] for key in ("reason", "rule", "detail", "remediation")})
    assert str(tmp_path) not in text


def test_the_package_less_entry_point_stays_inert_with_no_model_on_disk(tmp_path: Path) -> None:
    """A2 — the other half of the branch, unchanged. Between `pip install` and the first
    `agami-connect` there is no declared surface, so there is nothing 4b could be exceeded against
    and nothing 4c could be undetermined about. The pass hands the statement back untouched."""
    empty = tmp_path / "art"
    empty.mkdir()  # a real directory with no profile in it, so nothing is declared

    safety = _package_less(empty, IN_SCOPE_SQL)["safety"]

    assert safety["refused"] is False
    assert safety["sql_unchanged"] is True


def test_the_read_only_gate_holds_on_both_interpreters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A4 — `sql_guard` is vendored and `execute_guarded` runs it ABOVE the semantic-model pass, so
    a write is refused on the package-less interpreter with or without this spec's branch. Pinned
    per path rather than assumed: the branch under test sits directly beneath this gate, and a
    reordering that let a `DROP` reach it would be invisible to every other test in this file."""
    artifacts = tmp_path / "art"
    _write_model(artifacts / PROFILE)

    package_less = _package_less(artifacts, WRITE_SQL)["envelope"]
    assert package_less["status"] == "refused"
    assert package_less["rule"] == guardrail.RULE_READ_ONLY
    assert package_less["reason"] == guardrail.REASON_FOR_RULE[guardrail.RULE_READ_ONLY]

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    supported = execute_sql.execute_guarded(WRITE_SQL, PROFILE, None, executor=_NeverRuns())
    assert supported.status == "refused"
    assert supported.refusal.rule == guardrail.RULE_READ_ONLY
    assert supported.refusal.reason == guardrail.REASON_FOR_RULE[guardrail.RULE_READ_ONLY]
    _silent(capsys)


def test_the_refusal_and_the_receipt_agree(tmp_path: Path) -> None:
    """A5 — the defect was the disagreement, not the gap. The receipt said the runtime was absent
    and nothing could be established; the executor read that and ran the statement. Both halves are
    asserted in one test on purpose, because either one alone is consistent with the defect."""
    artifacts = tmp_path / "art"
    _write_model(artifacts / PROFILE)

    envelope = _package_less(artifacts, IN_SCOPE_SQL)["envelope"]

    assert envelope["status"] == "refused"
    assert envelope["rule"] == guardrail.RULE_MODEL_UNAVAILABLE
    # `RULE_MODEL_UNAVAILABLE` is deliberately absent from `PRE_MODEL_RULES`, so this refusal keeps
    # the receipt the builder actually produced rather than the "refused before any model" marker —
    # which is what lets the two be compared at all.
    assert envelope["receipt_undetermined"] == guardrail.RECEIPT_NO_RUNTIME


def test_the_supported_interpreter_enforces_what_the_package_less_one_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A6 — the parity's other side. Same statement, same on-disk model, and here the guards import,
    so the pass resolves the model and the scope gates run: the declared statement is allowed and an
    undeclared table is refused by name of rule. That is what the package-less interpreter cannot
    do, and therefore what it now refuses instead of skipping."""
    artifacts = tmp_path / "art"
    _write_model(artifacts / PROFILE)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    sql, allowed = execute_sql._model_safety(IN_SCOPE_SQL, PROFILE, None)
    assert allowed is None  # the gates ran against the declared model and found nothing to refuse
    assert sql == IN_SCOPE_SQL

    _, refused = execute_sql._model_safety(OUT_OF_SCOPE_SQL, PROFILE, None)
    # Table scope, not `model_unavailable`: the gates were reachable here, so the refusal names what
    # the statement did rather than what the deployment could not do.
    assert _refusal(refused).rule == guardrail.RULE_TABLE_SCOPE
    _silent(capsys)


def test_a_package_that_raises_is_not_reported_as_a_package_that_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Absent and broken are two different facts. `_receipt_for` already refuses to report them as
    one — `RECEIPT_NO_RUNTIME` for an ImportError, `RECEIPT_BUILD_FAILED` (logged) for anything
    else. The refusal this spec adds sits under an `except Exception`, so it has to draw the same
    line: an installed package that raised while importing itself must not be described as missing,
    which would send the user to swap interpreters when the interpreter is not the problem, and
    would leave the real traceback unlogged.

    Both cases still refuse. Which one it is changes what we can honestly say, not whether the
    guards ran."""
    import builtins
    import logging

    real_import = builtins.__import__

    def boom(name, _globals=None, _locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "semantic_model" and fromlist and "runtime" in fromlist:
            raise RuntimeError("forced: the package is installed and raised while importing")
        return real_import(name, _globals, _locals, fromlist, level)

    artifacts = tmp_path / "art"
    _write_model(artifacts / PROFILE)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setattr(builtins, "__import__", boom)

    with caplog.at_level(logging.ERROR):
        _, verdict = execute_sql._model_safety(IN_SCOPE_SQL, PROFILE, None)

    refusal = _refusal(verdict)
    # Still fail-closed, and still the same rule: the guards did not run either way.
    assert refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    assert refusal.reason == guardrail.REASON_FOR_RULE[guardrail.RULE_MODEL_UNAVAILABLE]
    # But it does not claim the package is missing, and it does not send the user to another
    # interpreter — that advice is inert when the package is installed and broken.
    assert "not importable" not in refusal.detail
    assert "virtualenv" not in refusal.remediation
    # And the real failure is not swallowed: the traceback reaches the log.
    assert any(r.levelno >= logging.ERROR for r in caplog.records), caplog.text

    # Still value-free — the same bar the ImportError arm clears.
    text = json.dumps({"reason": refusal.reason, "rule": refusal.rule,
                       "detail": refusal.detail, "remediation": refusal.remediation})
    assert str(tmp_path) not in text


# A broken vendored slice, built by shadowing the runtime module the real one does not ship. Writing
# `semantic_model/runtime.py` into a COPY of the vendored dir is the only way to reach the
# broken-package arm through the CLI: the arm is chosen by what the import raises, and a child
# process cannot be monkeypatched.
_BROKEN_RUNTIME = "raise RuntimeError('forced: installed and raising while importing')\n"


def _broken_slice(root: Path) -> Path:
    """A copy of the vendored slice whose `semantic_model.runtime` raises a non-ImportError."""
    import shutil

    lib = root / "brokenlib"
    shutil.copytree(VENDORED_LIB, lib, ignore=shutil.ignore_patterns("__pycache__"))
    (lib / "semantic_model" / "runtime.py").write_text(_BROKEN_RUNTIME)
    return lib


def test_the_broken_package_refusal_survives_the_wire_it_travels(tmp_path: Path) -> None:
    """The refusal is only worth adding if the caller can still read it.

    On this entry point stderr IS the wire: `_write_refusal` puts one JSON object there and several
    callers, plus the fail-closed suite, parse the WHOLE stream as that object. A traceback printed
    alongside it does not merely add noise, it destroys the refusal — and the parent relays the
    child's stderr into `failure.message`, so the traceback's absolute paths would arrive in the
    caller's answer, which is the ACE-039 leak class through a different logger.

    The broken-package arm logs, so it is the arm that can break this. It only stayed readable
    because `main` silences `_LOG` as well as `_RAW_LOG`.
    """
    _require_a_package_less_interpreter()
    artifacts = tmp_path / "art"
    _write_model(artifacts / PROFILE)
    lib = _broken_slice(tmp_path)

    proc = subprocess.run([*_NOPKG, "-m", "execute_sql", "--profile", PROFILE, "--sql", IN_SCOPE_SQL],
                          env=_child_env(artifacts, pythonpath=str(lib)),
                          capture_output=True, text=True)

    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == ""  # no partial CSV alongside a refusal
    assert proc.stderr.count("\n") == 1, proc.stderr  # one line, so no traceback rode along
    payload = json.loads(proc.stderr)  # parses WHOLE -> a single object
    assert set(payload) == {"refusal"}
    refusal = guardrail.Refusal(**payload["refusal"])
    assert refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    # And it is the broken-package wording, not the absent-package one.
    assert "not importable" not in refusal.detail
    # Nothing about where anything lives reached the caller — the traceback would have carried the
    # slice's and the artifacts' absolute paths.
    assert str(tmp_path) not in proc.stderr


def test_a_probe_that_cannot_read_the_disk_refuses_rather_than_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`Path.exists()` answers False only for a path that is absent. For one it may not read at all
    — EACCES under macOS TCC, EPERM on a locked volume, ESTALE on NFS — it raises, and this probe
    runs INSIDE an `except` arm, where a raise escapes the enclosing `try` entirely.

    Unhandled, that surfaces as `failed`/`other` with no remediation and a traceback carrying the
    resolved artifacts path. Handled the wrong way — answering None — it is worse than either: the
    statement runs unchecked on a machine where we could not read whether there was a model to
    check it against. So doubt counts as declared, and the caller gets the refusal.
    """
    import builtins

    real_import = builtins.__import__

    def no_runtime(name, _globals=None, _locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "semantic_model" and fromlist and "runtime" in fromlist:
            raise ImportError("forced: semantic_model.runtime unavailable")
        return real_import(name, _globals, _locals, fromlist, level)

    def unreadable(_self: Path) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_runtime)
    monkeypatch.setattr(Path, "exists", unreadable)

    _, verdict = execute_sql._model_safety(IN_SCOPE_SQL, PROFILE, None)

    refusal = _refusal(verdict)  # a refusal, not an escaped PermissionError
    assert refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    _silent(capsys)
