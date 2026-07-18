"""End-state reliability summary for the harness fault matrix.

`summarize_scenarios` is the pure roll-up step the matrix calls after
its scenarios have produced their ``ScenarioResult`` rows. It does
no I/O — it only folds counts and the pass-at-k view into a dict
the renderer can consume.

Kept side-effect free on purpose: the matrix itself owns how each
scenario is observed (DB rows, queue receipts, ack tables); this
module only shapes the scorecard.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioResult:
    """One observation of one scenario from the fault matrix.

    ``k`` is the trial index (1-based) for repeated scenarios; for
    single-shot scenarios it is always 1. ``pass_at_k`` is derived
    by the summary across rows that share the same scenario name.
    """

    scenario: str
    passed: bool
    k: int
    side_effects_correct: bool
    unresolved: bool


def summarize_scenarios(results):
    """Roll a list of :class:`ScenarioResult` into the matrix scorecard.

    Returns a dict with ``total``, ``passed``, ``pass_rate``,
    ``pass_at_k``, ``unresolved``, and ``wrong_side_effects``.

    ``pass_at_k`` only contains a scenario name when the same name
    was observed more than once *and every trial passed* — single-
    trial scenarios and any-trial-failed scenarios are omitted.
    """

    total = len(results)
    passed = sum(1 for r in results if r.passed and not r.unresolved)
    unresolved = sum(1 for r in results if r.unresolved)
    wrong_side_effects = sum(1 for r in results if not r.side_effects_correct)

    # Group by scenario name; only repeated scenarios (>= 2 trials)
    # where every trial passed contribute to pass_at_k.
    by_name = {}
    for r in results:
        by_name.setdefault(r.scenario, []).append(r)

    pass_at_k = {
        name: True
        for name, rows in by_name.items()
        if len(rows) >= 2 and all(r.passed and not r.unresolved for r in rows)
    }

    pass_rate = (passed / total) if total else 0.0

    return {
        "total": total,
        "passed": passed,
        "pass_rate": pass_rate,
        "pass_at_k": pass_at_k,
        "unresolved": unresolved,
        "wrong_side_effects": wrong_side_effects,
    }