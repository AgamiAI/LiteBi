"""The save-golden skill's prose, and the shared docs that route to it.

Everything here asserts on Markdown rather than on code, for the reason `test_ah106_eval_skill.py`
gives about its own doc section: these behaviors have no code-level equivalent. The helper cannot
make the model render the parsed rows before importing them, refuse to invent SQL for a question
that has none, or decline to glob a neighbouring profile for a file shape — the skill is the only
place those live, so "the skill says X" is the only decidable check there is.

The one exception is the last test, which asserts on code precisely because it can: the answer key
must not be reachable from the MCP surface, and that is a fact about `tools.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "plugins" / "agami" / "skills"
SHARED = REPO_ROOT / "plugins" / "agami" / "shared"

SKILL = (SKILLS_DIR / "agami-save-golden" / "SKILL.md").read_text(encoding="utf-8")
FRONTMATTER = SKILL.split("---")[1]
CONVENTIONS = (SHARED / "invocation-conventions.md").read_text(encoding="utf-8")
FILE_LAYOUT = (SHARED / "file-layout.md").read_text(encoding="utf-8")
PLAN_MODE = (SHARED / "plan-mode-check.md").read_text(encoding="utf-8")


def test_the_skill_carries_the_four_frontmatter_keys():
    """The house shape. `argument-hint` is the one a new skill forgets, and without it the dataset
    name has nowhere to arrive from."""
    assert SKILL.startswith("---\n")  # frontmatter at the top, not prose
    assert "name: agami-save-golden" in FRONTMATTER
    assert "description:" in FRONTMATTER
    assert "when_to_use:" in FRONTMATTER
    assert 'argument-hint: "[dataset-name]"' in FRONTMATTER


def test_the_skill_refuses_in_plan_mode():
    """SC-9. Every door here writes to the model tree, so none of them can proceed read-only.

    The refusal has to be the literal sentence rather than any refusal, because the shared doc and
    the skill both carry it and a drift between the two is how a skill starts refusing with wording
    nobody wrote."""
    assert "shared/plan-mode-check.md" in SKILL  # the shared detection logic
    assert "I can't save a golden item in plan mode" in SKILL  # …and the sentence it ends on
    assert "DO NOT call `ExitPlanMode`" in SKILL  # …without leaving a plan file behind


def test_the_skill_carries_the_shared_shape_hard_rule():
    """A golden dataset is the business definitions AND the answer key in one file, so a glob for a
    sibling's returns another tenant's questions together with the SQL that answers them. The
    authoring skill is exactly where that temptation lands, so it carries the rule rather than only
    linking to it."""
    assert "shared/golden-dataset-shape.md" in SKILL
    assert "never read another profile" in SKILL


def test_the_skill_never_offers_to_generate_sql_for_an_imported_question():
    """The prohibition this skill exists around. An imported question has no verified answer, and
    filling one in would put a statement nobody ran into the thing every future verdict is measured
    against — a fabrication that, unlike a gap, does not report itself."""
    assert "never generates SQL for an imported question" in SKILL
    assert "fabricate ground truth" in SKILL
    # …and again as a hard rule, because a reason stated once in prose is a reason skimmed past.
    assert "Never generate SQL for an imported question" in SKILL


def test_the_routing_triggers_are_the_skills_own():
    """The table header says the triggers come from `when_to_use`, so a phrase in the row that the
    frontmatter does not carry routes nothing — it reads as a trigger and is not one."""
    row = next(
        line for line in CONVENTIONS.splitlines() if line.startswith("| agami-save-golden |")
    )
    quoted = re.findall(r'"([^"]+)"', row)

    assert quoted, "the agami-save-golden row no longer quotes any trigger phrase"
    for phrase in quoted:
        assert phrase in FRONTMATTER


def test_plan_mode_check_documents_this_skill():
    """The shared doc is the single source of the detection + ask logic, and its per-skill section
    is what a reader consults to see what a given skill does when the user stays in plan mode. A
    skill absent from it has no documented stay-in-plan-mode behavior at all."""
    assert "### `agami-save-golden`" in PLAN_MODE
    assert "I can't save a golden item in plan mode" in PLAN_MODE


def test_file_layout_no_longer_says_no_skill_writes_one():
    """The layout doc said no skill writes a golden dataset, which this slice makes false.

    Asserted here rather than left to `test_file_layout_documents_the_golden_dataset_path`, which
    only checks for the substring "golden_datasets" and stays green while the sentence beside it
    tells a reader the opposite of the truth."""
    assert "no skill writes one today" not in FILE_LAYOUT
    assert "/agami-save-golden" in FILE_LAYOUT
    assert "golden_datasets" in FILE_LAYOUT


def test_the_author_script_registers_no_mcp_tool():
    """REQ-006. The answer key is never reachable from the MCP surface.

    A hosted server's tool surface is the one place a remote caller can reach, and a tool that
    authored golden items would let one write the thing its own model is scored against. Authoring
    is a local, human-confirmed flow behind a skill; asserting the script's name appears nowhere in
    `tools.py` is what keeps a future registration from being a quiet one."""
    tools = (REPO_ROOT / "packages" / "agami-core" / "src" / "tools.py").read_text(
        encoding="utf-8"
    )
    assert "golden_author" not in tools
