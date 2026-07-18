"""Tests for the ``hermes reliability check`` subcommand.

The command is deterministic and offline: no model providers, no
network, no wall clock. It shells out the eight end-state fault
scenarios from :mod:`agent.reliability_scenarios` and renders the
matrix summary from :mod:`agent.reliability_report`. Tests assert
three exit codes — 0 (all pass), 1 (scenario failure), 2 (invalid
usage) — plus human and JSON output shapes.
"""

from __future__ import annotations

import io
import json

import pytest

from hermes_cli.reliability import (
    RELIABILITY_INVALID_USAGE,
    RELIABILITY_SCENARIO_FAILURE,
    RELIABILITY_SUCCESS,
    ReliabilityError,
    build_reliability_parser,
    cmd_reliability,
    render_human_table,
)


# ── exit codes ────────────────────────────────────────────────────────────


def test_exit_code_constants_match_spec():
    # 0 / 1 / 2 — the contract this subcommand promises in its docs.
    assert RELIABILITY_SUCCESS == 0
    assert RELIABILITY_SCENARIO_FAILURE == 1
    assert RELIABILITY_INVALID_USAGE == 2


# ── parser ────────────────────────────────────────────────────────────────


def test_parser_requires_check_subcommand():
    parser = build_reliability_parser()
    # `hermes reliability` with no subcommand must error — invalid usage.
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([])
    assert exc.value.code == RELIABILITY_INVALID_USAGE


def test_parser_check_has_json_flag():
    parser = build_reliability_parser()
    args = parser.parse_args(["check", "--json"])
    assert args.json is True


def test_parser_check_without_json_defaults_to_human():
    parser = build_reliability_parser()
    args = parser.parse_args(["check"])
    assert args.json is False


def test_parser_rejects_unknown_subcommand():
    parser = build_reliability_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["bogus"])
    assert exc.value.code == RELIABILITY_INVALID_USAGE


# ── human output ──────────────────────────────────────────────────────────


def test_render_human_table_shows_scenario_pass_status():
    rows = [
        {
            "scenario": "rate_limit_fallback",
            "passed": True,
            "unresolved": False,
            "wrong_side_effect_count": 0,
            "recovery_steps": ("retry", "ack"),
        },
        {
            "scenario": "broken_scenario",
            "passed": False,
            "unresolved": False,
            "wrong_side_effect_count": 2,
            "recovery_steps": (),
        },
    ]
    out = render_human_table(rows)
    assert "rate_limit_fallback" in out
    assert "PASS" in out
    assert "FAIL" in out
    assert "broken_scenario" in out
    # Wrong-side-effect count and recovery steps must surface in the table
    # so operators can act without re-reading the full report.
    assert "2" in out


def test_render_human_table_marks_unresolved_rows():
    rows = [
        {
            "scenario": "timeout_after_dispatch",
            "passed": True,
            "unresolved": True,
            "wrong_side_effect_count": 0,
            "recovery_steps": ("reconcile",),
        },
    ]
    out = render_human_table(rows)
    # Unresolved rows are reported distinctly from clean passes so the
    # operator can see "we don't know" without it counting as PASS.
    assert "timeout_after_dispatch" in out
    assert "UNRESOLVED" in out


def test_render_human_table_emits_recovery_steps():
    rows = [
        {
            "scenario": "process_restart",
            "passed": True,
            "unresolved": True,
            "wrong_side_effect_count": 0,
            "recovery_steps": ("restart", "reconcile_journal"),
        },
    ]
    out = render_human_table(rows)
    assert "restart" in out
    assert "reconcile_journal" in out


# ── cmd_reliability happy path ────────────────────────────────────────────


def test_cmd_reliability_check_returns_zero_when_all_scenarios_pass(
    monkeypatch, capsys, tmp_path
):
    # Run the real scenarios — they are deterministic and offline.
    monkeypatch.chdir(tmp_path)
    parser = build_reliability_parser()
    args = parser.parse_args(["check"])

    rc = cmd_reliability(args)
    out = capsys.readouterr().out

    assert rc == RELIABILITY_SUCCESS
    # The matrix summary must be printed for the human reader.
    assert "rate_limit_fallback" in out
    assert "passed" in out.lower() or "pass" in out.lower()


def test_cmd_reliability_check_json_emits_summary_dict_plus_rows(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.chdir(tmp_path)
    parser = build_reliability_parser()
    args = parser.parse_args(["check", "--json"])

    rc = cmd_reliability(args)

    captured = capsys.readouterr().out
    payload = json.loads(captured)

    assert rc == RELIABILITY_SUCCESS
    # Top-level keys come from summarize_scenarios() exactly.
    assert set(payload["summary"]) == {
        "total",
        "passed",
        "pass_rate",
        "pass_at_k",
        "unresolved",
        "wrong_side_effects",
    }
    # Per-scenario rows are an explicit list, not a side-effect of
    # rolling up the summary — operators read them directly.
    assert isinstance(payload["scenarios"], list)
    assert payload["scenarios"], "scenarios list must not be empty"


def test_cmd_reliability_check_rejects_invalid_args_with_usage_code():
    parser = build_reliability_parser()
    # Pass a deliberately invalid arg so argparse errors at parse time.
    # We don't go through cmd_reliability; this is pure parser behavior.
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["check", "--bogus-flag"])
    assert exc.value.code == RELIABILITY_INVALID_USAGE


# ── cmd_reliability failure path ──────────────────────────────────────────


def test_cmd_reliability_check_returns_one_when_a_scenario_fails(
    monkeypatch, capsys, tmp_path
):
    # Patch a single scenario function to fail so we exercise the
    # non-zero exit path without rebuilding the fault matrix from
    # scratch. SCENARIOS is a tuple captured at import time, so we
    # patch the tuple directly rather than the module-level name.
    from agent.reliability_fakes import ScenarioRow
    import agent.reliability_scenarios as scenarios_mod

    def _failing_scenario(_path):
        return ScenarioRow(
            scenario="rate_limit_fallback",
            passed=False,
            unresolved=False,
            wrong_side_effect_count=3,
            recovery_steps=("retry_with_backoff",),
        )

    new_tuple = tuple(
        _failing_scenario if fn is scenarios_mod.rate_limit_fallback else fn
        for fn in scenarios_mod.SCENARIOS
    )
    monkeypatch.setattr(scenarios_mod, "SCENARIOS", new_tuple)

    monkeypatch.chdir(tmp_path)
    args = build_reliability_parser().parse_args(["check"])

    rc = cmd_reliability(args)

    assert rc == RELIABILITY_SCENARIO_FAILURE


def test_cmd_reliability_check_invalid_subcommand_returns_usage_code():
    # An empty argument list must trigger argparse's "missing subcommand"
    # path with exit 2 (= RELIABILITY_INVALID_USAGE). The contract is
    # enforced at parse time — the handler is never invoked when the
    # invocation is already invalid.
    parser = build_reliability_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([])
    assert exc.value.code == RELIABILITY_INVALID_USAGE


# ── ReliabilityError surface ─────────────────────────────────────────────


def test_reliability_error_carries_exit_code():
    err = ReliabilityError("bad arg", code=RELIABILITY_INVALID_USAGE)
    assert err.code == RELIABILITY_INVALID_USAGE
    assert "bad arg" in str(err)
