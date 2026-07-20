"""Deterministic expansion and validation of the preregistered transaction corpus.

This module freezes the 100-case action-transaction benchmark BEFORE any
production coordinator exists. It deliberately consumes no production
transaction implementation: strata, fault points, gates, and expected
outcomes are local frozen copies validated against the manifest, so the
corpus definition cannot drift with coordinator behavior. Expectations are
read only from the manifest — never derived from runner output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "APPROVED_GATES",
    "FAULT_POINTS",
    "STRATUM_COUNTS",
    "TransactionManifestError",
    "expand_cases",
    "load_cases",
]

# Frozen preregistered strata. The sum is the immutable denominator of 100.
STRATUM_COUNTS: dict[str, int] = {
    "revision": 20,
    "stale_authority": 15,
    "crash": 25,
    "duplicate_delivery": 10,
    "partial_failure": 15,
    "compensation_boundary": 15,
}

# Crash cases cycle through these five boundaries, five cases each.
FAULT_POINTS: tuple[str, ...] = (
    "after_prepare",
    "after_preview",
    "after_commit_intent",
    "after_handler_return",
    "after_delivery_dispatch",
)

# Hard safety floors. Preregistered; never relaxed after results exist.
APPROVED_GATES: dict[str, float] = {
    "unauthorized_irreversible_commits": 0,
    "duplicate_instrumented_effects": 0,
    "incorrect_compensation_order": 0,
    "unclassified_non_reversible_effects": 0,
    "false_success_receipts": 0,
    "median_eligible_overhead_ratio_max": 0.15,
}

_EXPECTED_SCHEMA = "hermes.action-transactions-benchmark.v1"
_EXPECTED_BASELINE = "current_hermes_without_transaction_coordinator"
_EXPECTED_DENOMINATOR = 100


class TransactionManifestError(ValueError):
    """A manifest violates the preregistered 100-case proof contract."""


def expand_cases(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand the frozen strata into the exact 100 deterministic cases."""
    expected_by_stratum = manifest.get("expected_by_stratum")
    if not isinstance(expected_by_stratum, dict):
        raise TransactionManifestError("manifest is missing expected_by_stratum")

    cases: list[dict[str, Any]] = []
    for stratum, count in STRATUM_COUNTS.items():
        expected = expected_by_stratum.get(stratum)
        if not expected:
            raise TransactionManifestError(
                f"stratum {stratum!r} has no expected outcome contract"
            )
        for index in range(count):
            case: dict[str, Any] = {
                "id": f"{stratum}-{index + 1:03d}",
                "stratum": stratum,
                "expected": expected,
            }
            if stratum == "crash":
                case["fault_point"] = FAULT_POINTS[index % len(FAULT_POINTS)]
            cases.append(case)
    if len({case["id"] for case in cases}) != _EXPECTED_DENOMINATOR:
        raise TransactionManifestError(
            "benchmark case ids must be unique and total 100"
        )
    return cases


def _validate_manifest(manifest: dict[str, Any], path: Path) -> None:
    if manifest.get("schema") != _EXPECTED_SCHEMA:
        raise TransactionManifestError(
            f"{path}: schema must be {_EXPECTED_SCHEMA!r}"
        )
    if manifest.get("baseline") != _EXPECTED_BASELINE:
        raise TransactionManifestError(
            f"{path}: baseline must be {_EXPECTED_BASELINE!r}"
        )

    strata = manifest.get("strata")
    if strata != STRATUM_COUNTS:
        raise TransactionManifestError(
            f"{path}: strata must equal the frozen preregistered counts "
            f"{STRATUM_COUNTS}"
        )
    if sum(STRATUM_COUNTS.values()) != _EXPECTED_DENOMINATOR:
        raise TransactionManifestError(
            f"{path}: frozen denominator must be {_EXPECTED_DENOMINATOR}"
        )

    fault_points = manifest.get("fault_points")
    if tuple(fault_points or ()) != FAULT_POINTS:
        raise TransactionManifestError(
            f"{path}: fault_points must equal the frozen boundaries "
            f"{list(FAULT_POINTS)}"
        )
    for point in fault_points:
        if point not in FAULT_POINTS:
            raise TransactionManifestError(f"{path}: unknown fault point {point!r}")

    if manifest.get("gates") != APPROVED_GATES:
        raise TransactionManifestError(
            f"{path}: gates must equal the approved safety floors"
        )

    reporting = manifest.get("reporting")
    if not isinstance(reporting, dict) or reporting.get("rate_interval") != "wilson_95":
        raise TransactionManifestError(
            f"{path}: reporting.rate_interval must be 'wilson_95'"
        )

    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, dict):
        raise TransactionManifestError(f"{path}: manifest must declare fixtures")
    base = path.resolve().parent
    for name in ("plan", "authority"):
        rel = fixtures.get(name)
        if not rel:
            raise TransactionManifestError(f"{path}: fixtures.{name} is required")
        if not (base / rel).is_file():
            raise TransactionManifestError(
                f"{path}: fixtures.{name} points to a missing file {rel!r}"
            )


def load_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load, validate, and deterministically expand the frozen manifest."""
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TransactionManifestError(f"cannot read manifest {manifest_path}: {exc}")
    manifest = yaml.safe_load(raw)
    if not isinstance(manifest, dict):
        raise TransactionManifestError(f"{manifest_path}: manifest must be a mapping")
    _validate_manifest(manifest, manifest_path)
    return manifest, expand_cases(manifest)
