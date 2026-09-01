"""AH-111 — the reconcile skill's promotion offer, and the phases it must not have disturbed.

Everything here asserts on Markdown, for the reason `test_ah109_save_golden_skill.py` gives about
its own: the helpers cannot make the model offer the agreeing rows once instead of twelve times,
decline to promote a row that has no statement, or rewrite a question rather than re-anchoring its
SQL. The skill is the only place those live, so "the skill says X" is the only decidable check
there is.

This is also the first file to pin `agami-reconcile`'s prose at all, which is why the last test
here is a regression pin rather than a claim about the new behaviour: `agami-reconcile` is a
shipped skill and the strongest onboarding demo in the product, so a slice that adds an offer after
the summary has to leave everything up to the summary reading exactly as it did.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "plugins" / "agami" / "skills"
SHARED = REPO_ROOT / "plugins" / "agami" / "shared"

SKILL = (SKILLS_DIR / "agami-reconcile" / "SKILL.md").read_text(encoding="utf-8")
FRONTMATTER = SKILL.split("---")[1]
WHEN_TO_USE = re.search(r"^when_to_use: \"(.*)\"$", FRONTMATTER, re.MULTILINE).group(1)
CONVENTIONS = (SHARED / "invocation-conventions.md").read_text(encoding="utf-8")

# The offer's own section, bounded by the heading that follows it. Several assertions below are
# about what the offer does NOT say, and those are only meaningful over the section rather than
# over a file that also documents the mismatch conversation.
OFFER = SKILL.split("### 3e — Offer promotion")[1].split("### 3f — Closing prompt")[0]


def test_the_skill_carries_the_four_frontmatter_keys():
    """The house shape, and the frontmatter is where the new routing triggers have to land: a
    trigger phrase that is only in the conventions table routes nothing."""
    assert SKILL.startswith("---\n")
    assert "name: agami-reconcile" in FRONTMATTER
    assert "description:" in FRONTMATTER
    assert "when_to_use:" in FRONTMATTER
    assert 'argument-hint: "<screenshot | path-to-csv | pasted numbers>"' in FRONTMATTER


def test_the_offer_is_made_once_after_the_summary_and_only_for_agreeing_rows():
    """The offer's two structural rules. Once and after, because a per-row prompt buries the
    mismatches that are the run's actual value; and only the rows the run itself scored as
    agreeing, because `reconcile.diff`'s verdict is the only notion of agreement in this tree."""
    assert "### 3e — Offer promotion" in SKILL
    # Between the matches summary and the closing prompt, in that order.
    assert (
        SKILL.index("### 3d — Matches summary")
        < SKILL.index("### 3e — Offer promotion")
        < SKILL.index("### 3f — Closing prompt")
    )
    assert "Make the offer once, here, after the summary. Never per row." in OFFER
    assert "Only rows whose `status` is `match` are offered." in OFFER
    assert "`reconcile.diff`" in OFFER
    # One predicate, naming every status it drops — including the two only `diff` knows about.
    for dropped in ("`mismatch`", "`error`", "`missing_expected`", "`missing_actual`"):
        assert dropped in OFFER


def test_the_agreeing_rows_start_selected_and_the_statement_is_shown():
    """The person already reviewed each row's agreement during the run, so re-confirming every one
    asks the same question twice. What they have not yet accepted is the STATEMENT — that is what a
    later run replays — so the offer shows it rather than the number alone."""
    assert "Every agreeing row starts selected." in OFFER
    assert "they can edit any question before it is written" in OFFER
    assert "Show the statement and the result, not just the number." in OFFER


def test_a_run_with_no_agreeing_rows_makes_no_offer():
    """An empty offer interrupts the conversation an all-mismatch run is actually having."""
    assert "If no row agreed, make no offer at all" in OFFER
    assert "not an empty one" in OFFER


def test_an_error_row_is_never_offered_and_the_prose_says_why():
    """The reason is the point: an error row has no statement to replay, so there is nothing a
    promotion could write that a later run could score."""
    assert "A row with no statement is never offered" in OFFER
    assert "`sql: null`" in OFFER


