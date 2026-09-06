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

# Slice 1's own product: the record a row is written down as, which is what Phase 3e later reads.
RECORD = SKILL.split("### 2d — Build the row record")[1].split("## Phase 3: Present")[0]


def test_the_skill_carries_the_four_frontmatter_keys():
    """The house shape, and the frontmatter is where the new routing triggers have to land: a
    trigger phrase that is only in the conventions table routes nothing."""
    assert SKILL.startswith("---\n")
    assert "name: agami-reconcile" in FRONTMATTER
    assert "description:" in FRONTMATTER
    assert "when_to_use:" in FRONTMATTER
    assert 'argument-hint: "<screenshot | path-to-csv | pasted numbers>"' in FRONTMATTER


def test_the_row_record_keeps_the_statement_and_the_result_beside_the_number():
    """Slice 1's whole product, asserted on the RECORD rather than on anything rendered from it.

    The record is what Phase 3e reads to build an item, so a promotion that FORWARDS the statement
    and the receipt instead of rebuilding them is only possible if the record carried both. And the
    keys a reader already knows have to still be there beside them: the run writes this shape to a
    jsonl a person opens later, and the two new keys are additive or they are a break.
    """
    assert '"sql":' in RECORD
    assert '"recorded":' in RECORD
    # `recorded` is shaped like the golden receipt — the two keys a promotion hands straight on.
    recorded = next(line for line in RECORD.splitlines() if line.strip().startswith('"recorded":'))
    assert '"columns"' in recorded and '"rows"' in recorded
    # An error row carries neither, so nothing on it can be mistaken for a verified answer.
    assert '**On a `status: "error"` row both `sql` and `recorded` are `null`.**' in RECORD

    for key in (
        "label", "question", "expected", "actual", "delta_pct",
        "match", "status", "report_path", "error",
    ):
        assert f'"{key}":' in RECORD, key


def test_sql_stays_out_of_the_narration_except_where_it_is_the_thing_being_accepted():
    """Both halves of the shipped rule.

    The rule itself still stands — agami-query spells out what it forbids, and this skill defers to
    it. The offer is named as its one exception because what the person is accepting there is the
    STATEMENT: a receipt link is not where you read something you are about to agree to replay.
    """
    style = SKILL.split("## Conversation style")[1].split("\n---")[0]
    assert "**Don't paste raw SQL in chat.**" in style
    assert "Same hard rule as agami-query" in style
    assert "with one exception, Phase 3e's promotion offer" in style
    assert "the thing being accepted into an answer key" in style
    # One exception in the whole skill, and it is that one.
    assert SKILL.count("exception") == 1


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


def test_only_a_single_cell_result_is_offered():
    """A value band judges one number. Handed a wider result the comparator finds no single cell,
    skips the value band and scores the item on its row count alone — so a two-column row promoted
    here writes an answer key that passes forever without checking the number it was promoted for.
    """
    assert "**Only a single-cell result is offered.**" in OFFER
    assert "more than one column" in OFFER
    assert "scored on its row count alone" in OFFER


def test_the_offer_carries_the_runs_own_tolerance_and_never_a_hardcoded_one():
    """The offer is told to pass the tolerance the run diffed with, so every literal in the section
    has to be that placeholder. A hardcoded ±1% in the sentence said out loud, in the provenance
    line or in the band command makes a `tolerance=5%` run offer a band it did not agree on."""
    assert "±<the run's tolerance> band" in OFFER
    assert "agreed within ±<the run's tolerance>" in OFFER
    assert "--tolerance <the run's tolerance>" in OFFER
    assert "0.01" not in OFFER
    assert "±1%" not in OFFER


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

    # The skill mentions `CURRENT_DATE` in exactly two places and both are these lines, verbatim.
    # A looser sweep — any line carrying a forbidding word somewhere on it — would wave through
    # "never rewrite the question, anchor to CURRENT_DATE", which is the instruction being banned.
    permitted = {
        "- **Never re-anchor the statement to `CURRENT_DATE`.** It clears the lint and it is a "
        "**trap**: the band was recorded around today's value of a window that slides, so next "
        "month the same question asks about different days, returns a different number, and fails "
        "against a band nobody moved. That is a false alarm on the one surface whose whole job is "
        "to be believed.",
        "| A promotion exits `2` saying the question moves with time and the answer key doesn't | "
        "**Rename the question, don't re-anchor the statement.** Ask which window it meant, "
        'rewrite the question to name it ("…in August 2026"), and re-run with the SQL exactly as '
        "it ran. Anchoring the SQL to `CURRENT_DATE` clears the lint and bands a sliding window "
        "around one day's value — a false alarm at the next run. |",
    }
    mentions = {line for line in SKILL.splitlines() if "CURRENT_DATE" in line}
    assert mentions == permitted, f"CURRENT_DATE said in a way this test has not read: {mentions}"


