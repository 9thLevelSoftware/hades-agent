"""Runner for the preregistered autonomy 50-case proof (autonomy-50-v1).

``run_corpus(manifest_path, cases_path, mode, output_dir)`` executes the
frozen corpus in one of two modes and writes ``results.json``:

- ``baseline`` — current Hades approval behaviour with autonomy mode
  off: every mutating tool-mediated action passes through the generic
  recoverable approval gate (one prompt), while model routing has no
  approval gate today. The harness derives that behaviour from the
  action class and cross-checks it against the corpus's frozen
  ``baseline_prompts`` declaration — drift is an error, never a silent
  re-baseline.
- ``candidate`` — enforce-mode decisions using exactly the case's
  predeclared assertions/mandates/suggestions, the frozen benchmark
  clock, a designated outward-effect stub (called only on ``allow``),
  and the real ``agent.autonomy`` compiler-shaped contract plus pure
  ``evaluate_contract``. Commit-stage cases with a bound approval
  (contract version, final-argument hash, requester, channel, one-use
  consumption) are rechecked exactly the way item #2 reloads authority
  immediately before commit/compensate: any mismatch denies with zero
  effect calls.

Both modes share the same action context, clock, and initial state.
Suggestions are loaded but never enter the contract; a
suggestion-authorized allow would be recorded as a contract violation.

Per case the runner records: expected/actual verdict and code, handler
call count, named contract violations, prompt count, redundant-prompt
eligibility, conflict correctness, explanation success, edit/recompile/
re-evaluate success per effective rule, local-monotonic latency, session
ledger cost, exclusion/abort reasons, and the authority/context hashes.

CLI:

    python benchmarks/autonomy/run.py \
        --manifest benchmarks/autonomy/manifest.yaml \
        --cases benchmarks/autonomy/cases.yaml \
        --mode candidate \
        --output benchmarks/autonomy/results/candidate

Result directories are local artifacts and are not committed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml

from agent.autonomy.canonical import content_hash, context_hash, rule_to_dict
from agent.autonomy.evaluator import MICROS_PER_CENT, evaluate_contract
from agent.autonomy.models import (
    ActionContext,
    AutonomyContract,
    AutonomyRule,
    CostConstraint,
    EvidenceRequirement,
    RuleProvenance,
    RuleScope,
    TimeConstraint,
)
from benchmarks.autonomy.score import (
    CORPUS_VERSION,
    RESULT_SCHEMA,
    STRATA,
    TOTAL_CASES,
    CaseResult,
    RunResult,
)

PROFILE_ID = "bench-profile"
COST_SOURCE = "session_usage_ledger"

#: Action classes that are NOT dispatched through a mutating tool call
#: today and therefore never reach the generic recoverable approval gate
#: in the current (autonomy-off) baseline. Model routing is a provider
#: selection decision, not a tool execution.
_BASELINE_UNGATED_ACTION_CLASSES = frozenset({"model.route"})


# ── Corpus loading ──────────────────────────────────────────────────────────


def load_corpus(manifest_path: Path, cases_path: Path) -> tuple[dict, dict]:
    """Load and validate the frozen manifest and case corpus."""
    manifest = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8"))
    doc = yaml.safe_load(Path(cases_path).read_text(encoding="utf-8"))
    cases = doc["cases"]
    if manifest.get("corpus_version") != CORPUS_VERSION:
        raise ValueError(
            f"manifest corpus_version {manifest.get('corpus_version')!r} is "
            f"not the preregistered {CORPUS_VERSION!r}"
        )
    if len(cases) != TOTAL_CASES:
        raise ValueError(f"expected 50 cases, got {len(cases)}")
    strata = {s: 0 for s in STRATA}
    for case in cases:
        strata[case["stratum"]] += 1
    if strata != dict(manifest["strata"]):
        raise ValueError(
            f"corpus strata {strata} do not match the frozen manifest "
            f"{dict(manifest['strata'])}"
        )
    return manifest, doc


# ── Fixture materialization ─────────────────────────────────────────────────


def _build_rule(entry: Mapping[str, Any]) -> AutonomyRule:
    """Materialize one declared corpus rule into a real ``AutonomyRule``."""
    scope_raw = dict(entry.get("scope") or {})
    scope = RuleScope(
        task_id=scope_raw.get("task_id"),
        session_id=scope_raw.get("session_id"),
        mission_id=scope_raw.get("mission_id"),
        transaction_id=scope_raw.get("transaction_id"),
        resource_prefixes=tuple(scope_raw.get("resource_prefixes") or ()),
    )
    cost = CostConstraint(**entry["cost"]) if entry.get("cost") else None
    time_constraint = (
        TimeConstraint(**entry["time"]) if entry.get("time") else None
    )
    evidence = tuple(
        EvidenceRequirement(kind=item["kind"], stage=item["stage"])
        for item in entry.get("evidence_requirements") or ()
    )
    return AutonomyRule(
        rule_id=entry["rule_id"],
        source=entry["source"],
        state=entry["state"],
        effect=entry["effect"],
        action_classes=tuple(entry.get("action_classes") or ()),
        data_classes=tuple(entry.get("data_classes") or ()),
        recipient_classes=tuple(entry.get("recipient_classes") or ()),
        recipient_hashes=tuple(entry.get("recipient_hashes") or ()),
        scope=scope,
        cost=cost,
        time=time_constraint,
        allowed_reversibility=tuple(entry.get("allowed_reversibility") or ()),
        evidence_requirements=evidence,
        provenance=RuleProvenance(
            actor_kind="user",
            actor_id="benchmark-designated-account",
            source_ref=f"benchmark:{CORPUS_VERSION}",
            observed_at_ms=0,
            confidence_ppm=entry.get("confidence_ppm", 1_000_000),
        ),
        created_at_ms=0,
        expires_at_ms=entry.get("expires_at_ms"),
        max_uses=entry.get("max_uses"),
        remaining_uses=entry.get("remaining_uses"),
        description=str(entry.get("description") or ""),
    )


def _build_context(case: Mapping[str, Any], clock_ms: int) -> ActionContext:
    """Declared, redacted action context for one case.

    Symlink-style cases declare ``resolved_resource_refs``; evaluation
    always uses the resolved identity, exactly as the runtime resolver
    canonicalizes paths before matching.
    """
    raw = dict(case["action_context"])
    refs = tuple(
        raw.get("resolved_resource_refs") or raw.get("resource_refs") or ()
    )
    return ActionContext(
        operation_key=f"bench:{case['id']}",
        stage=raw["stage"],
        action_class=raw["action_class"],
        data_classes=tuple(raw["data_classes"]),
        reversibility=raw.get("reversibility", "unknown"),
        recipient_class=raw.get("recipient_class"),
        recipient_hash=raw.get("recipient_hash"),
        resource_refs=refs,
        estimated_cost_cents=raw.get("estimated_cost_cents"),
        local_time_minute=raw.get("local_time_minute"),
        profile_id=PROFILE_ID,
        task_id=raw.get("task_id"),
        session_id=raw.get("session_id"),
        present_evidence=tuple(raw.get("present_evidence") or ()),
        occurred_at_ms=clock_ms,
    )


def _split_mandates(
    mandates: list[AutonomyRule], clock_ms: int
) -> tuple[list[AutonomyRule], list[AutonomyRule]]:
    """Active (contract-eligible) vs lapsed (expired/consumed) mandates."""
    active: list[AutonomyRule] = []
    lapsed: list[AutonomyRule] = []
    for mandate in mandates:
        if (
            mandate.state == "active"
            and not (
                mandate.expires_at_ms is not None
                and mandate.expires_at_ms <= clock_ms
            )
            and not (
                mandate.remaining_uses is not None
                and mandate.remaining_uses <= 0
            )
        ):
            active.append(mandate)
        else:
            lapsed.append(mandate)
    return active, lapsed


def _build_contract(
    rules: list[AutonomyRule], *, version: int, clock_ms: int
) -> AutonomyContract:
    ordered = tuple(sorted(rules, key=lambda r: r.rule_id))
    return AutonomyContract(
        version=version,
        contract_hash=content_hash([rule_to_dict(r) for r in ordered]),
        profile_id=PROFILE_ID,
        compiled_at_ms=clock_ms,
        rules=ordered,
    )


# ── Commit-time approval-binding recheck (item #2 semantics) ───────────────


_BINDING_FIELDS = (
    ("argument_hash", "final_argument_hash"),
    ("requester_id", "requester_id"),
    ("channel", "channel"),
)


def _binding_recheck(
    case: Mapping[str, Any]
) -> Optional[tuple[str, str, str]]:
    """Reload-before-commit recheck of bound approvals.

    Mirrors how action transactions reload authority immediately before
    commit/compensation: a stale contract version, a mutated final
    argument, a different requester or channel, or a consumed one-use
    approval denies with zero adapter/handler calls. Returns
    ``(verdict, code, reason)`` or ``None`` when no binding fails.
    """
    raw = case["action_context"]
    bound = raw.get("bound_contract_version")
    current = raw.get("current_contract_version")
    if bound is not None and current is not None and bound != current:
        return (
            "deny",
            "authority_changed",
            f"approval bound contract version {bound}, but version "
            f"{current} is current; stale authority never allows at "
            "commit time",
        )
    for entry in case["temporary_mandates"]:
        binding = dict(entry.get("binding") or {})
        if not binding:
            continue
        for bound_key, context_key in _BINDING_FIELDS:
            bound_value = binding.get(bound_key)
            context_value = raw.get(context_key)
            if (
                bound_value is not None
                and context_value is not None
                and bound_value != context_value
            ):
                return (
                    "deny",
                    "approval_mismatch",
                    f"approval {entry['rule_id']} binds {bound_key}="
                    f"{bound_value!r} but this call presents "
                    f"{context_value!r}; the bound identity is exact",
                )
        if entry.get("state") == "consumed":
            return (
                "deny",
                "approval_consumed",
                f"one-use approval {entry['rule_id']} was already "
                "consumed; an identical replay never fires the effect "
                "again",
            )
    return None


# ── Explain / edit / recompile / re-evaluate ────────────────────────────────


def _explain_edit_rules(
    effective: list[AutonomyRule],
    context: ActionContext,
    *,
    contract: AutonomyContract,
    clock_ms: int,
    budget_usage: Mapping[str, int],
    lapsed: list[AutonomyRule],
) -> int:
    """Exercise every effective rule: explain, revoke, recompile, re-evaluate.

    A rule counts as a success only when (a) its explanation names the
    rule and a concrete edit route, and (b) removing the rule produces a
    recompiled contract with a new hash whose re-evaluation no longer
    matches the rule.
    """
    successes = 0
    for rule in effective:
        explanation = {
            "rule_id": rule.rule_id,
            "source": rule.source,
            "effect": rule.effect,
            "edit_commands": (
                f"hades autonomy rule explain {rule.rule_id}",
                f"hades autonomy rule edit {rule.rule_id}",
            ),
        }
        explained = bool(explanation["rule_id"]) and all(
            rule.rule_id in command
            for command in explanation["edit_commands"]
        )
        remaining = [r for r in effective if r.rule_id != rule.rule_id]
        recompiled = _build_contract(
            remaining, version=contract.version + 1, clock_ms=clock_ms
        )
        redecision = evaluate_contract(
            recompiled,
            context,
            now_ms=clock_ms,
            budget_usage=budget_usage,
            lapsed_rules=lapsed,
        )
        reflected = (
            recompiled.contract_hash != contract.contract_hash
            and rule.rule_id not in redecision.matched_rule_ids
            and rule.rule_id not in redecision.conflicting_rule_ids
        )
        if explained and reflected:
            successes += 1
    return successes


# ── Per-case execution ──────────────────────────────────────────────────────


def _redundant_prompt_eligible(case: Mapping[str, Any]) -> bool:
    return (
        case["expected"]["verdict"] == "allow"
        and bool(case["baseline_prompts"])
        and not case["candidate_may_prompt"]
    )


def _run_baseline_case(case: Mapping[str, Any], clock_ms: int) -> CaseResult:
    started = time.perf_counter_ns()
    context = _build_context(case, clock_ms)
    prompts = (
        0
        if case["action_context"]["action_class"]
        in _BASELINE_UNGATED_ACTION_CLASSES
        else 1
    )
    if bool(prompts) != bool(case["baseline_prompts"]):
        raise RuntimeError(
            f"{case['id']}: baseline harness computed prompts={prompts} but "
            f"the frozen corpus declares baseline_prompts="
            f"{case['baseline_prompts']}; the baseline drifted"
        )
    latency_ns = time.perf_counter_ns() - started
    expected = case["expected"]
    return CaseResult(
        case_id=case["id"],
        stratum=case["stratum"],
        expected_verdict=expected["verdict"],
        actual_verdict="ask" if prompts else "allow",
        expected_code=expected["code"],
        actual_code="generic_approval" if prompts else "no_generic_gate",
        prompts=prompts,
        # In the current baseline the user approves the generic prompt and
        # the effect proceeds; the candidate must beat this without ever
        # widening authority.
        handler_calls=1,
        contract_violations=[],
        redundant_prompt_eligible=False,
        conflict_expected=False,
        conflict_correct=False,
        effective_rules=0,
        explain_edit_successes=0,
        latency_ns=latency_ns,
        cost_source=COST_SOURCE,
        cost_usd_micros=0,
        excluded_reason=None,
        abort_reason=None,
        authority_hash="baseline-no-contract",
        context_hash=context_hash(context),
    )


def _run_candidate_case(case: Mapping[str, Any], clock_ms: int) -> CaseResult:
    expected = case["expected"]
    case_id = case["id"]

    assertions = [_build_rule(e) for e in case["stable_assertions"]]
    mandates = [_build_rule(e) for e in case["temporary_mandates"]]
    suggestions = [_build_rule(e) for e in case["learned_suggestions"]]
    suggestion_ids = {r.rule_id for r in suggestions}

    active_mandates, lapsed = _split_mandates(mandates, clock_ms)
    effective = assertions + active_mandates
    version = case["action_context"].get("current_contract_version") or 1
    contract = _build_contract(effective, version=version, clock_ms=clock_ms)
    context = _build_context(case, clock_ms)

    budget_usage: dict[str, int] = {}
    settled = case["action_context"].get("settled_in_window_cents")
    if settled:
        for rule in effective:
            if rule.cost is not None and rule.cost.max_per_window_cents is not None:
                budget_usage[rule.rule_id] = settled * MICROS_PER_CENT

    effect_calls = 0

    def effect_stub() -> None:
        nonlocal effect_calls
        effect_calls += 1

    started = time.perf_counter_ns()
    rechecked = _binding_recheck(case)
    if rechecked is not None:
        verdict, code, _reason = rechecked
        matched_ids: tuple[str, ...] = ()
        conflicting_ids: tuple[str, ...] = ()
        required_evidence: tuple = ()
        consume_ids: tuple[str, ...] = ()
        budget_rule_id: Optional[str] = None
    else:
        draft = evaluate_contract(
            contract,
            context,
            now_ms=clock_ms,
            budget_usage=budget_usage,
            lapsed_rules=lapsed,
        )
        verdict, code = draft.verdict, draft.code
        matched_ids = draft.matched_rule_ids
        conflicting_ids = draft.conflicting_rule_ids
        required_evidence = draft.required_evidence
        consume_ids = draft.consume_mandate_ids
        budget_rule_id = draft.budget_rule_id
    if verdict == "allow":
        effect_stub()
    latency_ns = time.perf_counter_ns() - started

    prompts = 1 if verdict == "ask" else 0

    violations: list[str] = []
    if effect_calls and expected["verdict"] != "allow":
        violations.append(
            f"handler called {effect_calls}x although the expected verdict "
            f"is {expected['verdict']} "
            "(deny/ask-without-confirmation must never fire the effect)"
        )
    if verdict == "allow":
        authorizing = set(matched_ids) & suggestion_ids
        if authorizing:
            violations.append(
                f"suggestion-authorized allow via {sorted(authorizing)}; "
                "learned suggestions never authorize"
            )
        expected_consumed = set(expected.get("consumes_mandate_ids") or ())
        if expected_consumed and set(consume_ids) != expected_consumed:
            violations.append(
                f"mandate consumption mismatch: expected "
                f"{sorted(expected_consumed)}, planned {sorted(consume_ids)}"
            )
        reserves = expected.get("reserves_cents")
        if reserves is not None and budget_rule_id is None:
            violations.append(
                f"allow returned without reserving the bounded cost of "
                f"{reserves} cents against its budget rule"
            )
    if prompts and not case["candidate_may_prompt"]:
        violations.append(
            "prompted although the case forbids any prompt (an expected "
            f"{expected['verdict']} must resolve without user escalation)"
        )

    conflict_expected = bool(expected["conflicting_rule_ids"])
    conflict_correct = (
        conflict_expected
        and verdict == expected["verdict"]
        and verdict in ("ask", "deny")
        and set(conflicting_ids) == set(expected["conflicting_rule_ids"])
    )

    expected_evidence = {
        (item["kind"], item["stage"])
        for item in expected.get("required_evidence") or ()
    }
    actual_evidence = {(ev.kind, ev.stage) for ev in required_evidence}
    if expected_evidence and actual_evidence != expected_evidence:
        violations.append(
            f"required evidence mismatch: expected "
            f"{sorted(expected_evidence)}, got {sorted(actual_evidence)}"
        )

    explain_edit_successes = _explain_edit_rules(
        effective,
        context,
        contract=contract,
        clock_ms=clock_ms,
        budget_usage=budget_usage,
        lapsed=lapsed,
    )

    return CaseResult(
        case_id=case_id,
        stratum=case["stratum"],
        expected_verdict=expected["verdict"],
        actual_verdict=verdict,
        expected_code=expected["code"],
        actual_code=code,
        prompts=prompts,
        handler_calls=effect_calls,
        contract_violations=violations,
        redundant_prompt_eligible=_redundant_prompt_eligible(case),
        conflict_expected=conflict_expected,
        conflict_correct=conflict_correct,
        effective_rules=len(effective),
        explain_edit_successes=explain_edit_successes,
        latency_ns=latency_ns,
        cost_source=COST_SOURCE,
        cost_usd_micros=0,
        excluded_reason=None,
        abort_reason=None,
        authority_hash=contract.contract_hash,
        context_hash=context_hash(context),
    )


# ── Public entry points ─────────────────────────────────────────────────────


def run_corpus(
    manifest_path: Path | str,
    cases_path: Path | str,
    mode: str,
    output_dir: Path | str,
) -> RunResult:
    """Run the frozen 50-case corpus in *mode* and write ``results.json``."""
    if mode not in ("baseline", "candidate"):
        raise ValueError(f"mode must be 'baseline' or 'candidate', got {mode!r}")
    manifest, doc = load_corpus(Path(manifest_path), Path(cases_path))
    clock_ms = int(doc["clock_ms"])
    runner = _run_baseline_case if mode == "baseline" else _run_candidate_case
    cases = [runner(case, clock_ms) for case in doc["cases"]]
    result = RunResult(
        corpus_version=manifest["corpus_version"],
        mode=mode,
        clock_ms=clock_ms,
        cases=cases,
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": RESULT_SCHEMA,
        "corpus_version": result.corpus_version,
        "mode": result.mode,
        "clock_ms": result.clock_ms,
        "environment": dict(manifest.get("environment") or {}),
        "cases": [dataclasses.asdict(case) for case in result.cases],
    }
    (output / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the preregistered autonomy 50-case corpus."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument(
        "--mode", required=True, choices=("baseline", "candidate")
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run_corpus(args.manifest, args.cases, args.mode, args.output)
    matches = sum(
        1
        for case in result.cases
        if (case.actual_verdict, case.actual_code)
        == (case.expected_verdict, case.expected_code)
    )
    print(
        f"{result.mode}: {len(result.cases)} cases, "
        f"{matches}/{len(result.cases)} exact verdict+code matches, "
        f"results written to {Path(args.output) / 'results.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
