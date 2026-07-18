"""Tests for tools/skill_usage.py — sidecar telemetry + provenance filtering."""

import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest


def _bump_view_many(hermes_home: str, skill_name: str, iterations: int) -> None:
    os.environ["HERMES_HOME"] = hermes_home
    from tools.skill_usage import bump_view

    for _ in range(iterations):
        bump_view(skill_name)


def _bump_outcome_many(hermes_home: str, skill_name: str, iterations: int) -> None:
    os.environ["HERMES_HOME"] = hermes_home
    from tools.skill_usage import bump_outcome

    for _ in range(iterations):
        bump_outcome(skill_name, "verified", 0.001)


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean skills/ dir for each test.

    Pins ``curator.prune_builtins`` OFF so the bundled/hub-protection tests in
    this module exercise the off-path semantics regardless of the shipped
    default. Tests that want built-ins to be curation-eligible flip it back on
    explicitly via ``monkeypatch.setattr(mod, "_prune_builtins_enabled", ...)``.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Force skill_usage module to re-resolve paths per test
    import importlib
    import tools.skill_usage as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_prune_builtins_enabled", lambda: False)
    return home


def _write_skill(skills_dir: Path, name: str, category: str = ""):
    """Create a minimal SKILL.md with a name: frontmatter field."""
    if category:
        d = skills_dir / category / name
    else:
        d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill
---

