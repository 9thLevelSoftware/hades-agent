"""Adapter SDK capability-contract and schema-invariance tests (plan Task 2).

The registry must refuse capability claims an adapter cannot honor, and
effect metadata on the tool registry must never alter the model-visible
tool schema.
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.effects.models import (
    CommitOutcome,
    CommitRequest,
    CompensationRequest,
    CompensationResult,
    EffectContext,
    EffectPreview,
    EffectSemantics,
    NormalizedEffect,
    PreparedEffect,
    ReconciliationResult,
    VerificationResult,
)
from agent.effects.registry import (
    AdapterContractError,
    AdapterDescriptor,
    EffectAdapter,
    EffectAdapterRegistry,
    get_effect_adapter,
    register_effect_adapter,
)
from tools.registry import ToolRegistry


def _make_schema(name: str) -> dict:
    return {
        "name": name,
        "description": f"{name} test tool",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }


def _dummy_handler(args):
    return {"ok": True}


class _BaseFakeAdapter(EffectAdapter):
    """Minimal adapter implementing every abstract commit-path method."""

    descriptor = AdapterDescriptor(
        adapter_id="fake.v1",
        actions=frozenset({"write"}),
        idempotency="none",
        reconciliation="none",
        compensation="none",
        irreversible_after="commit",
    )

    def normalize(self, node, context):
        return NormalizedEffect(
            node_id=node.node_id, adapter_id=self.descriptor.adapter_id,
            action=node.action, args=dict(node.args),
            resource_keys=tuple(node.resource_keys),
        )

    def prepare(self, effect, context):
        return PreparedEffect(
            node_id=effect.node_id, adapter_id=effect.adapter_id,
            action=effect.action, args=dict(effect.args),
            resources=tuple(effect.resource_keys),
            semantics=EffectSemantics(
                fidelity=self.descriptor.compensation,
                reconciliation=self.descriptor.reconciliation,
                idempotency=self.descriptor.idempotency,
                irreversible_after=self.descriptor.irreversible_after,
            ),
        )

    def preview(self, prepared, context):
        return EffectPreview(
            node_id=prepared.node_id, summary="write",
            before={}, after={}, resources=prepared.resources,
            semantics=prepared.semantics, requires_approval=False,
        )

    def commit(self, request, context):
        return CommitOutcome(status="committed", result={}, evidence={})

    def verify(self, outcome, context):
        return VerificationResult(verified=True, evidence={})


def valid_fake_adapter(adapter_id: str = "fake.v1") -> EffectAdapter:
    class _Adapter(_BaseFakeAdapter):
        descriptor = dataclasses.replace(
            _BaseFakeAdapter.descriptor, adapter_id=adapter_id,
        )

    return _Adapter()


class AdapterWithExactClaimButNoCompensate(_BaseFakeAdapter):
    descriptor = dataclasses.replace(
        _BaseFakeAdapter.descriptor,
        adapter_id="claims-exact.v1",
        compensation="exact",
        irreversible_after="never",
    )


class AdapterWithQueryClaimButNoReconcile(_BaseFakeAdapter):
    descriptor = dataclasses.replace(
        _BaseFakeAdapter.descriptor,
        adapter_id="claims-query.v1",
        reconciliation="query",
    )


def test_effect_metadata_never_changes_model_schema():
    registry = ToolRegistry()
    registry.register(
        name="write_file", toolset="file", schema=_make_schema("write_file"),
        handler=_dummy_handler,
    )
    before = registry.get_definitions({"write_file"})
    entry = registry.get_entry("write_file")
    registry.register(
        name="write_file", toolset=entry.toolset, schema=entry.schema,
        handler=entry.handler, override=True,
        effect_adapter="workspace.v1", effect_action="write_file",
    )
    assert registry.get_definitions({"write_file"}) == before
    metadata = registry.get_operation_metadata("write_file")
    assert metadata["effect_adapter"] == "workspace.v1"
    assert metadata["effect_action"] == "write_file"


def test_effect_action_requires_effect_adapter():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="effect_action"):
        registry.register(
            name="orphan", toolset="file", schema=_make_schema("orphan"),
            handler=_dummy_handler, effect_action="write_file",
        )


def test_operation_metadata_defaults_include_effect_action():
    registry = ToolRegistry()
    registry.register(
        name="plain", toolset="file", schema=_make_schema("plain"),
        handler=_dummy_handler,
    )
    assert registry.get_operation_metadata("plain")["effect_action"] is None
    assert registry.get_operation_metadata("missing")["effect_action"] is None


def test_adapter_registry_rejects_false_capability_claims():
    registry = EffectAdapterRegistry()
    with pytest.raises(AdapterContractError, match="exact compensation"):
        registry.register(AdapterWithExactClaimButNoCompensate())
    adapter = valid_fake_adapter()
    registry.register(adapter)
    with pytest.raises(AdapterContractError, match="duplicate adapter_id"):
        registry.register(adapter)


def test_adapter_registry_rejects_query_claim_without_reconcile_override():
    registry = EffectAdapterRegistry()
    with pytest.raises(AdapterContractError, match="reconcil"):
        registry.register(AdapterWithQueryClaimButNoReconcile())


@pytest.mark.parametrize(
    ("descriptor_kwargs", "match"),
    [
        ({"adapter_id": ""}, "adapter id"),
        ({"adapter_id": "unversioned"}, "adapter id"),
        ({"actions": frozenset()}, "action"),
        (
            {"compensation_window_seconds": 60},
            "compensation_window_seconds",
        ),
        ({"irreversible_after": "never"}, "irreversible_after"),
    ],
)
def test_adapter_registry_rejects_incoherent_descriptors(
    descriptor_kwargs, match
):
    class _Adapter(_BaseFakeAdapter):
        descriptor = dataclasses.replace(
            _BaseFakeAdapter.descriptor, **descriptor_kwargs,
        )

    registry = EffectAdapterRegistry()
    with pytest.raises(AdapterContractError, match=match):
        registry.register(_Adapter())


def test_descriptor_snapshots_are_immutable_and_lookup_is_loud():
    registry = EffectAdapterRegistry()
    adapter = valid_fake_adapter("snapshot.v1")
    registry.register(adapter)
    descriptor = registry.get_descriptor("snapshot.v1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        descriptor.adapter_id = "mutated.v1"
    assert registry.get("snapshot.v1") is adapter
    with pytest.raises(KeyError):
        registry.get("missing.v1")
    assert {d.adapter_id for d in registry.list_descriptors()} == {"snapshot.v1"}


def test_module_level_registration_supports_plugins():
    adapter = valid_fake_adapter("plugin-owned.v9")
    register_effect_adapter(adapter)
    try:
        assert get_effect_adapter("plugin-owned.v9") is adapter
    finally:
        # Keep the process-global registry clean for other tests.
        from agent.effects.registry import default_effect_adapter_registry

        default_effect_adapter_registry().unregister("plugin-owned.v9")


def test_sdk_value_objects_are_frozen():
    semantics = EffectSemantics(
        fidelity="none", reconciliation="none", idempotency="none",
        irreversible_after="commit",
    )
    prepared = PreparedEffect(
        node_id="n", adapter_id="fake.v1", action="write", args={},
        resources=("file:x",), semantics=semantics,
    )
    request = CommitRequest(
        prepared=prepared, operation_id="op-1", idempotency_key="key-1",
    )
    context = EffectContext(transaction_id="tx-1", revision=1, node_id="n")
    reconciliation = ReconciliationResult(disposition="unknown", evidence={})
    compensation_request = CompensationRequest(
        effect_id="ef-1", prepared=prepared, verified_result_hash="h",
    )
    compensation = CompensationResult(
        fidelity="none", status="blocked", evidence={},
    )
    for frozen in (
        semantics, prepared, request, context, reconciliation,
        compensation_request, compensation,
    ):
        field_name = dataclasses.fields(frozen)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(frozen, field_name, "tampered")
    assert reconciliation.disposition == "unknown"
    with pytest.raises(ValueError):
        ReconciliationResult(disposition="maybe", evidence={})
