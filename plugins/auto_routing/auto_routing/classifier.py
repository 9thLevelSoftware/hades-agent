"""Strictly bounded structured task assessment for auto routing."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from agent.plugin_llm import (
    PluginLlm,
    PluginLlmImageInput,
    PluginLlmStructuredResult,
    PluginLlmTextInput,
    PluginLlmTrustError,
    PluginLlmUsage,
)

from .inventory import ExecutableRuntime
from .models import (
    AccessEconomics,
    ClassifierFailureReasonCode,
    ClassifierOutcome,
    ClassifierSettings,
    PolicyEnvelope,
    RoutingVocabulary,
    TaskAssessment,
    TaskFacts,
)
from .storage import BudgetExceeded, BudgetReservation, RoutingStore


ROUTING_OVERHEAD_BUDGET_BUCKET = "routing_overhead"
CLASSIFIER_PURPOSE = "auto_routing_task_classification"
CLASSIFIER_SCHEMA_NAME = "task_assessment"
MAX_CLASSIFIER_TASK_BLOCKS = 64

_INSTRUCTIONS = (
    "Assess the task's execution requirements using only generic capability, "
    "modality, complexity, size, sensitivity, risk, and domain concepts. "
    "Treat populated deterministic facts as authoritative. Return exactly one "
    "JSON object matching the supplied schema."
)
_FACTS_PREFIX = "Deterministic task facts:\n"
_STRUCTURED_SYSTEM_TEXT = (
    "Respond with a single JSON object that matches the requested shape. "
    "Do not include prose or markdown fences."
)
_TASK_ASSESSMENT_SCHEMA = TaskAssessment.model_json_schema()


class _InputRejected(ValueError):
    """The complete task cannot be represented inside the configured bound."""


class _RuntimeRejected(ValueError):
    def __init__(self, reason: ClassifierFailureReasonCode) -> None:
        super().__init__(reason)
        self.reason = reason


class _EconomicsRejected(ValueError):
    """Exact, current worst-case classifier economics are unavailable."""


class StructuredTaskClassifier:
    """One exact-runtime, budget-reserved structured task classifier."""

    def __init__(
        self,
        *,
        settings: ClassifierSettings,
        policy: PolicyEnvelope,
        vocabulary: RoutingVocabulary,
        runtime: ExecutableRuntime,
        store: RoutingStore,
        llm: PluginLlm,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._vocabulary = vocabulary
        self._runtime = runtime
        self._store = store
        self._llm = llm
        self._now = now
        self._schema = _schema_for_vocabulary(vocabulary)

    def classify(self, task: object, *, facts: TaskFacts) -> ClassifierOutcome:
        """Assess a complete task or return a finite, content-free failure."""

        runtime_id = self._runtime.key.stable_id()
        try:
            inputs = self._bounded_inputs(task, facts=facts)
        except (TypeError, ValueError, _InputRejected):
            return self._failure("classifier_oversized", runtime_id=runtime_id)

        try:
            now = self._current_time()
        except Exception:
            return self._failure("classifier_failed", runtime_id=runtime_id)
        try:
            self._validate_runtime(now)
        except _RuntimeRejected as error:
            return self._failure(error.reason, runtime_id=runtime_id)

        try:
            worst_case = self._worst_case_cost(now)
        except _EconomicsRejected:
            return self._failure("classifier_unknown_price", runtime_id=runtime_id)

        try:
            reservation = self._store.reserve_budget(
                ROUTING_OVERHEAD_BUDGET_BUCKET,
                worst_case_usd=worst_case,
                daily_limit_usd=self._policy.max_routing_overhead_usd_per_day,
                now=now,
            )
        except BudgetExceeded:
            return self._failure("classifier_budget", runtime_id=runtime_id)
        except Exception:
            return self._failure("classifier_failed", runtime_id=runtime_id)

        try:
            result = self._llm.complete_structured(
                instructions=_INSTRUCTIONS,
                input=inputs,
                json_schema=copy.deepcopy(self._schema),
                schema_name=CLASSIFIER_SCHEMA_NAME,
                provider=self._settings.provider,
                model=self._settings.model,
                temperature=0,
                reasoning_config={
                    "enabled": self._settings.reasoning_effort != "none",
                    "effort": self._settings.reasoning_effort,
                },
                max_tokens=self._settings.maximum_output_tokens,
                timeout=self._settings.timeout_seconds,
                purpose=CLASSIFIER_PURPOSE,
            )
        except PluginLlmTrustError:
            return self._failed_after_reconciliation(
                reservation,
                actual_usd=0.0,
                reason="classifier_trust",
                runtime_id=runtime_id,
            )
        except Exception as error:
            if _is_structured_schema_validation_error(error):
                reason: ClassifierFailureReasonCode = "classifier_malformed"
            elif _is_timeout(error):
                reason = "classifier_timeout"
            else:
                reason = "classifier_failed"
            return self._failed_after_reconciliation(
                reservation,
                actual_usd=worst_case,
                reason=reason,
                runtime_id=runtime_id,
                cost_usd=worst_case,
            )

        actual_cost, input_tokens, output_tokens = self._account_usage(
            getattr(result, "usage", None),
            worst_case=worst_case,
        )
        reason, assessment = self._validate_result(result)
        if not self._reconcile(reservation, actual_usd=actual_cost):
            return self._failure("classifier_failed", runtime_id=runtime_id)
        if reason is not None or assessment is None:
            return self._failure(
                reason or "classifier_malformed",
                runtime_id=runtime_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=actual_cost,
            )
        return ClassifierOutcome(
            assessment=assessment,
            classifier_runtime_id=runtime_id,
            classifier_input_tokens=input_tokens,
            classifier_output_tokens=output_tokens,
            classifier_cost_usd=actual_cost,
        )

    def _bounded_inputs(
        self,
        task: object,
        *,
        facts: TaskFacts,
    ) -> list[PluginLlmTextInput | PluginLlmImageInput]:
        task_inputs, image_semantic_bytes = self._normalize_task(task)
        facts_text = _FACTS_PREFIX + json.dumps(
            facts.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        inputs: list[PluginLlmTextInput | PluginLlmImageInput] = [
            PluginLlmTextInput(text=facts_text),
            *task_inputs,
        ]
        token_bound = _structured_input_token_bound(
            inputs,
            instructions=_INSTRUCTIONS,
            schema=self._schema,
            schema_name=CLASSIFIER_SCHEMA_NAME,
            image_semantic_bytes=image_semantic_bytes,
        )
        if token_bound > self._settings.maximum_input_tokens:
            raise _InputRejected("classifier input exceeds its configured bound")
        return inputs

    def _normalize_task(
        self,
        task: object,
    ) -> tuple[list[PluginLlmTextInput | PluginLlmImageInput], int]:
        if isinstance(task, str):
            blocks: tuple[object, ...] = (PluginLlmTextInput(text=task),)
        elif isinstance(task, (PluginLlmTextInput, PluginLlmImageInput, Mapping)):
            blocks = (task,)
        elif isinstance(task, (list, tuple)):
            blocks = tuple(task)
        else:
            raise _InputRejected("unsupported classifier input")
        if not blocks or len(blocks) > MAX_CLASSIFIER_TASK_BLOCKS:
            raise _InputRejected("classifier input block count is outside its bound")

        normalized: list[PluginLlmTextInput | PluginLlmImageInput] = []
        image_count = 0
        image_bytes = 0
        for block in blocks:
            item, semantic_bytes = _normalize_task_block(block)
            if isinstance(item, PluginLlmImageInput):
                image_count += 1
                image_bytes += semantic_bytes
            normalized.append(item)
        if image_count > self._settings.maximum_image_count:
            raise _InputRejected("classifier image count exceeds its bound")
        if image_bytes > self._settings.maximum_image_bytes:
            raise _InputRejected("classifier image bytes exceed their bound")
        return normalized, image_bytes

    def _current_time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise _RuntimeRejected("classifier_failed")
        return value.astimezone(UTC)

    def _validate_runtime(self, now: datetime) -> None:
        key = self._runtime.key
        if key.provider.casefold() == "moa":
            raise _RuntimeRejected("classifier_trust")
        if (
            key.provider != self._settings.provider
            or key.model != self._settings.model
            or key.provider in self._policy.denied_providers
            or key.model in self._policy.denied_models
        ):
            raise _RuntimeRejected("classifier_trust")
        if self._runtime.state != "verified":
            raise _RuntimeRejected("classifier_failed")
        if (
            self._runtime.verification_source is None
            or self._runtime.verified_at is None
            or self._runtime.verification_expires_at is None
        ):
            raise _RuntimeRejected("classifier_failed")
        try:
            verified_at = _parse_time(self._runtime.verified_at)
            expires_at = _parse_time(self._runtime.verification_expires_at)
        except ValueError as error:
            raise _RuntimeRejected("classifier_failed") from error
        if verified_at > now or expires_at <= now:
            raise _RuntimeRejected("classifier_failed")

        support = self._runtime.reasoning_support
        aliases = dict(support.provider_aliases)
        effort = self._settings.reasoning_effort
        supported = effort in support.efforts or aliases.get(effort) in support.efforts
        if not support.exact or not supported:
            raise _RuntimeRejected("classifier_trust")
        if (
            self._runtime.economics.billing_kind == "subscription"
            and not self._policy.allow_subscription
        ):
            raise _RuntimeRejected("classifier_trust")

    def _worst_case_cost(self, now: datetime) -> float:
        economics = self._runtime.economics
        ttl = economics.evidence_ttl_seconds
        if ttl is None:
            raise _EconomicsRejected("economics evidence has no finite lifetime")
        try:
            observed_at = _parse_time(economics.observed_at)
        except ValueError as error:
            raise _EconomicsRejected("economics observation is invalid") from error
        age_seconds = (now - observed_at).total_seconds()
        if age_seconds < 0 or age_seconds > ttl:
            raise _EconomicsRejected("economics observation is stale")

        if economics.billing_kind == "metered":
            input_price = economics.metered_input_usd_per_million_tokens
            output_price = economics.metered_output_usd_per_million_tokens
            if input_price is None or output_price is None:
                raise _EconomicsRejected("metered pricing is incomplete")
            cost = (
                self._settings.maximum_input_tokens * input_price
                + self._settings.maximum_output_tokens * output_price
            ) / 1_000_000
        elif economics.billing_kind == "subscription":
            cost = _subscription_cost(economics)
        elif economics.billing_kind == "local":
            cost = _local_cost(economics)
        else:  # pragma: no cover - closed BillingKind model contract
            raise _EconomicsRejected("unsupported billing kind")
        if not math.isfinite(float(cost)) or float(cost) < 0:
            raise _EconomicsRejected("classifier cost is unbounded")
        return float(cost)

    def _account_usage(
        self,
        usage: object,
        *,
        worst_case: float,
    ) -> tuple[float, int, int]:
        if not isinstance(usage, PluginLlmUsage):
            return worst_case, 0, 0
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        total_tokens = usage.total_tokens
        if not all(
            type(value) is int and value >= 0
            for value in (input_tokens, output_tokens, total_tokens)
        ):
            return worst_case, 0, 0
        if (
            input_tokens == 0
            and output_tokens == 0
            or input_tokens > self._settings.maximum_input_tokens
            or output_tokens > self._settings.maximum_output_tokens
            or total_tokens not in {0, input_tokens + output_tokens}
        ):
            return worst_case, 0, 0

        reported_cost = usage.cost_usd
        if reported_cost is not None:
            if (
                isinstance(reported_cost, bool)
                or not isinstance(reported_cost, (int, float))
                or not math.isfinite(float(reported_cost))
                or float(reported_cost) < 0
            ):
                return worst_case, 0, 0
            actual_cost = float(reported_cost)
        elif self._runtime.economics.billing_kind == "metered":
            economics = self._runtime.economics
            input_price = economics.metered_input_usd_per_million_tokens
            output_price = economics.metered_output_usd_per_million_tokens
            if input_price is None or output_price is None:  # pragma: no cover
                return worst_case, 0, 0
            actual_cost = (
                input_tokens * input_price + output_tokens * output_price
            ) / 1_000_000
        else:
            actual_cost = worst_case
        if not math.isfinite(actual_cost) or actual_cost < 0:
            return worst_case, 0, 0
        return actual_cost, input_tokens, output_tokens

    def _validate_result(
        self,
        result: object,
    ) -> tuple[ClassifierFailureReasonCode | None, TaskAssessment | None]:
        if not isinstance(result, PluginLlmStructuredResult):
            return "classifier_malformed", None
        if (
            result.provider != self._settings.provider
            or result.model != self._settings.model
        ):
            return "classifier_trust", None
        if result.content_type != "json" or not isinstance(result.parsed, Mapping):
            return "classifier_malformed", None
        try:
            assessment = TaskAssessment.model_validate(result.parsed)
        except Exception:
            return "classifier_malformed", None
        if not set(assessment.required_capabilities).issubset(
            self._vocabulary.capabilities
        ) or not set(assessment.required_modalities).issubset(
            self._vocabulary.modalities
        ):
            return "classifier_malformed", None
        return None, assessment

    def _failed_after_reconciliation(
        self,
        reservation: BudgetReservation,
        *,
        actual_usd: float,
        reason: ClassifierFailureReasonCode,
        runtime_id: str,
        cost_usd: float | None = None,
    ) -> ClassifierOutcome:
        if not self._reconcile(reservation, actual_usd=actual_usd):
            return self._failure("classifier_failed", runtime_id=runtime_id)
        return self._failure(
            reason,
            runtime_id=runtime_id,
            cost_usd=cost_usd if cost_usd is not None else actual_usd,
        )

    def _reconcile(
        self,
        reservation: BudgetReservation,
        *,
        actual_usd: float,
    ) -> bool:
        try:
            self._store.reconcile_budget(
                reservation.reservation_id,
                actual_usd=actual_usd,
                now=self._current_time(),
            )
        except Exception:
            return False
        return True

    @staticmethod
    def _failure(
        reason: ClassifierFailureReasonCode,
        *,
        runtime_id: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> ClassifierOutcome:
        return ClassifierOutcome(
            assessment=None,
            safe_default_reason=reason,
            classifier_runtime_id=runtime_id,
            classifier_input_tokens=input_tokens,
            classifier_output_tokens=output_tokens,
            classifier_cost_usd=cost_usd,
        )


def _normalize_task_block(
    block: object,
) -> tuple[PluginLlmTextInput | PluginLlmImageInput, int]:
    if isinstance(block, PluginLlmTextInput):
        if not isinstance(block.text, str):
            raise _InputRejected("text input must be a string")
        return PluginLlmTextInput(text=block.text), 0
    if isinstance(block, PluginLlmImageInput):
        return _normalize_image(
            data=block.data,
            url=block.url,
            mime_type=block.mime_type,
            file_name=block.file_name,
        )
    if not isinstance(block, Mapping) or len(block) > 16:
        raise _InputRejected("input block must be a bounded mapping")
    kind = block.get("type")
    if not isinstance(kind, str):
        raise _InputRejected("input block type must be a string")
    normalized_kind = kind.strip().casefold()
    if normalized_kind in {"text", "input_text"}:
        text = block.get("text")
        if not isinstance(text, str):
            raise _InputRejected("text input must be a string")
        return PluginLlmTextInput(text=text), 0
    if normalized_kind in {"audio", "input_audio", "document", "file", "input_file"}:
        raise _InputRejected("input modality cannot be represented completely")
    if normalized_kind not in {"image", "image_url", "input_image"}:
        raise _InputRejected("unsupported input block type")

    url = block.get("url")
    if url is None:
        image_url = block.get("image_url")
        if isinstance(image_url, Mapping):
            url = image_url.get("url")
    return _normalize_image(
        data=block.get("data"),
        url=url,
        mime_type=block.get("mime_type", "image/png"),
        file_name=block.get("file_name", ""),
    )


def _normalize_image(
    *,
    data: object,
    url: object,
    mime_type: object,
    file_name: object,
) -> tuple[PluginLlmImageInput, int]:
    if not isinstance(mime_type, str) or not isinstance(file_name, str):
        raise _InputRejected("image metadata is invalid")
    if data is not None and url is not None:
        raise _InputRejected("image input is ambiguous")
    if data is not None:
        if not isinstance(data, (bytes, bytearray)):
            raise _InputRejected("image data must be bytes")
        payload = bytes(data)
        return (
            PluginLlmImageInput(
                data=payload,
                mime_type=mime_type,
                file_name=file_name,
            ),
            len(payload),
        )
    if not isinstance(url, str) or not url:
        raise _InputRejected("image input requires bytes or a URL")
    if url[:5].casefold() == "data:":
        payload_size = _data_url_size(url)
    else:
        raise _InputRejected("remote image size has no trusted bound")
    return (
        PluginLlmImageInput(
            url=url,
            mime_type=mime_type,
            file_name=file_name,
        ),
        payload_size,
    )


def _data_url_size(url: str) -> int:
    header, separator, payload = url.partition(",")
    if not separator or ";base64" not in header.casefold():
        raise _InputRejected("image data URL is not bounded base64")
    try:
        return len(base64.b64decode(payload, validate=True))
    except (binascii.Error, ValueError) as error:
        raise _InputRejected("image data URL is malformed") from error


def _structured_input_token_bound(
    inputs: list[PluginLlmTextInput | PluginLlmImageInput],
    *,
    instructions: str,
    schema: Mapping[str, Any],
    schema_name: str,
    image_semantic_bytes: int,
) -> int:
    schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    exposed_text = (
        _STRUCTURED_SYSTEM_TEXT,
        instructions,
        f"Schema name: {schema_name}",
        "JSON schema:",
        schema_text,
        *(item.text for item in inputs if isinstance(item, PluginLlmTextInput)),
    )
    text_bytes = sum(len(value.encode("utf-8")) for value in exposed_text)
    image_transport_bytes = 0
    for item in inputs:
        if not isinstance(item, PluginLlmImageInput):
            continue
        if item.data is not None:
            image_transport_bytes += 4 * math.ceil(len(item.data) / 3)
            image_transport_bytes += len(item.mime_type.encode("utf-8")) + 16
        elif item.url is not None:
            image_transport_bytes += len(item.url.encode("utf-8"))
    structural_bytes = 128 * (len(inputs) + 2)
    return text_bytes + image_transport_bytes + image_semantic_bytes + structural_bytes


def _schema_for_vocabulary(vocabulary: RoutingVocabulary) -> dict[str, Any]:
    schema = copy.deepcopy(_TASK_ASSESSMENT_SCHEMA)
    properties = schema["properties"]
    for field, labels in (
        ("required_capabilities", vocabulary.capabilities),
        ("required_modalities", vocabulary.modalities),
    ):
        property_schema = properties[field]
        property_schema["items"]["enum"] = sorted(labels)
        property_schema["uniqueItems"] = True
    return schema


def _subscription_cost(economics: AccessEconomics) -> float:
    marginal = economics.effective_marginal_cost_usd_per_task
    amortized = economics.effective_amortized_cost_usd_per_task
    remaining = economics.subscription_quota_remaining
    if (
        marginal is None
        or amortized is None
        or economics.subscription_plan is None
        or economics.subscription_quota_unit is None
        or economics.subscription_state is None
        or economics.subscription_state.casefold() != "active"
        or remaining is None
        or not math.isfinite(float(remaining))
        or float(remaining) <= 0
    ):
        raise _EconomicsRejected("subscription economics are incomplete")
    return max(float(marginal), float(amortized))


def _local_cost(economics: AccessEconomics) -> float:
    energy = economics.local_energy_cost_usd_per_task
    compute = economics.local_compute_cost_usd_per_task
    if energy is None or compute is None:
        raise _EconomicsRejected("local economics are incomplete")
    return float(energy) + float(compute)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _is_timeout(error: Exception) -> bool:
    return isinstance(error, TimeoutError) or "timeout" in type(error).__name__.casefold()


def _is_structured_schema_validation_error(error: Exception) -> bool:
    cause = error.__cause__
    return (
        isinstance(error, ValueError)
        and cause is not None
        and type(cause).__name__ == "ValidationError"
        and type(cause).__module__.partition(".")[0] == "jsonschema"
    )


__all__ = [
    "CLASSIFIER_PURPOSE",
    "CLASSIFIER_SCHEMA_NAME",
    "ROUTING_OVERHEAD_BUDGET_BUCKET",
    "StructuredTaskClassifier",
]
