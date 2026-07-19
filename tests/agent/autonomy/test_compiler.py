"""Task 3 layer tests for ``agent.autonomy.compiler``.

The compiler merges confirmed config assertions (``autonomy.stable_rules``)
with active temporary mandates into a deterministic contract snapshot.
Learned suggestions never compile; invalid config never yields a partial
rule set (``invalid_stable_authority``); there is no profile inheritance.
"""

from __future__ import annotations

import pytest

from agent.autonomy import AutonomyRule, RuleProvenance, RuleScope
from agent.autonomy.compiler import (
    INVALID_STABLE_AUTHORITY,
    InvalidStableAuthority,
    compile_contract,
    compile_draft,
    parse_stable_rules,
    stable_rule_to_config_entry,
    validate_autonomy_section,
)

NOW_MS = 1_000


# ── Fixture builders ────────────────────────────────────────────────────────


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


def user_rule(rule_id: str, **overrides) -> dict:
    """Config-shaped stable rule entry, exactly as YAML would parse it."""
    base: dict = {
        "rule_id": rule_id,
        "effect": "allow",
        "action_classes": ["message.send"],
    }
    base.update(overrides)
    return base


def config_with(*entries: dict, **section_overrides) -> dict:
    section: dict = {
        "schema_version": 1,
        "mode": "enforce",
        "default_known_reversible": "ask",
        "default_unknown_or_irreversible": "deny",
        "decision_ttl_seconds": 300,
        "audit_retention_days": 90,
        "stable_rules": list(entries),
    }
    section.update(section_overrides)
    return {"autonomy": section}


def mandate(
    rule_id: str,
    *,
    remaining_uses: int | None = 1,
    expires_at_ms: int | None = None,
    state: str = "active",
    scope: RuleScope | None = None,
    **overrides,
) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="temporary_mandate",
        state=state,
        effect="allow",
        action_classes=("workspace.delete",),
        provenance=provenance(),
        created_at_ms=0,
        max_uses=None if remaining_uses is None else max(remaining_uses, 1),
        remaining_uses=remaining_uses,
        expires_at_ms=expires_at_ms,
        scope=scope or RuleScope(),
    )
    base.update(overrides)
    return AutonomyRule(**base)


def suggestion(rule_id: str, *, confidence_ppm: int = 990_000, **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="learned_suggestion",
        state="awaiting_confirmation",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(
            actor_kind="learner", confirmed_at_ms=None, confidence_ppm=confidence_ppm
        ),
        created_at_ms=0,
    )
    base.update(overrides)
    return AutonomyRule(**base)


# ── Merge semantics ─────────────────────────────────────────────────────────


def test_compiler_excludes_suggestions_and_includes_active_mandates():
    contract = compile_contract(
        config_with(user_rule("stable-1")),
        [suggestion("s-1", confidence_ppm=999999), mandate("m-1", remaining_uses=1)],
        profile_id="work",
        now_ms=1000,
    )
    assert [r.rule_id for r in contract.rules] == ["m-1", "stable-1"]


def test_suggestions_never_change_the_contract_hash():
    without = compile_contract(
        config_with(user_rule("stable-1")), [mandate("m-1")],
        profile_id="work", now_ms=NOW_MS,
    )
    with_suggestion = compile_contract(
        config_with(user_rule("stable-1")),
        [mandate("m-1"), suggestion("s-1", confidence_ppm=1_000_000)],
        profile_id="work", now_ms=NOW_MS,
    )
    assert without.contract_hash == with_suggestion.contract_hash


def test_compile_is_deterministic_for_identical_inputs():
    args = (config_with(user_rule("stable-1")), [mandate("m-1")])
    first = compile_contract(*args, profile_id="work", now_ms=NOW_MS)
    second = compile_contract(*args, profile_id="work", now_ms=NOW_MS)
    assert first.contract_hash == second.contract_hash
    assert first == second


