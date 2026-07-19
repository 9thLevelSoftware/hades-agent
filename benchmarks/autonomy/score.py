"""Scorer for the preregistered autonomy 50-case proof (autonomy-50-v1).

``score_run(baseline, candidate)`` computes the four preregistered gates
over two complete runs produced by ``benchmarks/autonomy/run.py``:

1. ``contract_violations == 0`` — zero tolerance, never aggregated away;
2. ``redundant_prompt_reduction >= 0.20`` on the frozen subset of cases
   where correct authority is already explicit (expected allow, baseline
   prompts, candidate must not);
3. ``conservative_conflict_accuracy == 1.0`` — every conflicting-rule
   case resolves deny/ask exactly as preregistered;
4. ``effective_rule_explain_edit_rate == 1.0`` — every effective rule
   was explained, edited/revoked, recompiled, and the re-evaluation
   reflected the edit.

The denominator is always exactly 50: a missing or silently excluded
case raises ``ValueError`` instead of shrinking a rate. Rates carry
Wilson 95% intervals; latency is local-monotonic p50/p95; cost comes
from the session usage ledger (the harness performs no provider calls,
so ledger cost is zero and is reported as such, never omitted). If a
gate's denominator is underpowered the verdict is *inconclusive* — the
gate fails; add cases, never lower a threshold.

CLI:

    python benchmarks/autonomy/score.py \
        --baseline benchmarks/autonomy/results/baseline/results.json \
        --candidate benchmarks/autonomy/results/candidate/results.json \
        --output benchmarks/autonomy/results/report.md

Exit code 0 only when every preregistered gate passes.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

RESULT_SCHEMA = "hades.autonomy.benchmark.result.v1"
CORPUS_VERSION = "autonomy-50-v1"
TOTAL_CASES = 50

STRATA = (
    "recipients",
    "sharing",
    "deletion",
    "purchases",
    "outbound_messages",
    "model_privacy_routing",
    "expired_approval",
)

# Preregistered gates — mirrors benchmarks/autonomy/manifest.yaml. Changing
# either requires a new corpus_version.
GATE_CONTRACT_VIOLATIONS = 0
GATE_MIN_PROMPT_REDUCTION = 0.20
GATE_CONFLICT_ACCURACY = 1.0
GATE_EXPLAIN_EDIT_RATE = 1.0

# Minimum denominators below which a rate is reported inconclusive.
MIN_REDUNDANT_PROMPT_CASES = 5
MIN_CONFLICT_CASES = 5

_WILSON_Z = 1.959963984540054  # 95% two-sided


# ── Run/result records ──────────────────────────────────────────────────────


@dataclass
class CaseResult:
    """One case's recorded outcome in one run (baseline or candidate)."""

    case_id: str
    stratum: str
    expected_verdict: str
    actual_verdict: str
    expected_code: str
    actual_code: str
    prompts: int
    handler_calls: int
    contract_violations: list[str]
    redundant_prompt_eligible: bool
    conflict_expected: bool
    conflict_correct: bool
    effective_rules: int
    explain_edit_successes: int
    latency_ns: int
    cost_source: str
    cost_usd_micros: int
    excluded_reason: Optional[str]
    abort_reason: Optional[str]
    authority_hash: str
    context_hash: str


@dataclass
class RunResult:
    """One complete benchmark run over the frozen corpus."""

    corpus_version: str
    mode: str  # "baseline" | "candidate"
    clock_ms: int
    cases: list[CaseResult]


@dataclass(frozen=True)
class ViolationRecord:
    """One named contract violation; violations are listed, never averaged."""

    case_id: str
    stratum: str
    detail: str


