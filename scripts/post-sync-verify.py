#!/usr/bin/env python3
"""
Post-sync verification gate for hades-agent.

Catches the class of bugs that cause spontaneous NameError/ImportError
crashes after upstream syncs: incomplete hermes→hades renames, missing
imports, used-before-defined symbols.

Exit code 0 = all checks pass, 1 = issues found (details on stdout).

Usage:
    python3 scripts/post-sync-verify.py [--fix]

Without --fix: reports issues only.
With --fix: applies safe fixes (os.environ for bare env_set calls, etc.)
"""

import ast
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parent.parent
FIX_MODE = "--fix" in sys.argv

# Directories to skip
SKIP = {
    "__pycache__", ".worktrees", ".git", "node_modules", "venv", "dist",
    "build", ".eggs", "*.egg-info",
}


class Issue(NamedTuple):
    file: str
    line: int
    category: str
    message: str
    fixable: bool


def should_skip(path: Path) -> bool:
    for part in path.parts:
        if part in SKIP or part.endswith(".egg-info"):
            return True
    return False


def find_python_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.endswith(".egg-info")]
        for f in files:
            if f.endswith(".py") and "test" not in f.lower():
                p = Path(root) / f
                if not should_skip(p.relative_to(REPO)):
                    yield p


def check_import_order(filepath: Path, content: str) -> list[Issue]:
    """Find symbols used before their import."""
    issues = []
    lines = content.split("\n")

    # Find all import lines and what they import
    import_positions: dict[str, int] = {}  # symbol -> line number
    for i, line in enumerate(lines, 1):
        m = re.match(r"from\s+(\w+)\s+import\s+(.+)", line)
        if m:
            module = m.group(1)
            symbols = [s.strip().split(" as ")[0].strip() for s in m.group(2).split(",")]
            for sym in symbols:
                sym = sym.strip().lstrip("(").rstrip(")")
                if sym and sym not in import_positions:
                    import_positions[sym] = i

    # Find usages of imported symbols before their import
    # (only for symbols from hades_constants, hades_cli, etc.)
    hades_modules = {"hades_constants", "hades_cli", "hades_state", "hades_logging"}
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("'''") or stripped.startswith('"""'):
            continue
        # Skip lines inside function bodies (Python resolves at call time)
        # We only care about module-level code
        # Simple heuristic: if line is indented, it's inside a function/class
        if line and line[0] in (" ", "\t"):
            continue

        for sym, import_line in import_positions.items():
            if i >= import_line:
                continue
            if re.search(rf"\b{re.escape(sym)}\b", line):
                issues.append(Issue(
                    str(filepath.relative_to(REPO)), i, "IMPORT-ORDER",
                    f"`{sym}` used on line {i} but imported on line {import_line}",
                    fixable=False,
                ))
    return issues


def check_env_set_without_import(filepath: Path, content: str) -> list[Issue]:
    """Find env_set/env_get used without importing from hades_constants."""
    issues = []
    lines = content.split("\n")

    has_import = bool(re.search(r"from\s+hades_constants\s+import.*\benv_(set|get)\b", content))

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Module-level only
        if line and line[0] in (" ", "\t"):
            continue

        for func in ("env_set", "env_get"):
            if re.search(rf"\b{func}\b", line) and not has_import:
                issues.append(Issue(
                    str(filepath.relative_to(REPO)), i, "MISSING-IMPORT",
                    f"`{func}` used but not imported from hades_constants",
                    fixable=(func == "env_set"),
                ))
    return issues


def check_hermes_env_leaks(filepath: Path, content: str) -> list[Issue]:
    """Find HERMES_* env vars that should be HADES_*."""
    issues = []
    lines = content.split("\n")

    # Env vars that SHOULD be renamed
    RENAME = {
        "HADES_HOME", "HADES_QUIET", "HADES_SESSION_ID",
        "HADES_SESSION_SOURCE", "HADES_PREFILL_MESSAGES_FILE",
        "HADES_IGNORE_USER_CONFIG", "HERMES_DEV_BILLING_FIXTURE",
        "HERMES_DEFER_AGENT_STARTUP", "HERMES_ACCEPT_HOOKS",
        "HERMES_EXIT_WATCHDOG_S", "HERMES_REDACT_SECRETS",
        "HERMES_TUI_THEME", "HERMES_TUI_BACKGROUND",
        "HERMES_FAST_STARTUP_BANNER", "HERMES_MAX_TOKENS",
        "HERMES_INFERENCE_PROVIDER", "HERMES_MAX_ITERATIONS",
        "HERMES_IGNORE_RULES", "HERMES_EPHEMERAL_SYSTEM_PROMPT",
        "HERMES_SPINNER_PAUSE", "HERMES_TUI_SLASH_TIMEOUT_S",
        "HERMES_TUI_WS_ORPHAN_REAP_GRACE_S", "HERMES_TUI_RPC_POOL_WORKERS",
        "HERMES_TUI_SESSION_TTL_S", "HERMES_COMPUTE_HOST_CHILD",
        "HERMES_GATEWAY_DETACHED", "HERMES_RESTART_DRAIN_TIMEOUT",
    }
    # Env vars that should NOT be renamed
    KEEP = {"HERMES_PROFILE", "HERMES_KANBAN_", "HERMES_TUI", "HERMES_YOLO_MODE"}

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for var in RENAME:
            if var in line:
                # Check it's not already renamed
                hades_var = var.replace("HERMES_", "HADES_")
                if hades_var not in line:
                    # Check it's not in a KEEP pattern
                    skip = False
                    for keep in KEEP:
                        if keep in var:
                            skip = True
                            break
                    if not skip:
                        issues.append(Issue(
                            str(filepath.relative_to(REPO)), i, "ENV-LEAK",
                            f"`{var}` should be `{hades_var}`",
                            fixable=True,
                        ))
    return issues


