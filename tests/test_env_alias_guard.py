"""Ratchet guard: no new single-spelling reads/writes of HADES_/HERMES_ env vars.

Direct ``os.getenv``/``os.environ`` access with a ``HADES_``- or
``HERMES_``-prefixed literal name only sees one spelling of the variable.
All such sites must go through the dual-spelling helpers in
``hades_constants`` (``env_get`` / ``env_set`` / ``env_pop`` /
``env_is_set``) so hermes- and hades-spelled environments agree.

Deliberately-left sites (e.g. the code_execution_tool child-source
templates, which run inside a sandbox child that may not have our modules
on its path, and docstring examples) are enumerated in ``ALLOWLIST`` below.
Do not add new entries for real code — convert the site instead.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = [
    "tools",
    "gateway",
    "cron",
    "hades_cli",
    "agent",
    "acp_adapter",
    "tui_gateway",
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

PATTERNS = [
    re.compile(r"os\.getenv\(\s*[\"'](?:HERMES_|HADES_)"),
    re.compile(
        r"os\.environ(?:\[|\.get\(|\.setdefault\(|\.pop\()\s*[\"'](?:HERMES_|HADES_)"
    ),
]

# "relative/posix/path::stripped-line prefix" entries for sites deliberately
# left as direct reads.  Matching is prefix-based on the stripped line.
ALLOWLIST = {
    # tools/approval.py docstring example (not code).
    'tools/approval.py::Use this instead of mutating ``os.environ["HADES_INTERACTIVE"]``',
    # gateway/session_context.py module/function docstrings documenting the
    # legacy os.environ session-state pattern this module replaced.
    'gateway/session_context.py::os.environ["HADES_SESSION_THREAD_ID"] = str(context.source.thread_id)',
    'gateway/session_context.py::``os.getenv("HADES_SESSION_*", ...)`` calls.',
    'gateway/session_context.py::platform = os.getenv("HADES_SESSION_PLATFORM", "")',
    'gateway/session_context.py::Drop-in replacement for ``os.getenv("HADES_SESSION_*", default)``.',
    # tools/code_execution_tool.py: triple-quoted child-source templates that
    # execute inside the sandbox child process, which may not have our
    # modules importable — they dual-read both spellings inline instead.
    'tools/code_execution_tool.py::endpoint = os.environ.get("HADES_RPC_SOCKET") or os.environ["HERMES_RPC_SOCKET"]',
    'tools/code_execution_tool.py::"token": os.environ.get("HADES_RPC_TOKEN") or os.environ.get("HERMES_RPC_TOKEN", ""),',
    'tools/code_execution_tool.py::_RPC_DIR = os.environ.get("HADES_RPC_DIR") or os.environ.get("HERMES_RPC_DIR")',
    'tools/code_execution_tool.py::endpoint = os.environ.get("HADES_KERNEL_SOCKET") or os.environ["HERMES_KERNEL_SOCKET"]',
    'tools/code_execution_tool.py::writer.write((json.dumps({"token": os.environ.get("HADES_KERNEL_TOKEN") or os.environ["HERMES_KERNEL_TOKEN"]})',
}


def _iter_scan_files():
    for name in SCAN_DIRS:
        root = REPO_ROOT / name
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))
    for name in SCAN_ROOT_MODULES:
        path = REPO_ROOT / name
        if path.is_file():
            yield path


def _allowed(rel_path: str, stripped: str) -> bool:
    for entry in ALLOWLIST:
        entry_path, _, prefix = entry.partition("::")
        if entry_path == rel_path and stripped.startswith(prefix):
            return True
    return False


def test_no_new_single_spelling_env_access():
    offenders = []
    for path in _iter_scan_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if not any(p.search(line) for p in PATTERNS):
                continue
            if _allowed(rel, stripped):
                continue
            offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        "Direct os.getenv/os.environ access to HADES_/HERMES_-prefixed env "
        "vars found. Use hades_constants.env_get/env_set/env_pop/env_is_set "
        "instead (they read/write BOTH prefix spellings), or — only for "
        "genuinely unavoidable sites — add an ALLOWLIST entry in "
        "tests/test_env_alias_guard.py:\n" + "\n".join(offenders)
    )
