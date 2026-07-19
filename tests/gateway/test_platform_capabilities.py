"""Capability contract tests for durable outbound delivery recovery."""

import pytest

from gateway.mission_outbox import MissionOutboxStore, delivery_capabilities
from gateway.platforms.base import BasePlatformAdapter
from hades_state import SessionDB


class _SafeReplayAdapter:
    supports_idempotent_delivery = True
    supports_delivery_reconciliation = True


class _MalformedTruthyAdapter:
    """A plugin adapter with a string ``"false"`` where a bool belongs.

    ``bool("false")`` is ``True`` in Python — the opposite of what the
    declaration means to a human/config author. Capability lookup is
    structural (see ``delivery_capabilities``'s docstring), so nothing
    stops a third-party adapter from getting this wrong.
    """

    supports_idempotent_delivery = "false"
    supports_delivery_reconciliation = "false"


class _LegacyOnlyAdapter:
    """Declares only the legacy attribute names."""

    delivery_is_idempotent = True
    delivery_is_reconcilable = True


class _MalformedCurrentWithValidLegacyAdapter:
    """The current attribute is present but malformed; legacy is valid.

    A malformed *declaration* is not an *absence* — it must fail closed
    directly, not fall through to the (differently-named) legacy flag.
    """

    supports_idempotent_delivery = "false"
    delivery_is_idempotent = True


def test_base_adapter_delivery_capabilities_fail_closed():
    assert BasePlatformAdapter.supports_idempotent_delivery is False
    assert BasePlatformAdapter.supports_delivery_reconciliation is False


def test_materialized_effect_records_live_adapter_capabilities(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        row = MissionOutboxStore(db).materialize(
            execution_id="exec-capability-materialization",
            node_id="notify",
            mission_id="mission-capability-materialization",
            platform="telegram",
            target="chat:7",
            content="hello",
            adapter=_SafeReplayAdapter(),
        )
        assert row.transaction_id is not None
        effect = db.get_effect_transaction(row.transaction_id)
        assert effect is not None
        assert effect.semantics["idempotent"] is True
        assert effect.semantics["reconcilable"] is True
    finally:
        db.close()


def test_delivery_capabilities_treats_malformed_truthy_string_as_false():
    """P2-5: a string ``"false"`` declaration must fail closed, not coerce
    truthy via ``bool("false") == True``."""
    capabilities = delivery_capabilities(_MalformedTruthyAdapter())
    assert capabilities == {"idempotent": False, "reconcilable": False}


@pytest.mark.parametrize("value", ["false", "except-python-strings", 1, 1.0, [True], {}])
def test_delivery_capabilities_only_trusts_literal_true(value):
    """Only the literal ``True`` is a valid capability declaration — every
    other truthy-but-not-``True`` value fails closed."""

    class _Adapter:
        supports_idempotent_delivery = value
        supports_delivery_reconciliation = value

    capabilities = delivery_capabilities(_Adapter())
    assert capabilities == {"idempotent": False, "reconcilable": False}


def test_delivery_capabilities_preserves_legacy_attribute_fallback():
    """P2-5: an adapter declaring only the legacy attribute names must
    still be trusted when the current attribute is genuinely absent."""
    capabilities = delivery_capabilities(_LegacyOnlyAdapter())
    assert capabilities == {"idempotent": True, "reconcilable": True}


def test_delivery_capabilities_malformed_current_attribute_fails_closed_without_legacy_fallback():
    """A malformed *declaration* on the current attribute name is not the
    same as an *absence* — it must not fall through to a valid legacy
    value sitting right behind it."""
    capabilities = delivery_capabilities(_MalformedCurrentWithValidLegacyAdapter())
    assert capabilities == {"idempotent": False, "reconcilable": False}


def test_materialized_effect_fails_closed_for_malformed_truthy_adapter_capabilities(tmp_path):
    """End-to-end: a malformed truthy declaration must not be recorded as
    a trusted idempotent/reconcilable effect."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        row = MissionOutboxStore(db).materialize(
            execution_id="exec-capability-malformed-materialization",
            node_id="notify",
            mission_id="mission-capability-malformed-materialization",
            platform="telegram",
            target="chat:7",
            content="hello",
            adapter=_MalformedTruthyAdapter(),
        )
        assert row.transaction_id is not None
        effect = db.get_effect_transaction(row.transaction_id)
        assert effect is not None
        assert effect.semantics["idempotent"] is False
        assert effect.semantics["reconcilable"] is False
    finally:
        db.close()
