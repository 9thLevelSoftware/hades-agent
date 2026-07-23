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
import os
import re
import stat
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
_CRON_SHELL_LANGUAGES = frozenset({"bash", "sh", "shell"})


@dataclass(frozen=True)
class _ApiContract:
    name: str
    verb: str
    route_expression: str
    route_display: str
    plain_static_route: bool = False


_API_CONTRACTS = (
    _ApiContract(
        "getAutonomyStatus",
        "GET",
        "/api/autonomy/status",
        "/api/autonomy/status",
        True,
    ),
    _ApiContract(
        "getAutonomyRules",
        "GET",
        '`/api/autonomy/rules${qs ? `?${qs}` : ""}`',
        "/api/autonomy/rules",
    ),
    _ApiContract(
        "explainAutonomyRule",
        "GET",
        "`/api/autonomy/rules/${encodeURIComponent(ruleId)}`",
        "/api/autonomy/rules/${encodeURIComponent(ruleId)}",
    ),
    _ApiContract(
        "previewAutonomyChange",
        "POST",
        "/api/autonomy/preview",
        "/api/autonomy/preview",
        True,
    ),
    _ApiContract(
        "applyAutonomyPreview",
        "POST",
        "/api/autonomy/apply",
        "/api/autonomy/apply",
        True,
    ),
    _ApiContract(
        "acceptAutonomySuggestion",
        "POST",
        "`/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/accept`",
        "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/accept",
    ),
    _ApiContract(
        "rejectAutonomySuggestion",
        "POST",
        "`/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/reject`",
        "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/reject",
    ),
    _ApiContract(
        "getAutonomyMandates",
        "GET",
        '`/api/autonomy/mandates${state ? `?state=${encodeURIComponent(state)}` : ""}`',
        "/api/autonomy/mandates",
    ),
    _ApiContract(
        "revokeAutonomyMandate",
        "POST",
        "`/api/autonomy/mandates/${encodeURIComponent(ruleId)}/revoke`",
        "/api/autonomy/mandates/${encodeURIComponent(ruleId)}/revoke",
    ),
    _ApiContract(
        "getAutonomyAudit",
        "GET",
        '`/api/autonomy/audit?limit=${limit}${verdict ? `&verdict=${encodeURIComponent(verdict)}` : ""}`',
        "/api/autonomy/audit",
    ),
    _ApiContract(
        "getReceipts",
        "GET",
        '`/api/receipts${qs ? `?${qs}` : ""}`',
        "/api/receipts",
    ),
    _ApiContract(
        "getReceipt",
        "GET",
        "`/api/receipts/${encodeURIComponent(receiptId)}`",
        "/api/receipts/${encodeURIComponent(receiptId)}",
    ),
    _ApiContract(
        "getReceiptObservations",
        "GET",
        "`/api/receipts/${encodeURIComponent(receiptId)}/observations`",
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
    confine_to_repo: bool = True,
) -> str | None:
    """Read a bounded UTF-8 source file and report expected failures."""
    label = _path_label(path, repo_root)
    fd: int | None = None
    try:
        if confine_to_repo:
            try:
                relative = path.relative_to(repo_root)
            except ValueError:
                failures.append(
                    f"{category}: path is outside repository root {label}"
                )
                return None
            current = repo_root
            for component in relative.parts:
                current /= component
                try:
                    mode = current.lstat().st_mode
                except FileNotFoundError:
                    failures.append(f"{category}: missing file {label}")
                    return None
                except OSError as exc:
                    failures.append(
                        f"{category}: cannot inspect {label}: {exc.strerror or exc}"
                    )
                    return None
                if stat.S_ISLNK(mode):
                    failures.append(
                        f"{category}: refusing symlink path component {label}; "
                        "repository assets must remain beneath the repository root"
                    )
                    return None
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(repo_root)
            except (FileNotFoundError, RuntimeError):
                failures.append(f"{category}: missing file {label}")
                return None
            except ValueError:
                failures.append(
                    f"{category}: refusing path outside repository root {label}"
                )
                return None
            except OSError as exc:
                failures.append(
                    f"{category}: cannot resolve {label}: {exc.strerror or exc}"
                )
                return None

        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            failures.append(f"{category}: missing file {label}")
            return None
        except OSError as exc:
            failures.append(
                f"{category}: cannot inspect {label}: {exc.strerror or exc}"
            )
            return None
        if stat.S_ISLNK(path_stat.st_mode):
            failures.append(
                f"{category}: refusing symlink file {label}; no-follow reads are required"
            )
            return None
        if not stat.S_ISREG(path_stat.st_mode):
            failures.append(f"{category}: expected file but found non-file {label}")
            return None
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        descriptor_stat = os.fstat(fd)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            failures.append(f"{category}: expected regular file {label}")
            return None
        if descriptor_stat.st_size == 0:
            failures.append(f"{category}: empty file {label}")
            return None
        if descriptor_stat.st_size > max_bytes:
            failures.append(
                f"{category}: {label} is too large to inspect ({descriptor_stat.st_size} bytes; "
                f"limit {max_bytes} bytes)"
            )
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                failures.append(
                    f"{category}: {label} grew beyond inspection limit {max_bytes} bytes"
                )
                return None
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            failures.append(f"{category}: {label} is not valid UTF-8 text")
            return None
    except PermissionError:
        failures.append(f"{category}: permission denied reading {label}")
        return None
    except OSError as exc:
        failures.append(f"{category}: cannot read {label}: {exc.strerror or exc}")
        return None
    finally:
        if fd is not None:
            os.close(fd)
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


def _skip_ts_quoted(text: str, start: int, quote: str) -> int | None:
    """Return the index after a quoted string, or ``None`` if unterminated."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if char in ("\n", "\r"):
            return None
        index += 1
    return None


def _skip_ts_comment(text: str, start: int) -> int | None:
    """Return the index after a comment beginning at ``start``."""
    if text.startswith("//", start):
        newline = text.find("\n", start + 2)
        return len(text) if newline < 0 else newline
    if text.startswith("/*", start):
        closing = text.find("*/", start + 2)
        return None if closing < 0 else closing + 2
    return None


def _skip_ts_template(text: str, start: int) -> int | None:
    """Skip a template literal, including nested ``${...}`` expressions."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            return index + 1
        if char == "$" and index + 1 < len(text) and text[index + 1] == "{":
            end = _skip_ts_balanced_group(text, index + 1)
            if end is None:
                return None
            index = end
            continue
        index += 1
    return None


def _skip_ts_balanced_group(text: str, start: int) -> int | None:
    """Skip one balanced ``()``, ``{}``, or ``[]`` group lexically."""
    opening = text[start] if start < len(text) else ""
    closing_for = {"(": ")", "{": "}", "[": "]"}
    if opening not in closing_for:
        return None
    stack = [opening]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char in ("'", '"'):
            end = _skip_ts_quoted(text, index, char)
            if end is None:
                return None
            index = end
            continue
        if char == "`":
            end = _skip_ts_template(text, index)
            if end is None:
                return None
            index = end
            continue
        if char in "([{":
            stack.append(char)
            index += 1
            continue
        if char in ")]}":
            if not stack or closing_for[stack[-1]] != char:
                return None
            stack.pop()
            index += 1
            if not stack:
                return index
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] in "/*":
            end = _skip_ts_comment(text, index)
            if end is None:
                return None
            index = end
            continue
        index += 1
    return None


