"""Context-local workspace roots for shared CLI services.

Native gateway calls run concurrently in one process, so workspace-sensitive
services must never use ``os.chdir`` as a routing mechanism.  This module keeps
the active root in a ``ContextVar`` and validates it once at the boundary.
Ordinary CLI callers with no override use the process cwd.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

_WORKSPACE_ROOT: ContextVar[Path | None] = ContextVar(
    "HADES_CLI_WORKSPACE_ROOT", default=None
)


def resolve_workspace_root(root: str | Path | None = None) -> Path:
    """Return a normalized, existing directory for one CLI invocation.

    ``None`` means the current context override, or ``Path.cwd()`` for normal
    one-shot CLI calls.  Invalid roots fail closed with ``ValueError`` rather
    than silently selecting a different directory.
    """
    candidate = root if root is not None else _WORKSPACE_ROOT.get()
    if candidate is None:
        candidate = Path.cwd()
    try:
        normalized = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ValueError(f"workspace root is not a usable directory: {candidate}") from exc
    if not normalized.is_dir():
        raise ValueError(f"workspace root is not a directory: {normalized}")
    return normalized


def get_workspace_root() -> Path:
    """Return the validated root active in this context."""
    return resolve_workspace_root()


@contextmanager
def workspace_context(root: str | Path | None) -> Iterator[Path]:
    """Temporarily bind a validated workspace root to the current context.

    Contexts are nestable and thread/task-local.  The process cwd is never
    changed, and the prior value is restored even when the wrapped operation
    raises.
    """
    normalized = resolve_workspace_root(root)
    token = _WORKSPACE_ROOT.set(normalized)
    try:
        yield normalized
    finally:
        _WORKSPACE_ROOT.reset(token)