# body
""",
        encoding="utf-8",
    )
    return d


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_empty_usage_returns_empty_dict(skills_home):
    from tools.skill_usage import load_usage
    assert load_usage() == {}


def test_save_and_load_roundtrip(skills_home):
    from tools.skill_usage import load_usage, save_usage
    data = {"skill-a": {"use_count": 3, "state": "active"}}
    save_usage(data)
    loaded = load_usage()
    assert loaded["skill-a"]["use_count"] == 3
    assert loaded["skill-a"]["state"] == "active"


def test_save_is_atomic_no_partial_tmp_files(skills_home):
    from tools.skill_usage import save_usage, _usage_file
    save_usage({"x": {"use_count": 1}})
    skills_dir = _usage_file().parent
    # No leftover tempfile
    for p in skills_dir.iterdir():
        assert not p.name.startswith(".usage_"), f"leftover tmp: {p.name}"


def test_get_record_missing_returns_empty_record(skills_home):
    from tools.skill_usage import get_record
    rec = get_record("nonexistent")
    assert rec["use_count"] == 0
    assert rec["view_count"] == 0
    assert rec["state"] == "active"
    assert rec["pinned"] is False
    assert rec["archived_at"] is None


def test_get_record_backfills_missing_keys(skills_home):
    from tools.skill_usage import get_record, save_usage
    save_usage({"legacy": {"use_count": 5}})  # old-format record
    rec = get_record("legacy")
    assert rec["use_count"] == 5
    assert "view_count" in rec  # backfilled
    assert "state" in rec


def test_load_usage_handles_corrupt_file(skills_home):
    from tools.skill_usage import load_usage, _usage_file
    _usage_file().write_text("{ not json }", encoding="utf-8")
    assert load_usage() == {}


# ---------------------------------------------------------------------------
# Counter bumps
# ---------------------------------------------------------------------------

def test_bump_view_increments_and_timestamps(skills_home):
    from tools.skill_usage import bump_view, get_record
    bump_view("my-skill")
    bump_view("my-skill")
    rec = get_record("my-skill")
    assert rec["view_count"] == 2
    assert rec["last_viewed_at"] is not None


def test_bump_use_increments_and_timestamps(skills_home):
    from tools.skill_usage import bump_use, get_record
    bump_use("my-skill")
    rec = get_record("my-skill")
    assert rec["use_count"] == 1
    assert rec["last_used_at"] is not None


def test_bump_patch_increments_and_timestamps(skills_home):
    from tools.skill_usage import bump_patch, get_record
    bump_patch("my-skill")
    rec = get_record("my-skill")
    assert rec["patch_count"] == 1
    assert rec["last_patched_at"] is not None


def test_bump_on_empty_name_is_noop(skills_home):
    from tools.skill_usage import bump_view, load_usage
    bump_view("")
    assert load_usage() == {}


def test_bumps_do_not_corrupt_other_skills(skills_home):
    from tools.skill_usage import bump_view, bump_use, get_record
    bump_view("skill-a")
    bump_use("skill-b")
    bump_view("skill-a")
    assert get_record("skill-a")["view_count"] == 2
    assert get_record("skill-a")["use_count"] == 0
    assert get_record("skill-b")["use_count"] == 1


def test_concurrent_bump_view_preserves_all_updates(skills_home):
    from tools.skill_usage import get_record

    process_count = 6
    iterations = 25
    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(
            target=_bump_view_many,
            args=(str(skills_home), "shared-skill", iterations),
        )
        for _ in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    for process in processes:
        assert process.exitcode == 0
    assert get_record("shared-skill")["view_count"] == process_count * iterations


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def test_set_state_active(skills_home):
    from tools.skill_usage import set_state, get_record, STATE_ACTIVE
    set_state("x", STATE_ACTIVE)
    assert get_record("x")["state"] == "active"


def test_set_state_archived_records_timestamp(skills_home):
    from tools.skill_usage import set_state, get_record, STATE_ARCHIVED
    set_state("x", STATE_ARCHIVED)
    rec = get_record("x")
    assert rec["state"] == "archived"
    assert rec["archived_at"] is not None


def test_set_state_invalid_is_noop(skills_home):
    from tools.skill_usage import set_state, get_record
    set_state("x", "bogus")
    # No record created for invalid state
    rec = get_record("x")
    assert rec["state"] == "active"  # default


def test_restoring_from_archive_clears_timestamp(skills_home):
    from tools.skill_usage import set_state, get_record, STATE_ARCHIVED, STATE_ACTIVE
    set_state("x", STATE_ARCHIVED)
    assert get_record("x")["archived_at"] is not None
    set_state("x", STATE_ACTIVE)
    assert get_record("x")["archived_at"] is None


def test_set_pinned(skills_home):
    from tools.skill_usage import set_pinned, get_record
    set_pinned("x", True)
    assert get_record("x")["pinned"] is True
    set_pinned("x", False)
    assert get_record("x")["pinned"] is False


def test_forget_removes_record(skills_home):
    from tools.skill_usage import bump_view, forget, load_usage
    bump_view("x")
    assert "x" in load_usage()
    forget("x")
    assert "x" not in load_usage()


# ---------------------------------------------------------------------------
# Provenance filter — the load-bearing safety check
# ---------------------------------------------------------------------------

def test_agent_created_excludes_bundled(skills_home):
    from tools.skill_usage import list_agent_created_skill_names, mark_agent_created
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled-skill", category="github")
    _write_skill(skills_dir, "my-skill")
    mark_agent_created("my-skill")
    # Seed a bundled manifest marking bundled-skill as upstream
    (skills_dir / ".bundled_manifest").write_text(
        "bundled-skill:abc123\n", encoding="utf-8",
    )
    names = list_agent_created_skill_names()
    assert "my-skill" in names
    assert "bundled-skill" not in names


def test_agent_created_excludes_hub_installed(skills_home):
    from tools.skill_usage import list_agent_created_skill_names, mark_agent_created
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "hub-skill")
    _write_skill(skills_dir, "my-skill")
    mark_agent_created("my-skill")
    hub_dir = skills_dir / ".hub"
    hub_dir.mkdir()
    (hub_dir / "lock.json").write_text(
        json.dumps({"version": 1, "installed": {"hub-skill": {"source": "taps/main"}}}),
        encoding="utf-8",
    )
    names = list_agent_created_skill_names()
    assert "my-skill" in names
    assert "hub-skill" not in names


def test_agent_created_excludes_hub_installed_frontmatter_name(skills_home):
    from tools.skill_usage import (
        is_agent_created,
        list_agent_created_skill_names,
        mark_agent_created,
    )

    skills_dir = skills_home / "skills"
    hub_skill = skills_dir / "productivity" / "getnote"
    hub_skill.mkdir(parents=True)
    (hub_skill / "SKILL.md").write_text(
        """---
name: Get笔记
description: test skill
---

