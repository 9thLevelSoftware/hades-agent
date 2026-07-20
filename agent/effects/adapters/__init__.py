"""Built-in effect adapter families for action transactions.

``register_builtin_adapters()`` wires the first adapter families into a
registry. Adapter construction needs profile context (workspace root,
durable lookup), so registration is explicit rather than import-time.
"""

from __future__ import annotations

from typing import Callable, Optional

__all__ = ["register_builtin_adapters"]


def register_builtin_adapters(
    registry,
    *,
    workspace_root,
    transaction_lookup: Optional[Callable] = None,
) -> None:
    from agent.effects.adapters.workspace import (
        WorkspaceAdapter,
        WorkspaceGitAdapter,
    )

    registry.register(
        WorkspaceAdapter(
            workspace_root=workspace_root,
            transaction_lookup=transaction_lookup,
        )
    )
    registry.register(
        WorkspaceGitAdapter(transaction_lookup=transaction_lookup)
    )
