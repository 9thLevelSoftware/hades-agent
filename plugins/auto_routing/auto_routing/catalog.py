"""Provenance-preserving catalog evidence collection."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel

from .models import (
    CatalogApplicability,
    CatalogEvidence,
    StoredCatalogRecord,
)
from .storage import RevisionChecksumError


PATH_LOCAL_METRICS = frozenset(
    {
        "cost",
        "effective_cost",
        "latency",
        "price",
        "quota",
        "reliability",
        "throttle",
    }
)
MAX_JSON_BYTES = 1_000_000
MAX_JSON_RECORDS = 5_000
MAX_JSON_DEPTH = 16
_SECRET_TEXT = re.compile(
    r"(?i)(?:access[_-]?token|api[_-]?key|authorization|bearer\s+|password|secret)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|bearer\s+\S{8,})"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password)\s*[:=]\s*\S+"
)
_CANONICAL_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"
)
_CONTROL_TEXT = re.compile(r"[\x00-\x1f\x7f]")
_NUMERIC_HOST = re.compile(
    r"(?i)(?:0x[0-9a-f]+|0[0-7]+|\d+)"
    r"(?:\.(?:0x[0-9a-f]+|0[0-7]+|\d+))*"
)


class CatalogValidationError(ValueError):
    """A catalog record failed safe, provenance-complete validation."""


class CatalogRefreshError(RuntimeError):
    """All requested sources failed and no complete snapshot was available."""


@dataclass(frozen=True)
class CatalogRecord:
    """Validated evidence plus its exact model/runtime applicability."""

    evidence: CatalogEvidence
    canonical_provider: str
    canonical_model: str
    canonical_version: str
    runtime_id: str | None = None


@dataclass(frozen=True)
class CatalogSnapshotView:
    """Current complete catalog state and sanitized refresh status."""

    snapshot_id: str
    evidence: tuple[CatalogEvidence, ...]
    created_at: str
    stale_fallback: bool
    source_errors: tuple[str, ...]


class JsonCatalogSource:
    """Strict, non-executable user JSON evidence source."""

    def __init__(self, payload: str | bytes, *, clock: Any = None) -> None:
        self.payload = payload
        self.clock = clock

    def load(self) -> tuple[CatalogRecord, ...]:
        raw = self.payload.encode("utf-8") if isinstance(self.payload, str) else self.payload
        if len(raw) > MAX_JSON_BYTES:
            raise CatalogValidationError("catalog JSON exceeds the size limit")
        try:
            decoded = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda _value: _reject_json_constant(),
            )
        except CatalogValidationError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CatalogValidationError("catalog JSON is invalid") from error
        if not isinstance(decoded, list):
            raise CatalogValidationError("catalog JSON must contain a record list")
        if len(decoded) > MAX_JSON_RECORDS:
            raise CatalogValidationError("catalog JSON contains too many records")
        _validate_json_depth(decoded)

        records: list[CatalogRecord] = []
        for index, value in enumerate(decoded):
            if not isinstance(value, dict):
                raise CatalogValidationError(
                    f"catalog JSON record {index} must be an object"
                )
            data = dict(value)
            provider_value = data.pop("canonical_provider", "")
            model_value = data.pop("canonical_model", data.get("model", ""))
            version_value = data.pop(
                "canonical_version",
                data.get("model_version", ""),
            )
            runtime_id_value = data.pop("runtime_id", None)
            if (
                not isinstance(provider_value, str)
                or not isinstance(model_value, str)
                or not isinstance(version_value, str)
                or (
                    runtime_id_value is not None
                    and not isinstance(runtime_id_value, str)
                )
            ):
                raise CatalogValidationError("catalog evidence schema is invalid")
            provider = provider_value.strip().casefold()
            canonical_model = model_value.strip()
            canonical_version = version_value.strip()
            runtime_id = runtime_id_value
            item = CatalogRecord(
                evidence=_validated_evidence(data, strict=True),
                canonical_provider=provider,
                canonical_model=canonical_model,
                canonical_version=canonical_version,
                runtime_id=runtime_id,
            )
            _validate_record(item, now=_clock_now(self.clock))
            records.append(item)
        return tuple(records)


class ModelsDevCatalogSource:
    """Exact model-level capability evidence from ``agent.models_dev``."""

    def __init__(
        self,
        runtimes: Iterable[Any],
        *,
        get_model_info: Any = None,
        clock: Any = None,
    ) -> None:
        self.runtimes = tuple(runtimes)
        self.get_model_info = get_model_info
        self.clock = clock

    def load(self) -> tuple[CatalogRecord, ...]:
        if self.get_model_info is None:
            from agent.models_dev import get_model_info

            lookup = get_model_info
        else:
            lookup = self.get_model_info
        now = _clock_now(self.clock)
        retrieved_at = _iso(now)
        records: list[CatalogRecord] = []
        seen: set[tuple[str, str]] = set()
        for runtime in self.runtimes:
            provider = _canonical_provider(runtime.key.provider)
            model = runtime.key.model.strip()
            identity = (provider, model)
            if identity in seen:
                continue
            seen.add(identity)
            info = lookup(provider, model)
            if info is None or str(info.id).strip() != model:
                continue
            published_at = _release_timestamp(str(info.release_date or ""))
            if published_at is None:
                continue
            metrics = (
                ("capability_reasoning", bool(info.reasoning)),
                ("capability_tools", bool(info.tool_call)),
            )
            for metric_name, supported in metrics:
                evidence = CatalogEvidence(
                    source_id="models-dev",
                    source_url="https://models.dev/",
                    retrieved_at=retrieved_at,
                    published_at=published_at,
                    model=model,
                    model_version=model,
                    domain="general",
                    task_definition="catalog capability",
                    metric_name=metric_name,
                    metric_direction="higher_is_better",
                    metric_scale="unit_interval",
                    value=1.0 if supported else 0.0,
                    confidence=0.7,
                    normalization_method="identity",
                )
                record = CatalogRecord(
                    evidence=evidence,
                    canonical_provider=provider,
                    canonical_model=model,
                    canonical_version=model,
                )
                _validate_record(record, now=now)
                records.append(record)
        return tuple(records)


class HermesCatalogSource:
    """Picker capability and raw metered-price evidence with exact path scope."""

    def __init__(
        self,
        runtimes: Iterable[Any],
        *,
        load_payload: Any,
        clock: Any = None,
    ) -> None:
        self.runtimes = tuple(runtimes)
        self.load_payload = load_payload
        self.clock = clock

    def load(self) -> tuple[CatalogRecord, ...]:
        payload = self.load_payload()
        if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
            raise CatalogValidationError("Hermes catalog payload is invalid")
        now = _clock_now(self.clock)
        records: list[CatalogRecord] = []
        for row in payload["providers"]:
            if not isinstance(row, dict):
                raise CatalogValidationError("Hermes catalog provider row is invalid")
            provider = _canonical_provider(str(row.get("slug", "")))
            models = row.get("models")
            capabilities = row.get("capabilities")
            discovery = row.get("discovery")
            if not isinstance(models, list):
                continue
            capabilities = capabilities if isinstance(capabilities, dict) else {}
            discovery = discovery if isinstance(discovery, dict) else {}
            observed_at = str(discovery.get("observed_at") or _iso(now))
            for model_value in models:
                model = str(model_value).strip()
                if not model:
                    continue
                model_caps = capabilities.get(model)
                if isinstance(model_caps, dict):
                    for name in ("reasoning", "fast"):
                        if name not in model_caps:
                            continue
                        capability_value = model_caps[name]
                        if not isinstance(capability_value, bool):
                            raise CatalogValidationError(
                                "Hermes catalog capability value is invalid"
                            )
                        record = CatalogRecord(
                            evidence=CatalogEvidence(
                                source_id="hermes-picker-capabilities",
                                source_url="https://hermes.nousresearch.com/",
                                retrieved_at=observed_at,
                                published_at=observed_at,
                                model=model,
                                model_version=model,
                                domain="general",
                                task_definition="catalog capability",
                                metric_name=f"capability_{name}",
                                metric_direction="higher_is_better",
                                metric_scale="unit_interval",
                                value=1.0 if capability_value else 0.0,
                                confidence=0.8,
                                normalization_method="identity",
                            ),
                            canonical_provider=provider,
                            canonical_model=model,
                            canonical_version=model,
                        )
                        _validate_record(record, now=now)
                        records.append(record)

                pricing = discovery.get("pricing")
                raw_price = pricing.get(model) if isinstance(pricing, dict) else None
                if not isinstance(raw_price, dict):
                    continue
                bound_runtime_ids = {
                    runtime.key.stable_id()
                    for runtime in self.runtimes
                    if _discovery_identifies_runtime(
                        discovery,
                        runtime=runtime,
                        provider=provider,
                        model=model,
                    )
                }
                if len(bound_runtime_ids) != 1:
                    continue
                bound_runtime_id = next(iter(bound_runtime_ids))
                ttl_seconds = raw_price.get("ttl_seconds")
                if (
                    raw_price.get("fresh") is not True
                    or isinstance(ttl_seconds, bool)
                    or not isinstance(ttl_seconds, int)
                    or ttl_seconds <= 0
                ):
                    continue
                price_observed_at = str(raw_price.get("observed_at") or observed_at)
                expires_at = _iso(
                    _parse_timestamp(price_observed_at)
                    + timedelta(seconds=ttl_seconds)
                )
                for runtime in self.runtimes:
                    if (
                        _canonical_provider(runtime.key.provider) != provider
                        or runtime.key.model.strip() != model
                        or runtime.economics.billing_kind != "metered"
                        or runtime.key.stable_id() != bound_runtime_id
                    ):
                        continue
                    for side, field in (
                        ("input", "input_usd_per_token"),
                        ("output", "output_usd_per_token"),
                    ):
                        price = _price_per_million(raw_price.get(field))
                        if price is None:
                            continue
                        record = CatalogRecord(
                            evidence=CatalogEvidence(
                                source_id=str(
                                    raw_price.get("source_id")
                                    or "hermes-picker-pricing"
                                ),
                                source_url="https://hermes.nousresearch.com/",
                                retrieved_at=price_observed_at,
                                published_at=price_observed_at,
                                expires_at=expires_at,
                                model=model,
                                model_version=model,
                                domain="economics",
                                task_definition="metered access-path price",
                                metric_name=f"metered_{side}_price",
                                metric_direction="lower_is_better",
                                metric_scale="usd_per_million_tokens",
                                value=price,
                                confidence=0.9,
                                normalization_method="path_local_only",
                            ),
                            canonical_provider=provider,
                            canonical_model=model,
                            canonical_version=model,
                            runtime_id=runtime.key.stable_id(),
                        )
                        _validate_record(record, now=now)
                        records.append(record)
        return tuple(records)


class CatalogService:
    """Keep immutable evidence rows separate from executable inventory state."""

    def __init__(self, *, store: Any = None, clock: Any = None) -> None:
        self.store = store
        self.clock = clock
        self._records: tuple[CatalogRecord, ...] = ()
        self._committed_records: tuple[CatalogRecord, ...] = ()
        self._snapshot: CatalogSnapshotView | None = None
        self._load_latest_snapshot()

    @property
    def snapshot(self) -> CatalogSnapshotView | None:
        return self._snapshot

    def current_time(self) -> datetime:
        """Return the service clock as a timezone-aware UTC timestamp."""
        return _clock_now(self.clock)

    def import_records(
        self,
        records: Iterable[CatalogEvidence | CatalogRecord],
    ) -> None:
        validated: list[CatalogRecord] = []
        for index, record in enumerate(records):
            try:
                if isinstance(record, CatalogRecord):
                    item = CatalogRecord(
                        evidence=_validated_evidence(record.evidence),
                        canonical_provider=record.canonical_provider.strip().casefold(),
                        canonical_model=record.canonical_model.strip(),
                        canonical_version=record.canonical_version.strip(),
                        runtime_id=record.runtime_id,
                    )
                else:
                    evidence = _validated_evidence(record)
                    item = CatalogRecord(
                        evidence=evidence,
                        canonical_provider="",
                        canonical_model=evidence.model.strip(),
                        canonical_version=evidence.model_version.strip(),
                    )
                _validate_record(item, now=_clock_now(self.clock))
            except CatalogValidationError:
                raise
            except (TypeError, ValueError) as error:
                raise CatalogValidationError(
                    f"catalog record {index} is invalid"
                ) from error
            validated.append(item)
        self._records = (*self._records, *validated)

    def refresh(self, sources: Iterable[Any]) -> CatalogSnapshotView:
        now = _clock_now(self.clock)
        sources = tuple(sources)
        candidate: list[CatalogRecord] = []
        errors: list[str] = []
        empty_sources: list[str] = []
        if not sources:
            errors.append("CatalogRefresh:NoSources")
        for source in sources:
            try:
                loaded = source.load()
                validator = CatalogService(clock=self.clock)
                validator.import_records(loaded)
                if not validator._records:
                    empty_sources.append(
                        f"{type(source).__name__}:EmptySource"
                    )
                candidate.extend(validator._records)
            except Exception as error:
                errors.append(f"{type(source).__name__}:{type(error).__name__}")
        errors.extend(empty_sources)
        if errors:
            if self._snapshot is None or not self._committed_records:
                self._load_latest_snapshot()
            if self._snapshot is None or not self._committed_records:
                raise CatalogRefreshError(
                    "catalog refresh failed with no valid snapshot"
                )
            self._records = self._committed_records
            self._snapshot = CatalogSnapshotView(
                snapshot_id=self._snapshot.snapshot_id,
                evidence=tuple(
                    record.evidence for record in self._committed_records
                ),
                created_at=self._snapshot.created_at,
                stale_fallback=True,
                source_errors=tuple(sorted(errors)),
            )
            return self._snapshot
        records = _deduplicate_records(candidate)
        snapshot_id = _records_hash(records)
        created_at = _iso(now)
        if self.store is not None:
            persisted = self.store.write_catalog_snapshot(
                snapshot_id,
                tuple(_stored_record(record) for record in records),
                created_at=created_at,
            )
            created_at = persisted.created_at
            records = tuple(_catalog_record(record) for record in persisted.records)
        evidence = tuple(record.evidence for record in records)
        self._records = records
        self._committed_records = records
        self._snapshot = CatalogSnapshotView(
            snapshot_id=snapshot_id,
            evidence=evidence,
            created_at=created_at,
            stale_fallback=False,
            source_errors=(),
        )
        return self._snapshot

    def evidence_for(self, runtime: Any) -> tuple[CatalogEvidence, ...]:
        model = runtime.key.model.strip()
        provider = _canonical_provider(runtime.key.provider)
        runtime_id = runtime.key.stable_id()
        return tuple(
            record.evidence
            for record in self._records
            if record.canonical_model == model
            and record.canonical_version == model
            and (
                not record.canonical_provider
                or record.canonical_provider == provider
            )
            and (record.runtime_id is None or record.runtime_id == runtime_id)
        )

    def staleness_penalty(
        self,
        runtime: Any,
        *,
        evidence: Iterable[CatalogEvidence] | None = None,
    ) -> float:
        rows = (
            self.evidence_for(runtime)
            if evidence is None
            else tuple(evidence)
        )
        if not rows:
            return 0.0
        now = _clock_now(self.clock)
        oldest_age = max(
            0.0,
            max((now - _parse_timestamp(row.retrieved_at)).total_seconds() for row in rows),
        )
        age_penalty = min(0.25, oldest_age / (24 * 60 * 60) * 0.01)
        fallback_penalty = 0.05 if self._snapshot and self._snapshot.stale_fallback else 0.0
        return age_penalty + fallback_penalty

    def economics_is_stale(self, runtime: Any) -> bool:
        economics = runtime.economics
        ttl = economics.evidence_ttl_seconds
        if ttl is None:
            return False
        try:
            observed_at = _parse_timestamp(economics.observed_at)
        except CatalogValidationError:
            return True
        return _clock_now(self.clock) >= observed_at + timedelta(seconds=ttl)

    def evidence_is_expired(self, evidence: CatalogEvidence) -> bool:
        """Return whether time-bounded catalog evidence is no longer current."""
        if evidence.expires_at is None:
            return False
        return _clock_now(self.clock) >= _parse_timestamp(evidence.expires_at)

    def economics_staleness_penalty(self, runtime: Any) -> float:
        return 0.05 if self.economics_is_stale(runtime) else 0.0

    def _load_latest_snapshot(self) -> None:
        if self.store is None:
            return
        rows = self.store.connection.execute(
            "SELECT snapshot_id FROM catalog_snapshots "
            "WHERE complete = 1 ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        newest_error: CatalogValidationError | RevisionChecksumError | None = None
        for row in rows:
            try:
                snapshot = self.store.read_catalog_snapshot(
                    str(row["snapshot_id"])
                )
            except RevisionChecksumError as error:
                if newest_error is None:
                    newest_error = error
                continue
            if snapshot is None:
                continue
            candidate_records = tuple(
                CatalogRecord(
                    evidence=item.evidence,
                    canonical_provider=item.applicability.canonical_provider,
                    canonical_model=item.applicability.canonical_model,
                    canonical_version=item.applicability.canonical_version,
                    runtime_id=item.applicability.runtime_id,
                )
                for item in snapshot.records
            )
            try:
                for record in candidate_records:
                    _validate_record(record, now=_clock_now(self.clock))
            except CatalogValidationError as error:
                if newest_error is None:
                    newest_error = error
                continue
            self._records = candidate_records
            self._committed_records = self._records
            self._snapshot = CatalogSnapshotView(
                snapshot_id=snapshot.snapshot_id,
                evidence=snapshot.evidence,
                created_at=snapshot.created_at,
                stale_fallback=False,
                source_errors=(),
            )
            return
        if newest_error is not None:
            raise newest_error


def _validated_evidence(
    value: Any,
    *,
    strict: bool = False,
) -> CatalogEvidence:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else value
    )
    try:
        return CatalogEvidence.model_validate(payload, strict=strict)
    except (TypeError, ValueError) as error:
        raise CatalogValidationError("catalog evidence schema is invalid") from error


def _record_payload(record: CatalogRecord) -> dict[str, Any]:
    return {
        "evidence": record.evidence.model_dump(mode="json"),
        "canonical_provider": record.canonical_provider,
        "canonical_model": record.canonical_model,
        "canonical_version": record.canonical_version,
        "runtime_id": record.runtime_id,
    }


def _stored_record(record: CatalogRecord) -> StoredCatalogRecord:
    return StoredCatalogRecord(
        evidence=record.evidence,
        applicability=CatalogApplicability(
            canonical_provider=record.canonical_provider,
            canonical_model=record.canonical_model,
            canonical_version=record.canonical_version,
            runtime_id=record.runtime_id,
        ),
    )


def _catalog_record(record: StoredCatalogRecord) -> CatalogRecord:
    return CatalogRecord(
        evidence=record.evidence,
        canonical_provider=record.applicability.canonical_provider,
        canonical_model=record.applicability.canonical_model,
        canonical_version=record.applicability.canonical_version,
        runtime_id=record.applicability.runtime_id,
    )


def _records_hash(records: tuple[CatalogRecord, ...]) -> str:
    payload = json.dumps(
        [_record_payload(record) for record in records],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deduplicate_records(records: Iterable[CatalogRecord]) -> tuple[CatalogRecord, ...]:
    by_payload: dict[str, CatalogRecord] = {}
    for record in records:
        key = json.dumps(
            _record_payload(record),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        by_payload[key] = record
    return tuple(by_payload[key] for key in sorted(by_payload))


def _validate_record(record: CatalogRecord, *, now: datetime) -> None:
    evidence = record.evidence
    _validate_safe_text_fields(evidence)
    if not record.canonical_model or not record.canonical_version:
        raise CatalogValidationError("canonical model and version are required")
    if record.canonical_model != evidence.model:
        raise CatalogValidationError(
            "catalog canonical model must match evidence model"
        )
    if record.canonical_version != evidence.model_version:
        raise CatalogValidationError(
            "catalog canonical version must match evidence version"
        )
    binding_values = (
        record.canonical_provider,
        record.canonical_model,
        record.canonical_version,
    )
    if any(
        len(value) > 512
        or "://" in value
        or _SECRET_VALUE.search(value) is not None
        for value in binding_values
    ):
        raise CatalogValidationError("catalog canonical binding is unsafe")
    if record.runtime_id is not None and (
        len(record.runtime_id) != 64
        or any(
            character not in "0123456789abcdef" for character in record.runtime_id
        )
    ):
        raise CatalogValidationError("runtime binding must be a stable runtime ID")
    if _is_path_local_metric(evidence.metric_name):
        if record.runtime_id is None:
            raise CatalogValidationError(
                "path-local evidence requires an exact runtime binding"
            )
    supported_normalization = {
        ("unit_interval", "identity"),
        ("percent", "divide_by_100"),
        ("seconds", "divide_by_limit"),
        ("usd_per_million_tokens", "path_local_only"),
    }
    pair = (
        evidence.metric_scale.strip().casefold(),
        evidence.normalization_method.strip().casefold(),
    )
    if pair not in supported_normalization:
        raise CatalogValidationError("unknown metric scale or normalization")
    _validate_metric_contract(evidence, pair=pair)
    if pair[0] == "unit_interval" and not 0.0 <= evidence.value <= 1.0:
        raise CatalogValidationError("unit interval metric value is out of range")
    if pair[0] == "percent" and not 0.0 <= evidence.value <= 100.0:
        raise CatalogValidationError("percent metric value is out of range")
    if pair[0] in {"seconds", "usd_per_million_tokens"} and evidence.value < 0:
        raise CatalogValidationError("time and price metrics must be non-negative")
    _validate_source_url(evidence.source_url)
    published_at = _parse_timestamp(evidence.published_at)
    retrieved_at = _parse_timestamp(evidence.retrieved_at)
    expires_at = (
        None
        if evidence.expires_at is None
        else _parse_timestamp(evidence.expires_at)
    )
    if published_at > retrieved_at:
        raise CatalogValidationError(
            "catalog publication timestamp follows retrieval timestamp"
        )
    if retrieved_at > now + timedelta(minutes=5):
        raise CatalogValidationError("catalog retrieval timestamp is in the future")
    if expires_at is not None and expires_at < retrieved_at:
        raise CatalogValidationError(
            "catalog expiry timestamp precedes retrieval timestamp"
        )


def _canonical_provider(provider: str) -> str:
    normalized = provider.strip().casefold()
    try:
        from agent.models_dev import PROVIDER_TO_MODELS_DEV

        return str(PROVIDER_TO_MODELS_DEV.get(normalized, normalized)).casefold()
    except ImportError:  # pragma: no cover - core dependency is always present
        return normalized


def _is_path_local_metric(metric_name: str) -> bool:
    normalized = metric_name.strip().casefold()
    return normalized in PATH_LOCAL_METRICS or any(
        token in normalized
        for token in ("cost", "latency", "price", "quota", "throttle")
    )


def _validate_metric_contract(
    evidence: CatalogEvidence,
    *,
    pair: tuple[str, str],
) -> None:
    metric = evidence.metric_name.strip().casefold()
    if metric in {"quality", "reliability"} or metric.startswith("capability_"):
        valid = (
            evidence.metric_direction == "higher_is_better"
            and pair
            in {
                ("unit_interval", "identity"),
                ("percent", "divide_by_100"),
            }
        )
    elif metric == "latency":
        valid = (
            evidence.metric_direction == "lower_is_better"
            and pair == ("seconds", "divide_by_limit")
        )
    elif "price" in metric or metric in {"cost", "effective_cost"}:
        valid = (
            evidence.metric_direction == "lower_is_better"
            and pair == ("usd_per_million_tokens", "path_local_only")
        )
    else:
        return
    if not valid:
        raise CatalogValidationError(
            "catalog metric contract does not match its direction and scale"
        )


def _price_per_million(value: Any) -> float | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return float(parsed * Decimal(1_000_000))


def _discovery_identifies_runtime(
    discovery: dict[str, Any],
    *,
    runtime: Any,
    provider: str,
    model: str,
) -> bool:
    required = {
        "provider",
        "auth_identity",
        "credential_pool_identity",
        "endpoint_identity",
        "api_mode",
    }
    if not required.issubset(discovery):
        return False
    key = runtime.key
    return (
        runtime.economics.billing_kind == "metered"
        and not key.local_backend
        and _canonical_provider(str(discovery["provider"])) == provider
        and _canonical_provider(key.provider) == provider
        and key.model.strip() == model
        and key.auth_identity == str(discovery["auth_identity"])
        and key.credential_pool_identity
        == str(discovery["credential_pool_identity"])
        and key.endpoint_identity == str(discovery["endpoint_identity"])
        and key.api_mode == str(discovery["api_mode"])
    )


def _clock_now(clock: Any) -> datetime:
    if clock is None:
        value = datetime.now(UTC)
    elif hasattr(clock, "now"):
        value = clock.now()
    elif callable(clock):
        value = clock()
    else:
        value = clock
    if not isinstance(value, datetime):
        raise CatalogValidationError("catalog clock must provide a datetime")
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or _CANONICAL_TIMESTAMP.fullmatch(value) is None:
        raise CatalogValidationError("catalog timestamp is not canonical RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise CatalogValidationError("catalog timestamp is not valid RFC3339") from error
    if parsed.tzinfo is None:
        raise CatalogValidationError("catalog timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _release_timestamp(value: str) -> str | None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return _iso(parsed)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_source_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise CatalogValidationError("catalog source URL is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CatalogValidationError("catalog source URL must be canonical and public")
    normalized_host = hostname.casefold().rstrip(".")
    if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith(
        (".localhost", ".local", ".internal", ".test")
    ):
        raise CatalogValidationError("catalog source URL must be public")
    if _NUMERIC_HOST.fullmatch(normalized_host) is not None:
        raise CatalogValidationError("catalog source URL must be public")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise CatalogValidationError("catalog source URL must be public")
    if port is not None and port not in {80, 443}:
        raise CatalogValidationError("catalog source URL uses a non-public port")
    if _SECRET_TEXT.search(unquote(value)) is not None:
        raise CatalogValidationError("catalog source URL contains credential material")


def _validate_safe_text_fields(evidence: CatalogEvidence) -> None:
    for field in (
        "source_id",
        "model",
        "model_version",
        "domain",
        "task_definition",
        "metric_name",
        "metric_scale",
        "normalization_method",
    ):
        value = str(getattr(evidence, field))
        if not value.strip() or len(value) > 4_096 or _CONTROL_TEXT.search(value):
            raise CatalogValidationError("catalog text field is blank or unsafe")
        if "://" in value or value.lstrip().startswith("//"):
            raise CatalogValidationError(
                "catalog evidence contains endpoint or URL content"
            )
        if (
            _SECRET_VALUE.search(value) is not None
            or _SECRET_ASSIGNMENT.search(value) is not None
        ):
            raise CatalogValidationError("catalog evidence contains credential material")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogValidationError("catalog JSON contains a duplicate field")
        result[key] = value
    return result


def _reject_json_constant() -> None:
    raise CatalogValidationError("catalog JSON numbers must be finite")


def _validate_json_depth(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise CatalogValidationError("catalog JSON exceeds the nesting limit")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


__all__ = [
    "CatalogRecord",
    "CatalogRefreshError",
    "CatalogService",
    "CatalogSnapshotView",
    "CatalogValidationError",
    "HermesCatalogSource",
    "JsonCatalogSource",
    "MAX_JSON_DEPTH",
    "MAX_JSON_BYTES",
    "MAX_JSON_RECORDS",
    "ModelsDevCatalogSource",
    "PATH_LOCAL_METRICS",
]
