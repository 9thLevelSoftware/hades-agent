"""Compile stable config assertions and runtime mandates into contracts.

The compiler is the only place where the two authorizing layers meet:

- **Stable layer** — confirmed ``user_assertion`` rules from the
  profile-local ``config.yaml`` ``autonomy.stable_rules`` list. Direct
  manual edits remain supported: parsing re-validates everything on each
  read and an invalid section raises :class:`InvalidStableAuthority`
  (``invalid_stable_authority``) instead of yielding a partial rule set.
- **Runtime layer** — active ``temporary_mandate`` rules from the
  profile-local ``state.db``. Expired, exhausted, revoked, consumed, and
  wrong-profile mandates are discarded; ``learned_suggestion`` rules are
  always excluded regardless of confidence.

There is no profile inheritance: callers pass exactly one profile's
config mapping and runtime rules, and nothing here resolves another
profile's home. Compilation is a pure, deterministic function of its
inputs (``now_ms`` is a caller-supplied clock).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from agent.autonomy.canonical import content_hash, rule_to_dict
from agent.autonomy.models import (
    AUTONOMY_CONTRACT_SCHEMA,
    AutonomyContract,
    AutonomyRule,
    CostConstraint,
    EvidenceRequirement,
    RuleProvenance,
    RuleScope,
    TimeConstraint,
)
from agent.autonomy.store import ContractDraft

__all__ = [
    "AUTONOMY_MODES",
    "INVALID_STABLE_AUTHORITY",
    "InvalidStableAuthority",
    "compile_contract",
    "compile_draft",
    "parse_stable_rules",
    "stable_rule_to_config_entry",
    "validate_autonomy_section",
]

INVALID_STABLE_AUTHORITY = "invalid_stable_authority"

#: ``off`` — evaluator never consulted; ``shadow`` — decisions recorded but
#: not enforced; ``enforce`` — decisions gate execution.
AUTONOMY_MODES = ("off", "shadow", "enforce")


class InvalidStableAuthority(ValueError):
    """The ``autonomy`` config section cannot be trusted — fail closed.

    Raised for any unparseable, unknown, or forbidden material in the
    stable config layer. Callers must never fall back to a partial rule
    set; enforce mode is disabled while this is raised.
    """

    code = INVALID_STABLE_AUTHORITY


def _fail(message: str) -> None:
    raise InvalidStableAuthority(f"{INVALID_STABLE_AUTHORITY}: {message}")


# ── Allowed shapes (strict allowlists; anything else fails closed) ──────────

_SECTION_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "default_known_reversible",
        "default_unknown_or_irreversible",
        "decision_ttl_seconds",
        "audit_retention_days",
        "stable_rules",
    }
)

_RULE_KEYS = frozenset(
    {
        "rule_id",
        "source",
        "state",
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
        "provenance",
        "created_at_ms",
        "expires_at_ms",
        "description",
    }
)

#: Runtime lifecycle counters live only in ``state.db``.
_RUNTIME_COUNTER_KEYS = frozenset({"max_uses", "remaining_uses"})

_SCOPE_KEYS = frozenset({"resource_prefixes"})
#: Task/session/mission/transaction/profile scopes are runtime state; a
#: durable config assertion scoped to them would silently rot (and a
#: ``profile_id`` scope would violate profile isolation — config.yaml is
#: already profile-local).
_RUNTIME_SCOPE_KEYS = frozenset(
    {"profile_id", "task_id", "session_id", "mission_id", "transaction_id"}
)

_COST_KEYS = frozenset(
    {"currency", "max_per_action_cents", "max_per_window_cents", "window_ms"}
)
_TIME_KEYS = frozenset({"window_start_minute", "window_end_minute", "timezone"})
_EVIDENCE_KEYS = frozenset({"kind", "stage"})
_PROVENANCE_KEYS = frozenset(
    {
        "actor_kind",
        "actor_id",
        "source_ref",
        "observed_at_ms",
        "confirmed_at_ms",
        "confidence_ppm",
    }
)

#: Key names that indicate secret material. Credentials never belong in the
#: autonomy config layer — the ``credential`` *data-class label* is fine
#: (e.g. a deny rule over credential data), secret *values* are not.
_SECRET_KEY_TOKENS = frozenset(
    {
        "secret",
        "secrets",
        "credential",
        "credentials",
        "token",
        "tokens",
        "password",
        "passwords",
        "passphrase",
        "api_key",
        "apikey",
        "private_key",
    }
)


def _reject_secret_keys(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEY_TOKENS:
                _fail(
                    f"{path}.{key}: credential/secret material is never valid "
                    "in the autonomy config layer"
                )
            _reject_secret_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_keys(item, f"{path}[{index}]")


def _as_tuple(value: Any, path: str) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    _fail(f"{path} must be a list")
    raise AssertionError("unreachable")


def _subsection(
    data: Any, path: str, allowed: frozenset, forbidden: frozenset = frozenset()
) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        _fail(f"{path} must be a mapping")
    hit = set(data) & forbidden
    if hit:
        _fail(
            f"{path}: runtime scope key(s) {sorted(hit)} never belong in "
            "durable config"
        )
    unknown = set(data) - allowed
    if unknown:
        _fail(f"{path}: unknown key(s) {sorted(unknown)}")
    return data


# ── Section validation ──────────────────────────────────────────────────────


def validate_autonomy_section(section: Any) -> Mapping[str, Any]:
    """Validate the ``autonomy:`` config section shape, failing closed.

    Missing keys fall back to the stable defaults; unknown keys, wrong
    types, and an ``allow`` conservative default are all rejected with
    :class:`InvalidStableAuthority`. Returns the (possibly empty) section
    mapping for further parsing.
    """
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        _fail("autonomy section must be a mapping")
    unknown = set(section) - _SECTION_KEYS
    if unknown:
        _fail(f"unknown autonomy setting(s): {sorted(unknown)}")

    schema_version = section.get("schema_version", 1)
    if schema_version != 1:
        _fail(f"unsupported autonomy schema_version {schema_version!r}")

    mode = section.get("mode", "off")
    if mode not in AUTONOMY_MODES:
        _fail(f"mode must be one of {list(AUTONOMY_MODES)} (got {mode!r})")

    for key in ("default_known_reversible", "default_unknown_or_irreversible"):
        value = section.get(key, "ask" if key == "default_known_reversible" else "deny")
        if value not in ("ask", "deny"):
            _fail(
                f"{key} must be 'ask' or 'deny'; a no-match default can "
                f"never be 'allow' (got {value!r})"
            )

    for key, minimum in (("decision_ttl_seconds", 1), ("audit_retention_days", 1)):
        value = section.get(key, 300 if key == "decision_ttl_seconds" else 90)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            _fail(f"{key} must be an integer >= {minimum} (got {value!r})")

    rules = section.get("stable_rules", [])
    if rules is not None and not isinstance(rules, list):
        _fail("stable_rules must be a list")
    return section


# ── Stable rule parsing ─────────────────────────────────────────────────────


def _parse_provenance(entry: Mapping[str, Any], path: str, created_at_ms: int) -> RuleProvenance:
    data = entry.get("provenance")
    if data is None:
        # Manual config edits carry no provenance block; synthesize a
        # deterministic one (no clock — determinism over freshness).
        return RuleProvenance(
            actor_kind="user",
            actor_id="config",
            source_ref="config.yaml",
            observed_at_ms=created_at_ms,
            confirmed_at_ms=created_at_ms,
            confidence_ppm=1_000_000,
        )
    data = _subsection(data, f"{path}.provenance", _PROVENANCE_KEYS)
    try:
        return RuleProvenance(**dict(data))
    except (TypeError, ValueError) as exc:
        _fail(f"{path}.provenance: {exc}")
    raise AssertionError("unreachable")


def _parse_stable_rule(entry: Any, index: int) -> AutonomyRule:
    path = f"stable_rules[{index}]"
    if not isinstance(entry, Mapping):
        _fail(f"{path} must be a mapping")
    counters = set(entry) & _RUNTIME_COUNTER_KEYS
    if counters:
        _fail(
            f"{path}: runtime counter(s) {sorted(counters)} live in "
            "state.db mandates, never in config.yaml"
        )
    unknown = set(entry) - _RULE_KEYS
    if unknown:
        _fail(f"{path}: unknown key(s) {sorted(unknown)}")
    _reject_secret_keys(entry, path)

    source = entry.get("source", "user_assertion")
    if source != "user_assertion":
        _fail(
            f"{path}: only user_assertion rules are valid in stable_rules "
            f"(got {source!r}); suggestions and mandates live in state.db"
        )
    state = entry.get("state", "active")
    if state != "active":
        _fail(f"{path}: stable rules are always 'active' (got {state!r})")

    if "rule_id" not in entry:
        _fail(f"{path}: rule_id is required")

    scope_data = _subsection(
        entry.get("scope") or {}, f"{path}.scope", _SCOPE_KEYS, _RUNTIME_SCOPE_KEYS
    )
    cost_data = entry.get("cost")
    if cost_data is not None:
        cost_data = _subsection(cost_data, f"{path}.cost", _COST_KEYS)
    time_data = entry.get("time")
    if time_data is not None:
        time_data = _subsection(time_data, f"{path}.time", _TIME_KEYS)
    evidence_data = _as_tuple(
        entry.get("evidence_requirements"), f"{path}.evidence_requirements"
    )
    for pos, item in enumerate(evidence_data):
        _subsection(item, f"{path}.evidence_requirements[{pos}]", _EVIDENCE_KEYS)

    created_at_ms = entry.get("created_at_ms", 0)
    if isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int):
        _fail(f"{path}: created_at_ms must be an integer")
    provenance = _parse_provenance(entry, path, created_at_ms)

    try:
        return AutonomyRule(
            rule_id=entry.get("rule_id"),
            source="user_assertion",
            state="active",
            effect=entry.get("effect"),
            action_classes=_as_tuple(
                entry.get("action_classes"), f"{path}.action_classes"
            ),
            data_classes=_as_tuple(entry.get("data_classes"), f"{path}.data_classes"),
            recipient_classes=_as_tuple(
                entry.get("recipient_classes"), f"{path}.recipient_classes"
            ),
            recipient_hashes=_as_tuple(
                entry.get("recipient_hashes"), f"{path}.recipient_hashes"
            ),
            scope=RuleScope(
                resource_prefixes=_as_tuple(
                    scope_data.get("resource_prefixes"),
                    f"{path}.scope.resource_prefixes",
                )
            ),
            cost=CostConstraint(**dict(cost_data)) if cost_data is not None else None,
            time=TimeConstraint(**dict(time_data)) if time_data is not None else None,
            allowed_reversibility=_as_tuple(
                entry.get("allowed_reversibility"), f"{path}.allowed_reversibility"
            ),
            evidence_requirements=tuple(
                EvidenceRequirement(**dict(item)) for item in evidence_data
            ),
            provenance=provenance,
            created_at_ms=created_at_ms,
            expires_at_ms=entry.get("expires_at_ms"),
            description=entry.get("description", ""),
        )
    except InvalidStableAuthority:
        raise
    except (TypeError, ValueError) as exc:
        _fail(f"{path}: {exc}")
    raise AssertionError("unreachable")


def parse_stable_rules(section: Any) -> tuple[AutonomyRule, ...]:
    """Parse ``autonomy.stable_rules`` into validated ``user_assertion`` rules.

    All-or-nothing: one invalid entry invalidates the whole layer — a
    partial rule set could silently drop a deny.
    """
    section = validate_autonomy_section(section)
    entries = section.get("stable_rules") or []
    rules = tuple(_parse_stable_rule(entry, i) for i, entry in enumerate(entries))
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            _fail(f"duplicate stable rule_id {rule.rule_id!r}")
        seen.add(rule.rule_id)
    return rules


def stable_rule_to_config_entry(rule: AutonomyRule) -> dict[str, Any]:
    """Canonical config.yaml entry for one stable rule (round-trip safe).

    Implied fields (``source``/``state``) and empty/None fields are
    omitted so the on-disk YAML stays human-readable. Runtime material is
    rejected: only a durable ``user_assertion`` may be serialized here.
    """
    if not isinstance(rule, AutonomyRule):
        _fail("stable rule must be an AutonomyRule")
    if rule.source != "user_assertion":
        _fail(
            f"rule {rule.rule_id!r} is a {rule.source}; only user_assertion "
            "rules belong in config.yaml"
        )
    if rule.max_uses is not None or rule.remaining_uses is not None:
        _fail(
            f"rule {rule.rule_id!r} carries runtime counter(s); those live "
            "in state.db mandates, never in config.yaml"
        )
    for name in sorted(_RUNTIME_SCOPE_KEYS):
        if getattr(rule.scope, name) is not None:
            _fail(
                f"rule {rule.rule_id!r} has runtime scope {name!r}; durable "
                "config rules may only scope resource_prefixes"
            )

    data = rule_to_dict(rule)
    entry: dict[str, Any] = {"rule_id": rule.rule_id, "effect": rule.effect}
    for key in (
        "action_classes",
        "data_classes",
        "recipient_classes",
        "recipient_hashes",
        "allowed_reversibility",
    ):
        if data[key]:
            entry[key] = list(data[key])
    if rule.scope.resource_prefixes:
        entry["scope"] = {"resource_prefixes": list(rule.scope.resource_prefixes)}
    for key in ("cost", "time"):
        if data[key] is not None:
            entry[key] = {k: v for k, v in data[key].items() if v is not None}
    if data["evidence_requirements"]:
        entry["evidence_requirements"] = [dict(item) for item in data["evidence_requirements"]]
    entry["provenance"] = {
        k: v for k, v in data["provenance"].items() if v is not None
    }
    if rule.created_at_ms:
        entry["created_at_ms"] = rule.created_at_ms
    if rule.expires_at_ms is not None:
        entry["expires_at_ms"] = rule.expires_at_ms
    if rule.description:
        entry["description"] = rule.description
    return entry


# ── Compilation ─────────────────────────────────────────────────────────────


def _mandate_is_active(rule: AutonomyRule, profile_id: str, now_ms: int) -> bool:
    if rule.state != "active":
        return False
    if rule.expires_at_ms is not None and rule.expires_at_ms <= now_ms:
        return False
    if rule.remaining_uses is not None and rule.remaining_uses <= 0:
        return False
    if rule.scope.profile_id is not None and rule.scope.profile_id != profile_id:
        return False
    return True


def _iter_runtime_rules(runtime_rules: Iterable[Any]) -> Iterable[AutonomyRule]:
    for item in runtime_rules or ():
        rule = getattr(item, "rule", item)
        if not isinstance(rule, AutonomyRule):
            raise ValueError(
                f"runtime rules must be AutonomyRule/StoredRuntimeRule "
                f"(got {type(item).__name__})"
            )
        if rule.source == "user_assertion":
            raise ValueError(
                f"rule {rule.rule_id!r} is a user_assertion; durable rules "
                "live in config.yaml and never arrive through the runtime layer"
            )
        yield rule


def compile_draft(
    config: Mapping[str, Any],
    runtime_rules: Iterable[Any],
    *,
    profile_id: str,
    now_ms: int,
    source_fingerprint: str | None = None,
) -> ContractDraft:
    """Merge one profile's stable and runtime layers into a contract draft.

    ``config`` is the profile's (raw or loaded) config mapping;
    ``runtime_rules`` are that profile's ``state.db`` rules
    (``AutonomyRule`` or ``StoredRuntimeRule``). Suggestions are always
    excluded; inactive/expired/exhausted/wrong-profile mandates are
    discarded; the result is ordered by ``rule_id`` for determinism.
    """
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id is required")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    if not isinstance(config, Mapping):
        _fail("config must be a mapping")

    stable = parse_stable_rules(config.get("autonomy"))
    mandates = [
        rule
        for rule in _iter_runtime_rules(runtime_rules)
        if rule.source == "temporary_mandate"
        and _mandate_is_active(rule, profile_id, now_ms)
    ]
    rules = tuple(sorted((*stable, *mandates), key=lambda r: r.rule_id))
    if source_fingerprint is None:
        source_fingerprint = "stable:" + content_hash(
            [rule_to_dict(rule) for rule in stable]
        )
    return ContractDraft(
        profile_id=profile_id,
        compiled_at_ms=now_ms,
        rules=rules,
        source_fingerprint=source_fingerprint,
    )


def compile_contract(
    config: Mapping[str, Any],
    runtime_rules: Iterable[Any],
    *,
    profile_id: str,
    now_ms: int,
    version: int = 1,
) -> AutonomyContract:
    """Compile an immutable :class:`AutonomyContract` snapshot.

    ``version`` defaults to 1 for a not-yet-materialized snapshot; the
    store assigns the real monotonic version when the draft is
    materialized (see ``AutonomyStore.materialize_contract``).
    """
    draft = compile_draft(
        config, runtime_rules, profile_id=profile_id, now_ms=now_ms
    )
    return AutonomyContract(
        schema=draft.schema_id,
        version=version,
        contract_hash=draft.content_hash(),
        profile_id=profile_id,
        compiled_at_ms=now_ms,
        rules=draft.rules,
    )