def _split_top_level(text: str, delimiter: str = ",") -> list[str] | None:
    """Split text on delimiters outside balanced groups and literals."""
    pieces: list[str] = []
    start = 0
    stack: list[str] = []
    closing_for = {"(": ")", "{": "}", "[": "]"}
    index = 0
    while index < len(text):
        char = text[index]
        if char in ("'", '"'):
            end = _skip_ts_quoted(text, index, char)
            if end is None:
                return None
            index = end
            continue
        if char == "`":
            end = _skip_ts_template(text, index)
            if end is None:
                return None
            index = end
            continue
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or closing_for[stack[-1]] != char:
                return None
            stack.pop()
        elif char == delimiter and not stack:
            pieces.append(text[start:index].strip())
            start = index + 1
        index += 1
    if stack:
        return None
    pieces.append(text[start:].strip())
    return pieces


def _extract_call_arguments(text: str, opening: int) -> tuple[str, ...] | None:
    """Extract one call's top-level comma-separated argument expressions."""
    inner_end = _skip_ts_balanced_group(text, opening)
    if inner_end is None:
        return None
    inner = text[opening + 1 : inner_end - 1]
    if not inner.strip():
        return ()
    pieces = _split_top_level(inner)
    if pieces is None:
        return None
    while len(pieces) > 1 and not pieces[-1]:
        pieces.pop()
    return tuple(pieces)


