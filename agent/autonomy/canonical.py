"""Canonicalization and hashing for the Preferences & Autonomy Center.

Deterministic, float-free canonical JSON plus SHA-256 content hashes are
the identity layer of the authority contract: an immutable contract
version is addressed by the hash of its canonical bytes, every decision
records the exact contract and redacted action-context hashes, and raw
recipient identifiers never reach storage — only profile-local keyed
HMAC hashes do.

Rules of this module:

- Canonical JSON uses ``sort_keys=True``, separators ``(',', ':')``,
  UTF-8, and rejects floats/NaN outright (canonical authority is integer
  fixed-point only).
- ``hash_recipient()``/``hash_resource()`` are keyed (HMAC-SHA256) with a
  random profile-local key so hashes cannot be compared across profiles
  or brute-forced offline from an exported audit table.
- All validation fails closed with :class:`ValueError` subclasses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import asdict
from typing import Any, Mapping

from agent.autonomy.models import (
    ActionContext,
    AutonomyRule,
    CostConstraint,
    EvidenceRequirement,
    RuleProvenance,
    RuleScope,
    TimeConstraint,
)

__all__ = [
    "CanonicalizationError",
    "canonical_json",
    "content_hash",
    "contract_hash",
    "context_hash",
    "hash_recipient",
    "hash_resource",
    "normalize_action_class",
    "rule_from_dict",
    "rule_to_dict",
]

_ACTION_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_MIN_HASH_KEY_BYTES = 32


class CanonicalizationError(ValueError):
    """A value cannot be represented in canonical authority form."""


# ── Canonical JSON ──────────────────────────────────────────────────────────


def _ensure_canonical(value: Any, path: str = "$") -> None:
    """Reject anything outside the canonical JSON subset (fail closed)."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, float):
        raise CanonicalizationError(
            f"{path}: float values are forbidden in canonical authority; "
            f"use integer fixed-point (got {value!r})"
        )
    if isinstance(value, int):
        return
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"{path}: object keys must be strings (got {key!r})"
                )
            _ensure_canonical(value[key], f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _ensure_canonical(item, f"{path}[{index}]")
        return
    raise CanonicalizationError(
        f"{path}: {type(value).__name__} is not canonical-JSON representable"
    )


def canonical_json(value: Any) -> str:
    """Serialize *value* to deterministic canonical JSON.

    Sorted keys, compact separators, UTF-8 text (``ensure_ascii=False``),
    floats/NaN rejected. Equal logical values always produce identical
    bytes, so SHA-256 over this text is a stable content identity.
    """
    _ensure_canonical(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    """SHA-256 hex digest of the canonical JSON of *value*."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def contract_hash(body: Mapping[str, Any]) -> str:
    """Content hash of a compiled contract body (excludes the DB version)."""
    if not isinstance(body, Mapping):
        raise CanonicalizationError("contract body must be a mapping")
    return content_hash(body)


def context_hash(context: ActionContext) -> str:
    """Redacted action-context hash recorded on every decision.

    :class:`ActionContext` carries only labels, hashes, and identifiers —
    never message bodies, secrets, or raw recipient identifiers — so the
    canonical dict is safe to hash and store.
    """
    if not isinstance(context, ActionContext):
        raise CanonicalizationError("context must be an ActionContext")
    return content_hash(asdict(context))


# ── Normalization ───────────────────────────────────────────────────────────


def normalize_action_class(raw: object) -> str:
    """Normalize a dotted action-class identifier, failing closed.

    Strips whitespace, applies NFKC, and lowercases. Anything that does
    not then match the canonical dotted grammar raises — callers must map
    unclassifiable actions to ``unknown.mutation`` explicitly, never by a
    silent wildcard.
    """
    if not isinstance(raw, str):
        raise CanonicalizationError(
            f"action_class must be a string (got {type(raw).__name__})"
        )
    candidate = unicodedata.normalize("NFKC", raw).strip().lower()
    if not _ACTION_CLASS_RE.match(candidate):
        raise CanonicalizationError(
            f"action_class {raw!r} does not normalize to a dotted identifier "
            "such as 'message.send'"
        )
    return candidate


# ── Keyed recipient/resource hashing ────────────────────────────────────────


def _keyed_hash(domain: bytes, value: str, key: bytes) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) < _MIN_HASH_KEY_BYTES:
        raise ValueError(
            f"hash key must be at least {_MIN_HASH_KEY_BYTES} random bytes"
        )
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value to hash must be a non-empty string")
    normalized = unicodedata.normalize("NFC", value.strip())
    return hmac.new(
        bytes(key), domain + b"\x00" + normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def hash_recipient(value: str, *, key: bytes) -> str:
    """Profile-local keyed hash of a recipient identifier.

    Case-insensitive (casefold) but byte-exact otherwise, so Unicode
    confusables (``aлice@…`` vs ``alice@…``) always hash differently.
    The key is random per profile and never exported or displayed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("recipient must be a non-empty string")
    return _keyed_hash(b"recipient", value.strip().casefold(), key)


def hash_resource(value: str, *, key: bytes) -> str:
    """Profile-local keyed hash of a resource reference (case preserved)."""
    return _keyed_hash(b"resource", value, key)


# ── Rule serialization (round-trip through frozen validators) ───────────────


def rule_to_dict(rule: AutonomyRule) -> dict[str, Any]:
    """Canonical plain-dict form of a rule (tuples become lists)."""
    if not isinstance(rule, AutonomyRule):
        raise CanonicalizationError("rule must be an AutonomyRule")
    return asdict(rule)


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    return tuple(value or ())


def rule_from_dict(data: Mapping[str, Any]) -> AutonomyRule:
    """Rebuild a frozen :class:`AutonomyRule`, re-running all validators.

    Fail-closed: unknown keys, missing sections, or values outside the
    finite vocabularies raise ``ValueError`` from the model validators.
    """
    if not isinstance(data, Mapping):
        raise CanonicalizationError("rule data must be a mapping")
    payload = dict(data)
    provenance = RuleProvenance(**dict(payload.pop("provenance")))
    scope_data = dict(payload.pop("scope", None) or {})
    scope_data["resource_prefixes"] = _tuple_of_str(
        scope_data.get("resource_prefixes")
    )
    scope = RuleScope(**scope_data)
    cost_data = payload.pop("cost", None)
    cost = CostConstraint(**dict(cost_data)) if cost_data else None
    time_data = payload.pop("time", None)
    time = TimeConstraint(**dict(time_data)) if time_data else None
    evidence = tuple(
        EvidenceRequirement(**dict(item))
        for item in payload.pop("evidence_requirements", None) or ()
    )
    for field_name in (
        "action_classes",
        "data_classes",
        "recipient_classes",
        "recipient_hashes",
        "allowed_reversibility",
    ):
        payload[field_name] = _tuple_of_str(payload.get(field_name))
    return AutonomyRule(
        provenance=provenance,
        scope=scope,
        cost=cost,
        time=time,
        evidence_requirements=evidence,
        **payload,
    )