def test_a_disagreeing_row_is_not_offered_and_needs_the_explicit_resolution_step():
    """Promoting a mismatch writes an answer key that contradicts the number the team currently
    believes. That is a legitimate thing to do and it should cost a sentence, not a checkbox."""
    assert "The resolution path is not part of the offer." in OFFER
    assert "cannot be written by accepting the offer" in OFFER
    assert "friction is deliberate" in OFFER


def test_declining_writes_nothing():
    """The regression bar for this whole slice: a declined offer leaves a run looking exactly as it
    looked before there was an offer at all."""
    assert "**Declining writes nothing.**" in OFFER
    assert "No dataset is created, no file is touched" in OFFER


def test_the_relativity_guidance_renames_the_question_and_never_reanchors_the_sql():
    """The trap. Both ways past the lint save; only one of them stays true.

    Anchoring the statement to `CURRENT_DATE` clears the refusal and bands a window that slides
    around one day's value, so the item drifts out of its own band and fails a later run as a false
    alarm — on the surface whose whole job is to be believed. Every mention of `CURRENT_DATE` in the
    skill therefore has to be a prohibition, which is what the sweep below checks.
    """
    assert "Never re-anchor the statement to `CURRENT_DATE`." in OFFER
    assert "rewrite the question to name it" in OFFER
    assert "It never edits the SQL." in OFFER
    # And again in the cheat sheet, where somebody lands holding an exit 2.
    assert "**Rename the question, don't re-anchor the statement.**" in SKILL

    forbidding = ("Never", "never", "don't", "trap")
    offenders = [
        line
        for line in SKILL.splitlines()
        if "CURRENT_DATE" in line and not any(word in line for word in forbidding)
    ]
    assert not offenders, f"CURRENT_DATE offered rather than refused: {offenders}"


def test_the_promotion_writes_through_the_save_door_and_nothing_else():
    """One writer. The item is built with the Write tool and handed to the save door with the flags
    that door actually takes; `id` is omitted so the door derives it from the question, which is
    what makes a promotion land ON an already-imported question rather than beside it."""
    assert 'python3 "$AGAMI_PLUGIN_ROOT/scripts/golden_author.py" save' in OFFER
    for flag in ("--profile", "--dataset", "--item"):
        assert flag in OFFER
    assert "never a heredoc, never `python3 -c`" in OFFER
    assert "**`id` is omitted on purpose.**" in OFFER
    assert "rather than beside it" in OFFER


def test_the_band_comes_from_the_band_verb_and_not_from_prose_arithmetic():
    """A band computed in prose is a band nobody can reproduce, and this one is the thing a later
    run is scored against. `match: bounded` with no band is refused outright, so the two travel
    together."""
    assert 'python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" band' in OFFER
    assert "--value" in OFFER and "--tolerance" in OFFER
    assert "never from arithmetic written in prose" in OFFER
    assert '"match": "bounded"' in OFFER


def test_the_two_confirmed_by_method_shapes_are_documented_and_distinguishable():
    """A reader a year later has to be able to tell an agreement from a resolution, so the two
    method lines differ in words rather than only in tone — and each names the source, the date and
    the tolerance, which is the whole of what makes the claim auditable."""
    agreed = "reconciled against <source> on <date>; agreed within ±<tolerance>"
    resolved = (
        "reconciled against <source> on <date>; disagreed beyond ±<tolerance>, "
        "resolved in agami's favour by the analyst"
    )
    assert agreed in OFFER
    assert resolved in OFFER
    assert agreed != resolved
    for shape in (agreed, resolved):
        assert "<source>" in shape and "<date>" in shape and "±<tolerance>" in shape


def test_confirm_replace_is_never_preemptive_and_the_before_and_after_come_first():
    """Exit 1 is the append-only stop, and the flag means one thing: a person saw both sides and
    said yes. A replacement is wholesale, so whatever the `before` carries has to be carried
    forward or it is gone."""
    assert "Render the `before` AND the `after` for every id" in OFFER
    assert "Never pass `--confirm-replace` pre-emptively" in OFFER
    assert OFFER.index("Render the `before`") < OFFER.index("--confirm-replace` pre-emptively")
    assert "carry forward whatever it holds that still applies" in OFFER
    assert "On a no, say the file is untouched and stop." in OFFER