def _skip_ts_type_arguments(text: str, start: int) -> int | None:
    """Skip a simple generic type argument list before a call parenthesis."""
    if start >= len(text) or text[start] != "<":
        return start
    depth = 1
    index = start + 1
    while index < len(text):
        char = text[index]
        if char in ("'", '"'):
            end = _skip_ts_quoted(text, index, char)
            if end is None:
                return None
            index = end
            continue
        if char == "`":
            end = _skip_ts_template(text, index)
            if end is None:
                return None
            index = end
            continue
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


@dataclass(frozen=True)
class _TransportCall:
    helper: str
    arguments: tuple[str, ...]
    direct: bool


def _transport_calls(text: str) -> tuple[list[_TransportCall], bool]:
    """Extract fetchJSON/apiGet calls, reporting malformed lexical calls."""
    calls: list[_TransportCall] = []
    index = 0
    arrow_count = 0
    function_seen = False
    while index < len(text):
        char = text[index]
        if char in ("'", '"'):
            end = _skip_ts_quoted(text, index, char)
            if end is None:
                return calls, True
            index = end
            continue
        if char == "`":
            end = _skip_ts_template(text, index)
            if end is None:
                return calls, True
            index = end
            continue
        if text.startswith("=>", index):
            arrow_count += 1
            index += 2
            continue
        if char.isalpha() or char in "_$":
            end = index + 1
            while end < len(text) and (text[end].isalnum() or text[end] in "_$"):
                end += 1
            word = text[index:end]
            if word == "function":
                function_seen = True
            if word in {"fetchJSON", "apiGet"}:
                cursor = end
                while cursor < len(text) and text[cursor].isspace():
                    cursor += 1
                if cursor < len(text) and text[cursor] == "<":
                    cursor = _skip_ts_type_arguments(text, cursor)
                    if cursor is None:
                        return calls, True
                    while cursor < len(text) and text[cursor].isspace():
                        cursor += 1
                if cursor < len(text) and text[cursor] == "(":
                    arguments = _extract_call_arguments(text, cursor)
                    if arguments is None:
                        return calls, True
                    calls.append(
                        _TransportCall(
                            word,
                            arguments,
                            direct=arrow_count <= 1 and not function_seen,
                        )
                    )
            index = end
            continue
        index += 1
    return calls, False


def _normalize_ts_expression(expression: str) -> str:
    """Remove whitespace outside quoted/template literal text."""
    normalized: list[str] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        if char in ("'", '"'):
            end = _skip_ts_quoted(expression, index, char)
            if end is None:
                normalized.append(expression[index:])
                break
            normalized.append(expression[index:end])
            index = end
            continue
        if char == "`":
            end = _skip_ts_template(expression, index)
            if end is None:
                normalized.append(expression[index:])
                break
            normalized.append(_normalize_ts_template(expression, index, end))
            index = end
            continue
        normalized.append(char)
        index += 1
    return "".join(normalized)


def _normalize_ts_template(text: str, start: int, end: int) -> str:
    """Normalize whitespace in template interpolations, not literal chunks."""
    normalized: list[str] = ["`"]
    index = start + 1
    while index < end - 1:
        char = text[index]
        if char == "\\":
            normalized.append(text[index : min(index + 2, end - 1)])
            index += 2
            continue
        if char == "$" and index + 1 < end - 1 and text[index + 1] == "{":
            interpolation_end = _skip_ts_balanced_group(text, index + 1)
            if interpolation_end is None or interpolation_end > end:
                normalized.append(text[index : end - 1])
                break
            normalized.append("${")
            normalized.append(
                _normalize_ts_expression(text[index + 2 : interpolation_end - 1])
            )
            normalized.append("}")
            index = interpolation_end
            continue
        normalized.append(char)
        index += 1
    normalized.append("`")
    return "".join(normalized)


