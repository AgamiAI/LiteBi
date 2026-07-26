"""Org context for the LLM + the explorer — separated into two homes that never collide.

The architecture deliberately keeps the human's words and the auto-derived facts apart:

* **ORGANIZATION.md** is the human's narrative ONLY (what the company/product is, who the
  users are). agami never writes facts into it, so there is nothing for a human to
  accidentally overwrite or delete. `starter_organization_md()` is the blank-path prompt.
* **The factual context** — shape, subject areas, conventions, and the decoded glossary — is
  `derived_context()`, computed FRESH from the structured model at read time. The glossary
  lives in the structured `key_terminology` field, not inline prose, so it always reaches the
  LLM regardless of what's in (or missing from) ORGANIZATION.md.

`compose_context(human_md, org)` assembles the two for a reader (MCP / query skill / explorer):
the human's narrative, then the derived summary under its own heading. Never a re-listing of
the model — counts and bounded summaries only, so it stays usable at thousands of tables.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Organization, OrgRecord


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# Cap the subject-area summary — a model can have many areas (and tables can run to the
# thousands); list a bounded sample and count the rest rather than dumping everything. The
# full tables/metrics/entities live in the structured model, browsable in the explorer.
_MAX_AREAS_LISTED = 30
_AREA_DESC_CHARS = 110


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _strip_comments(text: str) -> str:
    """Drop HTML comments + collapse the blank lines they leave — the human-only scaffolding
    the LLM shouldn't read."""
    out = re.sub(r"<!--.*?-->", "", text or "", flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def derived_context(org: "Organization", *, with_curated_glossary: bool = True) -> str:
    """The model-DERIVED factual context: shape + subject areas + conventions + glossary.

    Computed fresh from the structured model every time and NOT persisted into ORGANIZATION.md
    — so there are no fragile markers for a human to clobber, and the glossary always reaches
    the LLM (it no longer depends on a file having been re-rendered). The glossary comes from
    the structured `key_terminology` field + `choice_field` enum legends. Never a re-listing
    of the model — counts + bounded summaries only, usable even at thousands of tables.

    `with_curated_glossary=False` omits the curated `key_terminology` terms (the enum legends
    still render) — the explorer uses this so it can present the curated glossary as an
    EDITABLE panel instead, while the rest of this block stays read-only."""
    areas = list(org.subject_areas)
    n_tables = sum(len(sa.tables_defined) for sa in areas)
    n_metrics = sum(len(sa.metrics) for sa in areas) + len(org.cross_subject_area_metrics)
    n_entities = sum(len(sa.entities) for sa in areas) + len(org.cross_subject_area_entities)

    lines: list[str] = [
        f"**{org.organization}** — {_plural(n_tables, 'table')} across {_plural(len(areas), 'subject area')}.",
        "",
    ]
    if areas:
        lines.append("### Subject areas")
        lines.append("")
        for sa in areas[:_MAX_AREAS_LISTED]:
            live = [t for t in sa.tables_defined if t.review_state != "rejected"]
            row = f"- **{sa.name}** [{_plural(len(live), 'table')}]"
            desc = _short(sa.description, _AREA_DESC_CHARS)
            if desc and not desc.lower().startswith("auto-proposed subject area covering"):
                row += f" — {desc}"
            lines.append(row)
        if len(areas) > _MAX_AREAS_LISTED:
            lines.append(f"- …and {len(areas) - _MAX_AREAS_LISTED} more subject areas")
        lines.append("")

    defined = []
    if n_metrics:
        defined.append(_plural(n_metrics, "metric"))
    if n_entities:
        defined.append(f"{n_entities} entit" + ("y" if n_entities == 1 else "ies"))
    if defined:
        lines.append(f"{' and '.join(defined)} are defined in the model.")
        lines.append("")

    units = sorted({c.unit for sa in areas for t in sa.tables_defined for c in t.columns if c.unit})
    if units:
        lines.append("### Conventions")
        lines.append("")
        lines.append(f"- Units / currency in use: {', '.join(units)}.")
        lines.append("")

    _key_terminology(lines, org, areas, include_curated=with_curated_glossary)
    return "\n".join(lines).strip()


def compose_context(human_md: str, org: "Organization") -> str:
    """Read-time assembly of the full org context: the human's narrative (HTML comments
    stripped) followed by the model-derived summary under its OWN heading. The two parts stay
    SEPARATE — the human's prose is never mixed with auto content, so nothing can be
    accidentally overwritten. Either part may be empty. Used by the MCP, the query skill, and
    the explorer's Organization view."""
    human = _strip_comments(human_md)
    derived = derived_context(org)
    parts: list[str] = []
    if human:
        parts.append(human)
    if derived:
        parts.append("## Model summary (auto-generated from your schema)\n\n" + derived)
    return "\n\n".join(parts).strip()


def _company_block(record: "OrgRecord", narrative: str) -> str:
    """The shared COMPANY context (F15): name/description + the root ORGANIZATION.md narrative +
    company-wide display conventions + the company glossary. Rendered ONCE above every datasource."""
    lines: list[str] = [f"# {record.name or 'Company'} — company context", ""]
    if record.description:
        lines += [record.description, ""]
    human = _strip_comments(narrative)
    if human:
        lines += [human, ""]

    dc = record.display_conventions
    conventions: list[str] = []
    if record.fiscal_year_start_month:
        conventions.append(f"Fiscal year starts in month {record.fiscal_year_start_month}.")
    if dc.currency:
        conventions.append(f"Currency: {dc.currency}.")
    if dc.rounding is not None:
        conventions.append(f"Rounding: {dc.rounding} decimal places.")
    if dc.week_start:
        conventions.append(f"Week starts on {dc.week_start}.")
    conventions += [n for n in (dc.notes or []) if n]
    if conventions:
        lines.append("### Company conventions")
        lines.append("")
        lines += [f"- {c}" for c in conventions]
        lines.append("")

    if record.glossary:
        lines.append("### Company glossary")
        lines.append("")
        lines += [f"- **{term}**: {defn}" for term, defn in record.glossary.items()]
        lines.append("")
    return "\n".join(lines).strip()


def _source_block(org: "Organization", narrative: str) -> str:
    """One datasource's context: its optional source-specific narrative + the model-derived summary,
    under a heading naming the datasource (so a federated answer keeps the vocabularies apart)."""
    seg = [f"## {org.organization} — datasource context"]
    src = _strip_comments(narrative)
    if src:
        seg.append(src)
    body = derived_context(org)
    if body:
        seg.append(body)
    return "\n\n".join(seg).strip()


def compose_org_context(
    org_record: "OrgRecord | None",
    ontologies: "list[Organization]",
    *,
    company_narrative: str = "",
    source_narratives: "list[str] | None" = None,
) -> str:
    """Two-level org context (F15 / ACE-069). Renders the shared COMPANY block ONCE from the ``OrgRecord``
    (name/description + the root ``ORGANIZATION.md`` narrative + display conventions + company glossary),
    then each datasource's model-derived ontology under its own heading. Single-source passes one
    ontology; a FEDERATED question passes several — the company block still renders exactly once, and each
    source keeps its own vocabulary (so "Account" resolves per source).

    ``source_narratives`` (optional, aligned by index to ``ontologies``) carries each datasource's
    source-specific ``ORGANIZATION.md`` prose; company-wide prose now lives in ``company_narrative`` (the
    root file), not the per-profile ones.

    Graceful degradation: when ``org_record`` is ``None`` this returns exactly today's per-profile
    assembly (``compose_context`` of each ontology with its narrative) — byte-identical to pre-F15, so
    every deployment without a record keeps working untouched."""
    ontologies = list(ontologies)
    narrs = list(source_narratives or [])
    narrs += [""] * (len(ontologies) - len(narrs))  # pad so index access is safe

    if org_record is None:
        # No record: fall back to the pre-F15 single-level assembly, one block per ontology.
        return "\n\n".join(compose_context(narrs[i], org) for i, org in enumerate(ontologies)).strip()

    parts = [_company_block(org_record, company_narrative)]
    parts += [_source_block(org, narrs[i]) for i, org in enumerate(ontologies)]
    return "\n\n".join(p for p in parts if p).strip()


def starter_organization_md(org: "Organization") -> str:
    """A human-narrative STARTER for the skip path — never blank. It seeds `# About this
    database` with a one-line factual SUMMARY drawn from the model (org + what the subject
    areas cover) so the section reads as something, then invites the human to make it theirs.

    This is a one-time editable DRAFT (not the maintained derived block) — onboarding normally
    overwrites it with a richer 1-2 sentence narrative synthesised from the table descriptions."""
    areas = list(org.subject_areas)
    n_tables = sum(len(sa.tables_defined) for sa in areas)
    names = [sa.name for sa in areas]
    lead = f"**{org.organization}** holds {_plural(n_tables, 'table')} across {_plural(len(areas), 'subject area')}"
    # Name the areas when they add signal (skip a lone area that just echoes the org name).
    if names and not (len(names) == 1 and names[0].lower() == org.organization.lower()):
        shown = ", ".join(names[:6]) + (f", and {len(names) - 6} more" if len(names) > 6 else "")
        lead += f" covering {shown}."
    else:
        lead += "."
    return "\n".join([
        "# About this database",
        "",
        "<!-- A starting summary from your schema — edit it to say what only you know: what the",
        "     company/product is, who the users are, what your key terms mean. -->",
        "",
        lead,
        "",
    ])


def draft_organization_md(org: "Organization") -> str:
    """Back-compat: the model-derived context as a standalone document (no human prose).
    Prefer compose_context()/derived_context() in new code."""
    return compose_context("", org)


# How much of an enum column's value→meaning map to inline before truncating, and how
# many such columns to auto-seed — enough to be useful, capped so a code-heavy schema
# doesn't bury the curated glossary in machine-readable enum dumps.
_MAX_ENUM_VALUES = 10
_MAX_ENUM_COLS = 25


def _key_terminology(lines: list[str], org: "Organization", areas: list,
                     include_curated: bool = True) -> None:
    """Append the glossary: the curated `key_terminology` terms (only when `include_curated`)
    plus auto-derived enum legends from `choice_field` columns. Omitted entirely when there's
    nothing to show. The explorer passes include_curated=False — it edits the curated terms in
    a dedicated panel, leaving only the derived enum legends here as read-only."""
    glossary = {str(k).strip(): str(v).strip()
                for k, v in (getattr(org, "key_terminology", {}) or {}).items()
                if str(k).strip() and str(v).strip()} if include_curated else {}

    enum_lines: list[str] = []
    for sa in areas:
        for t in sa.tables_defined:
            if t.review_state == "rejected":
                continue
            for c in t.columns:
                cf = getattr(c, "choice_field", None)
                if not cf or getattr(c, "review_state", "approved") == "rejected":
                    continue
                items = list(cf.items())
                shown = "; ".join(f"`{k}` = {v}" for k, v in items[:_MAX_ENUM_VALUES])
                if len(items) > _MAX_ENUM_VALUES:
                    shown += "; …"
                enum_lines.append(f"- **{t.name}.{c.name}** — {shown}")
                if len(enum_lines) >= _MAX_ENUM_COLS:
                    break
            if len(enum_lines) >= _MAX_ENUM_COLS:
                break
        if len(enum_lines) >= _MAX_ENUM_COLS:
            break

    # Derived context, not a human prompt: if there's nothing structured to show, omit the
    # section entirely (the "add terms you know" nudge lives in the human starter file).
    if not glossary and not enum_lines:
        return

    if glossary:
        lines.append("### Key terminology")
        lines.append("")
        for term, definition in glossary.items():
            lines.append(f"- **{term}** — {definition}")
        if enum_lines:
            lines.append("")
            lines.append("Coded value legends:")
    elif enum_lines:
        lines.append("### Coded value legends")
        lines.append("")
    lines.extend(enum_lines)
    lines.append("")