@pytest.mark.parametrize(
    "dead_mandate",
    [
        mandate("m-expired", expires_at_ms=NOW_MS),  # expiry <= now
        mandate("m-exhausted", remaining_uses=0),
        mandate("m-revoked", state="revoked"),
        mandate("m-consumed", remaining_uses=0, state="consumed"),
        mandate("m-other-profile", scope=RuleScope(profile_id="other")),
    ],
    ids=["expired", "exhausted", "revoked", "consumed", "wrong-profile"],
)
def test_inactive_or_foreign_mandates_are_discarded(dead_mandate):
    contract = compile_contract(
        config_with(user_rule("stable-1")),
        [dead_mandate, mandate("m-live")],
        profile_id="work",
        now_ms=NOW_MS,
    )
    assert [r.rule_id for r in contract.rules] == ["m-live", "stable-1"]


def test_unexpired_scoped_mandate_for_this_profile_compiles():
    contract = compile_contract(
        config_with(),
        [mandate("m-1", expires_at_ms=NOW_MS + 1, scope=RuleScope(profile_id="work"))],
        profile_id="work",
        now_ms=NOW_MS,
    )
    assert [r.rule_id for r in contract.rules] == ["m-1"]


def test_missing_autonomy_section_compiles_to_empty_contract():
    contract = compile_contract({}, [], profile_id="default", now_ms=NOW_MS)
    assert contract.rules == ()
    assert contract.profile_id == "default"
    assert contract.version == 1
    assert contract.contract_hash


def test_user_assertion_in_runtime_rules_is_a_programming_error():
    runtime_copy = AutonomyRule(
        rule_id="stable-copy",
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(),
    )
    with pytest.raises(ValueError, match="config.yaml"):
        compile_contract(
            config_with(), [runtime_copy], profile_id="work", now_ms=NOW_MS
        )


def test_duplicate_rule_id_across_layers_fails_closed():
    with pytest.raises(ValueError, match="duplicate"):
        compile_contract(
            config_with(user_rule("dup-1")),
            [mandate("dup-1")],
            profile_id="work",
            now_ms=NOW_MS,
        )


# ── Stable config parsing (fail closed, no partial rule sets) ───────────────


def test_parse_stable_rules_builds_full_user_assertions():
    section = config_with(
        user_rule(
            "r-1",
            data_classes=["internal"],
            scope={"resource_prefixes": ["workspace:/tmp"]},
            expires_at_ms=99_999,
            description="allow status messages",
        )
    )["autonomy"]
    rules = parse_stable_rules(section)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.source == "user_assertion"
    assert rule.state == "active"
    assert rule.action_classes == ("message.send",)
    assert rule.scope.resource_prefixes == ("workspace:/tmp",)
    assert rule.may_authorize


@pytest.mark.parametrize(
    "bad_entry, match",
    [
        (user_rule("r", source="learned_suggestion"), "user_assertion"),
        (user_rule("r", source="temporary_mandate"), "user_assertion"),
        (user_rule("r", state="revoked"), "active"),
        (user_rule("r", max_uses=3), "runtime counter"),
        (user_rule("r", remaining_uses=1), "runtime counter"),
        (user_rule("r", scope={"task_id": "t-1"}), "scope"),
        (user_rule("r", scope={"session_id": "s-1"}), "scope"),
        (user_rule("r", scope={"profile_id": "other"}), "scope"),
        (user_rule("r", api_key="sk-canary"), "unknown|secret|credential"),
        (user_rule("r", credentials={"user": "x"}), "unknown|secret|credential"),
        (user_rule("r", surprise=1), "unknown"),
        ({"effect": "allow", "action_classes": ["message.send"]}, "rule_id"),
        (user_rule("r", effect="allow", action_classes=[]), "allow"),
        ("not-a-mapping", "mapping"),
    ],
    ids=[
        "suggestion-source", "mandate-source", "inactive-state", "max-uses",
        "remaining-uses", "task-scope", "session-scope", "profile-scope",
        "api-key", "credentials", "unknown-key", "missing-rule-id",
        "wildcard-allow", "non-mapping",
    ],
)
def test_invalid_stable_rule_entries_fail_closed(bad_entry, match):
    config = config_with(user_rule("good-1"), bad_entry)
    with pytest.raises(InvalidStableAuthority, match=match) as excinfo:
        compile_contract(config, [], profile_id="work", now_ms=NOW_MS)
    assert excinfo.value.code == INVALID_STABLE_AUTHORITY
    assert INVALID_STABLE_AUTHORITY == "invalid_stable_authority"


