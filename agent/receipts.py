"""Canonical public facade for verified outcome and artifact receipts.

``agent.receipts`` is the ONE public receipt contract. Consumer code —
missions, transactions, experience, team, federation, commerce, CLI,
TUI, and Dashboard layers — imports receipt names only from here.
Implementation lives in the sibling ``agent.receipt_*`` modules; this
module contains no second implementation.

Frozen vocabulary: ``ReceiptStatus`` has exactly ``verified``,
``completed_unverified``, ``failed``, ``blocked``, and
``unknown_effect``. No consumer may add a receipt status. Only an
independent scorer can mint the sealed ``VerifiedReceiptDecision``
required for ``verified``; a signature proves provenance over a content
hash and never changes truth status.

The scorer protocol and sealing service (``EndStateScorer``,
``ReceiptScoringService``) resolve eagerly from
``agent.receipt_scoring``. Names delivered by later plan tasks
(``ReceiptStore``, ``digest_artifact``, the signer protocol, and the
issuer service) resolve lazily so the frozen contract surface is stable
from day one; accessing one before its module lands raises
``AttributeError`` naming the pending module.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from agent.receipt_hashing import canonical_content_hash
from agent.receipt_scoring import EndStateScorer, ReceiptScoringService
from agent.receipt_models import (
    RECEIPT_STATUSES,
    ArtifactDigest,
    EvidenceDigest,
    Receipt,
    ReceiptClaim,
    ReceiptObservation,
    ReceiptQuery,
    ReceiptSourceKey,
    ReceiptStatus,
    ReceiptSummary,
    RequestedOutcome,
    VerifiedReceiptDecision,
)

__all__ = [
    # Frozen status vocabulary.
    "ReceiptStatus",
    "RECEIPT_STATUSES",
    # Immutable public value objects.
    "RequestedOutcome",
    "ReceiptClaim",
    "EvidenceDigest",
    "ArtifactDigest",
    "ReceiptSourceKey",
    "Receipt",
    "ReceiptObservation",
    "VerifiedReceiptDecision",
    "ReceiptQuery",
    "ReceiptSummary",
    # Canonical hashing.
    "canonical_content_hash",
    # Storage (Task 2).
    "ReceiptStore",
    # Artifact digests (Task 3).
    "digest_artifact",
    # Scorer protocol and sealing service (Task 5).
    "EndStateScorer",
    "ReceiptScoringService",
    # Issuer service (Task 6).
    "ReceiptIssuer",
    # Signer protocol (Task 7).
    "ReceiptSigner",
]

# Public names whose implementation modules land in later plan tasks.
# Each resolves on first attribute access and is cached in module globals.
_FORWARD_EXPORTS: dict[str, tuple[str, str]] = {
    "ReceiptStore": ("agent.receipt_store", "ReceiptStore"),
    "digest_artifact": ("agent.receipt_artifacts", "digest_artifact"),
    "ReceiptIssuer": ("agent.receipt_ingest", "ReceiptIssuer"),
    "ReceiptSigner": ("agent.receipt_security", "ReceiptSigner"),
}

if TYPE_CHECKING:  # pragma: no cover - typing-only forward imports
    from agent.receipt_artifacts import digest_artifact  # noqa: F401
    from agent.receipt_ingest import ReceiptIssuer  # noqa: F401
    from agent.receipt_security import ReceiptSigner  # noqa: F401
    from agent.receipt_store import ReceiptStore  # noqa: F401


def __getattr__(name: str) -> object:
    try:
        module_name, attr = _FORWARD_EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AttributeError(
            f"{name!r} is provided by {module_name!r}, which is not "
            "available yet in this checkout"
        ) from exc
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
