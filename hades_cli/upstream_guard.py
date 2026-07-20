"""Detect a co-installed upstream ``hermes-agent`` PyPI distribution.

This fork ships as the ``hades-agent`` distribution but installs BOTH the
``hades*`` and ``hermes*`` console scripts.  If the upstream ``hermes-agent``
package is installed into the same environment it clobbers our hermes
compatibility shims (last-write-wins on the entry-point scripts), producing a
confusing mixed install.  This module detects that state and emits a one-line
warning with the remediation.

Our own distribution is named ``hades-agent``, so a plain metadata lookup of
``hermes-agent`` only ever matches the upstream package — it cannot be fooled
by this fork itself.
"""

from __future__ import annotations

import functools
import sys

from hades_constants import env_get

# Process-wide "already warned" latch so the warning prints at most once even
# when both the CLI startup hook and doctor run in the same process.
_warned = False

_REMEDIATION = "pip uninstall hermes-agent && pip install --force-reinstall hades-agent"


@functools.lru_cache(maxsize=1)
def detect_upstream_hermes_dist() -> str | None:
    """Return the installed version of the upstream ``hermes-agent``
    distribution, or ``None`` when it is not present in this environment."""
    import importlib.metadata

    try:
        return importlib.metadata.distribution("hermes-agent").version
    except importlib.metadata.PackageNotFoundError:
        return None


def warn_if_upstream_present(stream=None) -> bool:
    """Print a one-line co-install warning to *stream* (default stderr).

    Returns True only when a warning was actually printed.  Suppressed by the
    HADES_SUPPRESS_UPSTREAM_WARNING / HERMES_SUPPRESS_UPSTREAM_WARNING env
    knob, and prints at most once per process.
    """
    global _warned
    if _warned:
        return False
    if env_get("HADES_SUPPRESS_UPSTREAM_WARNING"):
        return False
    version = detect_upstream_hermes_dist()
    if version is None:
        return False
    if stream is None:
        stream = sys.stderr
    print(
        f"Warning: upstream hermes-agent {version} is co-installed and "
        f"clobbers hades-agent's hermes compatibility shims; fix with: "
        f"{_REMEDIATION}",
        file=stream,
    )
    _warned = True
    return True