def test_deny_rule_without_action_selector_is_valid_config():
    contract = compile_contract(
        config_with({"rule_id": "deny-all-credential", "effect": "deny",
                     "data_classes": ["credential"]}),
        [],
        profile_id="work",
        now_ms=NOW_MS,
    )
    assert [r.rule_id for r in contract.rules] == ["deny-all-credential"]
    assert contract.rules[0].effect == "deny"


@pytest.mark.parametrize(
    "section_overrides, match",
    [
        ({"mode": "yolo"}, "mode"),
        ({"schema_version": 2}, "schema_version"),
        ({"default_known_reversible": "allow"}, "allow"),
        ({"default_unknown_or_irreversible": "allow"}, "allow"),
        ({"decision_ttl_seconds": 0}, "decision_ttl_seconds"),
        ({"audit_retention_days": -1}, "audit_retention_days"),
        ({"stable_rules": "not-a-list"}, "list"),
        ({"surprise_setting": True}, "unknown"),
    ],
)
def test_invalid_autonomy_section_fails_closed(section_overrides, match):
    config = config_with(**section_overrides)
    with pytest.raises(InvalidStableAuthority, match=match):
        compile_contract(config, [], profile_id="work", now_ms=NOW_MS)


def test_validate_autonomy_section_accepts_missing_and_default_sections():
    assert validate_autonomy_section(None) is not None
    assert validate_autonomy_section({}) is not None
    validate_autonomy_section(config_with()["autonomy"])


def test_duplicate_stable_rule_ids_rejected():
    with pytest.raises(InvalidStableAuthority, match="duplicate"):
        parse_stable_rules(config_with(user_rule("r-1"), user_rule("r-1"))["autonomy"])


# ── Config entry round-trip ─────────────────────────────────────────────────


def test_stable_rule_config_entry_round_trips():
    rule = parse_stable_rules(
        config_with(
            user_rule(
                "r-1",
                data_classes=["internal", "public"],
                recipient_classes=["designated_test"],
                scope={"resource_prefixes": ["workspace:/tmp"]},
                cost={"currency": "USD", "max_per_action_cents": 500},
                description="bounded send",
            )
        )["autonomy"]
    )[0]
    entry = stable_rule_to_config_entry(rule)
    assert entry["rule_id"] == "r-1"
    assert "source" not in entry  # implied user_assertion
    assert "max_uses" not in entry
    reparsed = parse_stable_rules({"stable_rules": [entry]})[0]
    assert reparsed == rule


def test_stable_rule_config_entry_rejects_runtime_material():
    with pytest.raises(InvalidStableAuthority):
        stable_rule_to_config_entry(mandate("m-1"))
    with pytest.raises(InvalidStableAuthority):
        stable_rule_to_config_entry(suggestion("s-1"))


# ── Draft/contract identity ─────────────────────────────────────────────────


def test_compile_draft_matches_compile_contract_hash():
    config = config_with(user_rule("stable-1"))
    runtime = [mandate("m-1")]
    draft = compile_draft(config, runtime, profile_id="work", now_ms=NOW_MS)
    contract = compile_contract(config, runtime, profile_id="work", now_ms=NOW_MS)
    assert draft.content_hash() == contract.contract_hash
    assert draft.rules == contract.rules
    assert draft.source_fingerprint  # deterministic default fingerprint
    again = compile_draft(config, runtime, profile_id="work", now_ms=NOW_MS)
    assert again.source_fingerprint == draft.source_fingerprint
