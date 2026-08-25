"""Reader for the golden datasets under ``<artifacts_dir>/<profile>/golden_datasets/``.

A golden dataset is a file of questions whose answer is already agreed: the author writes the
question, the SQL they accept as the answer key, and how strictly a run has to match it. The
runner that consumes these decides pass/fail, so this module's whole job is to hand it records
it can trust — and to say out loud which files and cases it had to drop getting there.

Two properties are load-bearing:

* **Fault isolation.** One malformed file, or one malformed case inside an otherwise good file,
  must not cost the run every other case. Each fault becomes a finding and the read continues,
  so a typo costs one case rather than the whole suite.
* **No path escapes.** Nothing returned carries a filesystem path. A downstream runner cannot
  forward a dataset location into a subprocess because it never receives one — the records are
  self-sufficient by construction rather than by the runner's good manners.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

import agami_paths
import yaml
from pydantic import Field, ValidationError, model_validator

from .models import _Base
from .validator import ValidationResult

# How closely a run's result has to match the answer key, loosening left to right.
MatchLevel = Literal["exact", "values", "shape", "bounded", "nonempty"]


class GoldenRecorded(_Base):
    """What the author saw when they wrote the case down.

    This is a receipt, never the comparison target: a run is judged against `expected`, and
    these numbers exist so a reviewer can see what the answer looked like on the day.
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    at: Optional[str] = None


class GoldenConfirmedBy(_Base):
    """Who or what vouched for the answer key, and when."""

    # Free text while the set of methods is still being discovered; a Literal can narrow it once
    # the vocabulary settles, and would until then refuse files for saying something reasonable.
    method: str
    at: Optional[str] = None


class GoldenExpected(_Base):
    """The answer key: what a correct run has to produce."""

    sql: Optional[str] = None
    # The one field with no default. An author who forgets to say whether the SQL was confirmed
    # has to be told, because guessing either way silently changes whether the case can gate.
    sql_confirmed: bool
    tables_used: list[str] = Field(default_factory=list)
    chart_type: Optional[str] = None
    data_shape: Optional[str] = None
    validation_notes: Optional[str] = None


class GoldenItem(_Base):
    """One question, its answer key, and how strictly to compare against it."""

    id: str
    query: str
    expected: GoldenExpected
    match: MatchLevel = "exact"
    must_filter: list[str] = Field(default_factory=list)
    recorded: Optional[GoldenRecorded] = None
    tags: list[str] = Field(default_factory=list)
    confirmed_by: Optional[GoldenConfirmedBy] = None

    @property
    def item_key(self) -> str:
        """The author's own id. The reader mints nothing: a key derived from position would move
        the day a case is inserted above this one, detaching every result already stored under it."""
        return str(self.id)

    @model_validator(mode="after")
    def _confirmed_needs_answer_key(self) -> "GoldenItem":
        # A confirmed case is the one kind that can gate a run, so it is also the one kind that
        # must be able to fail one. With no SQL there is nothing to compare it against and it
        # would pass forever without anyone noticing.
        if self.expected.sql_confirmed and not self.expected.sql:
            raise ValueError("expected.sql_confirmed is true but expected.sql is missing")
        return self


class GoldenDataset(_Base):
    """One golden-dataset file's worth of cases."""

    # Injected by the reader from the filename. The file may not declare it — see the refusal in
    # `load_golden_datasets`.
    name: str
    description: str = ""
    category: Optional[str] = None
    user_context: Optional[str] = None
    test_cases: list[GoldenItem] = Field(default_factory=list)


# A question phrased against the day it is asked: the window it names slides forward on its own.
_RELATIVE_QUESTION_RE = re.compile(
    r"\b(?:last|past|previous|this|current|trailing|rolling)\s+(?:\d+\s+)?"
    r"(?:day|week|month|quarter|year|hour|minute)s?\b"
    r"|\b(?:today|yesterday|ytd|mtd|year[- ]to[- ]date|month[- ]to[- ]date)\b"
    r"|\bso far this\b|\brecent(?:ly)?\b",
    re.IGNORECASE,
)

# Deliberately only the "what is now" functions, and deliberately NOT INTERVAL, DATEADD, DATE_SUB
# or DATE_TRUNC. Those are date arithmetic, and arithmetic is relative only when its own anchor is
# — `CURRENT_DATE - INTERVAL '90 days'` already matches here on `CURRENT_DATE`, so adding INTERVAL
# would buy nothing. DATE_TRUNC would actively break the lint: it is how the frozen case this
# exists to catch is usually written (`DATE_TRUNC('quarter', placed_at) = '2024-01-01'`), so
# treating it as an anchor would suppress the report on exactly the statement it should fire on.
_NOW_ANCHOR_RE = re.compile(
    r"\b(?:CURRENT_DATE|CURRENT_TIME|CURRENT_TIMESTAMP|LOCALTIMESTAMP|"
    r"SYSDATE|NOW|GETDATE|CURDATE|TODAY)\b|'now'",
    re.IGNORECASE,
)