def _route_matches(expression: str, contract: _ApiContract) -> bool:
    actual = _normalize_ts_expression(expression)
    if contract.plain_static_route:
        return actual in {
            _normalize_ts_expression(f'"{contract.route_expression}"'),
            _normalize_ts_expression(f"'{contract.route_expression}'"),
            _normalize_ts_expression(f"`{contract.route_expression}`"),
        }
    return actual == _normalize_ts_expression(contract.route_expression)


def _inspect_options(options: str) -> tuple[str | None, str | None]:
    """Inspect a static request-options object and its top-level method field."""
    stripped = options.strip()
    if not stripped.startswith("{"):
        return None, "second argument must be an object literal"
    end = _skip_ts_balanced_group(stripped, 0)
    if end is None or stripped[end:].strip():
        return None, "request options object is malformed or unbalanced"
    fields = _split_top_level(stripped[1 : end - 1])
    if fields is None:
        return None, "request options object has malformed members"
    property_pattern = re.compile(
        r"^\s*(?P<key>[A-Za-z_$][\w$]*|'[^']*'|\"[^\"]*\")\s*:\s*(?P<value>.*)\s*$",
        re.DOTALL,
    )
    method_values: list[str] = []
    for field in fields:
        if not field:
            continue
        stripped_field = field.strip()
        if stripped_field.startswith("..."):
            return None, "request options must not contain spread members"
        if stripped_field.startswith("["):
            return None, "request options must not contain computed members"
        match = property_pattern.match(field)
        if match is None:
            return None, "request options contains a dynamic or shorthand member"
        key = match.group("key")
        if key[:1] in {"'", '"'}:
            key = key[1:-1]
        if key != "method":
            continue
        value = match.group("value").strip()
        if not value or value[:1] not in {"'", '"'}:
            return None, "method property must be a literal string"
        value_end = _skip_ts_quoted(value, 0, value[0])
        if value_end is None or value[value_end:].strip():
            return None, "method property must be a literal string"
        method_values.append(value[1 : value_end - 1])
    if not method_values:
        return None, "request options must contain exactly one top-level method property"
    if len(method_values) != 1:
        return None, "request options contains duplicate top-level method properties"
    return method_values[0], None


def _options_method(options: str) -> str | None:
    """Return a valid live top-level ``method`` property, if any."""
    method, _reason = _inspect_options(options)
    return method


def _get_call_is_valid(call: _TransportCall) -> bool:
    """GET contracts accept exactly the route argument and nothing else."""
    return len(call.arguments) == 1


def _post_call_is_valid(call: _TransportCall) -> bool:
    """POST requires exactly two arguments and a literal POST options field."""
    return len(call.arguments) == 2 and _options_method(call.arguments[1]) == "POST"


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

        calls, malformed = _transport_calls(method_body)
        if malformed:
            failures.append(
                f"api: method {contract.name} has malformed or unbalanced transport call"
            )
            failures.append(
                f"api: method {contract.name} missing exact route {contract.route_display} "
                "because the transport call could not be parsed"
            )
            continue
        if len(calls) != 1:
            failures.append(
                f"api: method {contract.name} must contain exactly one direct "
                f"apiGet/fetchJSON transport call (found {len(calls)})"
            )
            continue
        call = calls[0]
        if not call.direct:
            failures.append(
                f"api: method {contract.name} transport call must be direct to the "
                "method implementation, not nested in another function"
            )
            continue

        if not call.arguments or not _route_matches(call.arguments[0], contract):
            failures.append(
                f"api: method {contract.name} missing exact route {contract.route_display}"
            )
            continue

        if contract.verb == "GET":
            if not _get_call_is_valid(call):
                failures.append(
                    f"api: method {contract.name} GET transport call must have exactly one argument "
                    "(the canonical route; request options are not allowed)"
                )
        else:
            if len(call.arguments) != 2:
                failures.append(
                    f"api: method {contract.name} POST transport call must have exactly two arguments "
                    "(route and static request options; HTTP verb must be explicit)"
                )
                continue
            method, reason = _inspect_options(call.arguments[1])
            if method != "POST":
                failures.append(
                    f"api: method {contract.name} missing POST HTTP verb on the matching call "
                    "(expected method: POST; "
                    f"{reason or 'method property must be the literal string POST'})"
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


def _static_string_values(node: ast.AST | None) -> tuple[list[str] | None, str | None]:
    """Evaluate the runtime-selected strings in a conservative AST subset."""
    if node is None:
        return None, "missing assignment RHS"
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return [node.value], None
        return None, f"unsupported literal {node.value!r}"
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        values: list[str] = []
        for element in node.elts:
            nested, reason = _static_string_values(element)
            if nested is None:
                return None, reason
            values.extend(nested)
        return values, None
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"frozenset", "set"}
            and len(node.args) == 1
            and not node.keywords
        ):
            return _static_string_values(node.args[0])
        return None, "unsupported call expression"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, left_reason = _static_string_values(node.left)
        right, right_reason = _static_string_values(node.right)
        if left is None:
            return None, left_reason
        if right is None:
            return None, right_reason
        return left + right, None
    if isinstance(node, ast.IfExp):
        if not isinstance(node.test, ast.Constant) or not isinstance(node.test.value, bool):
            return None, "conditional test is not a literal bool"
        return _static_string_values(node.body if node.test.value else node.orelse)
    return None, f"unsupported expression {type(node).__name__}"


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
    for node in tree.body:
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
        for node in tree.body
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
    values, reason = _static_string_values(rhs)
    if values is None:
        failures.append(
            "server: _LONG_HANDLERS assignment RHS is not statically inspectable: "
            f"{reason or 'unsupported expression'}"
        )
        return
    if not values:
        failures.append("server: _LONG_HANDLERS assignment RHS has no inspectable strings")
    for handler in _REQUIRED_HANDLERS:
        count = values.count(handler)
        if count != 1:
            failures.append(
                f"server: _LONG_HANDLERS must contain {handler!r} exactly once (found {count})"
            )


