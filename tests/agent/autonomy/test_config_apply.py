"""Task 3 saga tests for ``agent.autonomy.config_apply``.

Real-path invariants against a temporary ``HADES_HOME`` (set by the
autouse conftest fixture): exact-hash CAS between preview and apply,
content-free crash-recovery journal, verified backup restore, and
fail-closed ``incomplete_authority_apply`` while a journal is pending.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from agent.autonomy import AutonomyRule, RuleProvenance
from agent.autonomy.compiler import InvalidStableAuthority
from agent.autonomy.config_apply import (
    AuthorityConflict,
    ConfigChange,
    IncompleteAuthorityApply,
    apply_config_change,
    backup_path,
    journal_path,
    lock_path,
    pending_apply,
    preview_config_change,
    recover_config_apply,
)
from hades_constants import get_hades_home
from hades_state import SessionDB


class SimulatedCrash(RuntimeError):
    """Injected mid-saga failure."""


def provenance(**overrides) -> RuleProvenance:
    base = dict(
        actor_kind="user",
        actor_id="user-1",
        source_ref="cli",
        observed_at_ms=100,
        confirmed_at_ms=200,
        confidence_ppm=1_000_000,
    )
    base.update(overrides)
    return RuleProvenance(**base)


def _autonomy_section_hash(config: dict) -> str:
    section = config.get("autonomy") or {}
    return hashlib.sha256(
        json.dumps(section, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def allow_rule(rule_id: str, **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(),
        created_at_ms=100,
    )
    base.update(overrides)
    return AutonomyRule(**base)


class Harness:
    """Drives the preview/apply/recover saga against the test profile home."""

    def __init__(self, db: SessionDB):
        self.db = db
        self.home = get_hades_home()
        self.config_path = self.home / "config.yaml"

    def preview_add(self, rule: AutonomyRule):
        return preview_config_change(ConfigChange(set_rules=(rule,)), db=self.db)

    def preview_remove(self, rule_id: str):
        return preview_config_change(
            ConfigChange(remove_rule_ids=(rule_id,)), db=self.db
        )

    def apply(self, preview, *, expected_contract_hash):
        return apply_config_change(
            preview, expected_contract_hash=expected_contract_hash, db=self.db
        )

    def external_edit(self):
        raw = self.read_config()
        raw["externally_edited"] = True
        self.config_path.write_text(
            yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
        )

    def crash_at(self, point: str, rule_id: str = "crash-rule"):
        preview = self.preview_add(allow_rule(rule_id))

        def hook(reached: str) -> None:
            if reached == point:
                raise SimulatedCrash(point)

        with pytest.raises(SimulatedCrash):
            apply_config_change(
                preview,
                expected_contract_hash=preview.before_contract_hash,
                db=self.db,
                _crash_hook=hook,
            )
        return preview

    def restart_and_recover(self):
        return recover_config_apply(db=self.db)

    def read_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def stable_rule_ids(self) -> list[str]:
        section = self.read_config().get("autonomy") or {}
        return [entry["rule_id"] for entry in section.get("stable_rules") or []]


@pytest.fixture
def harness():
    db = SessionDB(get_hades_home() / "state.db")
    try:
        yield Harness(db)
    finally:
        db.close()


# ── Happy path ──────────────────────────────────────────────────────────────


def test_preview_and_apply_add_rule_end_to_end(harness):
    preview = harness.preview_add(allow_rule("r1"))
    assert preview.added_rule_ids == ("r1",)
    assert preview.removed_rule_ids == ()
    assert preview.before_config_hash != preview.after_config_hash

    applied = harness.apply(
        preview, expected_contract_hash=preview.before_contract_hash
    )
    assert harness.stable_rule_ids() == ["r1"]
    head = harness.db.autonomy.get_head()
    assert head is not None
    assert head.content_hash == applied.contract.content_hash
    assert [r.rule_id for r in head.contract.rules] == ["r1"]
    section_hash = _autonomy_section_hash(harness.read_config())
    assert applied.contract.source_fingerprint == f"config:{section_hash}"
    assert not journal_path().exists()
    assert not backup_path().exists()


def test_apply_then_remove_rule(harness):
    p1 = harness.preview_add(allow_rule("r1"))
    harness.apply(p1, expected_contract_hash=p1.before_contract_hash)
    p2 = harness.preview_remove("r1")
    assert p2.removed_rule_ids == ("r1",)
    applied = harness.apply(p2, expected_contract_hash=p2.before_contract_hash)
    assert harness.stable_rule_ids() == []
    assert applied.contract.contract.rules == ()


def test_remove_unknown_rule_id_is_a_conflict(harness):
    with pytest.raises(AuthorityConflict, match="ghost"):
        harness.preview_remove("ghost")


# ── CAS and staleness ───────────────────────────────────────────────────────


def test_apply_rejects_changed_preview_and_recovers_after_config_replace(harness):
    preview = harness.preview_add(allow_rule("r1"))
    harness.external_edit()
    with pytest.raises(AuthorityConflict, match="config changed since preview"):
        harness.apply(preview, expected_contract_hash=preview.before_contract_hash)
    harness.crash_at("after_config_replace")
    recovered = harness.restart_and_recover()
    assert recovered.action == "completed"
    assert recovered.contract is not None
    section_hash = _autonomy_section_hash(harness.read_config())
    assert recovered.contract.source_fingerprint == f"config:{section_hash}"
    assert not journal_path().exists()
    assert "crash-rule" in harness.stable_rule_ids()


def test_apply_rejects_wrong_expected_contract_hash(harness):
    preview = harness.preview_add(allow_rule("r1"))
    with pytest.raises(AuthorityConflict, match="authority changed since preview"):
        harness.apply(preview, expected_contract_hash="0" * 64)
    # Nothing was written and no journal is pending.
    assert harness.stable_rule_ids() == []
    assert not journal_path().exists()


def test_stale_preview_after_successful_apply_is_rejected(harness):
    p1 = harness.preview_add(allow_rule("r1"))
    harness.apply(p1, expected_contract_hash=p1.before_contract_hash)
    with pytest.raises(AuthorityConflict, match="config changed since preview"):
        harness.apply(p1, expected_contract_hash=p1.before_contract_hash)


# ── Crash recovery ──────────────────────────────────────────────────────────


def test_crash_before_config_replace_rolls_back_cleanly(harness):
    p1 = harness.preview_add(allow_rule("r1"))
    harness.apply(p1, expected_contract_hash=p1.before_contract_hash)
    harness.crash_at("after_journal_write", rule_id="r2")
    assert pending_apply() is True
    recovered = harness.restart_and_recover()
    assert recovered.action == "rolled_back"
    assert harness.stable_rule_ids() == ["r1"]
    assert not journal_path().exists()
    assert not backup_path().exists()


def test_recovery_restores_verified_backup_on_divergent_config(harness):
    p1 = harness.preview_add(allow_rule("r1"))
    harness.apply(p1, expected_contract_hash=p1.before_contract_hash)
    harness.crash_at("after_config_replace", rule_id="r2")
    # A third party rewrites config.yaml while the journal is pending, so the
    # on-disk YAML matches neither the before nor the after hash.
    harness.config_path.write_text(
        yaml.safe_dump({"model": "divergent"}), encoding="utf-8"
    )
    recovered = harness.restart_and_recover()
    assert recovered.action == "restored"
    assert harness.stable_rule_ids() == ["r1"]  # the verified before-state
    assert "model" not in harness.read_config()
    assert not journal_path().exists()


def test_recovery_with_no_journal_is_a_noop(harness):
    result = harness.restart_and_recover()
    assert result.action == "none"
    assert result.contract is None


def test_journal_is_content_free(harness):
    harness.crash_at("after_config_replace", rule_id="secret-canary-rule")
    payload = json.loads(journal_path().read_text(encoding="utf-8"))
    text = json.dumps(payload)
    assert "secret-canary-rule" not in text
    assert "message.send" not in text
    for key in (
        "before_config_hash",
        "after_config_hash",
        "before_contract_hash",
        "after_contract_hash",
    ):
        assert isinstance(payload[key], str) and payload[key]
    harness.restart_and_recover()


# ── Fail-closed pending journal ─────────────────────────────────────────────


def test_pending_journal_blocks_mutations_until_recovery(harness):
    harness.crash_at("after_config_replace")
    assert pending_apply() is True

    with pytest.raises(IncompleteAuthorityApply) as excinfo:
        harness.preview_add(allow_rule("r2"))
    assert excinfo.value.code == "incomplete_authority_apply"

    stale = object.__new__(type("X", (), {}))  # never reached; guard fires first
    with pytest.raises(IncompleteAuthorityApply):
        apply_config_change(
            stale, expected_contract_hash="irrelevant", db=harness.db
        )

    harness.restart_and_recover()
    assert pending_apply() is False
    preview = harness.preview_add(allow_rule("r2"))
    harness.apply(preview, expected_contract_hash=preview.before_contract_hash)
    assert sorted(harness.stable_rule_ids()) == ["crash-rule", "r2"]


# ── Config-layer validation ─────────────────────────────────────────────────


def test_learned_suggestion_never_enters_stable_config(harness):
    sugg = AutonomyRule(
        rule_id="s-1",
        source="learned_suggestion",
        state="awaiting_confirmation",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(actor_kind="learner", confirmed_at_ms=None),
    )
    with pytest.raises(InvalidStableAuthority):
        preview_config_change(ConfigChange(set_rules=(sugg,)), db=harness.db)


def test_runtime_counters_never_enter_stable_config(harness):
    counted = allow_rule("r1", max_uses=3, remaining_uses=3)
    with pytest.raises(InvalidStableAuthority, match="runtime counter"):
        preview_config_change(ConfigChange(set_rules=(counted,)), db=harness.db)


def test_preview_warns_about_already_expired_stable_rule(harness):
    expired = allow_rule("r-old", created_at_ms=0, expires_at_ms=1)
    preview = preview_config_change(
        ConfigChange(set_rules=(expired,)), db=harness.db
    )
    assert any("r-old" in warning for warning in preview.warnings)


# ── Profile-local paths ─────────────────────────────────────────────────────


def test_saga_files_resolve_under_the_active_profile_home(harness):
    assert journal_path().parent == harness.home
    assert backup_path().parent == harness.home
    assert lock_path().parent == harness.home
    assert journal_path().name == "autonomy-apply.pending.json"
    assert lock_path().name == "autonomy.config.lock"
