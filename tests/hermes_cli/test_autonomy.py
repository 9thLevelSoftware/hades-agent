"""Task 7 CLI/classic-slash tests for ``hades_cli.autonomy``.

Real-path invariants against the temporary ``HADES_HOME`` set by the
autouse conftest fixture:

- rule changes preview by default and apply only under the exact current
  contract hash (stale hash fails with exit 2, writing nothing);
- every effective rule can be explained and carries an exact edit route;
- suggestion acceptance is explicit: a destination (``--stable`` or
  ``--temporary``) is required, and a temporary acceptance creates a
  bounded mandate — the suggestion itself never becomes authority;
- input files are bounded (1 MiB), durations/uses/limits are bounded,
  and validation failures exit 2 while denied evaluations exit 3;
- the top-level parser, the classic ``/autonomy`` slash path, and the
  registry alias all delegate to the same ``run_argv``.
"""

from __future__ import annotations

import json
import shlex
import time

import pytest
import yaml

from agent.autonomy import (
    ActionContext,
    AutonomyRule,
    AutonomyService,
    RuleProvenance,
)
from hades_constants import get_hades_home

FAR_FUTURE_MS = int(time.time() * 1000) + 365 * 86_400_000

RULE_ALLOW_SEND = {
    "rule_id": "allow-send",
    "effect": "allow",
    "action_classes": ["message.send"],
    "data_classes": ["public"],
    "recipient_classes": ["designated_test"],
    "description": "allow public sends to the designated test recipient",
}

ACTION_SEND = {
    "action_class": "message.send",
    "data_classes": ["public"],
    "reversibility": "reversible",
    "recipient_class": "designated_test",
}


# ── Fixture builders ────────────────────────────────────────────────────────


def write_config(stable_rules: list[dict], mode: str = "enforce") -> None:
    config_path = get_hades_home() / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "autonomy": {
                    "schema_version": 1,
                    "mode": mode,
                    "stable_rules": stable_rules,
                }
            }
        ),
        encoding="utf-8",
    )


def stable_entry(rule_id: str = "allow-status", effect: str = "allow") -> dict:
    return {
        "rule_id": rule_id,
        "effect": effect,
        "action_classes": ["message.send"],
        "data_classes": ["public"],
        "recipient_classes": ["designated_test"],
    }


def user_provenance(**overrides) -> RuleProvenance:
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


def learner_provenance(**overrides) -> RuleProvenance:
    base = dict(
        actor_kind="learner",
        actor_id="pattern-miner",
        source_ref="observed-behavior",
        observed_at_ms=100,
        confirmed_at_ms=None,
        confidence_ppm=990_000,
    )
    base.update(overrides)
    return RuleProvenance(**base)


def seed_suggestion(rule_id: str = "suggest-1") -> None:
    AutonomyService().propose_suggestion(
        AutonomyRule(
            rule_id=rule_id,
            source="learned_suggestion",
            state="awaiting_confirmation",
            effect="allow",
            action_classes=("message.send",),
            data_classes=("internal",),
            recipient_classes=("colleague",),
            provenance=learner_provenance(),
            created_at_ms=100,
            description="observed repeated sends to a colleague",
        )
    )


def seed_mandate(rule_id: str = "mandate-1") -> None:
    AutonomyService().create_mandate(
        AutonomyRule(
            rule_id=rule_id,
            source="temporary_mandate",
            state="active",
            effect="allow",
            action_classes=("workspace.delete",),
            data_classes=("internal",),
            allowed_reversibility=("reversible",),
            provenance=user_provenance(),
            created_at_ms=100,
            expires_at_ms=FAR_FUTURE_MS,
            max_uses=3,
            remaining_uses=3,
            description="bounded checkpointed delete",
        )
    )


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Slash-style runner over ``run_argv`` with canned input files."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "allow-send.yaml").write_text(
        yaml.safe_dump(RULE_ALLOW_SEND), encoding="utf-8"
    )
    (tmp_path / "action-send.yaml").write_text(
        yaml.safe_dump(ACTION_SEND), encoding="utf-8"
    )

    class _Cli:
        @staticmethod
        def run(command: str):
            from hades_cli.autonomy import run_argv

            return run_argv(shlex.split(command))

    return _Cli()


# ── Preview / apply ─────────────────────────────────────────────────────────


def test_rule_change_previews_by_default_and_requires_exact_apply_hash(cli):
    preview = cli.run("rule add --file allow-send.yaml")
    assert preview.exit_code == 0
    assert "not applied" in preview.output
    assert preview.json["before_contract_hash"]
    stale = cli.run("rule add --file allow-send.yaml --apply --expected-contract-hash wrong")
    assert stale.exit_code == 2