def _mask_ts_comments_and_strings(text: str) -> str:
    """Blank comments and all TS string/template contents, retaining newlines."""
    chars = list(text)

    def blank(start: int, end: int) -> None:
        for index in range(start, min(end, len(chars))):
            if chars[index] not in ("\n", "\r"):
                chars[index] = " "

    index = 0
    while index < len(chars):
        char = chars[index]
        if char == "/" and index + 1 < len(chars) and chars[index + 1] in "/*":
            end = _skip_ts_comment(text, index)
            end = len(text) if end is None else end
            blank(index, end)
            index = end
            continue
        if char in ("'", '"'):
            end = _skip_ts_quoted(text, index, char)
            end = len(text) if end is None else end
            blank(index, end)
            index = end
            continue
        if char == "`":
            end = _skip_ts_template(text, index)
            end = len(text) if end is None else end
            blank(index, end)
            index = end
            continue
        index += 1
    return "".join(chars)


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
    live_text = _mask_ts_comments_and_strings(text)
    for name in _REQUIRED_RESPONSE_EXPORTS:
        pattern = rf"(?m)^[ \t]*export\s+(?:interface|type)\s+{re.escape(name)}\b"
        count = len(re.findall(pattern, live_text))
        if count == 0:
            failures.append(f"types: missing response export {name}")
        elif count != 1:
            failures.append(
                f"types: response export {name} must appear exactly once (found {count}; duplicate export)"
            )


def _shell_fenced_commands(prompt: str) -> list[tuple[int, str]]:
    """Return executable lines from bash/sh/shell Markdown fences."""
    commands: list[tuple[int, str]] = []
    lines = prompt.splitlines()
    in_block = False
    shell_block = False
    for line_number, line in enumerate(lines):
        if not in_block:
            opening = re.match(r"^[ \t]*```(?P<language>[A-Za-z0-9_-]*)[ \t]*$", line)
            if opening is not None:
                in_block = True
                shell_block = opening.group("language").lower() in _CRON_SHELL_LANGUAGES
            continue
        if line.strip() == "```":
            in_block = False
            shell_block = False
            continue
        stripped = line.strip()
        if shell_block and stripped and not stripped.startswith("#"):
            commands.append((line_number, stripped))
    return commands


def _is_post_sync_command(line: str) -> bool:
    """Recognize an executable post-sync command, not prose or comments."""
    return _POST_SYNC_MARKER in line and not line.lstrip().startswith("#")