def test_the_cheat_sheet_carries_the_save_doors_exit_codes():
    """The cheat sheet is where somebody lands holding a non-zero exit, and `1` is the one that
    must not be read as a failure or as a success."""
    assert "| A promotion exits `0` |" in SKILL
    assert "| A promotion exits `1` with `needs_confirmation` |" in SKILL
    assert "| A promotion exits `2` |" in SKILL


def test_hard_rule_3_still_forbids_mutating_the_semantic_model():
    """The highest-regression edit in the slice. The carve-out is for the answer key that TESTS the
    model, and it must not have loosened the rule it is carved out of: no metric, join or column is
    ever mutated from here, and a definitional disagreement still routes to the correction skill."""
    rule = SKILL.split("3. **Don't write to the semantic model from this skill.**")[1].split(
        "\n4. "
    )[0]
    assert "never mutates a metric, a join, a column" in rule
    assert "/agami-save-correction" in rule
    # …and the carve-out is exactly one door, with the user's yes in front of it.
    assert "golden_author.py save" in rule
    assert "after the user has said yes" in rule


def test_the_hard_rules_are_not_renumbered():
    """The screenshot section cross-references rule #4 by number, so renumbering the list silently
    re-points that reference at a different rule."""
    assert "Hard rule #4" in SKILL
    assert "4. **CSV stays local.**" in SKILL
    assert "5. **" not in SKILL.split("## Hard rules")[1].split("---")[0]
    # Rule 4's artifact list now says the jsonl carries statements, so "stays local" still covers
    # everything the run wrote down.
    assert "carries the statement behind every row" in SKILL


def test_the_routing_triggers_are_the_skills_own():
    """The conventions table header says the triggers come from `when_to_use`, so a phrase in the
    row that the frontmatter does not carry reads as a trigger and routes nothing.

    This row carried two such phrases before this slice ("reconcile against my dashboard", which
    the skill spells with *this*, and "verify these numbers against agami", which the skill never
    said at all). Correcting them is why this test exists here rather than only beside the skill it
    was first written for.
    """
    row = next(line for line in CONVENTIONS.splitlines() if line.startswith("| agami-reconcile |"))
    quoted = re.findall(r'"([^"]+)"', row)

    assert quoted, "the agami-reconcile row no longer quotes any trigger phrase"
    for phrase in quoted:
        assert phrase in WHEN_TO_USE, phrase


def test_the_promotion_triggers_reach_both_surfaces():
    """A user who has just watched ten numbers agree says "keep these as golden questions", and
    that has to route here rather than nowhere."""
    for phrase in ("keep these as golden questions", "promote these to a golden dataset"):
        assert phrase in WHEN_TO_USE
        assert phrase in CONVENTIONS


def test_phases_3a_to_3d_read_exactly_as_they_did():
    """The regression pin. Reconciliation itself is untouched by this slice, and the way a run
    presents itself up to the matches summary is the shipped demo — so each phase's heading and the
    line it opens on are asserted verbatim, in order.
    """
    opening = [
        ("### 3a — Summary line first", "```"),
        (
            "### 3b — Mismatches table (lead with what didn't match)",
            "Render the mismatches as a markdown table BEFORE the matches:",
        ),
        ("### 3c — Errors block (if any)", "```markdown"),
        ("### 3d — Matches summary (last, compact)", "```markdown"),
    ]
    lines = SKILL.splitlines()
    positions = []
    for heading, first in opening:
        assert heading in lines, heading
        index = lines.index(heading)
        positions.append(index)
        # The blank line, then the line the phase opens on.
        assert lines[index + 1] == ""
        assert lines[index + 2] == first, (heading, lines[index + 2])
    assert positions == sorted(positions)

    # The lines each phase is recognised by, none of which this slice had any business touching.
    assert "Reconciled <N> numbers: <M> match (within ±1%), <K> mismatch, <E> error." in SKILL
    assert "This is where the trust win lands." in SKILL
    assert "Could not extract a single scalar — the question returned 47 rows." in SKILL
    assert (
        "Don't dump every match's drill-down — they're not interesting. The matches build the "
        "case; the mismatches drive the conversation." in SKILL
    )