# body
""",
        encoding="utf-8",
    )
    _write_skill(skills_dir, "my-skill")
    mark_agent_created("my-skill")
    hub_dir = skills_dir / ".hub"
    hub_dir.mkdir()
    (hub_dir / "lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "installed": {
                    "getnote": {
                        "source": "taps/main",
                        "install_path": "productivity/getnote",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    names = list_agent_created_skill_names()
    assert "my-skill" in names
    assert "Get笔记" not in names
    assert is_agent_created("Get笔记") is False
    assert is_agent_created("getnote") is False


def test_is_agent_created(skills_home):
    from tools.skill_usage import is_agent_created
    skills_dir = skills_home / "skills"
    (skills_dir / ".bundled_manifest").write_text("bundled:abc\n", encoding="utf-8")
    hub_dir = skills_dir / ".hub"
    hub_dir.mkdir()
    (hub_dir / "lock.json").write_text(
        json.dumps({"installed": {"hubbed": {}}}), encoding="utf-8",
    )
    assert is_agent_created("my-skill") is True
    assert is_agent_created("bundled") is False
    assert is_agent_created("hubbed") is False


def test_agent_created_skips_archive_and_hub_dirs(skills_home):
    from tools.skill_usage import list_agent_created_skill_names, mark_agent_created
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "real-skill")
    mark_agent_created("real-skill")
    # Dot-prefixed dirs must be ignored even if they contain SKILL.md
    archive = skills_dir / ".archive" / "old-skill"
    archive.mkdir(parents=True)
    (archive / "SKILL.md").write_text(
        "---\nname: old-skill\n---\n", encoding="utf-8",
    )
    names = list_agent_created_skill_names()
    assert "real-skill" in names
    assert "old-skill" not in names


def test_agent_created_excludes_external_dir_even_with_stale_agent_record(skills_home, monkeypatch):
    from tools.skill_usage import (
        agent_created_report,
        is_agent_created,
        list_agent_created_skill_names,
        save_usage,
    )

    skills_dir = skills_home / "skills"
    external = skills_dir / "shared-vault"
    _write_skill(external, "external-skill")
    save_usage({"external-skill": {"created_by": "agent"}})

    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [external.resolve()],
    )

    assert "external-skill" not in list_agent_created_skill_names()
    assert "external-skill" not in {r["name"] for r in agent_created_report()}
    assert is_agent_created("external-skill") is False


# ---------------------------------------------------------------------------
# Archive / restore
# ---------------------------------------------------------------------------

def test_archive_skill_moves_directory(skills_home):
    from tools.skill_usage import archive_skill, get_record
    skills_dir = skills_home / "skills"
    skill_dir = _write_skill(skills_dir, "old-skill")
    assert skill_dir.exists()

    ok, msg = archive_skill("old-skill")
    assert ok, msg
    assert not skill_dir.exists()
    assert (skills_dir / ".archive" / "old-skill" / "SKILL.md").exists()
    assert get_record("old-skill")["state"] == "archived"
    assert get_record("old-skill")["archived_at"] is not None


def test_archive_refuses_bundled_skill(skills_home):
    from tools.skill_usage import archive_skill
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled")
    (skills_dir / ".bundled_manifest").write_text("bundled:abc\n", encoding="utf-8")

    ok, msg = archive_skill("bundled")
    assert not ok
    assert "bundled" in msg.lower() or "hub" in msg.lower()


def test_archive_refuses_hub_skill(skills_home):
    from tools.skill_usage import archive_skill
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "hub-skill")
    hub_dir = skills_dir / ".hub"
    hub_dir.mkdir()
    (hub_dir / "lock.json").write_text(
        json.dumps({"installed": {"hub-skill": {}}}), encoding="utf-8",
    )

    ok, msg = archive_skill("hub-skill")
    assert not ok


def test_archive_refuses_external_skill(skills_home, monkeypatch):
    from tools.skill_usage import archive_skill

    skills_dir = skills_home / "skills"
    external = skills_dir / "shared-vault"
    skill_dir = _write_skill(external, "external-skill")
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [external.resolve()],
    )

    ok, msg = archive_skill("external-skill")
    assert not ok
    assert "external" in msg.lower()
    assert skill_dir.exists()


def test_archive_missing_skill_returns_error(skills_home):
    from tools.skill_usage import archive_skill
    ok, msg = archive_skill("nonexistent")
    assert not ok
    assert "not found" in msg.lower()


def test_restore_skill_moves_back(skills_home):
    from tools.skill_usage import archive_skill, restore_skill, get_record
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "temp-skill")
    archive_skill("temp-skill")
    assert not (skills_dir / "temp-skill").exists()

    ok, msg = restore_skill("temp-skill")
    assert ok, msg
    assert (skills_dir / "temp-skill" / "SKILL.md").exists()
    assert get_record("temp-skill")["state"] == "active"


def test_restore_skill_finds_nested_archive_subdir(skills_home):
    """Skills archived under nested category subdirs (e.g.
    .archive/<category>/<skill>/) — left behind by older archive layouts or
    external imports — must still be restorable by name."""
    from tools.skill_usage import restore_skill, get_record
    skills_dir = skills_home / "skills"
    nested = skills_dir / ".archive" / "openclaw-imports" / "nested-skill"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: nested-skill\ndescription: x\n---\n", encoding="utf-8",
    )

    ok, msg = restore_skill("nested-skill")
    assert ok, msg
    assert (skills_dir / "nested-skill" / "SKILL.md").exists()
    assert not nested.exists()
    assert get_record("nested-skill")["state"] == "active"


def test_restore_skill_finds_nested_timestamped_prefix(skills_home):
    """Prefix-match path (timestamped dupes) must also descend into nested
    archive subdirs, not just .archive/ top-level."""
    from tools.skill_usage import restore_skill
    skills_dir = skills_home / "skills"
    nested = skills_dir / ".archive" / "imports" / "dup-skill-20260101000000"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: dup-skill\ndescription: x\n---\n", encoding="utf-8",
    )

    ok, msg = restore_skill("dup-skill")
    assert ok, msg
    assert (skills_dir / "dup-skill" / "SKILL.md").exists()


def test_archive_collision_gets_suffix(skills_home):
    from tools.skill_usage import archive_skill
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "dup")
    archive_skill("dup")
    _write_skill(skills_dir, "dup")  # recreate
    ok, msg = archive_skill("dup")
    assert ok
    # Two entries under .archive/ — second should have a timestamp suffix
    archived = sorted(p.name for p in (skills_dir / ".archive").iterdir() if p.is_dir())
    assert "dup" in archived
    assert any(n.startswith("dup-") and n != "dup" for n in archived)


def test_restore_does_not_pull_unrelated_sibling_out_of_archive(skills_home):
    """Restoring a name with no exact archive entry must NOT grab a different
    archived skill that merely shares a ``<name>-`` prefix.

    The timestamped-duplicate fallback recognises only the suffix
    ``archive_skill`` writes on a collision (``-YYYYMMDDHHMMSS``). A bare
    ``startswith(f"{name}-")`` also matches sibling skills, so restoring
    ``git`` would rip an archived ``git-helpers`` out of the archive, rename
    it to ``git``, and report success — destroying the sibling's only copy."""
    from tools.skill_usage import (
        archive_skill, restore_skill, list_archived_skill_names, mark_agent_created,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "git-helpers")
    mark_agent_created("git-helpers")
    ok, msg = archive_skill("git-helpers")
    assert ok, msg

    # "git" was never archived; only its prefix-sharing sibling was.
    ok, msg = restore_skill("git")
    assert not ok, f"restore('git') should not match 'git-helpers': {msg}"
    assert "not found" in msg.lower()

    # The sibling must be untouched: still in the archive, never moved to skills/git.
    assert (skills_dir / ".archive" / "git-helpers" / "SKILL.md").exists()
    assert "git-helpers" in list_archived_skill_names()
    assert not (skills_dir / "git").exists()


