"""Ratchet: core packages must import hades_* — not hermes_* module paths.

Hermes shims remain for plugins and external code. New core code should
import the canonical Hades names so dual-compat debt does not grow.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = [
    "agent",
    "gateway",
    "tools",
    "hades_cli",
    "cron",
    "tui_gateway",
    "acp_adapter",
]

SCAN_ROOT_MODULES = [
    "cli.py",
    "run_agent.py",
    "model_tools.py",
    "mcp_serve.py",
    "toolsets.py",
    "batch_runner.py",
    "utils.py",
]

# Module roots that must not be imported under the hermes_ spelling in core.
_FORBIDDEN = re.compile(
    r"^\s*(?:from|import)\s+hermes_(?:constants|state|time|logging|bootstrap|cli)\b"
)

# Sites deliberately still on hermes_* (should stay empty; grow only with
# documented temporary exceptions).
ALLOWLIST: set[str] = set()


def _iter_files():
    for name in SCAN_DIRS:
        root = REPO_ROOT / name
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))
    for name in SCAN_ROOT_MODULES:
        path = REPO_ROOT / name
        if path.is_file():
            yield path


def test_core_does_not_import_hermes_module_paths():
    offenders = []
    for path in _iter_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if not _FORBIDDEN.search(line):
                continue
            key = f"{rel}:{lineno}"
            if key in ALLOWLIST or any(
                stripped.startswith(a.partition("::")[2])
                for a in ALLOWLIST
                if a.startswith(f"{rel}::")
            ):
                continue
            offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        "Core code must import hades_* packages (hermes_* is shim-only). "
        "Convert these sites or, only with justification, add to ALLOWLIST "
        "in tests/test_hades_import_ratchet.py:\n" + "\n".join(offenders)
    )


def test_hades_helper_aliases_exist():
    from tools.environments.local import hades_subprocess_env, hermes_subprocess_env
    from tools.xai_http import hades_xai_user_agent, hermes_xai_user_agent
    from agent.portal_tags import hades_client_tag, hermes_client_tag

    assert hades_subprocess_env is hermes_subprocess_env
    assert hades_xai_user_agent is hermes_xai_user_agent
    assert hades_client_tag is hermes_client_tag
