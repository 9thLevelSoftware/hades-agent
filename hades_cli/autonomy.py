"""Shared CLI surface for the Preferences & Autonomy Center.

One parser, one service, one set of bounded renderers behind every
control surface:

- top-level ``hades autonomy ...`` (``hades_cli/main.py`` argparse
  dispatch through :func:`build_parser` / :func:`autonomy_command`);
- classic ``/autonomy`` (and its ``/authority`` alias) in ``cli.py``
  through :func:`run_slash`;
- programmatic/test invocation through :func:`run_argv`.

Nothing here shells out or re-implements authority semantics: every
verb delegates to :class:`agent.autonomy.AutonomyService`, which owns
the deterministic evaluation, the exact-hash config saga, and the
fail-closed lifecycle rules (learned suggestions never authorize; a
stale or crashed apply never becomes an allow).

Exit codes: 0 success/preview, 2 validation or stale authority,
3 denied/blocked evaluation, 4 storage/recovery failure.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import secrets
import shlex
import sqlite3
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

__all__ = [
    "CliResult",
    "autonomy_command",
    "build_parser",
    "run_argv",
    "run_slash",
]

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_DENIED = 3
EXIT_STORAGE = 4

_MAX_INPUT_BYTES = 1_048_576  # 1 MiB cap on rule/action input files
_MIN_DURATION_MS = 60_000  # 1 minute
_MAX_DURATION_MS = 365 * 86_400_000  # 365 days
_MAX_USES = 10_000
_MIN_AUDIT_LIMIT = 1
_MAX_AUDIT_LIMIT = 500
_DEFAULT_AUDIT_LIMIT = 200
_ACTOR_ID = "cli"

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}

_SLASH_HELP = """autonomy — explain and edit what Hades may do
usage: /autonomy <subcommand> [options]

  status                       contract identity, mode, and rule counts
  list [--effective] [--json]  rules across both layers (or the compiled contract)
  rule show|explain <id>       full explanation with the exact edit route
  rule add|edit|remove ...     preview stable changes; --apply needs the exact hash
  evaluate --file ACTION.yaml  explain/preview a decision (never executes)
  suggestion list|show|accept|reject   confirmed-by-you-only learned suggestions
  mandate add|revoke           bounded task-scoped temporary authority
  audit [--json]               recorded decisions (redacted)
  export --output PATH         redacted portable authority export
  purge-audit --before T --apply   delete settled history
  doctor                       health of config, contract head, and state.db

