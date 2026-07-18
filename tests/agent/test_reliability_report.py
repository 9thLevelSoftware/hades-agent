"""Pure-function tests for the harness end-state reliability summary.

`summarize_scenarios` rolls a list of `ScenarioResult` rows up into the
scorecard the reliability matrix renders. It is intentionally
side-effect free: callers feed it rows that were already observed
elsewhere (DB rows, queue receipts, ack tables) and it returns a
plain dict the matrix can render.
"""

from __future__ import annotations

import pytest

from agent.reliability_report import ScenarioResult, summarize_scenarios


# ── ScenarioResult shape ────────────────────────────────────────────────────


def test_scenario_result_is_frozen_dataclass_with_required_fields():
    # The matrix feeds rows through these fields directly, so the
    # field set is part of the contract — name them in the test so a
    # rename here breaks loudly here.
    row = ScenarioResult(
        scenario="timeout_before_dispatch",
        passed=True,
        k=1,
        side_effects_correct=True,
        unresolved=False,
    )
    assert row.scenario == "timeout_before_dispatch"
    assert row.passed is True
    assert row.k == 1
    assert row.side_effects_correct is True
    assert row.unresolved is False


def test_scenario_result_rejects_mutation():
    row = ScenarioResult(
        scenario="x",
        passed=True,
        k=1,
        side_effects_correct=True,
        unresolved=False,
    )
    with pytest.raises(Exception):
        row.passed = False  # type: ignore[misc]


# ── summarize_scenarios: empty + invariants ──────────────────────────────────


def test_summarize_empty_list_returns_zeroed_scorecard():
    out = summarize_scenarios([])
    assert out["total"] == 0
    assert out["passed"] == 0
    assert out["pass_rate"] == 0.0
    # pass_at_k only for repeated identical scenarios — none here.
    assert out["pass_at_k"] == {}
    assert out["unresolved"] == 0
    assert out["wrong_side_effects"] == 0


def test_summarize_preserves_all_required_keys_for_matrix_render():
    # The matrix renders the scorecard by key — if a key is missing the
    # render crashes. Lock the contract here so render changes too.
    out = summarize_scenarios([])
    assert set(out) == {
        "total",
        "passed",
        "pass_rate",
        "pass_at_k",
        "unresolved",
        "wrong_side_effects",
    }


# ── totals, pass_rate, unresolved, wrong_side_effects ───────────────────────


def test_summarize_totals_count_all_rows_and_passed_only_passing():
    rows = [
        ScenarioResult("a", passed=True, k=1, side_effects_correct=True, unresolved=False),
        ScenarioResult("b", passed=False, k=1, side_effects_correct=True, unresolved=False),
        ScenarioResult("c", passed=True, k=1, side_effects_correct=True, unresolved=False),
    ]
    out = summarize_scenarios(rows)
    assert out["total"] == 3
    assert out["passed"] == 2
    assert out["pass_rate"] == pytest.approx(2 / 3)


def test_summarize_pass_rate_is_zero_when_total_is_zero_not_nan():
    # NaN would poison downstream rendering — guard the boundary.
    out = summarize_scenarios([])
    assert out["pass_rate"] == 0.0
    assert isinstance(out["pass_rate"], float)


def test_summarize_counts_unresolved_separately_from_passed():
    # An unresolved row is not a passed row — it must not inflate
    # `passed` even if passed=True was set.
    rows = [
        ScenarioResult("a", passed=True, k=1, side_effects_correct=True, unresolved=False),
        ScenarioResult("b", passed=True, k=1, side_effects_correct=True, unresolved=True),
    ]
    out = summarize_scenarios(rows)
    assert out["unresolved"] == 1
    assert out["passed"] == 1


def test_summarize_counts_wrong_side_effects_separately():
    rows = [
        ScenarioResult("a", passed=True, k=1, side_effects_correct=True, unresolved=False),
        ScenarioResult("b", passed=False, k=1, side_effects_correct=False, unresolved=False),
        ScenarioResult("c", passed=True, k=1, side_effects_correct=False, unresolved=False),
    ]
    out = summarize_scenarios(rows)
    # Only rows whose side-effects were wrong count, regardless of passed.
    assert out["wrong_side_effects"] == 2