def _check_cron(repo_root: Path, cron_jobs: Path, failures: list[str]) -> None:
    text = _read_text(
        cron_jobs,
        repo_root=repo_root,
        category="cron",
        failures=failures,
        max_bytes=_MAX_CRON_BYTES,
        confine_to_repo=False,
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
    job = integration_jobs[0]
    if job.get("enabled") is not True:
        failures.append("cron: hades-fork-integration job must be enabled=true")
    if job.get("state") != "scheduled":
        failures.append(
            "cron: hades-fork-integration job state must be exactly 'scheduled'"
        )
    schedule = job.get("schedule")
    if not isinstance(schedule, dict):
        failures.append(
            "cron: hades-fork-integration schedule must be an object with kind='cron' and expr='0 */4 * * *'"
        )
    else:
        if schedule.get("kind") != "cron" or schedule.get("expr") != "0 */4 * * *":
            failures.append(
                "cron: hades-fork-integration schedule must be "
                "{kind: 'cron', expr: '0 */4 * * *'}"
            )
    if job.get("schedule_display") != "0 */4 * * *":
        failures.append(
            "cron: hades-fork-integration schedule_display must be '0 */4 * * *'"
        )
    skills = job.get("skills")
    if not isinstance(skills, list) or "github-operations" not in skills:
        failures.append(
            "cron: hades-fork-integration skills must include 'github-operations'"
        )
    if job.get("deliver") != "local":
        failures.append("cron: hades-fork-integration deliver must be 'local'")

    prompt = job.get("prompt")
    if not isinstance(prompt, str):
        failures.append("cron: hades-fork-integration prompt must be a string")
        return

    if _INTEGRATION_HEADING not in prompt:
        failures.append(
            "cron: hades-fork-integration prompt is missing heading "
            f"{_INTEGRATION_HEADING!r}"
        )

    shell_commands = _shell_fenced_commands(prompt)
    command_positions = [
        line_number
        for line_number, line in shell_commands
        if line == _INTEGRATION_COMMAND
    ]
    verifier_line: int | None = None
    for index in range(len(shell_commands) - 1):
        current_line, current_command = shell_commands[index]
        next_line, next_command = shell_commands[index + 1]
        if (
            next_line == current_line + 1
            and current_command == "cd ~/.hermes/hermes-agent"
            and next_command == _INTEGRATION_COMMAND
        ):
            verifier_line = next_line
            break
    if verifier_line is None:
        failures.append(
            "cron: hades-fork-integration prompt must contain consecutive executable lines "
            "`cd ~/.hermes/hermes-agent` then the exact verifier command in a bash/sh/shell fenced block"
        )
    elif not command_positions:
        failures.append(
            "cron: hades-fork-integration prompt is missing exact verifier command "
            f"{_INTEGRATION_COMMAND!r} in a shell fenced block"
        )

    post_sync_positions = [
        line_number for line_number, line in shell_commands if _is_post_sync_command(line)
    ]
    if not post_sync_positions:
        failures.append(
            "cron: hades-fork-integration prompt is missing the post-sync-verify.py anchor"
        )
    elif command_positions and min(command_positions) > min(post_sync_positions):
        failures.append(
            "cron: verifier command must appear before the first post-sync-verify.py command"
        )


def verify(repo_root: Path, cron_jobs: Path) -> list[str]:
    """Return deterministic diagnostics for integration-contract failures.

    An empty list means every contract check passed.  All paths are resolved
    only for stable diagnostics; no files are modified.
    """
    repo_root = Path(repo_root).expanduser()
    cron_jobs = Path(cron_jobs).expanduser()
    failures: list[str] = []

    try:
        canonical_root = repo_root.resolve(strict=True)
    except FileNotFoundError:
        return [f"repo: missing repository root {repo_root}"]
    except OSError as exc:
        return [f"repo: cannot resolve repository root {repo_root}: {exc.strerror or exc}"]
    if not canonical_root.is_dir():
        return [f"repo: repository root is not a directory {repo_root}"]

    _check_api(canonical_root, failures)
    _check_rpc_tests(canonical_root, failures)
    _check_server(canonical_root, failures)
    _check_response_exports(canonical_root, failures)
    _check_cron(canonical_root, cron_jobs, failures)
    return failures


def _parser() -> argparse.ArgumentParser:
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