Alias: /authority. Same grammar as `hades autonomy ...`."""


class _CliUsageError(Exception):
    """Argument/validation failure — renders usage and exits 2."""


class _AutonomyArgumentParser(argparse.ArgumentParser):
    """Argparse that raises instead of calling ``sys.exit`` on errors."""

    def error(self, message: str) -> None:  # noqa: D401 - argparse contract
        raise _CliUsageError(f"{self.format_usage()}{self.prog}: error: {message}")


@dataclass(frozen=True)
class CliResult:
    """One executed autonomy command: exit code, rendered text, payload."""

    exit_code: int
    output: str
    payload: Optional[dict] = None

    @property
    def json(self) -> Optional[dict]:
        return self.payload


@dataclass(frozen=True)
class _Outcome:
    exit_code: int
    payload: dict
    lines: tuple[str, ...]


# ── Bounded input parsing ───────────────────────────────────────────────────


def _usage(message: str) -> _CliUsageError:
    return _CliUsageError(f"error: {message}")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_document(path_text: str) -> dict:
    """Read one UTF-8 YAML/JSON mapping, capped at 1 MiB, failing closed."""
    path = Path(path_text)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _usage(f"cannot read {path}: {exc}") from exc
    if len(data) > _MAX_INPUT_BYTES:
        raise _usage(
            f"{path} is {len(data)} bytes; input files are capped at "
            f"{_MAX_INPUT_BYTES} bytes (1 MiB)"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _usage(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _usage(f"{path} is not valid YAML/JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise _usage(f"{path} must contain a single mapping")
    return loaded


def _parse_duration_ms(text: str) -> int:
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise _usage(
            f"duration {text!r} must be <integer><unit> with unit s/m/h/d "
            "(e.g. 30m, 2h, 7d)"
        )
    duration_ms = int(match.group(1)) * _DURATION_UNIT_MS[match.group(2)]
    if not _MIN_DURATION_MS <= duration_ms <= _MAX_DURATION_MS:
        raise _usage(
            f"duration {text!r} is out of bounds: durations are bounded "
            "between 1 minute and 365 days"
        )
    return duration_ms


def _parse_uses(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if not 1 <= value <= _MAX_USES:
        raise _usage(f"--uses must be between 1 and {_MAX_USES} (got {value})")
    return value


def _parse_iso_ms(text: str) -> int:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _usage(f"{text!r} is not an ISO8601 timestamp: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _parse_stable_rule_file(path_text: str):
    """Parse one rule file into a validated stable ``user_assertion``."""
    from agent.autonomy.compiler import parse_stable_rules

    entry = _load_document(path_text)
    return parse_stable_rules({"stable_rules": [entry]})[0]


_MANDATE_KEYS = frozenset(
    {
        "rule_id",
        "effect",
        "action_classes",
        "data_classes",
        "recipient_classes",
        "recipient_hashes",
        "scope",
        "cost",
        "time",
        "allowed_reversibility",
        "evidence_requirements",
        "description",
        "max_uncertainty_ppm",
    }
)
_MANDATE_SCOPE_KEYS = frozenset(
    {"resource_prefixes", "task_id", "session_id", "mission_id", "transaction_id"}
)


def _parse_mandate_file(path_text: str, *, expires_at_ms: int, max_uses: Optional[int]):
    """Build a bounded, explicitly-confirmed temporary mandate from a file."""
    from agent.autonomy.models import (
        AutonomyRule,
        CostConstraint,
        EvidenceRequirement,
        RuleProvenance,
        RuleScope,
        TimeConstraint,
    )

    entry = _load_document(path_text)
    unknown = set(entry) - _MANDATE_KEYS
    if unknown:
        raise _usage(f"mandate file has unknown key(s): {sorted(unknown)}")
    scope_data = entry.get("scope") or {}
    if not isinstance(scope_data, dict):
        raise _usage("mandate scope must be a mapping")
    unknown_scope = set(scope_data) - _MANDATE_SCOPE_KEYS
    if unknown_scope:
        raise _usage(f"mandate scope has unknown key(s): {sorted(unknown_scope)}")
    now = _now_ms()
    scope_kwargs = {k: v for k, v in scope_data.items() if k != "resource_prefixes"}
    cost = entry.get("cost")
    time_window = entry.get("time")
    return AutonomyRule(
        rule_id=entry.get("rule_id") or f"mandate-{secrets.token_hex(6)}",
        source="temporary_mandate",
        state="active",
        effect=entry.get("effect"),
        action_classes=tuple(entry.get("action_classes") or ()),
        data_classes=tuple(entry.get("data_classes") or ()),
        recipient_classes=tuple(entry.get("recipient_classes") or ()),
        recipient_hashes=tuple(entry.get("recipient_hashes") or ()),
        scope=RuleScope(
            resource_prefixes=tuple(scope_data.get("resource_prefixes") or ()),
            **scope_kwargs,
        ),
        cost=CostConstraint(**dict(cost)) if cost else None,
        time=TimeConstraint(**dict(time_window)) if time_window else None,
        allowed_reversibility=tuple(entry.get("allowed_reversibility") or ()),
        evidence_requirements=tuple(
            EvidenceRequirement(**dict(item))
            for item in entry.get("evidence_requirements") or ()
        ),
        max_uncertainty_ppm=entry.get("max_uncertainty_ppm"),
        provenance=RuleProvenance(
            actor_kind="user",
            actor_id=_ACTOR_ID,
            source_ref="cli:mandate-add",
            observed_at_ms=now,
            confirmed_at_ms=now,
            confidence_ppm=1_000_000,
        ),
        created_at_ms=now,
        expires_at_ms=expires_at_ms,
        max_uses=max_uses,
        remaining_uses=max_uses,
        description=entry.get("description", ""),
    )


_ACTION_KEYS = frozenset(
    {
        "operation_key",
        "action_class",
        "data_classes",
        "reversibility",
        "recipient_class",
        "recipient_hash",
        "resource_refs",
        "estimated_cost_cents",
        "local_time_minute",
        "profile_id",
        "task_id",
        "session_id",
        "mission_id",
        "transaction_id",
        "tool_name",
        "present_evidence",
        "occurred_at_ms",
        "uncertainty_ppm",
    }
)


def _parse_action_file(path_text: str, *, stage: str):
    """Build a declared :class:`ActionContext` from an action file."""
    from agent.autonomy.models import ActionContext

    entry = _load_document(path_text)
    unknown = set(entry) - _ACTION_KEYS - {"stage"}
    if unknown:
        raise _usage(f"action file has unknown key(s): {sorted(unknown)}")
    kwargs: dict[str, Any] = {k: v for k, v in entry.items() if k in _ACTION_KEYS}
    for tuple_key in ("data_classes", "resource_refs", "present_evidence"):
        if tuple_key in kwargs:
            kwargs[tuple_key] = tuple(kwargs[tuple_key] or ())
    # Unclassified content is explicitly unknown — never an empty
    # high-risk field a wildcard could silently match.
    kwargs.setdefault("data_classes", ("unknown",))
    kwargs.setdefault("operation_key", f"cli-eval-{secrets.token_hex(6)}")
    return ActionContext(stage=stage, **kwargs)


# ── Bounded renderers ───────────────────────────────────────────────────────


def _clip(text: str, limit: int = 200) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _edit_command(rule) -> str:
    """The exact command route to change one rule, by source kind."""
    if rule.source == "user_assertion":
        return f"hades autonomy rule edit {rule.rule_id} --file RULE.yaml"
    if rule.source == "learned_suggestion":
        return (
            f"hades autonomy suggestion accept {rule.rule_id} "
            "(--stable | --temporary --expires-in DURATION)"
        )
    return f"hades autonomy mandate revoke {rule.rule_id} --reason TEXT"


def _rule_doc(rule) -> dict[str, Any]:
    """Redacted rule document: labels, hashes, and identifiers only."""
    provenance = rule.provenance
    scope = {
        name: getattr(rule.scope, name)
        for name in ("task_id", "session_id", "mission_id", "transaction_id")
        if getattr(rule.scope, name) is not None
    }
    return {
        "rule_id": rule.rule_id,
        "source": rule.source,
        "state": rule.state,
        "effect": rule.effect,
        "action_classes": list(rule.action_classes),
        "data_classes": list(rule.data_classes),
        "recipient_classes": list(rule.recipient_classes),
        "recipient_hashes": list(rule.recipient_hashes),
        "resource_prefixes": list(rule.scope.resource_prefixes),
        "scope": scope,
        "allowed_reversibility": list(rule.allowed_reversibility),
        "cost": None
        if rule.cost is None
        else {
            "currency": rule.cost.currency,
            "max_per_action_cents": rule.cost.max_per_action_cents,
            "max_per_window_cents": rule.cost.max_per_window_cents,
            "window_ms": rule.cost.window_ms,
        },
        "time": None
        if rule.time is None
        else {
            "window_start_minute": rule.time.window_start_minute,
            "window_end_minute": rule.time.window_end_minute,
            "timezone": rule.time.timezone,
        },
        "evidence_requirements": [
            {"kind": req.kind, "stage": req.stage}
            for req in rule.evidence_requirements
        ],
        "max_uncertainty_ppm": rule.max_uncertainty_ppm,
        "provenance": provenance.actor_kind + ":" + provenance.actor_id
        + (f" ({provenance.source_ref})" if provenance.source_ref else ""),
        "confidence_ppm": provenance.confidence_ppm,
        "created_at_ms": rule.created_at_ms,
        "expires_at_ms": rule.expires_at_ms,
        "max_uses": rule.max_uses,
        "remaining_uses": rule.remaining_uses,
        "description": _clip(rule.description),
        "edit_command": _edit_command(rule),
    }


def _rule_lines(doc: dict[str, Any]) -> list[str]:
    selectors = []
    for key in ("action_classes", "data_classes", "recipient_classes"):
        if doc[key]:
            selectors.append(f"{key.split('_')[0]}={','.join(doc[key])}")
    if doc["recipient_hashes"]:
        selectors.append(f"recipient_hashes={len(doc['recipient_hashes'])}")
    if doc["resource_prefixes"]:
        selectors.append(f"resources={','.join(doc['resource_prefixes'])}")
    bounds = []
    if doc["expires_at_ms"] is not None:
        bounds.append(f"expires_at_ms={doc['expires_at_ms']}")
    if doc["max_uses"] is not None:
        bounds.append(f"uses={doc['remaining_uses']}/{doc['max_uses']}")
    lines = [
        f"{doc['rule_id']}  [{doc['source']}/{doc['state']}]  {doc['effect']}",
        f"  matches: {'; '.join(selectors) or '(no selectors)'}",
        f"  provenance: {doc['provenance']} "
        f"(confidence {doc['confidence_ppm']} ppm)"
        + (f"  {'; '.join(bounds)}" if bounds else ""),
    ]
    if doc["evidence_requirements"]:
        lines.append(
            "  evidence: "
            + ", ".join(
                f"{req['kind']}@{req['stage']}"
                for req in doc["evidence_requirements"]
            )
        )
    if doc["description"]:
        lines.append(f"  note: {doc['description']}")
    lines.append(f"  edit: {doc['edit_command']}")
    return lines


def _preview_doc(preview, *, extra: Optional[dict] = None) -> dict[str, Any]:
    doc = {
        "applied": False,
        "profile_id": preview.profile_id,
        "before_contract_hash": preview.before_contract_hash,
        "after_contract_hash": preview.after_contract_hash,
        "added_rule_ids": list(preview.added_rule_ids),
        "removed_rule_ids": list(preview.removed_rule_ids),
        "changed_rule_ids": list(preview.changed_rule_ids),
        "warnings": list(preview.warnings),
    }
    doc.update(extra or {})
    return doc


def _preview_lines(doc: dict[str, Any], apply_hint_command: str) -> list[str]:
    lines = ["previewed change (not applied):"]
    for key in ("added_rule_ids", "changed_rule_ids", "removed_rule_ids"):
        if doc[key]:
            lines.append(f"  {key.replace('_rule_ids', '')}: {', '.join(doc[key])}")
    for warning in doc["warnings"]:
        lines.append(f"  warning: {warning}")
    lines.append(f"  before contract hash: {doc['before_contract_hash']}")
    lines.append(f"  after contract hash:  {doc['after_contract_hash']}")
    lines.append(
        "  apply with: "
        f"{apply_hint_command} --apply --expected-contract-hash "
        f"{doc['before_contract_hash']}"
    )
    return lines


def _applied_doc(applied, *, extra: Optional[dict] = None) -> dict[str, Any]:
    doc = {
        "applied": True,
        "config_hash": applied.config_hash,
        "contract_version": applied.contract.version,
        "contract_hash": applied.contract.content_hash,
    }
    doc.update(extra or {})
    return doc


def _decision_doc(decision, *, stage: str) -> dict[str, Any]:
    return {
        "verdict": decision.verdict,
        "code": decision.code,
        "reason": _clip(decision.reason, 500),
        "stage": stage,
        "authority_version": decision.authority_version,
        "authority_hash": decision.authority_hash,
        "context_hash": decision.context_hash,
        "matched_rule_ids": list(decision.matched_rule_ids),
        "conflicting_rule_ids": list(decision.conflicting_rule_ids),
        "required_evidence": [
            {"kind": req.kind, "stage": req.stage}
            for req in decision.required_evidence
        ],
        "clarification": None
        if decision.clarification is None
        else {
            "question": decision.clarification.question,
            "choices": list(decision.clarification.choices),
            "code": decision.clarification.code,
        },
        "expires_at_ms": decision.expires_at_ms,
        "edit_targets": list(decision.edit_targets),
    }


# ── Service access ──────────────────────────────────────────────────────────


def _service():
    from agent.autonomy import AutonomyService

    return AutonomyService()


def _autonomy_section() -> dict[str, Any]:
    from agent.autonomy.compiler import validate_autonomy_section
    from agent.autonomy.config_apply import _read_raw_config
    from hades_constants import get_hades_home

    raw = _read_raw_config(get_hades_home() / "config.yaml")
    return dict(validate_autonomy_section(raw.get("autonomy")))


# ── Handlers (each returns an _Outcome) ─────────────────────────────────────


def _cmd_status(args: argparse.Namespace) -> _Outcome:
    service = _service()
    stored = service.current_contract()
    section = _autonomy_section()
    stable = service.list_rules(source="user_assertion")
    mandates = service.list_rules(source="temporary_mandate", states=("active",))
    pending = service.list_rules(
        source="learned_suggestion", states=("awaiting_confirmation",)
    )
    from agent.autonomy.config_apply import pending_apply

    payload = {
        "profile_id": stored.contract.profile_id,
        "mode": section.get("mode", "off"),
        "contract_version": stored.version,
        "contract_hash": stored.content_hash,
        "stable_rules": len(stable),
        "active_mandates": len(mandates),
        "pending_suggestions": len(pending),
        "pending_apply": pending_apply(),
    }
    lines = [
        f"profile: {payload['profile_id']}  mode: {payload['mode']}",
        f"contract: version {payload['contract_version']} "
        f"hash {payload['contract_hash']}",
        f"rules: {payload['stable_rules']} stable, "
        f"{payload['active_mandates']} active mandate(s), "
        f"{payload['pending_suggestions']} suggestion(s) awaiting confirmation",
    ]
    if payload["pending_apply"]:
        lines.append(
            "WARNING: a crashed authority apply awaits recovery; "
            "mutations and evaluation fail closed"
        )
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_list(args: argparse.Namespace) -> _Outcome:
    service = _service()
    payload: dict[str, Any] = {"effective": bool(args.effective)}
    if args.effective:
        stored = service.current_contract()
        rules = stored.contract.rules
        payload["contract_version"] = stored.version
        payload["contract_hash"] = stored.content_hash
    else:
        rules = service.list_rules(
            source=args.source,
            states=(args.state,) if args.state else None,
        )
    docs = [_rule_doc(rule) for rule in rules]
    payload["rules"] = docs
    lines: list[str] = []
    if args.effective:
        lines.append(
            f"effective contract version {payload['contract_version']} "
            f"hash {payload['contract_hash']}"
        )
    if not docs:
        lines.append("(no rules)")
    for doc in docs:
        lines.extend(_rule_lines(doc))
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_rule_show(args: argparse.Namespace) -> _Outcome:
    service = _service()
    explanation = service.explain_rule(args.rule_id)
    doc = _rule_doc(explanation.rule)
    doc.update(
        {
            "layer": explanation.layer,
            "revision": explanation.revision,
            "in_current_contract": explanation.in_current_contract,
            "conflicts_with": list(explanation.conflicts_with),
            "edit_route": list(explanation.edit_route),
            "revoke_route": list(explanation.revoke_route),
        }
    )
    lines = _rule_lines(doc)
    lines.append(
        f"  layer: {doc['layer']}"
        + ("  (in current contract)" if doc["in_current_contract"] else
           "  (NOT in current contract)")
    )
    if doc["conflicts_with"]:
        lines.append(f"  conflicts with: {', '.join(doc['conflicts_with'])}")
    for route in doc["edit_route"]:
        lines.append(f"  edit route: {route}")
    return _Outcome(EXIT_OK, doc, tuple(lines))


def _require_apply_hash(args: argparse.Namespace) -> str:
    if not getattr(args, "expected_contract_hash", None):
        raise _usage("--apply requires --expected-contract-hash HASH")
    return args.expected_contract_hash


def _stable_change(args: argparse.Namespace, change, apply_hint: str) -> _Outcome:
    """Shared preview-then-exact-hash-apply flow for stable rule edits."""
    service = _service()
    preview = service.preview_rule_change(change)
    if not args.apply:
        doc = _preview_doc(preview)
        return _Outcome(EXIT_OK, doc, tuple(_preview_lines(doc, apply_hint)))
    expected = _require_apply_hash(args)
    applied = service.apply_rule_change(preview, expected_contract_hash=expected)
    doc = _applied_doc(applied)
    lines = (
        "applied: contract version "
        f"{doc['contract_version']} hash {doc['contract_hash']}",
    )
    return _Outcome(EXIT_OK, doc, lines)


def _cmd_rule_add(args: argparse.Namespace) -> _Outcome:
    from agent.autonomy.config_apply import ConfigChange

    rule = _parse_stable_rule_file(args.file)
    return _stable_change(
        args,
        ConfigChange(set_rules=(rule,)),
        f"hades autonomy rule add --file {args.file}",
    )


def _cmd_rule_edit(args: argparse.Namespace) -> _Outcome:
    from agent.autonomy.config_apply import ConfigChange

    rule = _parse_stable_rule_file(args.file)
    if rule.rule_id != args.rule_id:
        raise _usage(
            f"rule file names rule_id {rule.rule_id!r} but the command "
            f"targets {args.rule_id!r}"
        )
    existing = {r.rule_id for r in _service().list_rules(source="user_assertion")}
    if args.rule_id not in existing:
        raise _usage(
            f"no stable rule {args.rule_id!r} exists to edit; "
            "use `hades autonomy rule add`"
        )
    return _stable_change(
        args,
        ConfigChange(set_rules=(rule,)),
        f"hades autonomy rule edit {args.rule_id} --file {args.file}",
    )


def _cmd_rule_remove(args: argparse.Namespace) -> _Outcome:
    from agent.autonomy.config_apply import ConfigChange

    return _stable_change(
        args,
        ConfigChange(remove_rule_ids=(args.rule_id,)),
        f"hades autonomy rule remove {args.rule_id}",
    )


def _cmd_evaluate(args: argparse.Namespace) -> _Outcome:
    context = _parse_action_file(args.file, stage=args.stage)
    decision = _service().evaluate(context, consume=False)
    doc = _decision_doc(decision, stage=args.stage)
    lines = [
        f"{doc['verdict']} / {doc['code']}  (stage {doc['stage']}, "
        f"contract v{doc['authority_version']})",
        f"  {doc['reason']}",
    ]
    if doc["matched_rule_ids"]:
        lines.append(f"  matched: {', '.join(doc['matched_rule_ids'])}")
    if doc["conflicting_rule_ids"]:
        lines.append(f"  conflicts: {', '.join(doc['conflicting_rule_ids'])}")
    if doc["required_evidence"]:
        lines.append(
            "  required evidence: "
            + ", ".join(f"{r['kind']}@{r['stage']}" for r in doc["required_evidence"])
        )
    if doc["clarification"] is not None:
        lines.append(f"  question: {doc['clarification']['question']}")
        for choice in doc["clarification"]["choices"]:
            lines.append(f"    - {choice}")
    for target in doc["edit_targets"]:
        lines.append(f"  edit: {target}")
    exit_code = EXIT_DENIED if decision.verdict == "deny" else EXIT_OK
    return _Outcome(exit_code, doc, tuple(lines))


def _cmd_suggestion_list(args: argparse.Namespace) -> _Outcome:
    rules = _service().list_rules(source="learned_suggestion")
    docs = [_rule_doc(rule) for rule in rules]
    payload = {"suggestions": docs, "rules": docs}
    lines: list[str] = []
    if not docs:
        lines.append("(no learned suggestions)")
    for doc in docs:
        lines.extend(_rule_lines(doc))
    lines.append(
        "note: suggestions never authorize; accept one explicitly with "
        "`hades autonomy suggestion accept <id> (--stable | --temporary ...)`"
    )
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_suggestion_show(args: argparse.Namespace) -> _Outcome:
    outcome = _cmd_rule_show(args)
    if outcome.payload.get("source") != "learned_suggestion":
        raise _usage(f"{args.rule_id!r} is not a learned suggestion")
    return outcome


def _cmd_suggestion_accept(args: argparse.Namespace) -> _Outcome:
    if bool(args.stable) == bool(args.temporary):
        raise _usage(
            "exactly one destination is required: --stable or --temporary"
        )
    service = _service()
    if args.temporary:
        if getattr(args, "apply", False):
            raise _usage("--apply belongs to --stable; a temporary mandate commits immediately")
        if not args.expires_in:
            raise _usage("--temporary requires --expires-in DURATION")
        uses = _parse_uses(args.uses)
        expires_at_ms = _now_ms() + _parse_duration_ms(args.expires_in)
        confirmation = service.confirm_suggestion(
            args.suggestion_id,
            destination="mandate",
            actor_id=_ACTOR_ID,
            expires_at_ms=expires_at_ms,
            max_uses=uses,
        )
        payload = {
            "suggestion_id": confirmation.suggestion_id,
            "destination": "mandate",
            "new_rule_id": confirmation.new_rule_id,
            "expires_at_ms": expires_at_ms,
            "max_uses": uses,
        }
        lines = (
            f"created bounded mandate {confirmation.new_rule_id} "
            f"(expires_at_ms={expires_at_ms}"
            + (f", uses={uses}" if uses is not None else "")
            + f") from suggestion {args.suggestion_id}",
        )
        return _Outcome(EXIT_OK, payload, lines)

    if args.uses is not None or args.expires_in:
        raise _usage("--expires-in/--uses belong to --temporary")
    confirmation = service.confirm_suggestion(
        args.suggestion_id, destination="stable", actor_id=_ACTOR_ID
    )
    extra = {
        "suggestion_id": confirmation.suggestion_id,
        "destination": "stable",
        "new_rule_id": confirmation.new_rule_id,
    }
    if not args.apply:
        doc = _preview_doc(confirmation.preview, extra=extra)
        return _Outcome(
            EXIT_OK,
            doc,
            tuple(
                _preview_lines(
                    doc,
                    f"hades autonomy suggestion accept {args.suggestion_id} --stable",
                )
            ),
        )
    expected = _require_apply_hash(args)
    applied = service.apply_rule_change(
        confirmation, expected_contract_hash=expected
    )
    doc = _applied_doc(applied, extra=extra)
    lines = (
        f"confirmed suggestion {args.suggestion_id} as stable rule "
        f"{confirmation.new_rule_id}: contract version "
        f"{doc['contract_version']} hash {doc['contract_hash']}",
    )
    return _Outcome(EXIT_OK, doc, lines)


def _cmd_suggestion_reject(args: argparse.Namespace) -> _Outcome:
    stored = _service().reject_suggestion(args.suggestion_id, actor_id=_ACTOR_ID)
    payload = {
        "suggestion_id": args.suggestion_id,
        "state": stored.rule.state,
        "reason": _clip(args.reason),
    }
    return _Outcome(
        EXIT_OK,
        payload,
        (f"rejected suggestion {args.suggestion_id} ({payload['reason']})",),
    )


def _cmd_mandate_add(args: argparse.Namespace) -> _Outcome:
    uses = _parse_uses(args.uses)
    expires_at_ms = _now_ms() + _parse_duration_ms(args.expires_in)
    rule = _parse_mandate_file(args.file, expires_at_ms=expires_at_ms, max_uses=uses)
    stored = _service().create_mandate(rule)
    payload = _rule_doc(stored.rule)
    payload["revision"] = stored.revision
    return _Outcome(EXIT_OK, payload, tuple(_rule_lines(payload)))


def _cmd_mandate_revoke(args: argparse.Namespace) -> _Outcome:
    stored = _service().revoke_mandate(args.rule_id, actor_id=_ACTOR_ID)
    payload = {
        "rule_id": args.rule_id,
        "state": stored.rule.state,
        "reason": _clip(args.reason),
    }
    return _Outcome(
        EXIT_OK,
        payload,
        (f"mandate {args.rule_id} is now {payload['state']} ({payload['reason']})",),
    )


def _cmd_audit(args: argparse.Namespace) -> _Outcome:
    limit = args.limit
    if not _MIN_AUDIT_LIMIT <= limit <= _MAX_AUDIT_LIMIT:
        raise _usage(
            f"--limit must be between {_MIN_AUDIT_LIMIT} and "
            f"{_MAX_AUDIT_LIMIT} (got {limit})"
        )
    since_ms = _parse_iso_ms(args.since) if args.since else None
    records = _service().list_decisions(limit=limit)
    docs = []
    for record in records:
        if since_ms is not None and record.created_at_ms < since_ms:
            continue
        if args.verdict and record.decision.verdict != args.verdict:
            continue
        doc = _decision_doc(record.decision, stage=record.stage)
        doc.update(
            {
                "decision_id": record.decision.decision_id,
                "operation_key": record.operation_key,
                "created_at_ms": record.created_at_ms,
            }
        )
        docs.append(doc)
    payload = {"decisions": docs, "count": len(docs), "limit": limit}
    lines = [f"{len(docs)} decision(s)"]
    for doc in docs:
        lines.append(
            f"{doc['created_at_ms']}  {doc['verdict']:5s} {doc['code']}  "
            f"stage={doc['stage']} contract=v{doc['authority_version']} "
            f"op={doc['operation_key']}"
        )
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_export(args: argparse.Namespace) -> _Outcome:
    data = _service().export_redacted(include_decisions=args.include_audit)
    output = Path(args.output)
    output.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    payload = {
        "output": str(output),
        "include_audit": bool(args.include_audit),
        "stable_rules": len(data.get("stable_rules", [])),
        "runtime_rules": len(data.get("runtime_rules", [])),
    }
    return _Outcome(
        EXIT_OK,
        payload,
        (
            f"exported redacted authority to {output} "
            f"({payload['stable_rules']} stable, "
            f"{payload['runtime_rules']} runtime rule(s))",
        ),
    )


def _cmd_purge_audit(args: argparse.Namespace) -> _Outcome:
    before_ms = _parse_iso_ms(args.before)
    counts = _service().purge_runtime_history(before_ms=before_ms)
    payload = {"before_ms": before_ms, **counts}
    lines = [f"purged settled history older than {args.before}:"] + [
        f"  {key}: {value}" for key, value in counts.items()
    ]
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_doctor(args: argparse.Namespace) -> _Outcome:
    from agent.autonomy.config_apply import pending_apply
    from hades_constants import get_hades_home

    home = get_hades_home()
    checks: list[str] = []
    payload: dict[str, Any] = {
        "home": str(home),
        "pending_apply": pending_apply(),
        "config_ok": True,
        "db_ok": True,
        "mode": None,
        "contract_version": None,
        "contract_hash": None,
    }
    exit_code = EXIT_OK
    try:
        section = _autonomy_section()
        payload["mode"] = section.get("mode", "off")
        checks.append("config: ok")
    except Exception as exc:
        payload["config_ok"] = False
        checks.append(f"config: INVALID ({_clip(exc)})")
        exit_code = EXIT_VALIDATION
    if payload["pending_apply"]:
        checks.append(
            "apply journal: PENDING — a crashed authority apply awaits "
            "recovery; authority fails closed until it is resolved"
        )
        payload["db_ok"] = False
        exit_code = EXIT_STORAGE
    else:
        try:
            stored = _service().current_contract()
            payload["contract_version"] = stored.version
            payload["contract_hash"] = stored.content_hash
            payload["profile_id"] = stored.contract.profile_id
            checks.append(
                f"contract head: ok (version {stored.version}, "
                f"hash {stored.content_hash})"
            )
        except Exception as exc:
            payload["db_ok"] = False
            checks.append(f"contract head: FAILED ({_clip(exc)})")
            exit_code = EXIT_STORAGE
    payload["checks"] = checks
    return _Outcome(exit_code, payload, tuple(checks))


# ── Parser construction ─────────────────────────────────────────────────────


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _add_apply(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the previewed change (requires the exact current hash)",
    )
    parser.add_argument(
        "--expected-contract-hash",
        metavar="HASH",
        help="Exact current contract hash the apply is conditioned on",
    )


def build_parser(parent_subparsers) -> argparse.ArgumentParser:
    """Attach the full ``autonomy`` grammar to *parent_subparsers*."""
    parser = parent_subparsers.add_parser(
        "autonomy",
        aliases=["authority"],
        help="Explain and edit what Hades may do (allow/ask/deny authority)",
        description=(
            "Preferences & Autonomy Center: one place to inspect, explain, "
            "and edit the profile's authority contract. Changes preview by "
            "default and apply only under the exact current contract hash."
        ),
    )
    sub = parser.add_subparsers(dest="autonomy_command")

    p_status = sub.add_parser("status", help="Contract identity, mode, and rule counts")
    _add_json(p_status)
    p_status.set_defaults(_autonomy_handler=_cmd_status)

    p_list = sub.add_parser("list", help="List rules (both layers, or the compiled contract)")
    p_list.add_argument(
        "--source",
        choices=("user_assertion", "learned_suggestion", "temporary_mandate"),
    )
    p_list.add_argument(
        "--state",
        choices=(
            "active", "awaiting_confirmation", "rejected",
            "revoked", "expired", "consumed",
        ),
    )
    p_list.add_argument(
        "--effective",
        action="store_true",
        help="Only the compiled current contract (authorizing rules)",
    )
    _add_json(p_list)
    p_list.set_defaults(_autonomy_handler=_cmd_list)

    p_rule = sub.add_parser("rule", help="Show, explain, add, edit, or remove one rule")
    rule_sub = p_rule.add_subparsers(dest="rule_command", required=True)
    for verb in ("show", "explain"):
        p_verb = rule_sub.add_parser(verb, help=f"{verb.capitalize()} one rule")
        p_verb.add_argument("rule_id")
        _add_json(p_verb)
        p_verb.set_defaults(_autonomy_handler=_cmd_rule_show)
    p_rule_add = rule_sub.add_parser("add", help="Preview/apply a new stable rule")
    p_rule_add.add_argument("--file", required=True, metavar="RULE.yaml")
    _add_apply(p_rule_add)
    _add_json(p_rule_add)
    p_rule_add.set_defaults(_autonomy_handler=_cmd_rule_add)
    p_rule_edit = rule_sub.add_parser("edit", help="Preview/apply an edit to a stable rule")
    p_rule_edit.add_argument("rule_id")
    p_rule_edit.add_argument("--file", required=True, metavar="RULE.yaml")
    _add_apply(p_rule_edit)
    _add_json(p_rule_edit)
    p_rule_edit.set_defaults(_autonomy_handler=_cmd_rule_edit)
    p_rule_remove = rule_sub.add_parser("remove", help="Preview/apply removal of a stable rule")
    p_rule_remove.add_argument("rule_id")
    _add_apply(p_rule_remove)
    _add_json(p_rule_remove)
    p_rule_remove.set_defaults(_autonomy_handler=_cmd_rule_remove)

    p_eval = sub.add_parser(
        "evaluate", help="Explain/preview a decision for a declared action (never executes)"
    )
    p_eval.add_argument("--file", required=True, metavar="ACTION.yaml")
    p_eval.add_argument("--stage", choices=("explain", "preview"), default="explain")
    _add_json(p_eval)
    p_eval.set_defaults(_autonomy_handler=_cmd_evaluate)

    p_sugg = sub.add_parser("suggestion", help="List, show, accept, or reject learned suggestions")
    sugg_sub = p_sugg.add_subparsers(dest="suggestion_command", required=True)
    p_sugg_list = sugg_sub.add_parser("list", help="List learned suggestions")
    _add_json(p_sugg_list)
    p_sugg_list.set_defaults(_autonomy_handler=_cmd_suggestion_list)
    p_sugg_show = sugg_sub.add_parser("show", help="Show one learned suggestion")
    p_sugg_show.add_argument("rule_id")
    _add_json(p_sugg_show)
    p_sugg_show.set_defaults(_autonomy_handler=_cmd_suggestion_show)
    p_sugg_accept = sugg_sub.add_parser(
        "accept",
        help="Explicitly confirm a suggestion into NEW authority "
        "(--stable or --temporary is required)",
    )
    p_sugg_accept.add_argument("suggestion_id")
    p_sugg_accept.add_argument(
        "--stable", action="store_true",
        help="Confirm as a durable stable rule (preview + exact-hash apply)",
    )
    p_sugg_accept.add_argument(
        "--temporary", action="store_true",
        help="Confirm as a bounded temporary mandate",
    )
    p_sugg_accept.add_argument("--expires-in", metavar="DURATION")
    p_sugg_accept.add_argument("--uses", type=int, metavar="N")
    _add_apply(p_sugg_accept)
    _add_json(p_sugg_accept)
    p_sugg_accept.set_defaults(_autonomy_handler=_cmd_suggestion_accept)
    p_sugg_reject = sugg_sub.add_parser("reject", help="Reject a suggestion (terminal)")
    p_sugg_reject.add_argument("suggestion_id")
    p_sugg_reject.add_argument("--reason", required=True, metavar="TEXT")
    _add_json(p_sugg_reject)
    p_sugg_reject.set_defaults(_autonomy_handler=_cmd_suggestion_reject)

    p_mandate = sub.add_parser("mandate", help="Create or revoke bounded temporary mandates")
    mandate_sub = p_mandate.add_subparsers(dest="mandate_command", required=True)
    p_mandate_add = mandate_sub.add_parser("add", help="Create a bounded temporary mandate")
    p_mandate_add.add_argument("--file", required=True, metavar="RULE.yaml")
    p_mandate_add.add_argument("--expires-in", required=True, metavar="DURATION")
    p_mandate_add.add_argument("--uses", type=int, metavar="N")
    _add_json(p_mandate_add)
    p_mandate_add.set_defaults(_autonomy_handler=_cmd_mandate_add)
    p_mandate_revoke = mandate_sub.add_parser("revoke", help="Revoke an active mandate")
    p_mandate_revoke.add_argument("rule_id")
    p_mandate_revoke.add_argument("--reason", required=True, metavar="TEXT")
    _add_json(p_mandate_revoke)
    p_mandate_revoke.set_defaults(_autonomy_handler=_cmd_mandate_revoke)

    p_audit = sub.add_parser("audit", help="Recorded decisions (redacted)")
    p_audit.add_argument("--since", metavar="ISO8601")
    p_audit.add_argument("--verdict", choices=("allow", "ask", "deny"))
    p_audit.add_argument("--limit", type=int, default=_DEFAULT_AUDIT_LIMIT)
    _add_json(p_audit)
    p_audit.set_defaults(_autonomy_handler=_cmd_audit)

    p_export = sub.add_parser("export", help="Write a redacted portable authority export")
    p_export.add_argument("--output", required=True, metavar="PATH")
    p_export.add_argument("--include-audit", action="store_true")
    _add_json(p_export)
    p_export.set_defaults(_autonomy_handler=_cmd_export)

    p_purge = sub.add_parser("purge-audit", help="Delete settled runtime history")
    p_purge.add_argument("--before", required=True, metavar="ISO8601")
    p_purge.add_argument("--apply", action="store_true", required=True,
                         help="Purging is destructive; --apply is required")
    _add_json(p_purge)
    p_purge.set_defaults(_autonomy_handler=_cmd_purge_audit)

    p_doctor = sub.add_parser("doctor", help="Health of config, contract head, and state.db")
    _add_json(p_doctor)
    p_doctor.set_defaults(_autonomy_handler=_cmd_doctor)

    parser.set_defaults(_autonomy_parser=parser)
    return parser


# ── Execution and rendering ─────────────────────────────────────────────────


def _error_result(exc: Exception) -> _Outcome:
    """Map one failure to its contract exit code, never leaking secrets."""
    from agent.autonomy.config_apply import (
        AuthorityConflict,
        IncompleteAuthorityApply,
    )
    from agent.autonomy.service import AutonomyServiceError
    from agent.autonomy.store import AutonomyStoreError

    message = _clip(str(exc), 500)
    if isinstance(exc, IncompleteAuthorityApply):
        code, label = EXIT_STORAGE, "incomplete_authority_apply"
    elif isinstance(exc, (AutonomyStoreError, sqlite3.Error, OSError)):
        code, label = EXIT_STORAGE, "storage_failure"
    elif isinstance(exc, AuthorityConflict):
        code, label = EXIT_VALIDATION, "stale_authority"
    elif isinstance(exc, (AutonomyServiceError, ValueError, yaml.YAMLError)):
        code, label = EXIT_VALIDATION, "validation_error"
    else:
        raise exc
    return _Outcome(code, {"error": message, "code": label}, (f"error: {message}",))


def _execute(args: argparse.Namespace, *, output_mode: str = "text") -> CliResult:
    handler = getattr(args, "_autonomy_handler", None)
    if handler is None:
        parser = getattr(args, "_autonomy_parser", None)
        help_text = parser.format_help() if parser is not None else _SLASH_HELP
        return CliResult(EXIT_OK, help_text.rstrip(), {"help": True})
    try:
        outcome = handler(args)
    except _CliUsageError as exc:
        outcome = _Outcome(
            EXIT_VALIDATION,
            {"error": str(exc), "code": "usage_error"},
            (str(exc),),
        )
    except Exception as exc:  # noqa: BLE001 - mapped to contract exit codes
        outcome = _error_result(exc)
    as_json = output_mode == "json" or bool(getattr(args, "json", False))
    if as_json:
        output = json.dumps(
            outcome.payload, indent=2, sort_keys=True, ensure_ascii=False,
            default=str,
        )
    else:
        output = "\n".join(outcome.lines)
    return CliResult(outcome.exit_code, output, outcome.payload)


def autonomy_command(args: argparse.Namespace) -> int:
    """Entry point for ``hades autonomy ...`` argparse dispatch."""
    result = _execute(args)
    if result.output:
        print(result.output)
    return result.exit_code


def run_argv(argv, *, output_mode: str = "text") -> CliResult:
    """Parse and execute one autonomy invocation from an argv list.

    The single shared surface behind the top-level command, the classic
    slash path, and tests. Never raises for user input: parse errors
    return exit code 2 and ``--help`` returns the help text at exit 0.
    """
    wrap = _AutonomyArgumentParser(prog="hades", add_help=False)
    root_sub = wrap.add_subparsers(dest="_root")
    build_parser(root_sub)
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            args = wrap.parse_args(["autonomy", *list(argv)])
    except _CliUsageError as exc:
        return CliResult(
            EXIT_VALIDATION,
            (buffer.getvalue() + str(exc)).strip(),
            {"error": str(exc), "code": "usage_error"},
        )
    except SystemExit as exc:  # --help prints and exits 0
        code = exc.code if isinstance(exc.code, int) else EXIT_VALIDATION
        return CliResult(code, buffer.getvalue().rstrip(), {"help": True})
    return _execute(args, output_mode=output_mode)


def run_slash(rest: str) -> str:
    """Execute a classic ``/autonomy ...`` string and return its output.

    ``rest`` is everything after ``/autonomy`` (or ``/authority``); a bare
    or help invocation returns the short curated help block.
    """
    tokens = shlex.split(rest) if rest and rest.strip() else []
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_HELP
    return run_argv(tokens).output