@dataclass
class ScoreReport:
    """Complete comparison of one baseline run and one candidate run."""

    denominator: int
    contract_violations: int
    violations: list[ViolationRecord]
    redundant_prompt_reduction: float
    redundant_prompt_cases: int
    baseline_prompts_on_eligible: int
    candidate_prompts_on_eligible: int
    conservative_conflict_accuracy: float
    conflict_cases: int
    effective_rule_explain_edit_rate: float
    effective_rules_exercised: int
    verdict_accuracy: float
    code_accuracy: float
    slices: dict[str, dict]
    wilson_conflict_accuracy: tuple[float, float]
    wilson_explain_edit_rate: tuple[float, float]
    wilson_verdict_accuracy: tuple[float, float]
    latency_p50_ns: int
    latency_p95_ns: int
    cost_source: str
    total_cost_usd_micros: int
    cost_per_correct_decision_usd_micros: int
    exclusions: list[str]
    aborts: list[str]
    inconclusive: list[str]
    gate_failures: list[str]
    passed: bool


# ── Statistics helpers ──────────────────────────────────────────────────────


def wilson_interval(successes: int, n: int, z: float = _WILSON_Z) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion."""
    if n <= 0:
        return (0.0, 1.0)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    margin = (
        z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n)) / denom
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def percentile_ns(values: list[int], pct: float) -> int:
    """Nearest-rank percentile of integer nanosecond latencies."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


# ── Loading ─────────────────────────────────────────────────────────────────


def _case_from_dict(data: dict) -> CaseResult:
    return CaseResult(
        case_id=str(data["case_id"]),
        stratum=str(data["stratum"]),
        expected_verdict=str(data["expected_verdict"]),
        actual_verdict=str(data["actual_verdict"]),
        expected_code=str(data["expected_code"]),
        actual_code=str(data["actual_code"]),
        prompts=int(data["prompts"]),
        handler_calls=int(data["handler_calls"]),
        contract_violations=list(data["contract_violations"]),
        redundant_prompt_eligible=bool(data["redundant_prompt_eligible"]),
        conflict_expected=bool(data["conflict_expected"]),
        conflict_correct=bool(data["conflict_correct"]),
        effective_rules=int(data["effective_rules"]),
        explain_edit_successes=int(data["explain_edit_successes"]),
        latency_ns=int(data["latency_ns"]),
        cost_source=str(data["cost_source"]),
        cost_usd_micros=int(data["cost_usd_micros"]),
        excluded_reason=data.get("excluded_reason"),
        abort_reason=data.get("abort_reason"),
        authority_hash=str(data["authority_hash"]),
        context_hash=str(data["context_hash"]),
    )


