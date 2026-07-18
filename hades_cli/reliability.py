"""``hermes reliability`` subcommand — offline harness fault matrix.

The ``check`` action is deterministic: eight end-state fault scenarios
from :mod:`agent.reliability_scenarios`, rolled up through
:func:`agent.reliability_report.summarize_scenarios`. No model
providers, no network, no wall clock.

Exit codes:
    0 — every scenario passed.
    1 — at least one scenario failed (or had a wrong side effect).
    2 — invalid usage (missing subcommand, bad flag).

By design this command does not depend on Hermes running, only on the
local checkout of the hermes-agent repo (the matrix reads ScenarioRow
functions it imports on demand).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from agent.reliability_report import ScenarioResult, summarize_scenarios
from agent.reliability_scenarios import (
    ScenarioRow,
    run_all_scenarios,
)


RELIABILITY_SUCCESS = 0
RELIABILITY_SCENARIO_FAILURE = 1
RELIABILITY_INVALID_USAGE = 2


class ReliabilityError(Exception):
    """Raised by the handler when the user did something wrong."""

    def __init__(self, message: str, *, code: int = RELIABILITY_INVALID_USAGE) -> None:
        super().__init__(message)
        self.code = code


def build_reliability_parser() -> argparse.ArgumentParser:
    """Build the standalone ``hermes reliability`` parser.

    The top-level wrapper parser owns the ``check`` subcommand. The
    command itself is dispatched in :func:`cmd_reliability`.
    """
    parser = argparse.ArgumentParser(
        prog="hermes reliability",
        description="Offline harness fault matrix for end-state reliability.",
    )
    sub = parser.add_subparsers(
        dest="reliability_command",
        required=True,
    )
    check = sub.add_parser(
        "check",
        help="Run the eight end-state fault scenarios and render the matrix.",
        description=(
            "Deterministic, offline. Exits 0 when every scenario "
            "passes, 1 when any fails, 2 on invalid usage."
        ),
    )
    check.add_argument(
        "--json",
        action="store_true",
        help="Emit the matrix as JSON (summary + per-scenario rows).",
    )
    return parser


def render_human_table(rows: list[dict]) -> str:
    """Render a short human-readable table for ``rows``.

    Columns: scenario, status, unresolved, wrong-effects, recovery.
    The full roll-up summary (``summarize_scenarios`` output) is
    printed at the bottom — kept compact on purpose.
    """
    headers = ("SCENARIO", "STATUS", "UNRESOLVED", "WRONG_EFFECTS", "RECOVERY")
    widths = (24, 11, 11, 14, 36)

    def _row(name: str, status: str, unresolved: str, wrong: str, recovery: str) -> str:
        return "{:<24}  {:<11}  {:<11}  {:<14}  {}".format(
            name[:24], status[:11], unresolved[:11], wrong[:14], recovery[:36]
        )

    out: list[str] = [_row(*headers), "-" * 88]
    for row in rows:
        if row["passed"] and not row["unresolved"]:
            status = "PASS"
        elif row["unresolved"]:
            status = "UNRESOLVED"
        else:
            status = "FAIL"
        unresolved = "yes" if row["unresolved"] else "no"
        wrong = str(row["wrong_side_effect_count"])
        recovery = ", ".join(row["recovery_steps"]) or "-"
        out.append(_row(row["scenario"], status, unresolved, wrong, recovery))
    out.append("-" * 88)
    return "\n".join(out)


def _rows_as_dicts(rows: list[ScenarioRow]) -> list[dict]:
    return [asdict(r) for r in rows]


def _all_passed(rows: list[ScenarioRow]) -> bool:
    """A scenario row passes when no wrong side effects were recorded.

    An unresolved row is by design NOT a hard failure: it means the
    matrix didn't get a clean yes/no on it. We only fail the command
    when a row is ``passed=False`` (wrong_side_effect_count > 0 or
    state check failed).

    ponytail: the matrix's "pass rate" lives on
    :func:`agent.reliability_report.summarize_scenarios`; this is the
    CLI-level gate, and it deliberately doesn't penalize
    by-design-unresolved rows.
    """
    return all(r.passed for r in rows)


def _to_report_rows(rows: list[ScenarioRow]):
    """Adapt :class:`ScenarioRow` into the agent rollup's expected shape.

    :func:`agent.reliability_report.summarize_scenarios` accepts
    :class:`agent.reliability_report.ScenarioResult` rows. The CLI's
    readback row reuses the same names but drops the ``k`` field
    (every scenario is single-shot in the CLI run) and adds
    ``wrong_side_effect_count`` / ``recovery_steps`` for operator
    readability.
    """
    out = []
    for r in rows:
        out.append(
            ScenarioResult(
                scenario=r.scenario,
                passed=r.passed,
                k=1,
                side_effects_correct=(r.wrong_side_effect_count == 0),
                unresolved=r.unresolved,
            )
        )
    return out


def cmd_reliability(args: argparse.Namespace) -> int:
    """Dispatch the ``hermes reliability check`` command.

    Returns the exit code so ``main.py`` can ``return`` it without
    funnelling through SystemExit (tests in this module assert on
    the return value directly).
    """
    if getattr(args, "reliability_command", None) != "check":
        raise ReliabilityError(
            "usage: hermes reliability check [--json]",
            code=RELIABILITY_INVALID_USAGE,
        )

    json_mode = bool(getattr(args, "json", False))
    with tempfile.TemporaryDirectory(prefix="hermes-reliability-") as tmp:
        rows = run_all_scenarios(Path(tmp))

    scorecard = summarize_scenarios(_to_report_rows(rows))

    if json_mode:
        payload = {
            "summary": scorecard,
            "scenarios": _rows_as_dicts(rows),
        }
        sys.stdout.write(json.dumps(payload, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human_table(_rows_as_dicts(rows)))
        sys.stdout.write("\n")
        sys.stdout.write(
            "passed={passed}/{total} pass_rate={pass_rate:.0%} "
            "unresolved={unresolved} wrong_side_effects={wrong_side_effects}\n".format(
                **scorecard
            )
        )

    if not _all_passed(rows):
        return RELIABILITY_SCENARIO_FAILURE
    return RELIABILITY_SUCCESS


__all__ = [
    "RELIABILITY_SUCCESS",
    "RELIABILITY_SCENARIO_FAILURE",
    "RELIABILITY_INVALID_USAGE",
    "ReliabilityError",
    "build_reliability_parser",
    "cmd_reliability",
    "render_human_table",
]