def test_restore_still_matches_timestamped_duplicate(skills_home):
    """The fix must not over-narrow: a real collision dupe written by
    ``archive_skill`` (``<name>-YYYYMMDDHHMMSS``) is still restorable by name
    when no bare ``<name>`` entry exists."""
    from tools.skill_usage import restore_skill
    skills_dir = skills_home / "skills"
    dupe = skills_dir / ".archive" / "report-tool-20260101000000"
    dupe.mkdir(parents=True)
    (dupe / "SKILL.md").write_text(
        "---\nname: report-tool\ndescription: x\n---\n", encoding="utf-8",
    )

    ok, msg = restore_skill("report-tool")
    assert ok, msg
    assert (skills_dir / "report-tool" / "SKILL.md").exists()


def test_restore_prefers_timestamped_dupe_over_unrelated_sibling(skills_home):
    """With both a real timestamped duplicate and an unrelated sibling present,
    restoring the bare name picks the duplicate and leaves the sibling alone."""
    from tools.skill_usage import restore_skill
    archive = skills_home / "skills" / ".archive"

    dupe = archive / "report-20260101000000"          # real collision dupe of "report"
    sibling = archive / "report-card"                  # unrelated sibling skill
    for d, frontname in ((dupe, "report"), (sibling, "report-card")):
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {frontname}\ndescription: x\n---\n", encoding="utf-8",
        )

    ok, msg = restore_skill("report")
    assert ok, msg
    # The duplicate (name: report) was restored, not the sibling (name: report-card).
    restored = (skills_home / "skills" / "report" / "SKILL.md").read_text()
    assert "name: report\n" in restored
    assert "name: report-card" not in restored
    assert not dupe.exists()       # the dupe moved out of the archive
    assert sibling.exists()        # the unrelated sibling stayed put


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_agent_created_report_includes_marked_skills_with_defaults(skills_home):
    from tools.skill_usage import agent_created_report, bump_view, mark_agent_created
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "a")
    _write_skill(skills_dir, "b")
    mark_agent_created("a")
    mark_agent_created("b")
    bump_view("a")
    rows = agent_created_report()
    by_name = {r["name"]: r for r in rows}
    assert "a" in by_name and "b" in by_name
    assert by_name["a"]["view_count"] == 1
    # b has only the provenance marker — activity fields still default.
    assert by_name["b"]["view_count"] == 0
    assert by_name["b"]["state"] == "active"