def test_rule_add_apply_with_exact_hash_persists_the_rule(cli):
    preview = cli.run("rule add --file allow-send.yaml")
    applied = cli.run(
        "rule add --file allow-send.yaml --apply "
        f"--expected-contract-hash {preview.json['before_contract_hash']}"
    )
    assert applied.exit_code == 0
    assert applied.json["applied"] is True
    listed = cli.run("list --source user_assertion --json")
    assert listed.exit_code == 0
    assert "allow-send" in [r["rule_id"] for r in listed.json["rules"]]


def test_apply_requires_expected_contract_hash_flag(cli):
    result = cli.run("rule add --file allow-send.yaml --apply")
    assert result.exit_code == 2
    assert "--expected-contract-hash" in result.output


def test_rule_remove_unknown_rule_is_a_validation_error(cli):
    result = cli.run("rule remove no-such-rule")
    assert result.exit_code == 2


def test_stale_apply_writes_nothing(cli):
    cli.run("rule add --file allow-send.yaml --apply --expected-contract-hash wrong")
    listed = cli.run("list --source user_assertion --json")
    assert listed.json["rules"] == []


# ── Explain / edit routes ───────────────────────────────────────────────────


def test_every_effective_rule_can_be_explained_and_has_an_edit_route(cli):
    write_config([stable_entry("allow-status")])
    seed_mandate("mandate-1")
    listed = cli.run("list --effective --json").json["rules"]
    assert {r["rule_id"] for r in listed} == {"allow-status", "mandate-1"}
    for rule in listed:
        explanation = cli.run(f"rule explain {rule['rule_id']} --json").json
        assert explanation["source"] in {"user_assertion", "temporary_mandate"}
        assert explanation["edit_command"]


def test_rule_show_renders_provenance_without_secrets(cli):
    write_config([stable_entry("allow-status")])
    shown = cli.run("rule show allow-status --json")
    assert shown.exit_code == 0
    assert shown.json["state"] == "active"
    assert shown.json["confidence_ppm"] == 1_000_000
    assert "secret" not in shown.output.lower()


# ── Evaluate ────────────────────────────────────────────────────────────────


def test_evaluate_explicit_allow_exits_zero(cli):
    write_config([RULE_ALLOW_SEND])
    result = cli.run("evaluate --file action-send.yaml --json")
    assert result.exit_code == 0
    assert (result.json["verdict"], result.json["code"]) == ("allow", "explicit_allow")
    assert result.json["matched_rule_ids"] == ["allow-send"]


def test_evaluate_denied_action_exits_three(cli, tmp_path):
    (tmp_path / "action-cred.yaml").write_text(
        yaml.safe_dump(
            {
                "action_class": "message.send",
                "data_classes": ["credential"],
                "reversibility": "irreversible",
            }
        ),
        encoding="utf-8",
    )
    result = cli.run("evaluate --file action-cred.yaml --json")
    assert result.exit_code == 3
    assert result.json["verdict"] == "deny"


def test_evaluate_stage_is_restricted_to_explain_and_preview(cli):
    result = cli.run("evaluate --file action-send.yaml --stage execute")
    assert result.exit_code == 2


# ── Suggestions ─────────────────────────────────────────────────────────────


def test_suggestion_accept_is_explicit_and_destination_is_required(cli):
    seed_suggestion("suggest-1")
    result = cli.run("suggestion accept suggest-1")
    assert result.exit_code == 2
    assert "--stable or --temporary" in result.output


def test_suggestion_accept_temporary_creates_a_bounded_mandate(cli):
    seed_suggestion("suggest-1")
    accepted = cli.run(
        "suggestion accept suggest-1 --temporary --expires-in 1h --uses 2 --json"
    )
    assert accepted.exit_code == 0
    assert accepted.json["destination"] == "mandate"
    new_rule_id = accepted.json["new_rule_id"]
    listed = cli.run("list --source temporary_mandate --state active --json")
    assert new_rule_id in [r["rule_id"] for r in listed.json["rules"]]
    pending = cli.run(
        "list --source learned_suggestion --state awaiting_confirmation --json"
    )
    assert pending.json["rules"] == []