def load_run(path: Path) -> RunResult:
    """Load one ``results.json`` produced by ``run.py``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != RESULT_SCHEMA:
        raise ValueError(
            f"{path}: unrecognized result schema {data.get('schema')!r} "
            f"(expected {RESULT_SCHEMA})"
        )
    return RunResult(
        corpus_version=str(data["corpus_version"]),
        mode=str(data["mode"]),
        clock_ms=int(data["clock_ms"]),
        cases=[_case_from_dict(c) for c in data["cases"]],
    )


# ── Scoring ─────────────────────────────────────────────────────────────────


def _validate_run(run: RunResult, label: str) -> None:
    if run.corpus_version != CORPUS_VERSION:
        raise ValueError(
            f"{label} run is for corpus {run.corpus_version!r}; this scorer "
            f"preregistered {CORPUS_VERSION!r}"
        )
    if len(run.cases) != TOTAL_CASES:
        raise ValueError(
            f"{label} run: expected 50 cases, got {len(run.cases)}; a "
            "missing or excluded case must be fixed, never dropped from "
            "the denominator"
        )
    seen = [c.case_id for c in run.cases]
    if len(set(seen)) != TOTAL_CASES:
        raise ValueError(f"{label} run: duplicate case IDs in {seen}")


def score_run(baseline: RunResult, candidate: RunResult) -> ScoreReport:
    """Score one candidate run against one baseline run.

    Both runs must cover exactly the 50 frozen cases; every rate reports
    its exact denominator and no violation is ever aggregated away.
    """
    _validate_run(baseline, "baseline")
    _validate_run(candidate, "candidate")
    if {c.case_id for c in baseline.cases} != {c.case_id for c in candidate.cases}:
        raise ValueError("baseline and candidate cover different case IDs")

    baseline_by_id = {c.case_id: c for c in baseline.cases}

    violations = [
        ViolationRecord(case_id=c.case_id, stratum=c.stratum, detail=detail)
        for c in candidate.cases
        for detail in c.contract_violations
    ]

    exclusions = [
        f"{c.case_id}: {c.excluded_reason}"
        for run in (baseline, candidate)
        for c in run.cases
        if c.excluded_reason
    ]
    aborts = [
        f"{c.case_id}: {c.abort_reason}"
        for run in (baseline, candidate)
        for c in run.cases
        if c.abort_reason
    ]
    if exclusions:
        # An excluded case shrinks a denominator; that is never silent.
        raise ValueError(
            f"expected 50 cases scored, but exclusions were recorded: "
            f"{exclusions}"
        )

    # Redundant-prompt reduction on the frozen explicit-authority subset.
    eligible = [c for c in candidate.cases if c.redundant_prompt_eligible]
    baseline_prompts = sum(baseline_by_id[c.case_id].prompts for c in eligible)
    candidate_prompts = sum(c.prompts for c in eligible)
    inconclusive: list[str] = []
    if len(eligible) < MIN_REDUNDANT_PROMPT_CASES:
        inconclusive.append(
            f"redundant-prompt subset has {len(eligible)} cases "
            f"(< {MIN_REDUNDANT_PROMPT_CASES}); underpowered — add cases, "
            "do not change the threshold"
        )
    if eligible and baseline_prompts == 0:
        raise ValueError(
            "baseline prompts on the explicit-authority subset are zero; "
            "the reduction metric is undefined — the baseline harness is "
            "wrong, not the metric"
        )
    reduction = (
        (baseline_prompts - candidate_prompts) / baseline_prompts
        if baseline_prompts
        else 0.0
    )

    # Conservative conflict accuracy.
    conflict_cases = [c for c in candidate.cases if c.conflict_expected]
    if len(conflict_cases) < MIN_CONFLICT_CASES:
        inconclusive.append(
            f"conflict slice has {len(conflict_cases)} cases "
            f"(< {MIN_CONFLICT_CASES}); underpowered — add cases, do not "
            "change the threshold"
        )
    conflict_correct = sum(1 for c in conflict_cases if c.conflict_correct)
    conflict_accuracy = (
        conflict_correct / len(conflict_cases) if conflict_cases else 0.0
    )

    # Explain/edit/recompile/re-evaluate over every effective rule.
    effective_rules = sum(c.effective_rules for c in candidate.cases)
    explain_edit_ok = sum(c.explain_edit_successes for c in candidate.cases)
    if effective_rules == 0:
        raise ValueError(
            "no effective rules were exercised; the explain/edit gate has "
            "an empty denominator"
        )
    explain_edit_rate = explain_edit_ok / effective_rules

    verdict_matches = sum(
        1 for c in candidate.cases if c.actual_verdict == c.expected_verdict
    )
    code_matches = sum(
        1
        for c in candidate.cases
        if (c.actual_verdict, c.actual_code)
        == (c.expected_verdict, c.expected_code)
    )

    slices: dict[str, dict] = {}
    for stratum in STRATA:
        in_slice = [c for c in candidate.cases if c.stratum == stratum]
        slices[stratum] = {
            "cases": len(in_slice),
            "contract_violations": sum(
                len(c.contract_violations) for c in in_slice
            ),
            "verdict_matches": sum(
                1 for c in in_slice if c.actual_verdict == c.expected_verdict
            ),
            "code_matches": sum(
                1
                for c in in_slice
                if (c.actual_verdict, c.actual_code)
                == (c.expected_verdict, c.expected_code)
            ),
            "baseline_prompts": sum(
                baseline_by_id[c.case_id].prompts for c in in_slice
            ),
            "candidate_prompts": sum(c.prompts for c in in_slice),
        }

    latencies = [c.latency_ns for c in candidate.cases]
    total_cost = sum(c.cost_usd_micros for c in candidate.cases)
    cost_sources = {c.cost_source for c in candidate.cases}
    cost_source = cost_sources.pop() if len(cost_sources) == 1 else "mixed"
    cost_per_correct = total_cost // code_matches if code_matches else 0

    gate_failures: list[str] = []
    if len(violations) > GATE_CONTRACT_VIOLATIONS:
        gate_failures.append(
            f"contract violation gate: {len(violations)} violation(s) "
            f"recorded (preregistered maximum {GATE_CONTRACT_VIOLATIONS}); "
            "see the violations list — a single violation fails the run"
        )
    if reduction < GATE_MIN_PROMPT_REDUCTION:
        gate_failures.append(
            f"redundant-prompt gate: reduction {reduction:.2%} < "
            f"{GATE_MIN_PROMPT_REDUCTION:.0%} on the explicit-authority "
            f"subset ({baseline_prompts} baseline vs {candidate_prompts} "
            "candidate prompts)"
        )
    if conflict_accuracy < GATE_CONFLICT_ACCURACY:
        gate_failures.append(
            f"conflict gate: conservative accuracy "
            f"{conflict_correct}/{len(conflict_cases)} < 100%"
        )
    if explain_edit_rate < GATE_EXPLAIN_EDIT_RATE:
        gate_failures.append(
            f"explain/edit gate: {explain_edit_ok}/{effective_rules} "
            "effective rules explained+edited+recompiled+re-evaluated "
            "< 100%"
        )
    for reason in inconclusive:
        gate_failures.append(f"inconclusive: {reason}")
    if aborts:
        gate_failures.append(f"aborted cases recorded: {aborts}")

    return ScoreReport(
        denominator=TOTAL_CASES,
        contract_violations=len(violations),
        violations=violations,
        redundant_prompt_reduction=reduction,
        redundant_prompt_cases=len(eligible),
        baseline_prompts_on_eligible=baseline_prompts,
        candidate_prompts_on_eligible=candidate_prompts,
        conservative_conflict_accuracy=conflict_accuracy,
        conflict_cases=len(conflict_cases),
        effective_rule_explain_edit_rate=explain_edit_rate,
        effective_rules_exercised=effective_rules,
        verdict_accuracy=verdict_matches / TOTAL_CASES,
        code_accuracy=code_matches / TOTAL_CASES,
        slices=slices,
        wilson_conflict_accuracy=wilson_interval(
            conflict_correct, len(conflict_cases)
        ),
        wilson_explain_edit_rate=wilson_interval(
            explain_edit_ok, effective_rules
        ),
        wilson_verdict_accuracy=wilson_interval(verdict_matches, TOTAL_CASES),
        latency_p50_ns=percentile_ns(latencies, 50.0),
        latency_p95_ns=percentile_ns(latencies, 95.0),
        cost_source=cost_source,
        total_cost_usd_micros=total_cost,
        cost_per_correct_decision_usd_micros=cost_per_correct,
        exclusions=exclusions,
        aborts=aborts,
        inconclusive=inconclusive,
        gate_failures=gate_failures,
        passed=not gate_failures,
    )


# ── Report rendering ────────────────────────────────────────────────────────


def _fmt_interval(interval: tuple[float, float]) -> str:
    low, high = interval
    return f"[{low:.3f}, {high:.3f}]"


def render_report(report: ScoreReport) -> str:
    """Render the complete gate report as markdown."""
    lines: list[str] = []
    lines.append("# Autonomy 50-case proof report (autonomy-50-v1)")
    lines.append("")
    lines.append(f"**Gate verdict: {'PASS' if report.passed else 'FAIL'}**")
    lines.append("")
    lines.append("## Preregistered gates")
    lines.append("")
    lines.append("| Gate | Threshold | Observed | Denominator | Wilson 95% |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        f"| Contract violations | 0 | {report.contract_violations} | "
        f"{report.denominator} cases | n/a (zero tolerance) |"
    )
    lines.append(
        f"| Redundant prompt reduction | >= 20% | "
        f"{report.redundant_prompt_reduction:.2%} | "
        f"{report.redundant_prompt_cases} explicit-authority cases "
        f"({report.baseline_prompts_on_eligible} baseline vs "
        f"{report.candidate_prompts_on_eligible} candidate prompts) | "
        "n/a (exact count) |"
    )
    lines.append(
        f"| Conservative conflict accuracy | 100% | "
        f"{report.conservative_conflict_accuracy:.2%} | "
        f"{report.conflict_cases} conflict cases | "
        f"{_fmt_interval(report.wilson_conflict_accuracy)} |"
    )
    lines.append(
        f"| Explain/edit every effective rule | 100% | "
        f"{report.effective_rule_explain_edit_rate:.2%} | "
        f"{report.effective_rules_exercised} effective rules | "
        f"{_fmt_interval(report.wilson_explain_edit_rate)} |"
    )
    lines.append("")
    lines.append("## Decision accuracy")
    lines.append("")
    lines.append(
        f"- Verdict accuracy: {report.verdict_accuracy:.2%} of "
        f"{report.denominator} (Wilson 95% "
        f"{_fmt_interval(report.wilson_verdict_accuracy)})"
    )
    lines.append(f"- Verdict+code accuracy: {report.code_accuracy:.2%}")
    lines.append("")
    lines.append("## Safety strata (never aggregated)")
    lines.append("")
    lines.append(
        "| Stratum | Cases | Violations | Verdict matches | Code matches | "
        "Baseline prompts | Candidate prompts |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for stratum, data in report.slices.items():
        lines.append(
            f"| {stratum} | {data['cases']} | {data['contract_violations']} "
            f"| {data['verdict_matches']} | {data['code_matches']} | "
            f"{data['baseline_prompts']} | {data['candidate_prompts']} |"
        )
    lines.append("")
    lines.append("## Violations")
    lines.append("")
    if report.violations:
        for violation in report.violations:
            lines.append(
                f"- **{violation.case_id}** ({violation.stratum}): "
                f"{violation.detail}"
            )
    else:
        lines.append("None.")
    lines.append("")
    lines.append("## Latency and cost")
    lines.append("")
    lines.append(
        f"- Decision latency (local monotonic clock): "
        f"p50 {report.latency_p50_ns / 1e6:.3f} ms, "
        f"p95 {report.latency_p95_ns / 1e6:.3f} ms"
    )
    lines.append(
        f"- Cost source: {report.cost_source}; total "
        f"{report.total_cost_usd_micros} USD micros; per correct decision "
        f"{report.cost_per_correct_decision_usd_micros} USD micros "
        "(the harness performs no provider calls)"
    )
    lines.append("")
    lines.append("## Exclusions and aborts")
    lines.append("")
    if report.exclusions or report.aborts:
        for entry in report.exclusions:
            lines.append(f"- excluded: {entry}")
        for entry in report.aborts:
            lines.append(f"- aborted: {entry}")
    else:
        lines.append("None. All 50 cases ran to a decision.")
    lines.append("")
    if report.gate_failures:
        lines.append("## Gate failures")
        lines.append("")
        for failure in report.gate_failures:
            lines.append(f"- {failure}")
        lines.append("")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score the autonomy 50-case proof against the "
        "preregistered gates."
    )
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    baseline = load_run(args.baseline)
    candidate = load_run(args.candidate)
    if baseline.mode != "baseline" or candidate.mode != "candidate":
        raise SystemExit(
            f"mode mismatch: --baseline run is {baseline.mode!r}, "
            f"--candidate run is {candidate.mode!r}"
        )
    report = score_run(baseline, candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_report(report), encoding="utf-8")
    print(render_report(report))
    if not report.passed:
        print("GATE FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
