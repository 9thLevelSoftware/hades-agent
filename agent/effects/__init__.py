"""Stable public SDK surface for reversible action transactions.

Import canonical contract names from here; module paths below are
implementation detail and may reorganize.
"""

from agent.effects.models import (
    COMPENSATION_FIDELITIES,
    EFFECT_PHASES,
    ELIGIBILITY_CODES,
    FAILURE_POLICIES,
    RECONCILE_DISPOSITIONS,
    TRANSACTION_STATUSES,
    ActionTransaction,
    EffectTransaction,
    ImmutableRecordError,
    RevisionConflict,
    RevisionEdge,
    RevisionNode,
    TransactionEvent,
    TransactionRevision,
    TransactionSnapshot,
    TransactionStoreError,
    canonical_json,
    content_hash,
)
from agent.effects.store import TransactionStore

__all__ = [
    "COMPENSATION_FIDELITIES",
    "EFFECT_PHASES",
    "ELIGIBILITY_CODES",
    "FAILURE_POLICIES",
    "RECONCILE_DISPOSITIONS",
    "TRANSACTION_STATUSES",
    "ActionTransaction",
    "EffectTransaction",
    "ImmutableRecordError",
    "RevisionConflict",
    "RevisionEdge",
    "RevisionNode",
    "TransactionEvent",
    "TransactionRevision",
    "TransactionSnapshot",
    "TransactionStore",
    "TransactionStoreError",
    "canonical_json",
    "content_hash",
]