def test_the_window_is_settled_in_the_offer_and_a_late_refusal_rolls_nothing_back():
    """Which window a relative question meant is asked BEFORE anything is written, not in reaction
    to the refusal — because a batch is one call per row, so a refusal on the seventh row lands
    with six items already on disk. Saying that out loud is the point: nothing is rolled back, and
    a model that believed otherwise would go looking for an undo that does not exist."""
    assert "**Ask which window it means in the offer, before anything is written**" in OFFER
    assert "**items already written stay written, and nothing is rolled back**" in OFFER
    # The cheat-sheet row stays, demoted to the fallback it is.
    assert "is the fallback for a question that slips through, not the plan" in OFFER


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
    # And it is called once per row. The door reads `payload["query"]` — an array handed to it is
    # a TypeError nobody catches, and "ask which dataset once" reads like one call if nothing says
    # otherwise.
    assert "**One call per kept row.**" in OFFER
    assert "one item, not an array" in OFFER


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
    # …and the carve-out is two doors now, with the person's yes in front of both. It names BOTH
    # routes: a hard rule is the most authoritative prose in a skill, so one that named only the
    # offer would have a model refuse the resolution write the rest of Phase 3e requires.
    assert "golden_author.py save" in rule
    assert "sm add-example" in rule
    assert "both need the person's yes in front of them" in rule
    assert "this run scored it as agreeing" in rule
    assert "resolved in agami's favour" in rule
    # The split is what keeps a row out of both, and the hard rule is where that is unmissable.
    assert "No row goes to both" in rule


def test_the_roadmap_no_longer_promises_the_half_this_slice_shipped():
    """Promotion put reconciled numbers into the golden suite. What is still ahead is the recurring
    run against a pinned dashboard, and that is all the entry may now claim."""
    roadmap = SKILL.split("## Roadmap (not in v1)")[1]
    assert "**Recurring reconcile runs**" in roadmap
    assert "golden-test suite" not in roadmap
    assert "agami test" not in roadmap


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


# --- The split (#265) ----------------------------------------------------------------------------
#
# A reconcile run proves statements correct against evidence from outside agami, and only half of
# that value was kept: agreeing rows became tests and taught the model nothing. They cannot be both,
# because an item that is also an example grades its own study material — so the batch is split.


def test_the_agreeing_rows_are_split_between_teaching_and_testing():
    """Both destinations, and the reason one row cannot be both."""
    assert "**golden item** tests the model" in OFFER
    assert "**prompt example** teaches it" in OFFER
    assert "grading its own study material" in OFFER
    assert "the batch is split rather than duplicated" in OFFER


def test_the_product_does_the_splitting_and_not_the_user():
    """Which rows should teach depends on what the example library already covers, which nobody can
    answer without reading it. `high_confidence` is the product's own judgement of that, so the
    split is a lookup rather than a question."""
    assert "You do the splitting, not the user." in OFFER
    assert "high_confidence" in OFFER
    assert "sm" in OFFER and "--query" in OFFER
    # Both directions are stated, or the rule is half a rule.
    assert "`high_confidence: false`" in OFFER and "Teach with it" in OFFER
    assert "`high_confidence: true`" in OFFER and "Test with it" in OFFER


def test_a_batch_too_small_to_split_is_not_split():
    """A split of three leaves too little on either side to be worth the explanation, and a profile
    with nothing curated needs teaching more than gating."""
    assert "Fewer than four agreeing rows" in OFFER
    # A cap and not a preference: an empty library reads every row as novel, so a floor on the
    # teaching side would send all of them there and write no test at all — on exactly the profile
    # this skill is aimed at.
    assert "Never send more than half the batch to the examples." in OFFER
    assert "an onboarding run that writes no test at all has failed" in OFFER


def test_the_user_makes_one_decision_and_it_is_not_the_split():
    """A per-row prompt turns a twelve-number reconcile into twelve interruptions. The override
    exists and is one line, not twelve."""
    assert "Make the offer once, here, after the summary. Never per row." in OFFER
    assert "Save all ten?" in OFFER
    assert "let me choose" in OFFER


def test_what_went_where_is_said_out_loud():
    """Writing to the examples changes how agami answers future questions. The split may be
    automatic; it must not be invisible."""
    assert "Say what goes where. This is not optional." in OFFER
    assert "so questions like them get answered this way from now on" in OFFER
    assert "so you're told if any of those numbers move" in OFFER


def test_a_promoted_row_does_not_re_ask_the_convention_question():
    """The save door checks a statement against the profile's examples, which is right when somebody
    is authoring a key by hand. Here the statement being saved is the one agami generated FROM those
    examples minutes ago."""
    assert "--confirm-convention" in OFFER
    assert "a convention they never departed from" in OFFER


def test_an_example_is_written_through_the_packaged_writer():
    """One writer, and never a hand-edited library — the same door `agami-save-correction` uses."""
    assert "add-example" in OFFER
    assert "Do not hand-edit `examples.yaml`" in OFFER
    assert '"source": "reconcile"' in OFFER
