"""Resolve HADES_HOME for standalone skill scripts.

Skill scripts may run outside the Hades process (e.g. system Python,
nix env, CI) where ``hermes_constants`` is not importable.  This module
provides the same ``get_hades_home()`` and ``display_hades_home()``
contracts as ``hermes_constants`` without requiring it on ``sys.path``.

When ``hermes_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``hades_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``HADES_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from hades_constants import display_hades_home as display_hades_home
    from hades_constants import get_hades_home as get_hades_home
except (ModuleNotFoundError, ImportError):

    def get_hades_home() -> Path:
        """Return the Hades home directory (default: ~/.hades).

        Mirrors ``hades_constants.get_hades_home()``."""
        val = os.environ.get("HADES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hades"

    def display_hades_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``hades_constants.display_hades_home()``."""
        home = get_hades_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