def test_suggestion_accept_stable_previews_until_exact_hash_apply(cli):
    seed_suggestion("suggest-1")
    preview = cli.run("suggestion accept suggest-1 --stable")
    assert preview.exit_code == 0
    assert "not applied" in preview.output
    applied = cli.run(
        "suggestion accept suggest-1 --stable --apply "
        f"--expected-contract-hash {preview.json['before_contract_hash']}"
    )
    assert applied.exit_code == 0
    listed = cli.run("list --source user_assertion --json")
    assert applied.json["new_rule_id"] in [r["rule_id"] for r in listed.json["rules"]]


def test_suggestion_reject_requires_a_reason(cli):
    seed_suggestion("suggest-1")
    missing = cli.run("suggestion reject suggest-1")
    assert missing.exit_code == 2
    rejected = cli.run("suggestion reject suggest-1 --reason 'not wanted'")
    assert rejected.exit_code == 0
    assert rejected.json["state"] == "rejected"


# ── Mandates ────────────────────────────────────────────────────────────────


def test_mandate_add_requires_bounded_duration(cli):
    result = cli.run("mandate add --file allow-send.yaml --expires-in 10s")
    assert result.exit_code == 2
    result = cli.run("mandate add --file allow-send.yaml --expires-in 400d")
    assert result.exit_code == 2


def test_mandate_add_and_revoke_roundtrip(cli):
    added = cli.run("mandate add --file allow-send.yaml --expires-in 1h --uses 2 --json")
    assert added.exit_code == 0
    rule_id = added.json["rule_id"]
    missing_reason = cli.run(f"mandate revoke {rule_id}")
    assert missing_reason.exit_code == 2
    revoked = cli.run(f"mandate revoke {rule_id} --reason done --json")
    assert revoked.exit_code == 0
    assert revoked.json["state"] == "revoked"


# ── Bounded inputs ──────────────────────────────────────────────────────────


def test_input_file_over_one_mib_is_rejected(cli, tmp_path):
    big = tmp_path / "big.yaml"
    big.write_text("description: " + "x" * (1_048_576 + 1), encoding="utf-8")
    result = cli.run("rule add --file big.yaml")
    assert result.exit_code == 2


def test_audit_limit_is_bounded(cli):
    assert cli.run("audit --limit 0").exit_code == 2
    assert cli.run("audit --limit 501").exit_code == 2


def test_uses_are_bounded(cli):
    seed_suggestion("suggest-1")
    result = cli.run(
        "suggestion accept suggest-1 --temporary --expires-in 1h --uses 10001"
    )
    assert result.exit_code == 2


# ── Audit / export / purge / doctor / status ────────────────────────────────


def test_audit_lists_recorded_decisions(cli):
    write_config([RULE_ALLOW_SEND])
    AutonomyService().evaluate(
        ActionContext(
            operation_key="op-audit-1",
            stage="execute",
            action_class="message.send",
            data_classes=("public",),
            reversibility="reversible",
            recipient_class="designated_test",
        ),
        consume=True,
    )
    result = cli.run("audit --json")
    assert result.exit_code == 0
    decisions = result.json["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["verdict"] == "allow"
    filtered = cli.run("audit --verdict deny --json")
    assert filtered.json["decisions"] == []


def test_export_writes_redacted_file(cli, tmp_path):
    write_config([stable_entry("allow-status")])
    result = cli.run("export --output authority.json")
    assert result.exit_code == 0
    exported = json.loads((tmp_path / "authority.json").read_text(encoding="utf-8"))
    assert exported["stable_rules"][0]["rule_id"] == "allow-status"
    assert "decisions" not in exported


def test_purge_audit_requires_apply(cli):
    result = cli.run("purge-audit --before 2026-01-01T00:00:00")
    assert result.exit_code == 2


def test_status_and_doctor_report_contract_identity(cli):
    write_config([stable_entry("allow-status")])
    status = cli.run("status --json")
    assert status.exit_code == 0
    assert status.json["mode"] == "enforce"
    assert status.json["contract_hash"]
    assert status.json["stable_rules"] == 1
    doctor = cli.run("doctor --json")
    assert doctor.exit_code == 0
    assert doctor.json["pending_apply"] is False


# ── One parser for every surface ────────────────────────────────────────────


def test_classic_slash_and_top_level_share_one_surface(cli):
    from hades_cli.autonomy import run_argv, run_slash

    write_config([stable_entry("allow-status")])
    assert run_slash("status") == run_argv(["status"]).output
    assert "usage" in run_slash("").lower() or "autonomy" in run_slash("").lower()


def test_registry_exposes_autonomy_with_authority_alias():
    from hades_cli.commands import resolve_command

    command = resolve_command("autonomy")
    assert command is not None and command.name == "autonomy"
    assert resolve_command("authority").name == "autonomy"
    assert command.category == "Configuration"
