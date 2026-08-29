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


class GoldenBounds(_Base):
    """The band a `bounded` item's result has to land in.

    Every edge is optional and any one of them is a band: a case that only cares the answer is not
    zero says so with a floor, and inventing a ceiling to go with it would assert something the
    author never checked.
    """

    min_rows: Optional[int] = None
    max_rows: Optional[int] = None
    # A numeric band on a single-cell answer, for an item whose result is one number.
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    @model_validator(mode="after")
    def _band_bounds_something(self) -> "GoldenBounds":
        # Each refusal here is a band that reads as passing while comparing nothing (all-empty) or
        # as failing forever (a floor above its ceiling) — both look like a model fault to whoever
        # reads the run, which is the wrong thing to be told. No message names a value: the band
        # is authored data on a case that also carries the answer key, and a finding travels.
        rows, values = (self.min_rows, self.max_rows), (self.min_value, self.max_value)
        if all(edge is None for edge in rows + values):
            raise ValueError(
                "bounds names no bound; set at least one of min_rows, max_rows, min_value, "
                "max_value, or drop the block and use a looser match level"
            )
        if any(edge is not None and edge < 0 for edge in rows):
            raise ValueError(
                "min_rows and max_rows count rows and cannot be negative; use 0 to mean no rows"
            )
        for low, high, floor, ceiling in ((*rows, "min_rows", "max_rows"),
                                          (*values, "min_value", "max_value")):
            if low is not None and high is not None and low > high:
                raise ValueError(
                    f"{floor} is above {ceiling}, so no result can land in the band; swap them"
                )
        return self


class GoldenItem(_Base):
    """One question, its answer key, and how strictly to compare against it."""

    id: str
    query: str
    expected: GoldenExpected
    match: MatchLevel = "exact"
    bounds: Optional[GoldenBounds] = None
    must_filter: list[str] = Field(default_factory=list)
    recorded: Optional[GoldenRecorded] = None
    tags: list[str] = Field(default_factory=list)
    confirmed_by: Optional[GoldenConfirmedBy] = None

    @property
    def item_key(self) -> str:
        """The author's own id. The reader mints nothing: a key derived from position would move
        the day a case is inserted above this one, detaching every result already stored under it."""
        return self.id

    @model_validator(mode="after")
    def _confirmed_needs_answer_key(self) -> "GoldenItem":
        # A confirmed case is the one kind that can gate a run, so it is also the one kind that
        # must be able to fail one. With no SQL there is nothing to compare it against and it
        # would pass forever without anyone noticing.
        if self.expected.sql_confirmed and not self.expected.sql:
            raise ValueError("expected.sql_confirmed is true but expected.sql is missing")
        return self

    @model_validator(mode="after")
    def _bounded_needs_a_band(self) -> "GoldenItem":
        # `bounds` is read by one comparison and no other, so either half alone is a case that
        # silently does not do what it says — a band nothing consults, or a level with nothing to
        # consult. Both keep passing, which is why neither is allowed to be written.
        if self.match == "bounded" and self.bounds is None:
            raise ValueError(
                "match is bounded but there is no bounds block to compare against; add one, or "
                "pick a match level that judges the result on its own"
            )
        if self.bounds is not None and self.match != "bounded":
            raise ValueError("bounds is set but match is not bounded, so nothing reads the band")
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
# The function-style names need their call paren: bare, they match a comment ("frozen as of
# now"), a CTE (`WITH today AS ...`) or an alias (`AS now`), and a false anchor is the dangerous
# direction — it silently suppresses the report on a genuinely frozen answer key.
_NOW_ANCHOR_RE = re.compile(
    r"\b(?:CURRENT_DATE|CURRENT_TIME|CURRENT_TIMESTAMP|LOCALTIMESTAMP|SYSDATE)\b"
    r"|\b(?:NOW|GETDATE|CURDATE|TODAY)\s*\(|'now'",
    re.IGNORECASE,
)