def test_manual_skill_with_usage_is_not_curator_managed(skills_home):
    from tools.skill_usage import agent_created_report, bump_view, list_agent_created_skill_names
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "manual-skill")

    bump_view("manual-skill")

    assert "manual-skill" not in list_agent_created_skill_names()
    assert "manual-skill" not in {r["name"] for r in agent_created_report()}


def test_agent_created_report_excludes_bundled_and_hub(skills_home):
    from tools.skill_usage import agent_created_report, mark_agent_created
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "mine")
    _write_skill(skills_dir, "bundled")
    _write_skill(skills_dir, "hubbed")
    mark_agent_created("mine")
    (skills_dir / ".bundled_manifest").write_text("bundled:abc\n", encoding="utf-8")
    hub = skills_dir / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text(
        json.dumps({"installed": {"hubbed": {}}}), encoding="utf-8",
    )
    names = {r["name"] for r in agent_created_report()}
    assert "mine" in names
    assert "bundled" not in names
    assert "hubbed" not in names


def test_agent_created_report_derives_activity_from_view_and_patch(skills_home, monkeypatch):
    import tools.skill_usage as skill_usage

    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "mine")
    timestamps = iter([
        "2026-04-30T10:00:00+00:00",
        "2026-04-30T11:00:00+00:00",
        "2026-04-30T12:00:00+00:00",
        "2026-04-30T13:00:00+00:00",
    ])
    monkeypatch.setattr(skill_usage, "_now_iso", lambda: next(timestamps))

    skill_usage.mark_agent_created("mine")
    skill_usage.bump_view("mine")
    skill_usage.bump_patch("mine")

    row = next(r for r in skill_usage.agent_created_report() if r["name"] == "mine")
    assert row["activity_count"] == 2
    assert row["last_activity_at"] == "2026-04-30T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Telemetry vs curation — usage is tracked for ALL skills; curation is not
# ---------------------------------------------------------------------------

def test_bump_view_tracks_bundled_skill(skills_home):
    """Telemetry IS recorded for bundled skills (observability), but the record
    must NOT make the skill a curation candidate by itself."""
    from tools.skill_usage import (
        bump_view, load_usage, list_agent_created_skill_names,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "ship-bundled")
    (skills_dir / ".bundled_manifest").write_text(
        "ship-bundled:abc\n", encoding="utf-8",
    )

    bump_view("ship-bundled")
    rec = load_usage().get("ship-bundled")
    assert isinstance(rec, dict), "bundled skill telemetry should be recorded"
    assert rec["view_count"] == 1
    # Pruning is off by default in this fixture → not a curation candidate.
    assert "ship-bundled" not in list_agent_created_skill_names()


def test_bump_patch_tracks_hub_skill(skills_home):
    from tools.skill_usage import (
        bump_patch, load_usage, list_agent_created_skill_names,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "from-hub")
    hub = skills_dir / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text(
        json.dumps({"installed": {"from-hub": {}}}), encoding="utf-8",
    )

    bump_patch("from-hub")
    rec = load_usage().get("from-hub")
    assert isinstance(rec, dict), "hub skill telemetry should be recorded"
    assert rec["patch_count"] == 1
    # Hub skills are NEVER curation candidates regardless of any flag.
    assert "from-hub" not in list_agent_created_skill_names()


def test_bump_use_tracks_hub_skill(skills_home):
    from tools.skill_usage import bump_use, load_usage
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "from-hub")
    hub = skills_dir / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text(
        json.dumps({"installed": {"from-hub": {}}}), encoding="utf-8",
    )

    bump_use("from-hub")
    rec = load_usage().get("from-hub")
    assert isinstance(rec, dict)
    assert rec["use_count"] == 1


def test_set_state_no_op_for_bundled_skill(skills_home):
    """State transitions on bundled skills must not land in the sidecar."""
    from tools.skill_usage import set_state, load_usage, STATE_ARCHIVED
    skills_dir = skills_home / "skills"
    (skills_dir / ".bundled_manifest").write_text(
        "locked:abc\n", encoding="utf-8",
    )
    set_state("locked", STATE_ARCHIVED)
    assert "locked" not in load_usage()


