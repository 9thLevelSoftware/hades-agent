"""Leased transaction-outbox dispatcher over ``DeliveryRouter``.

Action-transaction outbox rows live in the same durable store as
mission/workflow rows and drain through the same claim-fenced
dispatcher; this module gives transactions their own bounded entry
point. Dispatch certainty comes from the delivery operation journal —
an unknown acknowledgement stays unknown and is never re-claimed
automatically.
"""

from __future__ import annotations

from gateway.mission_delivery import (
    MissionOutboxDispatcher,
    OutboxDrainReport,
)
from gateway.mission_outbox import MissionOutboxStore

__all__ = [
    "OutboxDrainReport",
    "TransactionOutboxDispatcher",
    "release_transaction_outbox",
]


class TransactionOutboxDispatcher(MissionOutboxDispatcher):
    """Claim due rows and hand them to ``DeliveryRouter`` under a lease.

    Identical drain semantics to the mission dispatcher; the subclass
    exists so gateway wiring and tests can express intent and bound the
    batch size for transaction traffic explicitly.
    """

    DEFAULT_DRAIN_LIMIT = 20

    async def drain_transactions(
        self, limit: int | None = None
    ) -> OutboxDrainReport:
        return await self.drain(limit=limit or self.DEFAULT_DRAIN_LIMIT)


def release_transaction_outbox(
    store: MissionOutboxStore,
    outbox_id: str,
    *,
    approval,
) -> bool:
    """Release one pending-approval row for dispatch.

    ``approval`` must be a consumed exact approval
    (:class:`agent.effects.authority.ApprovalConsumption` with
    ``approved=True``); session or permanent allowlisting never releases
    an outbound message.
    """
    if not getattr(approval, "approved", False):
        return False
    return store.schedule(outbox_id, expected_status="pending_approval")