# A day pinned in the text: a quoted ISO date, with an optional time and — on that time — an
# optional UTC designator or offset. The zone has to be part of the pattern rather than left off
# the end: the closing quote is what terminates the match, so `'2024-01-01T00:00:00Z'` and
# `'2024-01-01 00:00:00+00'` would otherwise not match at all, and a timestamp is no less frozen
# for carrying the zone it was written in. Deliberately NOT a bare four-digit integer —
# `LIMIT 2000`, `total_amount > 1999` and `order_id = 2024` are all far more common than a year
# written loose, and this lint is error severity, so a false positive here flips a correct file to
# not-ok and teaches readers to skim the findings that matter.
_FROZEN_DATE_RE = re.compile(
    r"'\d{4}-\d{2}-\d{2}(?:[ T][\d:.]+(?:[Zz]|[+-]\d{2}(?::?\d{2})?)?)?'"
)


def _read_failure(exc: OSError | UnicodeDecodeError | yaml.YAMLError) -> str:
    """Describe a failed read without the location the exception may carry.

    `OSError.__str__` interpolates the full path, and in a hosted deployment the artifacts path
    encodes the tenant — so only the error class and its `strerror` survive. A YAML or decode
    error is already path-free (`safe_load` is handed a string, so its mark reads
    `<unicode string>`) and keeps its full, useful text.
    """
    if isinstance(exc, OSError):
        return f"{exc.__class__.__name__}: {exc.strerror or 'cannot be read'}"
    return str(exc)


def _validation_digest(exc: ValidationError) -> str:
    """Render a pydantic failure as field, reason and rule — never the value that failed.

    Pydantic's own text includes `input_value=`, which for these models is the author's answer key:
    the SQL, the `must_filter` entries, the recorded rows. A finding is forwarded wherever the
    caller sends its `ValidationResult`, so the value is the one part that must not travel; the
    field and the reason are what a fix needs anyway.
    """
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"])
        reason = f"{err['msg']} [{err['type']}]"
        parts.append(f"{where}: {reason}" if where else reason)
    return "; ".join(parts)


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
            res.error(
                "golden_unreadable_file", f"{locator}: {_read_failure(exc)}", locator=locator
            )
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

        stray_keys = [k for k in body if not isinstance(k, str)]
        if stray_keys:
            # YAML 1.1 resolves a bare `no:`/`on:`/`yes:` to a boolean and `2024:` to an int, and
            # splatting one raises a TypeError that would escape the read and cost every other
            # file in the directory.
            res.error(
                "golden_invalid_dataset",
                f"{locator}: the top level has a non-text key ({', '.join(repr(k) for k in stray_keys)}); "
                "quote it if it is meant to be a name",
                locator=locator,
            )
            continue

        raw_cases = doc.get("test_cases")
        if raw_cases is None:
            raw_cases = []
        if not isinstance(raw_cases, list):
            # Iterating a string would emit a finding per character and a scalar would raise out
            # of the whole read, so the file is refused here instead.
            res.error(
                "golden_invalid_dataset",
                f"{locator}: `test_cases` is a {type(raw_cases).__name__}, not a list of cases",
                locator=locator,
            )
            continue

        items: list[GoldenItem] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_cases):
            authored_id = raw.get("id") if isinstance(raw, dict) else None
            # The author's id is the name they can search for; the index is the fallback for a
            # case too broken to carry one.
            case_locator = f"{locator}[{authored_id if isinstance(authored_id, str) else index}]"
            try:
                item = GoldenItem.model_validate(raw)
            except ValidationError as exc:
                res.error(
                    "golden_invalid_case",
                    f"{case_locator}: {_validation_digest(exc)}",
                    locator=case_locator,
                )
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
            res.error(
                "golden_invalid_dataset", f"{locator}: {_validation_digest(exc)}", locator=locator
            )

    return datasets, res


__all__ = [
    "MatchLevel",
    "GoldenBounds",
    "GoldenRecorded",
    "GoldenConfirmedBy",
    "GoldenExpected",
    "GoldenItem",
    "GoldenDataset",
    "load_golden_datasets",
]
