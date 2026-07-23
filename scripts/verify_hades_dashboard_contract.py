#!/usr/bin/env python3
"""Verify the cross-surface Hades dashboard/TUI integration contract.

This is deliberately a small semantic verifier, not a TypeScript or Python
parser.  It checks the exact API method/route/verb surface, native TUI RPC
registration, response type exports, required RPC tests, and the integration
cron prompt.  It is safe to run from a sync job: expected file and manifest
problems become actionable diagnostics instead of tracebacks.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_MAX_API_BYTES = 512 * 1024
_MAX_SERVER_BYTES = 1024 * 1024
_MAX_TYPES_BYTES = 256 * 1024
_MAX_TEST_BYTES = 512 * 1024
_MAX_CRON_BYTES = 2 * 1024 * 1024

_INTEGRATION_JOB_NAME = "hades-fork-integration"
_INTEGRATION_HEADING = "## Integration Manifest + Handler Verification"
_INTEGRATION_COMMAND = "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py"
_POST_SYNC_MARKER = "scripts/post-sync-verify.py"


@dataclass(frozen=True)
class _ApiContract:
    name: str
    verb: str
    route_pattern: str
    route_display: str


def _quoted_route_pattern(route: str) -> str:
    """Match one complete quoted/backticked static route expression."""
    escaped = re.escape(route)
    return rf"(?P<quote>[\"'`]){escaped}(?P=quote)"


def _static_route_pattern(route: str, *, allow_query_template: bool = False) -> str:
    """Build a static route pattern, including known query-template forms."""
    exact = _quoted_route_pattern(route)
    if not allow_query_template:
        return exact
    # A few read methods append a query built by a separate template
    # interpolation.  The endpoint path itself must still end exactly at the
    # interpolation boundary; literal suffixes such as ``-v2``, ``/extra``, or
    # ``?unexpected=1`` do not match this alternative.
    return rf"(?:{exact}|`{re.escape(route)}\$\{{)"


_ENCODED_PARAMETER = (
    r"\$\{\s*encodeURIComponent\s*\(\s*"
    r"[A-Za-z_$][\w$]*\s*\)\s*\}"
)


def _dynamic_route_pattern(route_prefix: str, suffix: str = "") -> str:
    """Match an exact backticked encoded-parameter route template."""
    return f"`{re.escape(route_prefix)}{_ENCODED_PARAMETER}{re.escape(suffix)}`"


def _audit_route_pattern() -> str:
    """Match the audit endpoint's required literal query prefix.

    The implementation appends an optional verdict interpolation after the
    required ``limit`` parameter.  Keep that known form while rejecting a
    changed endpoint path before it.
    """
    return r"(?:`/api/autonomy/audit\?limit=\$\{\s*limit\s*\}|['\"]/api/autonomy/audit['\"])"


_API_CONTRACTS = (
    _ApiContract(
        "getAutonomyStatus",
        "GET",
        _static_route_pattern("/api/autonomy/status"),
        "/api/autonomy/status",
    ),
    _ApiContract(
        "getAutonomyRules",
        "GET",
        _static_route_pattern("/api/autonomy/rules", allow_query_template=True),
        "/api/autonomy/rules",
    ),
    _ApiContract(
        "explainAutonomyRule",
        "GET",
        _dynamic_route_pattern("/api/autonomy/rules/"),
        "/api/autonomy/rules/${encodeURIComponent(ruleId)}",
    ),
    _ApiContract(
        "previewAutonomyChange",
        "POST",
        _static_route_pattern("/api/autonomy/preview"),
        "/api/autonomy/preview",
    ),
    _ApiContract(
        "applyAutonomyPreview",
        "POST",
        _static_route_pattern("/api/autonomy/apply"),
        "/api/autonomy/apply",
    ),
    _ApiContract(
        "acceptAutonomySuggestion",
        "POST",
        _dynamic_route_pattern("/api/autonomy/suggestions/", "/accept"),
        "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/accept",
    ),
    _ApiContract(
        "rejectAutonomySuggestion",
        "POST",
        _dynamic_route_pattern("/api/autonomy/suggestions/", "/reject"),
        "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/reject",
    ),
    _ApiContract(
        "getAutonomyMandates",
        "GET",
        _static_route_pattern("/api/autonomy/mandates", allow_query_template=True),
        "/api/autonomy/mandates",
    ),
    _ApiContract(
        "revokeAutonomyMandate",
        "POST",
        _dynamic_route_pattern("/api/autonomy/mandates/", "/revoke"),
        "/api/autonomy/mandates/${encodeURIComponent(ruleId)}/revoke",
    ),
    _ApiContract(
        "getAutonomyAudit",
        "GET",
        _audit_route_pattern(),
        "/api/autonomy/audit",
    ),
    _ApiContract(
        "getReceipts",
        "GET",
        _static_route_pattern("/api/receipts", allow_query_template=True),
        "/api/receipts",
    ),
    _ApiContract(
        "getReceipt",
        "GET",
        _dynamic_route_pattern("/api/receipts/"),
        "/api/receipts/${encodeURIComponent(receiptId)}",
    ),
    _ApiContract(
        "getReceiptObservations",
        "GET",
        _dynamic_route_pattern("/api/receipts/", "/observations"),
        "/api/receipts/${encodeURIComponent(receiptId)}/observations",
    ),
)

_REQUIRED_RPC_TESTS = (
    ("tests/tui_gateway/test_autonomy_rpc.py", "autonomy RPC test"),
    ("tests/tui_gateway/test_receipt_rpc.py", "receipt RPC test"),
    (
        "tests/tui_gateway/test_transaction_rpc.py",
        "transaction RPC test (same-sync dependency)",
    ),
)
_REQUIRED_RESPONSE_EXPORTS = (
    "AutonomyExecResponse",
    "ReceiptExecResponse",
    "TransactionExecResponse",
)
_REQUIRED_HANDLERS = ("autonomy.exec", "receipt.exec", "transaction.exec")


def _path_label(path: Path, repo_root: Path) -> str:
    """Prefer stable repo-relative paths in diagnostics."""
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _read_text(
    path: Path,
    *,
    repo_root: Path,
    category: str,
    failures: list[str],
    max_bytes: int,
) -> str | None:
    """Read a bounded UTF-8 source file and report expected failures."""
    label = _path_label(path, repo_root)
    try:
        if not path.exists():
            failures.append(f"{category}: missing file {label}")
            return None
        if not path.is_file():
            failures.append(f"{category}: expected file but found non-file {label}")
            return None
        size = path.stat().st_size
        if size == 0:
            failures.append(f"{category}: empty file {label}")
            return None
        if size > max_bytes:
            failures.append(
                f"{category}: {label} is too large to inspect ({size} bytes; "
                f"limit {max_bytes} bytes)"
            )
            return None
        text = path.read_text(encoding="utf-8")
    except PermissionError:
        failures.append(f"{category}: permission denied reading {label}")
        return None
    except UnicodeError:
        failures.append(f"{category}: {label} is not valid UTF-8 text")
        return None
    except OSError as exc:
        failures.append(f"{category}: cannot read {label}: {exc.strerror or exc}")
        return None
    if not text.strip():
        failures.append(f"{category}: empty file {label}")
        return None
    return text


def _api_object(text: str) -> str | None:
    """Return the body of the exported ``api`` object."""
    declaration = re.search(r"\bexport\s+const\s+api\s*=\s*\{", text)
    if declaration is None:
        return None
    body_start = declaration.end()
    closing = re.search(r"(?m)^[ \t]*\};[ \t]*$", text[body_start:])
    if closing is None:
        return None
    return text[body_start : body_start + closing.start()]


def _api_properties(body: str) -> dict[str, list[tuple[int, int]]]:
    """Index top-level object properties using indentation, not a TS parser."""
    candidates = list(
        re.finditer(
            r"(?m)^(?P<indent>[ \t]+)(?P<name>[A-Za-z_$][\w$]*)\s*:",
            body,
        )
    )
    if not candidates:
        return {}
    indent_widths = [len(match.group("indent").expandtabs(4)) for match in candidates]
    top_width = min(indent_widths)
    top = [
        match
        for match in candidates
        if len(match.group("indent").expandtabs(4)) == top_width
    ]
    properties: dict[str, list[tuple[int, int]]] = {}
    for index, match in enumerate(top):
        end = top[index + 1].start() if index + 1 < len(top) else len(body)
        properties.setdefault(match.group("name"), []).append((match.start(), end))
    return properties


def _strip_ts_comments(text: str) -> str:
    """Blank TypeScript comments without touching quoted route expressions.

    This deliberately remains a small lexical state machine rather than a
    TypeScript parser.  It preserves all characters inside single-quoted,
    double-quoted, and backticked strings (including escaped delimiters),
    while replacing comment characters with spaces and retaining newlines so
    diagnostics and method boundaries remain stable.
    """
    chars = list(text)
    state = "normal"
    quote = ""
    index = 0
    while index < len(chars):
        char = chars[index]
        next_char = chars[index + 1] if index + 1 < len(chars) else ""

        if state == "normal":
            if char == "/" and next_char == "/":
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if char in ("'", '"', "`"):
                quote = char
                state = "quoted"
            index += 1
            continue

        if state == "line_comment":
            if char in ("\n", "\r"):
                state = "normal"
            else:
                chars[index] = " "
            index += 1
            continue

        if state == "block_comment":
            if char == "*" and next_char == "/":
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
                state = "normal"
                continue
            if char not in ("\n", "\r"):
                chars[index] = " "
            index += 1
            continue

        # quoted string/template literal: preserve route text and escaped
        # delimiters.  Template interpolation is intentionally kept intact;
        # the API contract only needs its route expression and this avoids
        # mistaking comment-looking text inside a template string for source.
        if char == "\\":
            index += 2
            continue
        if char == quote:
            state = "normal"
            quote = ""
        index += 1

    return "".join(chars)


def _check_api(repo_root: Path, failures: list[str]) -> None:
    path = repo_root / "web/src/lib/api.ts"
    text = _read_text(
        path,
        repo_root=repo_root,
        category="api",
        failures=failures,
        max_bytes=_MAX_API_BYTES,
    )
    if text is None:
        return
    body = _api_object(text)
    if body is None:
        failures.append("api: missing or unterminated `export const api = { ... }` object")
        return
    properties = _api_properties(body)
    if not properties:
        failures.append("api: exported api object has no inspectable method properties")
        return

    for contract in _API_CONTRACTS:
        spans = properties.get(contract.name, [])
        if not spans:
            failures.append(f"api: missing method {contract.name}")
            continue
        if len(spans) != 1:
            failures.append(
                f"api: method {contract.name} must appear exactly once (found {len(spans)}; duplicate method)"
            )
            continue
        start, end = spans[0]
        method_body = _strip_ts_comments(body[start:end])

        if contract.verb == "GET":
            if not re.search(r"\b(?:apiGet|fetchJSON)\s*(?:<[^>\n]+>)?\s*\(", method_body):
                failures.append(
                    f"api: method {contract.name} expected GET via apiGet/fetchJSON helper"
                )
            for method_match in re.finditer(
                r"\bmethod\s*:\s*[\"'](?P<verb>[A-Za-z]+)[\"']", method_body
            ):
                if method_match.group("verb").upper() != "GET":
                    failures.append(
                        f"api: method {contract.name} has {method_match.group('verb').upper()} verb; expected GET"
                    )
                    break
        else:
            if not re.search(r"\bfetch(?:JSON)?\s*(?:<[^>\n]+>)?\s*\(", method_body):
                failures.append(
                    f"api: method {contract.name} expected fetch/fetchJSON with POST"
                )
            post_match = re.search(
                r"\bmethod\s*:\s*[\"'](?P<verb>[A-Za-z]+)[\"']", method_body
            )
            if post_match is None:
                failures.append(
                    f"api: method {contract.name} missing POST HTTP verb (expected method: POST)"
                )
            elif post_match.group("verb").upper() != "POST":
                failures.append(
                    f"api: method {contract.name} has {post_match.group('verb').upper()} verb; expected POST"
                )

        if re.search(contract.route_pattern, method_body) is None:
            failures.append(
                f"api: method {contract.name} missing exact route {contract.route_display}"
            )


def _check_rpc_tests(repo_root: Path, failures: list[str]) -> None:
    for relative, description in _REQUIRED_RPC_TESTS:
        _read_text(
            repo_root / relative,
            repo_root=repo_root,
            category=f"rpc-tests ({description})",
            failures=failures,
            max_bytes=_MAX_TEST_BYTES,
        )


def _method_decorator_handler(decorator: ast.AST) -> str | None:
    """Return the literal handler from a strict ``@method("...")`` call."""
    if not isinstance(decorator, ast.Call):
        return None
    if not isinstance(decorator.func, ast.Name) or decorator.func.id != "method":
        return None
    if len(decorator.args) != 1 or decorator.keywords:
        return None
    argument = decorator.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def _assignment_to_long_handlers(node: ast.AST) -> ast.Assign | ast.AnnAssign | None:
    """Return an assignment whose target is exactly ``_LONG_HANDLERS``."""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return None
    if any(isinstance(target, ast.Name) and target.id == "_LONG_HANDLERS" for target in targets):
        return node
    return None


def _string_constants(node: ast.AST | None) -> list[str]:
    """Collect literal strings from one assignment RHS, including wrappers."""
    if node is None:
        return []
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _check_server(repo_root: Path, failures: list[str]) -> None:
    path = repo_root / "tui_gateway/server.py"
    text = _read_text(
        path,
        repo_root=repo_root,
        category="server",
        failures=failures,
        max_bytes=_MAX_SERVER_BYTES,
    )
    if text is None:
        return

    label = _path_label(path, repo_root)
    try:
        tree = ast.parse(text, filename=label)
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno is not None else "unknown line"
        failures.append(f"server: invalid Python syntax in {label} at {location}: {exc.msg}")
        return

    registrations = {handler: 0 for handler in _REQUIRED_HANDLERS}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            handler = _method_decorator_handler(decorator)
            if handler in registrations:
                registrations[handler] += 1

    for handler in _REQUIRED_HANDLERS:
        count = registrations[handler]
        if count != 1:
            failures.append(
                f"server: handler registration {handler!r} must appear exactly once "
                f"(found {count}; missing or duplicate registration)"
            )

    assignments = [
        assignment
        for node in ast.walk(tree)
        if (assignment := _assignment_to_long_handlers(node)) is not None
    ]
    if not assignments:
        failures.append(
            "server: could not locate live _LONG_HANDLERS assignment; "
            "registration text alone is insufficient"
        )
        return
    if len(assignments) != 1:
        failures.append(
            "server: _LONG_HANDLERS assignment must appear exactly once "
            f"(found {len(assignments)}; duplicate or ambiguous assignment)"
        )
        return

    assignment = assignments[0]
    rhs = assignment.value
    values = _string_constants(rhs)
    if not values:
        failures.append(
            "server: _LONG_HANDLERS assignment RHS has no inspectable string constants"
        )
    for handler in _REQUIRED_HANDLERS:
        count = values.count(handler)
        if count != 1:
            failures.append(
                f"server: _LONG_HANDLERS must contain {handler!r} exactly once (found {count})"
            )


def _check_response_exports(repo_root: Path, failures: list[str]) -> None:
    path = repo_root / "ui-tui/src/gatewayTypes.ts"
    text = _read_text(
        path,
        repo_root=repo_root,
        category="types",
        failures=failures,
        max_bytes=_MAX_TYPES_BYTES,
    )
    if text is None:
        return
    for name in _REQUIRED_RESPONSE_EXPORTS:
        pattern = rf"(?m)^[ \t]*export\s+(?:interface|type)\s+{re.escape(name)}\b"
        count = len(re.findall(pattern, text))
        if count == 0:
            failures.append(f"types: missing response export {name}")
        elif count != 1:
            failures.append(
                f"types: response export {name} must appear exactly once (found {count}; duplicate export)"
            )


def _check_cron(repo_root: Path, cron_jobs: Path, failures: list[str]) -> None:
    text = _read_text(
        cron_jobs,
        repo_root=repo_root,
        category="cron",
        failures=failures,
        max_bytes=_MAX_CRON_BYTES,
    )
    if text is None:
        return
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        failures.append(f"cron: invalid JSON in {_path_label(cron_jobs, repo_root)}: {exc.msg}")
        return

    if not isinstance(manifest, dict):
        failures.append("cron: manifest root must be a JSON object")
        return
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        failures.append("cron: manifest must contain a jobs list")
        return
    if any(not isinstance(job, dict) for job in jobs):
        failures.append("cron: every entry in jobs must be an object")
        return

    integration_jobs = [job for job in jobs if job.get("name") == _INTEGRATION_JOB_NAME]
    if len(integration_jobs) != 1:
        failures.append(
            "cron: expected exactly one job named "
            f"{_INTEGRATION_JOB_NAME!r}, found {len(integration_jobs)}"
        )
        return
    prompt = integration_jobs[0].get("prompt")
    if not isinstance(prompt, str):
        failures.append("cron: hades-fork-integration prompt must be a string")
        return

    if _INTEGRATION_HEADING not in prompt:
        failures.append(
            "cron: hades-fork-integration prompt is missing heading "
            f"{_INTEGRATION_HEADING!r}"
        )
    command_position = prompt.find(_INTEGRATION_COMMAND)
    if command_position < 0:
        failures.append(
            "cron: hades-fork-integration prompt is missing exact verifier command "
            f"{_INTEGRATION_COMMAND!r}"
        )
    post_sync_position = prompt.find(_POST_SYNC_MARKER)
    if post_sync_position < 0:
        failures.append(
            "cron: hades-fork-integration prompt is missing the post-sync-verify.py anchor"
        )
    elif command_position >= 0 and command_position > post_sync_position:
        failures.append(
            "cron: verifier command must appear before the first scripts/post-sync-verify.py occurrence"
        )


def verify(repo_root: Path, cron_jobs: Path) -> list[str]:
    """Return deterministic diagnostics for integration-contract failures.

    An empty list means every contract check passed.  All paths are resolved
    only for stable diagnostics; no files are modified.
    """
    repo_root = Path(repo_root).expanduser()
    cron_jobs = Path(cron_jobs).expanduser()
    failures: list[str] = []

    if not repo_root.exists():
        return [f"repo: missing repository root {repo_root}"]
    if not repo_root.is_dir():
        return [f"repo: repository root is not a directory {repo_root}"]

    _check_api(repo_root, failures)
    _check_rpc_tests(repo_root, failures)
    _check_server(repo_root, failures)
    _check_response_exports(repo_root, failures)
    _check_cron(repo_root, cron_jobs, failures)
    return failures


def _parser() -> argparse.ArgumentParser:
    script_root = Path(__file__).resolve().parent.parent
    return argparse.ArgumentParser(description=__doc__)


def main(argv: Iterable[str] | None = None) -> int:
    parser = _parser()
    script_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=script_root,
        help="repository root (default: the parent of scripts/)",
    )
    parser.add_argument(
        "--cron-jobs",
        type=Path,
        default=Path("~/.hermes/cron/jobs.json").expanduser(),
        help="cron manifest path (default: ~/.hermes/cron/jobs.json)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        failures = verify(args.repo_root, args.cron_jobs)
    except Exception as exc:  # defensive boundary for sync jobs; no traceback
        failures = [f"verifier: unexpected failure while checking contract: {exc}"]

    if failures:
        print("FAIL: Hades dashboard/TUI integration contract")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PASS: Hades dashboard/TUI integration contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
