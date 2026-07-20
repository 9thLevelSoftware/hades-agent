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
    workflow_conn_factory: Optional[Callable] = None,
) -> None:
    from agent.effects.adapters.hermes_state import (
        HermesConfigAdapter,
        HermesCronAdapter,
        HermesWorkflowAdapter,
    )
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
    registry.register(HermesConfigAdapter())
    registry.register(HermesCronAdapter())
    # Registered unconditionally: without a caller-owned connection
    # factory the adapter opens/closes the profile workflows.db per
    # operation, so the documented hermes-workflow.v1 family is always
    # available to CLI/slash/TUI plans.
    registry.register(
        HermesWorkflowAdapter(conn_factory=workflow_conn_factory)
    )