# A day pinned in the text: a quoted ISO date (with an optional time) or a bare four-digit year.
_FROZEN_DATE_RE = re.compile(r"'\d{4}-\d{2}-\d{2}(?:[ T][\d:.]+)?'|\b(?:19|20)\d{2}\b")


def _lint_relativity(item: GoldenItem, stem: str, res: ValidationResult) -> None:
    """Report an item whose question moves with time while its answer key does not.

    Unlike the refusals above this does not drop the item: the case is broken, not the model, so a
    runner that scored it as a failure would blame the wrong thing. It stays in the dataset and the
    fault is carried by a finding instead.

    An item with no `expected.sql` is skipped in silence — there is nothing to inspect, and an
    unconfirmed case with no answer key is a legal, in-progress shape.
    """
    sql = item.expected.sql
    if not sql or not _RELATIVE_QUESTION_RE.search(item.query):
        return
    if not _FROZEN_DATE_RE.search(sql) or _NOW_ANCHOR_RE.search(sql):
        return
    locator = f"{stem}.yaml[{item.id}]"
    res.error(
        "golden_relative_question_frozen_sql",
        f"{locator}: the question is asked relative to today but the answer key is pinned to fixed "
        "dates, so the question moves with time and the answer key does not; anchor the SQL to the "
        "current date or rewrite the question to name the window",
        locator=locator,
    )


def load_golden_datasets(
    profile: str, art: Path | None = None
) -> tuple[list[GoldenDataset], ValidationResult]:
    """Read every ``*.yaml`` golden dataset for `profile`, with findings for whatever was dropped.

    A profile with no golden datasets at all is the normal starting state, so a missing directory
    is silent: reporting it would teach the reader to skim past findings that do matter.
    """
    res = ValidationResult()
    gdir = agami_paths.profile_dir(profile, art) / "golden_datasets"
    if not gdir.exists():
        return [], res

    for stray in sorted(gdir.glob("*.yml")):
        # Ignoring it quietly would drop the file out of the run with nothing said, which is the
        # silent hole `extra="forbid"` exists to close everywhere else in this module.
        res.error(
            "golden_misnamed_file",
            f"{stray.name}: golden datasets are `*.yaml`, so this file is not read",
            locator=stray.name,
        )

    datasets: list[GoldenDataset] = []
    # Sorted, so two runs over the same directory order their datasets the same way.
    for path in sorted(gdir.glob("*.yaml")):
        locator = f"{path.stem}.yaml"
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            res.error("golden_unreadable_file", f"{locator}: {exc}", locator=locator)
            continue
        # An author who has created the file but not yet written a case has a dataset with no
        # cases, not a fault.
        if doc is None:
            doc = {}
        if not isinstance(doc, dict):
            res.error(
                "golden_unreadable_file",
                f"{locator}: the file's root is a {type(doc).__name__}, not a mapping",
                locator=locator,
            )
            continue

        body = {k: v for k, v in doc.items() if k != "test_cases"}
        if "name" in body:
            # The filename is the dataset's identity, so a second place to declare it cannot
            # exist — the two would disagree and nothing on disk would say which one won.
            res.error(
                "golden_invalid_dataset",
                f"{locator}: declares a `name:` key; the filename is the dataset's name",
                locator=locator,
            )
            continue

        items: list[GoldenItem] = []
        seen: set[str] = set()
        for index, raw in enumerate(doc.get("test_cases") or []):
            authored_id = raw.get("id") if isinstance(raw, dict) else None
            # The author's id is the name they can search for; the index is the fallback for a
            # case too broken to carry one.
            case_locator = f"{locator}[{authored_id if isinstance(authored_id, str) else index}]"
            try:
                item = GoldenItem.model_validate(raw)
            except ValidationError as exc:
                res.error("golden_invalid_case", f"{case_locator}: {exc}", locator=case_locator)
                continue
            if item.item_key in seen:
                res.error(
                    "golden_duplicate_item_key",
                    f"{case_locator}: this id was already used in this file; keeping the first",
                    locator=case_locator,
                )
                continue
            seen.add(item.item_key)
            _lint_relativity(item, path.stem, res)
            items.append(item)

        try:
            datasets.append(GoldenDataset(name=path.stem, test_cases=items, **body))
        except ValidationError as exc:
            res.error("golden_invalid_dataset", f"{locator}: {exc}", locator=locator)

    return datasets, res


__all__ = [
    "MatchLevel",
    "GoldenRecorded",
    "GoldenConfirmedBy",
    "GoldenExpected",
    "GoldenItem",
    "GoldenDataset",
    "load_golden_datasets",
]
