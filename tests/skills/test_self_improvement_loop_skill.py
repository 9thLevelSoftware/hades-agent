"""
Smoke tests for the self-improvement-loop optional skill.

The loop itself needs a gateway, a board, and a paid LLM, so CI verifies the
static contract instead:
  - SKILL.md frontmatter conforms to the validator's hardline format
  - every reference file linked from SKILL.md ships with the skill
  - every kanban_* tool the skill instructs the agent to call actually
    exists in the kanban toolset (guards against tool-name drift)
  - the load-bearing conventions ([SILENT] sentinel, idempotency keys,
    explicit board=) are present in the text
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from toolsets import TOOLSETS

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "autonomous-ai-agents"
    / "self-improvement-loop"
)

SKILL_FILES = [
    "SKILL.md",
    "references/setup.md",
    "references/task-filing.md",
]


@pytest.fixture(scope="module")
def skill_md() -> str:
    return (SKILL_DIR / "SKILL.md").read_text()


@pytest.fixture(scope="module")
def frontmatter(skill_md) -> dict:
    assert skill_md.startswith("---"), "frontmatter must start at byte 0"
    m = re.search(r"^---\n(.*?)\n---\n", skill_md, re.DOTALL)
    assert m, "SKILL.md missing closed YAML frontmatter"
    return yaml.safe_load(m.group(1))


@pytest.mark.parametrize("rel", SKILL_FILES)
def test_shipped_files_present(rel: str) -> None:
    assert (SKILL_DIR / rel).is_file(), f"missing skill file: {rel}"


def test_name_matches_dir(frontmatter) -> None:
    assert frontmatter["name"] == "self-improvement-loop"


def test_description_hardline(frontmatter) -> None:
    # AGENTS.md "Skill authoring standards (HARDLINE)" rule 1:
    # ≤ 60 characters, one sentence, ends with a period.
    desc = frontmatter["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars (hardline ≤60): {desc!r}"
    assert desc.endswith("."), f"description must end with a period: {desc!r}"
    assert desc.count(".") == 1, f"description must be one sentence: {desc!r}"


def test_license_and_platforms(frontmatter) -> None:
    assert frontmatter["license"] == "MIT"
    assert set(frontmatter["platforms"]) == {"linux", "macos", "windows"}


def test_body_nonempty_and_bounded(skill_md) -> None:
    body = skill_md.split("---", 2)[2]
    assert body.strip(), "SKILL.md body is empty"
    assert len(skill_md) <= 100_000


def test_referenced_files_exist(skill_md) -> None:
    linked = set(re.findall(r"\]\((references/[^)#]+)", skill_md))
    assert linked, "SKILL.md should link its reference files"
    for rel in sorted(linked):
        assert (SKILL_DIR / rel).is_file(), f"SKILL.md links missing file: {rel}"


def test_kanban_tool_names_exist(skill_md) -> None:
    real_tools = set(TOOLSETS["kanban"]["tools"])
    for rel in SKILL_FILES:
        text = (SKILL_DIR / rel).read_text()
        mentioned = set(re.findall(r"\bkanban_[a-z_]+\b", text))
        unknown = mentioned - real_tools
        assert not unknown, f"{rel} references nonexistent kanban tools: {sorted(unknown)}"


def test_load_bearing_conventions_present(skill_md) -> None:
    setup = (SKILL_DIR / "references/setup.md").read_text()
    filing = (SKILL_DIR / "references/task-filing.md").read_text()
    # Cron silence contract — the loop must be able to have quiet passes.
    assert "[SILENT]" in skill_md and "[SILENT]" in setup
    # Re-filing across passes must dedup.
    assert "idempotency_key" in skill_md and "idempotency_key" in filing
    # Automation must not depend on the user-mutable current-board pointer.
    assert 'board="self-improvement"' in filing
    # The human gate convention workers must follow.
    assert "review-required" in filing
