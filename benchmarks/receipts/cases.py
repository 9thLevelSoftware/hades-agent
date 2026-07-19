"""Deterministic expansion and validation of the preregistered receipt corpus.

This module freezes the 50-mission false-success benchmark BEFORE any
production receipt behavior exists. It deliberately consumes no production
receipt implementation: the status vocabulary below is a local copy of the
five canonical receipt statuses so the corpus definition cannot drift with
scorer behavior, and expectations are read only from the manifest — never
derived from scorer output.

Cases are built from turn/external truth sources plus fixture-backed
evidence/recheck adapters declared in the manifest. Mission and transaction
source kinds are accepted so the corpus can extend once the approved
vertical slice lands, but nothing here implements missions or effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = [
    "APPROVED_GATES",
    "RECEIPT_BENCHMARK_STATUSES",
    "ReceiptBenchmarkManifest",
    "ReceiptCase",
    "ReceiptGates",
    "ReceiptManifestError",
    "load_receipt_cases",
]

# Local copy of the five canonical receipt statuses. Intentionally NOT
# imported from agent.receipts: the benchmark contract is frozen before the
# production vocabulary exists and must fail loudly if either side drifts.
RECEIPT_BENCHMARK_STATUSES: frozenset[str] = frozenset(
    {"verified", "completed_unverified", "failed", "blocked", "unknown_effect"}
)

# Source kinds a benchmark case may declare. Mirrors ReceiptSourceKey minus
# "legacy" (legacy rows are migration inputs, not seedable missions).
_ALLOWED_SOURCE_KINDS: frozenset[str] = frozenset(
    {"turn", "mission", "transaction", "external"}
)

_EXPECTED_DENOMINATOR = 50


class ReceiptManifestError(ValueError):
    """A manifest violates the preregistered false-success proof contract."""


@dataclass(frozen=True)
class ReceiptGates:
    """Approved pass/fail gates for the 90-day proof."""

    max_false_verified: int
    min_correct_classifications: int
    min_traceable_claims_ratio: float
    min_recheckable_receipts_ratio: float


# The approved portfolio gate. A manifest may only be equal or stricter.
APPROVED_GATES = ReceiptGates(
    max_false_verified=0,
    min_correct_classifications=45,
    min_traceable_claims_ratio=1.0,
    min_recheckable_receipts_ratio=1.0,
)


@dataclass(frozen=True)
class ReceiptCase:
    """One frozen seeded false-success mission."""

    case_id: str
    stratum: str
    expected_status: str
    injected_fault: str
    source_kind: str
    evidence_source: str
    recheck_adapter: str
    safety_stratum: str
    allowed_root: str


@dataclass(frozen=True)
class ReceiptBenchmarkManifest:
    """Frozen corpus identity, gates, comparison arms, and stop conditions."""

    corpus_version: str
    random_seed: int
    denominator: int
    gates: ReceiptGates
    baseline: str
    candidate: str
    stop_conditions: tuple[str, ...]


_REQUIRED_TOP_LEVEL_KEYS = (
    "corpus_version",
    "random_seed",
    "denominator",
    "strata",
    "gates",
    "baseline",
    "candidate",
    "stop_conditions",
)

_REQUIRED_STRATUM_KEYS = (
    "count",
    "expected_status",
    "injected_fault",
    "source_kind",
    "evidence_source",
    "recheck_adapter",
    "safety_stratum",
    "allowed_root",
)

_REQUIRED_GATE_KEYS = (
    "max_false_verified",
    "min_correct_classifications",
    "min_traceable_claims_ratio",
    "min_recheckable_receipts_ratio",
)


def _require_str(mapping: dict, key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReceiptManifestError(
            f"{context}: '{key}' must be a non-empty string, got {value!r}"
        )
    return value


def _load_gates(raw: object) -> ReceiptGates:
    if not isinstance(raw, dict):
        raise ReceiptManifestError("gates must be a mapping")
    for key in _REQUIRED_GATE_KEYS:
        if key not in raw:
            raise ReceiptManifestError(f"gate '{key}' is missing")
    gates = ReceiptGates(
        max_false_verified=int(raw["max_false_verified"]),
        min_correct_classifications=int(raw["min_correct_classifications"]),
        min_traceable_claims_ratio=float(raw["min_traceable_claims_ratio"]),
        min_recheckable_receipts_ratio=float(raw["min_recheckable_receipts_ratio"]),
    )
    if gates.max_false_verified > APPROVED_GATES.max_false_verified:
        raise ReceiptManifestError(
            "gate max_false_verified is weaker than the approved gate of "
            f"{APPROVED_GATES.max_false_verified}"
        )
    if gates.min_correct_classifications < APPROVED_GATES.min_correct_classifications:
        raise ReceiptManifestError(
            "gate min_correct_classifications is weaker than the approved gate "
            f"of {APPROVED_GATES.min_correct_classifications}"
        )
    if gates.min_traceable_claims_ratio < APPROVED_GATES.min_traceable_claims_ratio:
        raise ReceiptManifestError(
            "gate min_traceable_claims_ratio is weaker than the approved gate "
            f"of {APPROVED_GATES.min_traceable_claims_ratio}"
        )
    if (
        gates.min_recheckable_receipts_ratio
        < APPROVED_GATES.min_recheckable_receipts_ratio
    ):
        raise ReceiptManifestError(
            "gate min_recheckable_receipts_ratio is weaker than the approved "
            f"gate of {APPROVED_GATES.min_recheckable_receipts_ratio}"
        )
    return gates


def _expand_stratum(name: str, raw: object) -> tuple[ReceiptCase, ...]:
    if not isinstance(raw, dict):
        raise ReceiptManifestError(f"stratum '{name}' must be a mapping")
    for key in _REQUIRED_STRATUM_KEYS:
        if key not in raw:
            raise ReceiptManifestError(f"stratum '{name}': '{key}' is missing")

    count = raw["count"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ReceiptManifestError(
            f"stratum '{name}': count must be a positive integer, got {count!r}"
        )

    expected_status = _require_str(raw, "expected_status", f"stratum '{name}'")
    if expected_status not in RECEIPT_BENCHMARK_STATUSES:
        raise ReceiptManifestError(
            f"stratum '{name}': unknown expected status {expected_status!r}"
        )
    if expected_status == "verified":
        raise ReceiptManifestError(
            f"stratum '{name}': a seeded false-success mission may never "
            "expect 'verified'"
        )

    source_kind = _require_str(raw, "source_kind", f"stratum '{name}'")
    if source_kind not in _ALLOWED_SOURCE_KINDS:
        raise ReceiptManifestError(
            f"stratum '{name}': unknown source_kind {source_kind!r}"
        )

    injected_fault = _require_str(raw, "injected_fault", f"stratum '{name}'")
    evidence_source = _require_str(raw, "evidence_source", f"stratum '{name}'")
    recheck_adapter = _require_str(raw, "recheck_adapter", f"stratum '{name}'")
    safety_stratum = _require_str(raw, "safety_stratum", f"stratum '{name}'")
    allowed_root = _require_str(raw, "allowed_root", f"stratum '{name}'")

    prefix = name.replace("_", "-")
    return tuple(
        ReceiptCase(
            case_id=f"{prefix}-{ordinal:02d}",
            stratum=name,
            expected_status=expected_status,
            injected_fault=injected_fault,
            source_kind=source_kind,
            evidence_source=evidence_source,
            recheck_adapter=recheck_adapter,
            safety_stratum=safety_stratum,
            allowed_root=allowed_root,
        )
        for ordinal in range(1, count + 1)
    )


def load_receipt_cases(
    path: Path,
) -> tuple[ReceiptBenchmarkManifest, tuple[ReceiptCase, ...]]:
    """Load and validate the manifest, expanding it into exactly 50 cases.

    Expansion is deterministic: strata expand in manifest order and each
    stratum yields ``<stratum-with-dashes>-01..NN``. Any drift from the
    preregistered contract raises :class:`ReceiptManifestError`.
    """

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ReceiptManifestError("manifest root must be a mapping")
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in raw:
            raise ReceiptManifestError(f"manifest key '{key}' is missing")

    corpus_version = _require_str(raw, "corpus_version", "manifest")
    baseline = _require_str(raw, "baseline", "manifest")
    candidate = _require_str(raw, "candidate", "manifest")

    random_seed = raw["random_seed"]
    if not isinstance(random_seed, int) or isinstance(random_seed, bool):
        raise ReceiptManifestError(
            f"random_seed must be an integer, got {random_seed!r}"
        )

    denominator = raw["denominator"]
    if denominator != _EXPECTED_DENOMINATOR:
        raise ReceiptManifestError(
            f"denominator must be exactly {_EXPECTED_DENOMINATOR}, "
            f"got {denominator!r}"
        )

    stop_conditions_raw = raw["stop_conditions"]
    if (
        not isinstance(stop_conditions_raw, list)
        or not stop_conditions_raw
        or not all(isinstance(s, str) and s.strip() for s in stop_conditions_raw)
    ):
        raise ReceiptManifestError(
            "stop_conditions must be a non-empty list of strings"
        )
    if len(set(stop_conditions_raw)) != len(stop_conditions_raw):
        raise ReceiptManifestError("stop_conditions contains duplicates")

    strata_raw = raw["strata"]
    if not isinstance(strata_raw, dict) or not strata_raw:
        raise ReceiptManifestError("strata must be a non-empty mapping")

    cases: list[ReceiptCase] = []
    for name, stratum in strata_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ReceiptManifestError(f"invalid stratum name {name!r}")
        cases.extend(_expand_stratum(name, stratum))

    total = len(cases)
    if total != denominator:
        raise ReceiptManifestError(
            f"stratum counts sum to {total}, which drifts from the "
            f"denominator of {denominator}"
        )

    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ReceiptManifestError(f"duplicate case ID '{case.case_id}'")
        seen.add(case.case_id)

    manifest = ReceiptBenchmarkManifest(
        corpus_version=corpus_version,
        random_seed=random_seed,
        denominator=denominator,
        gates=_load_gates(raw["gates"]),
        baseline=baseline,
        candidate=candidate,
        stop_conditions=tuple(stop_conditions_raw),
    )
    return manifest, tuple(cases)
