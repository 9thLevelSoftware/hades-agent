"""Transaction execution context: in-process ContextVar + subprocess bridge.

The ContextVar is the only in-process channel; the three
``HERMES_TRANSACTION_*`` environment variables are the only subprocess
channel, and they are internal correlation values set by a trusted
workflow/worker launcher — user plan text, tool arguments, and config
values can never supply them. A context without an exact planned node id
fails closed rather than appending a hidden graph node.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

__all__ = [
    "TransactionExecutionContext",
    "get_runtime_coordinator",
    "set_runtime_coordinator",
    "transaction_context",
    "transaction_context_from_runtime",
]

_ENV_TRANSACTION_ID = "HERMES_TRANSACTION_ID"
_ENV_TRANSACTION_REVISION = "HERMES_TRANSACTION_REVISION"
_ENV_TRANSACTION_NODE_ID = "HERMES_TRANSACTION_NODE_ID"


@dataclass(frozen=True)
class TransactionExecutionContext:
    transaction_id: str
    revision: int
    node_id: str
    coordinator: Any = None


_current: ContextVar[Optional[TransactionExecutionContext]] = ContextVar(
    "hermes_transaction_context", default=None
)

# Process-global coordinator used ONLY to honor subprocess correlation:
# a trusted launcher that sets the env triple also wires the coordinator
# at startup. Without it, env correlation fails closed to pass-through.
_runtime_coordinator: Any = None


def set_runtime_coordinator(coordinator: Any) -> None:
    global _runtime_coordinator
    _runtime_coordinator = coordinator


def get_runtime_coordinator() -> Any:
    return _runtime_coordinator


@contextmanager
def transaction_context(
    transaction_id: str,
    revision: int,
    node_id: str,
    coordinator: Any = None,
) -> Iterator[TransactionExecutionContext]:
    context = TransactionExecutionContext(
        transaction_id=str(transaction_id),
        revision=int(revision),
        node_id=str(node_id),
        coordinator=coordinator,
    )
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def transaction_context_from_runtime(
    context_kwargs: Optional[dict] = None,
) -> Optional[TransactionExecutionContext]:
    """Resolve the active execution context, ContextVar first.

    Environment correlation is accepted only when ALL THREE variables are
    set (a trusted launcher writes them together) AND a runtime
    coordinator is registered. Anything less returns ``None`` — the tool
    call passes through as a plain non-transactional call.
    """
    context = _current.get()
    if context is not None:
        if not context.node_id:
            return None
        return context

    transaction_id = os.environ.get(_ENV_TRANSACTION_ID)
    revision = os.environ.get(_ENV_TRANSACTION_REVISION)
    node_id = os.environ.get(_ENV_TRANSACTION_NODE_ID)
    if not transaction_id or not revision or not node_id:
        return None
    coordinator = _runtime_coordinator
    if coordinator is None:
        return None
    try:
        revision_number = int(revision)
    except ValueError:
        return None
    return TransactionExecutionContext(
        transaction_id=transaction_id,
        revision=revision_number,
        node_id=node_id,
        coordinator=coordinator,
    )