def check_broken_imports() -> list[Issue]:
    """Try importing key modules to catch NameError/ImportError."""
    issues = []
    venv_python = REPO / "venv" / "bin" / "python3"
    if not venv_python.exists():
        issues.append(Issue("venv", 0, "MISSING-VENV", "venv/bin/python3 not found", False))
        return issues

    import subprocess
    checks = [
        ("from hades_cli.main import main", "hades_cli.main"),
        ("import cli", "cli.py"),
        ("from hades_constants import env_get, env_set", "hades_constants"),
    ]
    for code, module in checks:
        result = subprocess.run(
            [str(venv_python), "-c", code],
            capture_output=True, text=True, timeout=30,
            cwd=str(REPO),
        )
        if result.returncode != 0:
            err = result.stderr.strip().split("\n")[-1]
            issues.append(Issue(
                module, 0, "IMPORT-FAILURE",
                f"Import failed: {err}",
                fixable=False,
            ))
    return issues


def apply_fixes(issues: list[Issue]) -> int:
    """Apply safe fixes for fixable issues."""
    fixed = 0
    for issue in issues:
        if not issue.fixable:
            continue
        filepath = REPO / issue.file
        if not filepath.exists():
            continue
        content = filepath.read_text()
        lines = content.split("\n")
        line_idx = issue.line - 1
        if line_idx >= len(lines):
            continue
        line = lines[line_idx]

        if issue.category == "ENV-LEAK":
            # Replace HERMES_ with HADES_ on this line
            for var in ("HADES_HOME", "HADES_QUIET", "HADES_SESSION_ID",
                        "HADES_SESSION_SOURCE", "HADES_PREFILL_MESSAGES_FILE",
                        "HADES_IGNORE_USER_CONFIG"):
                hades_var = var.replace("HERMES_", "HADES_")
                line = line.replace(var, hades_var)
            lines[line_idx] = line
            filepath.write_text("\n".join(lines))
            fixed += 1
            print(f"  FIXED {issue.file}:{issue.line}: {issue.message}")

    return fixed


def main():
    print("=" * 60)
    print("Hades Post-Sync Verification Gate")
    print("=" * 60)
    print()

    all_issues: list[Issue] = []

    # 1. Check Python imports work
    print("Checking imports...")
    import_issues = check_broken_imports()
    all_issues.extend(import_issues)
    if import_issues:
        for i in import_issues:
            print(f"  FAIL {i.file}: {i.message}")
    else:
        print("  OK — key modules import successfully")

    # 2. Scan all Python files
    print("\nScanning source files...")
    file_count = 0
    for filepath in find_python_files():
        file_count += 1
        try:
            content = filepath.read_text()
        except Exception:
            continue
        all_issues.extend(check_import_order(filepath, content))
        all_issues.extend(check_env_set_without_import(filepath, content))
        all_issues.extend(check_hermes_env_leaks(filepath, content))
    print(f"  Scanned {file_count} Python files")

    # 3. Summary
    print()
    print("=" * 60)
    if not all_issues:
        print("ALL CHECKS PASSED — no rename/import issues found")
        print("=" * 60)
        sys.exit(0)

    # Group by category
    from collections import Counter
    cats = Counter(i.category for i in all_issues)
    print(f"ISSUES FOUND: {len(all_issues)}")
    for cat, count in cats.most_common():
        fixable = sum(1 for i in all_issues if i.category == cat and i.fixable)
        print(f"  {cat}: {count} ({fixable} auto-fixable)")

    # Show details
    print()
    for issue in sorted(all_issues):
        tag = "🔧" if issue.fixable else "⚠️"
        print(f"  {tag} {issue.file}:{issue.line} [{issue.category}] {issue.message}")

    # Apply fixes if requested
    if FIX_MODE:
        print("\nApplying auto-fixes...")
        fixed = apply_fixes(all_issues)
        print(f"\nFixed {fixed} issues. Re-run without --fix to verify.")
    else:
        fixable = sum(1 for i in all_issues if i.fixable)
        if fixable:
            print(f"\n{fixable} issues are auto-fixable. Run with --fix to apply.")

    print("=" * 60)
    sys.exit(1)


if __name__ == "__main__":
    main()