def test_restore_refuses_to_shadow_bundled_skill(skills_home):
    """If a bundled skill now occupies the name, refuse to restore."""
    from tools.skill_usage import archive_skill, restore_skill
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "shared-name")
    archive_skill("shared-name")

    # Now a bundled skill appears with the same name
    (skills_dir / ".bundled_manifest").write_text(
        "shared-name:abc\n", encoding="utf-8",
    )
    _write_skill(skills_dir, "shared-name")  # bundled install landed

    ok, msg = restore_skill("shared-name")
    assert not ok
    assert "bundled" in msg.lower() or "shadow" in msg.lower()


def test_end_to_end_telemetry_tracked_but_lifecycle_refused(skills_home):
    """The combined guarantee under decoupled telemetry/curation:

    - Usage telemetry (view/use/patch) IS recorded for bundled & hub skills.
    - Lifecycle mutations (set_state, set_pinned, archive) are REFUSED for them
      (with pruning off, the fixture default), so no state/pinned/archived flag
      lands and the directories stay on disk.
    """
    from tools.skill_usage import (
        bump_view, bump_use, bump_patch, set_state, set_pinned,
        archive_skill, load_usage, STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled-one")
    _write_skill(skills_dir, "hub-one")
    _write_skill(skills_dir, "mine")

    (skills_dir / ".bundled_manifest").write_text(
        "bundled-one:abc\n", encoding="utf-8",
    )
    hub = skills_dir / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text(
        json.dumps({"installed": {"hub-one": {}}}), encoding="utf-8",
    )

    for name in ("bundled-one", "hub-one"):
        bump_view(name)
        bump_use(name)
        bump_patch(name)
        set_state(name, STATE_STALE)
        set_state(name, STATE_ARCHIVED)
        set_pinned(name, True)
        ok, _msg = archive_skill(name)
        assert not ok, f"archive_skill(\"{name}\") should refuse"

    data = load_usage()
    # Telemetry landed for both.
    for name in ("bundled-one", "hub-one"):
        assert name in data, f"{name} telemetry should be recorded"
        assert data[name]["view_count"] == 1
        assert data[name]["use_count"] == 1
        assert data[name]["patch_count"] == 1
        # But lifecycle mutators were refused — state stays the default, never
        # archived/stale/pinned, and created_by is never agent.
        assert data[name]["state"] == STATE_ACTIVE
        assert data[name]["archived_at"] is None
        assert data[name]["pinned"] is False
        assert data[name].get("created_by") != "agent"

    # Directories must still be in place on disk.
    assert (skills_dir / "bundled-one" / "SKILL.md").exists()
    assert (skills_dir / "hub-one" / "SKILL.md").exists()

    # The agent-created skill can still be mutated normally.
    bump_view("mine")
    assert load_usage()["mine"]["view_count"] == 1


def test_usage_report_covers_all_provenance(skills_home):
    """usage_report() surfaces every skill with provenance, unlike the
    curator-scoped agent_created_report()."""
    from tools.skill_usage import (
        bump_use, usage_report, mark_agent_created,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled-one")
    _write_skill(skills_dir, "hub-one")
    _write_skill(skills_dir, "mine")
    (skills_dir / ".bundled_manifest").write_text("bundled-one:abc\n", encoding="utf-8")
    hub = skills_dir / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text(
        json.dumps({"installed": {"hub-one": {}}}), encoding="utf-8",
    )
    mark_agent_created("mine")
    for n in ("bundled-one", "hub-one", "mine"):
        bump_use(n)

    rows = {r["name"]: r for r in usage_report()}
    assert set(rows) == {"bundled-one", "hub-one", "mine"}
    assert rows["bundled-one"]["provenance"] == "bundled"
    assert rows["hub-one"]["provenance"] == "hub"
    assert rows["mine"]["provenance"] == "agent"
    # All carry real usage now.
    for n in rows:
        assert rows[n]["use_count"] == 1
        assert rows[n]["_persisted"] is True


# ---------------------------------------------------------------------------
# Task 2 — outcome attribution sidecar + smoothed utility evidence.
#
# ``bump_outcome`` lives in tools.skill_usage.py and extends the sidecar with
# per-skill outcome counters. ``get_skill_utility`` reads them back and
# returns a shape the Insights + Curator layers can render. Both are
# best-effort like the existing bumps.
# ---------------------------------------------------------------------------

_HELPED_OUTCOMES = {"verified", "completed_unverified"}
_HURT_OUTCOMES = {"failed", "blocked", "unresolved"}


def test_bump_outcome_helped_increments_helped_and_counts(skills_home):
    from tools.skill_usage import bump_outcome, get_record

    bump_outcome("plan", "verified", 0.01)
    bump_outcome("plan", "verified", 0.02)
    bump_outcome("plan", "completed_unverified", 0.005)

    rec = get_record("plan")
    assert rec["outcome_counts"]["verified"] == 2
    assert rec["outcome_counts"]["completed_unverified"] == 1
    assert rec["helped"] == 3
    assert rec["hurt"] == 0
    assert rec["neutral"] == 0
    assert rec["outcome_cost_usd"] == pytest.approx(0.035)
    assert rec["last_outcome_at"] is not None


def test_bump_outcome_hurt_increments_hurt(skills_home):
    from tools.skill_usage import bump_outcome, get_record

    bump_outcome("plan", "failed", 0.10)
    bump_outcome("plan", "blocked", 0.20)
    bump_outcome("plan", "unresolved", 0.05)

    rec = get_record("plan")
    assert rec["outcome_counts"]["failed"] == 1
    assert rec["outcome_counts"]["blocked"] == 1
    assert rec["outcome_counts"]["unresolved"] == 1
    assert rec["helped"] == 0
    assert rec["hurt"] == 3
    assert rec["outcome_cost_usd"] == pytest.approx(0.35)


def test_bump_outcome_neutral_increments_neutral_not_helped_or_hurt(skills_home):
    from tools.skill_usage import bump_outcome, get_record

    bump_outcome("plan", "partial", 0.01)
    bump_outcome("plan", "interrupted", 0.02)
    bump_outcome("plan", "cancelled", 0.0)

    rec = get_record("plan")
    assert rec["neutral"] == 3
    assert rec["helped"] == 0
    assert rec["hurt"] == 0
    assert rec["outcome_cost_usd"] == pytest.approx(0.03)
    # Neutral outcomes still record specific counts under outcome_counts.
    assert rec["outcome_counts"]["partial"] == 1


def test_bump_outcome_mixed_keeps_helped_hurt_neutral_independent(skills_home):
    from tools.skill_usage import bump_outcome, get_record

    bump_outcome("plan", "verified", 0.10)
    bump_outcome("plan", "failed", 0.10)
    bump_outcome("plan", "verified", 0.10)
    bump_outcome("plan", "partial", 0.10)

    rec = get_record("plan")
    assert rec["helped"] == 2
    assert rec["hurt"] == 1
    assert rec["neutral"] == 1
    assert rec["outcome_cost_usd"] == pytest.approx(0.40)


def test_bump_outcome_unknown_outcome_is_treated_as_neutral(skills_home):
    """Outcomes not in either list count as neutral — record but don't bias utility."""
    from tools.skill_usage import bump_outcome, get_record

    bump_outcome("plan", "weird_outcome", 0.05)
    rec = get_record("plan")
    assert rec["outcome_counts"]["weird_outcome"] == 1
    assert rec["neutral"] == 1
    assert rec["helped"] == 0
    assert rec["hurt"] == 0
    assert rec["outcome_cost_usd"] == pytest.approx(0.05)


def test_bump_outcome_lazy_seeds_old_records(skills_home):
    """A sidecar record that pre-dates Task 2 (only legacy keys) gets the new
    outcome keys backfilled on first bump, without dropping legacy fields."""
    from tools.skill_usage import bump_outcome, get_record, save_usage

    save_usage({"legacy": {"use_count": 5, "view_count": 2, "state": "active"}})
    bump_outcome("legacy", "verified", 0.01)

    rec = get_record("legacy")
    # Legacy keys survive.
    assert rec["use_count"] == 5
    assert rec["view_count"] == 2
    assert rec["state"] == "active"
    # New fields seeded.
    assert rec["helped"] == 1
    assert rec["outcome_counts"]["verified"] == 1
    assert rec["outcome_cost_usd"] == pytest.approx(0.01)
    assert rec["last_outcome_at"] is not None


def test_bump_outcome_preserves_unknown_fields(skills_home):
    """Bumping must NOT clobber keys we did not write (e.g. future schema)."""
    from tools.skill_usage import bump_outcome, save_usage, load_usage

    save_usage({"plan": {"use_count": 3, "future_field": "kept"}})
    bump_outcome("plan", "verified", 0.01)

    raw = load_usage()["plan"]
    assert raw["future_field"] == "kept"
    assert raw["use_count"] == 3
    assert raw["outcome_counts"]["verified"] == 1


def test_bump_outcome_is_best_effort_and_does_not_raise(skills_home, monkeypatch):
    """If the sidecar write fails (e.g. disk full), the call returns None silently."""
    from tools import skill_usage as mod

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "save_usage", _boom)
    # Must not raise.
    mod.bump_outcome("plan", "verified", 0.01)


def test_bump_outcome_empty_name_is_noop(skills_home):
    from tools.skill_usage import bump_outcome, load_usage

    bump_outcome("", "verified", 0.0)
    assert load_usage() == {}


def test_bump_outcome_holds_lock_for_concurrent_processes(skills_home):
    """Concurrent processes bumping the same skill must not lose increments.

    Ponytail: global lock is fine here — outcome attribution is single-digit
    per-second, not a throughput-bottleneck path.
    """
    process_count = 4
    iterations = 50
    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(
            target=_bump_outcome_many,
            args=(str(skills_home), "shared", iterations),
        )
        for _ in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    for process in processes:
        assert process.exitcode == 0

    from tools.skill_usage import get_record
    rec = get_record("shared")
    assert rec["outcome_counts"]["verified"] == process_count * iterations
    assert rec["helped"] == process_count * iterations


def test_get_skill_utility_ineligible_below_minimum_sample(skills_home):
    """Below 5 attributed samples, eligible is False and utility is None — no ranking."""
    from tools.skill_usage import bump_outcome, get_skill_utility

    for _ in range(4):
        bump_outcome("plan", "verified", 0.001)
    result = get_skill_utility("plan")
    assert result["count"] == 4
    assert result["helped"] == 4
    assert result["hurt"] == 0
    assert result["eligible"] is False
    assert result["utility"] is None


def test_get_skill_utility_smoothed_laplace_formula(skills_home):
    """utility = (helped + 1) / (helped + hurt + 2) with 5+ samples makes it eligible."""
    from tools.skill_usage import bump_outcome, get_skill_utility

    for _ in range(7):
        bump_outcome("plan", "verified", 0.001)
    for _ in range(3):
        bump_outcome("plan", "failed", 0.001)
    result = get_skill_utility("plan")
    assert result["count"] == 10
    assert result["helped"] == 7
    assert result["hurt"] == 3
    assert result["eligible"] is True
    # (7 + 1) / (7 + 3 + 2) = 8/12 = 2/3
    assert result["utility"] == pytest.approx(8 / 12)


def test_get_skill_utility_zero_neutral_only_outcomes_eligible(skills_home):
    """A skill with 5 helped and 0 hurt has utility = (5+1)/(5+0+2) = 6/7."""
    from tools.skill_usage import bump_outcome, get_skill_utility

    for _ in range(5):
        bump_outcome("plan", "verified", 0.001)
    result = get_skill_utility("plan")
    assert result["eligible"] is True
    assert result["utility"] == pytest.approx(6 / 7)


def test_get_skill_utility_returns_default_when_unseen(skills_home):
    """Never-seen skill: count 0, utility None, eligible False — never errors."""
    from tools.skill_usage import get_skill_utility

    result = get_skill_utility("never-seen")
    assert result["count"] == 0
    assert result["helped"] == 0
    assert result["hurt"] == 0
    assert result["neutral"] == 0
    assert result["cost_usd"] == 0.0
    assert result["eligible"] is False
    assert result["utility"] is None


def test_get_skill_utility_does_not_archive_or_delete(skills_home):
    """Utility must be observability; a low score must never drive archival."""
    from tools import skill_usage as mod
    from tools.skill_usage import bump_outcome, get_skill_utility

    # All failed: lowest possible utility, far past the eligibility threshold.
    for _ in range(10):
        bump_outcome("low-score", "failed", 0.1)
    result = get_skill_utility("low-score")
    assert result["eligible"] is True
    assert result["utility"] == pytest.approx(1 / 12)

    rec = mod.get_record("low-score")
    # No archive state was applied as a side effect of poor utility.
    assert rec["state"] == "active"
    assert rec.get("archived_at") is None


def test_get_skill_utility_uses_preloaded_record_without_disk_read(monkeypatch):
    from tools import skill_usage as mod

    monkeypatch.setattr(
        mod,
        "get_record",
        lambda _name: (_ for _ in ()).throw(AssertionError("disk read")),
    )
    result = mod.get_skill_utility(
        "plan",
        {"helped": 5, "hurt": 1, "neutral": 0, "outcome_cost_usd": 0.25},
    )
    assert result["count"] == 6
    assert result["cost_usd"] == pytest.approx(0.25)
    assert result["eligible"] is True