# ── pass_at_k semantics ─────────────────────────────────────────────────────


def test_pass_at_k_only_emitted_for_repeated_identical_scenario_names():
    # Same scenario name run k>1 times → if all pass, key in pass_at_k
    # is True; if any fail, key is False (or absent — test for True
    # to keep the matrix's "all-k-pass" indicator simple).
    rows = [
        ScenarioResult("rate_limit_fallback", passed=True, k=1, side_effects_correct=True, unresolved=False),
        ScenarioResult("rate_limit_fallback", passed=True, k=2, side_effects_correct=True, unresolved=False),
        ScenarioResult("rate_limit_fallback", passed=True, k=3, side_effects_correct=True, unresolved=False),
        ScenarioResult("one_shot", passed=True, k=1, side_effects_correct=True, unresolved=False),
    ]
    out = summarize_scenarios(rows)
    assert out["pass_at_k"] == {"rate_limit_fallback": True}


def test_pass_at_k_omits_scenarios_where_any_trial_failed():
    # Brief: pass_at_k is a name->bool "all-k-pass" indicator. The
    # matrix renders it as a green check; a single trial failure
    # means the scenario does NOT earn the all-k-pass badge — so it
    # is omitted rather than recorded as False (no green check at
    # all, which is what the operator wants to see).
    rows = [
        ScenarioResult("flaky", passed=True, k=1, side_effects_correct=True, unresolved=False),
        ScenarioResult("flaky", passed=False, k=2, side_effects_correct=True, unresolved=False),
        ScenarioResult("flaky", passed=True, k=3, side_effects_correct=True, unresolved=False),
    ]
    out = summarize_scenarios(rows)
    assert out["pass_at_k"] == {}


def test_pass_at_k_omits_scenarios_observed_only_once():
    rows = [
        ScenarioResult("only_once", passed=True, k=1, side_effects_correct=True, unresolved=False),
    ]
    out = summarize_scenarios(rows)
    assert out["pass_at_k"] == {}


def test_pass_at_k_accepts_repeated_scenario_regardless_of_per_row_k():
    # The brief only requires the scenario name to repeat with every
    # trial passing; the per-row k value is incidental trial metadata
    # (1-based index within a run). k uniformity across rows is not a
    # precondition.
    rows = [
        ScenarioResult("repeated", passed=True, k=1, side_effects_correct=True, unresolved=False),
        ScenarioResult("repeated", passed=True, k=2, side_effects_correct=True, unresolved=False),
        ScenarioResult("repeated", passed=True, k=3, side_effects_correct=True, unresolved=False),
    ]
    out = summarize_scenarios(rows)
    assert out["pass_at_k"] == {"repeated": True}


# ── end-to-end shape on a representative matrix slice ──────────────────────


def test_summarize_combines_all_signals_for_a_realistic_matrix_slice():
    rows = [
        # 3 repeated scenarios, all clean.
        ScenarioResult("timeout_before_dispatch", passed=True, k=3,
                       side_effects_correct=True, unresolved=False),
        ScenarioResult("timeout_before_dispatch", passed=True, k=3,
                       side_effects_correct=True, unresolved=False),
        ScenarioResult("timeout_before_dispatch", passed=True, k=3,
                       side_effects_correct=True, unresolved=False),
        # 1 single-shot scenario with a wrong side effect.
        ScenarioResult("late_tool_completion", passed=False, k=1,
                       side_effects_correct=False, unresolved=False),
        # 1 unresolved single-shot.
        ScenarioResult("process_restart", passed=False, k=1,
                       side_effects_correct=True, unresolved=True),
    ]
    out = summarize_scenarios(rows)
    assert out["total"] == 5
    assert out["passed"] == 3
    assert out["unresolved"] == 1
    assert out["wrong_side_effects"] == 1
    assert out["pass_at_k"] == {"timeout_before_dispatch": True}
    assert out["pass_rate"] == pytest.approx(3 / 5)