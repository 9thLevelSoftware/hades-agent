#!/usr/bin/env python3
"""Execute the cross-surface Hades dashboard/TUI integration contract.

The contract is intentionally behavioral: every required surface is exercised
through its real test or typecheck command.  The only file content inspected by
this verifier is the operational cron JSON and its prompt; Python and
TypeScript implementation files are never read or parsed here.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


_MAX_CRON_BYTES = 2 * 1024 * 1024
_MAX_DIAGNOSTIC_CHARS = 4_096
_MAX_COMMAND_OUTPUT_CHARS = 64 * 1024

_ORIGINAL_OS_OPEN = os.open

_INTEGRATION_JOB_NAME = "hades-fork-integration"
_INTEGRATION_HEADING = "## Integration Manifest + Handler Verification"
_INTEGRATION_COMMAND = "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py"
_POST_SYNC_MARKER = "scripts/post-sync-verify.py"
_TRACKED_VERIFIER_REF = "fix/dashboard-api-contract"
_CRON_SHELL_LANGUAGES = frozenset({"bash", "sh", "shell"})

_AUTHORIZATION_RE = re.compile(
    r"(?im)(\bauthorization\b[ \t]*[:=][ \t]*)([^\r\n,;]*)"
)
_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?key|token|password|secret|authorization)\b"
    r"\s*[:=]\s*)([^\s,;]+)"
)


@dataclass(frozen=True)
class CommandSpec:
    """One executable behavioral contract surface."""

    name: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    tmpdir: Path


@dataclass(frozen=True)
class CommandResult:
    """Bounded result from a contract command."""

    returncode: int | None = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    missing_executable: bool = False
    error: str = ""


Runner = Callable[[CommandSpec], CommandResult]


@dataclass(frozen=True)
class _ShellCommand:
    line_number: int
    text: str
    block_id: int


@dataclass(frozen=True)
class _ShellScan:
    commands: list[_ShellCommand]
    heading_lines: tuple[int, ...]
    errors: tuple[str, ...]


_HEREDOC_RE = re.compile(
    r"<<(?P<strip>-?)[ \t]*(?P<quote>['\"]?)(?P<delimiter>[^\s'\";|&]+)(?P=quote)"
)
_SHELL_CONTROL_CHARS = frozenset(";|&<>")
_SAFE_GIT_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_ROLLOUT_GIT_SHOW_RE = re.compile(
    r'if ! git show (?P<ref>[A-Za-z0-9][A-Za-z0-9._/-]*):scripts/verify_hades_dashboard_contract\.py'
    r' > "\$verifier_tmp"; then'
)

_PRODUCTION_VERIFIER_LINES = (
    "set -euo pipefail",
    "cd ~/.hermes/hermes-agent",
    _INTEGRATION_COMMAND,
)
_ROLLOUT_VERIFIER_LINES: tuple[str | None, ...] = (
    "set -euo pipefail",
    "cd ~/.hermes/hermes-agent",
    "if [ -f scripts/verify_hades_dashboard_contract.py ]; then",
    _INTEGRATION_COMMAND,
    "else",
    "verifier_tmp=$(mktemp)",
    "trap 'rm -f \"$verifier_tmp\"' EXIT",
    None,
    'echo "dashboard verifier materialization failed" >&2',
    "exit 1",
    "fi",
    'if [ ! -s "$verifier_tmp" ]; then',
    'echo "dashboard verifier materialized empty" >&2',
    "exit 1",
    "fi",
    './venv/bin/python3 "$verifier_tmp" --repo-root "$PWD" --cron-jobs ~/.hermes/cron/jobs.json',
    "fi",
)


def _path_label(path: Path, repo_root: Path) -> str:
    """Prefer stable repo-relative paths in diagnostics."""
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _canonical_repo_root(repo_root: Path) -> tuple[Path | None, list[str]]:
    """Resolve a usable repository directory without inspecting source files."""
    requested = Path(repo_root).expanduser()
    try:
        canonical = requested.resolve(strict=True)
    except FileNotFoundError:
        return None, [f"repo: missing repository root {requested}"]
    except OSError as exc:
        return None, [f"repo: cannot resolve repository root {requested}: {exc.strerror or exc}"]
    if not canonical.is_dir():
        return None, [f"repo: repository root is not a directory {requested}"]
    if not os.access(canonical, os.R_OK | os.X_OK):
        return None, [f"repo: repository root is not usable {requested}"]
    return canonical, []


def _dirfd_safety_available() -> bool:
    """Return whether confined no-follow descriptor walks are supported."""
    try:
        return (
            _ORIGINAL_OS_OPEN in getattr(os, "supports_dir_fd", set())
            and isinstance(getattr(os, "O_NOFOLLOW", None), int)
            and os.O_NOFOLLOW != 0
            and isinstance(getattr(os, "O_DIRECTORY", None), int)
            and os.O_DIRECTORY != 0
        )
    except (AttributeError, TypeError):
        return False


def _read_cron_text(path: Path, failures: list[str], max_bytes: int = _MAX_CRON_BYTES) -> str | None:
    """Read only a bounded regular cron file through no-follow descriptors.

    The directory walk is descriptor-relative so an intermediate symlink swap
    cannot redirect the read.  ``O_NONBLOCK`` plus the regular-file check keeps
    a FIFO or other special file from hanging the sync job.
    """
    label = str(path)
    fd: int | None = None
    directory_fds: list[int] = []
    text: str | None = None
    if not _dirfd_safety_available():
        failures.append(
            "cron: refusing manifest read; required dir_fd/O_NOFOLLOW/O_DIRECTORY safety primitives are unavailable"
        )
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not isinstance(nofollow, int) or not nofollow:
        failures.append("cron: refusing manifest read; O_NOFOLLOW safety primitive is unavailable")
        return None
    if not isinstance(nonblock, int) or not nonblock:
        failures.append("cron: refusing manifest read; O_NONBLOCK safety primitive is unavailable")
        return None

    absolute = Path(os.path.abspath(path.expanduser()))
    anchor = absolute.anchor
    if not anchor:
        failures.append(f"cron: refusing unsafe manifest path {label}")
        return None
    relative_parts = absolute.relative_to(Path(anchor)).parts
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        failures.append(f"cron: refusing unsafe manifest path {label}")
        return None

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
        | os.O_DIRECTORY
    )
    try:
        try:
            filesystem_root_fd = os.open(Path(anchor), directory_flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                failures.append(f"cron: refusing symlink/no-follow filesystem root {label}")
            else:
                failures.append(f"cron: cannot open filesystem root for {label}: {exc.strerror or exc}")
            return None
        directory_fds.append(filesystem_root_fd)
        parent_fd = filesystem_root_fd
        for component in relative_parts[:-1]:
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                failures.append(f"cron: missing manifest file {label}")
                return None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    failures.append(
                        f"cron: refusing non-directory or symlink/no-follow path component {label}"
                    )
                else:
                    failures.append(f"cron: cannot open path component {label}: {exc.strerror or exc}")
                return None
            directory_fds.append(child_fd)
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                failures.append(f"cron: refusing non-directory path component {label}")
                return None
            parent_fd = child_fd

        final_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow
            | nonblock
        )
        try:
            fd = os.open(relative_parts[-1], final_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            failures.append(f"cron: missing manifest file {label}")
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                failures.append(f"cron: refusing symlink/no-follow manifest file {label}")
            else:
                failures.append(f"cron: cannot open manifest {label}: {exc.strerror or exc}")
            return None

        descriptor_stat = os.fstat(fd)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            failures.append(f"cron: expected regular file {label}")
            return None
        if descriptor_stat.st_size == 0:
            failures.append(f"cron: empty manifest file {label}")
            return None
        if descriptor_stat.st_size > max_bytes:
            failures.append(
                f"cron: {label} is too large to inspect ({descriptor_stat.st_size} bytes; limit {max_bytes} bytes)"
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
                failures.append(f"cron: {label} grew beyond inspection limit {max_bytes} bytes")
                return None
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            failures.append(f"cron: {label} is not valid UTF-8 text")
            return None
    except PermissionError:
        failures.append(f"cron: permission denied reading {label}")
        return None
    except OSError as exc:
        failures.append(f"cron: cannot read manifest {label}: {exc.strerror or exc}")
        return None
    finally:
        if fd is not None:
            os.close(fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
    if not text or not text.strip():
        failures.append(f"cron: empty manifest file {label}")
        return None
    return text


def _safe_diagnostic(text: str) -> str:
    """Redact common secret assignments and bound untrusted command output."""
    sanitized = text.replace("\x00", "?")
    sanitized = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _SECRET_RE.sub(r"\1[REDACTED]", sanitized)
    if len(sanitized) > _MAX_DIAGNOSTIC_CHARS:
        return sanitized[:_MAX_DIAGNOSTIC_CHARS] + "…"
    return sanitized


def _command_display(spec: CommandSpec) -> str:
    return shlex.join(spec.argv)


def run_command(spec: CommandSpec) -> CommandResult:
    """Run one contract command with an explicit timeout and no shell."""
    try:
        spec.tmpdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CommandResult(returncode=None, error=f"cannot prepare TMPDIR: {exc.strerror or exc}")
    environment = os.environ.copy()
    environment["TMPDIR"] = str(spec.tmpdir)
    try:
        completed = subprocess.run(
            list(spec.argv),
            cwd=spec.cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=spec.timeout_seconds,
        )
    except FileNotFoundError as exc:
        return CommandResult(returncode=None, missing_executable=True, error=str(exc))
    except PermissionError as exc:
        return CommandResult(returncode=None, error=f"permission denied: {exc}")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=None,
            stdout=_bounded_command_output(exc.stdout),
            stderr=_bounded_command_output(exc.stderr),
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(returncode=None, error=f"OS error: {exc.strerror or exc}")
    return CommandResult(
        returncode=completed.returncode,
        stdout=_bounded_command_output(completed.stdout),
        stderr=_bounded_command_output(completed.stderr),
    )


def _output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _bounded_command_output(value: object) -> str:
    output = _output_text(value)
    if len(output) > _MAX_COMMAND_OUTPUT_CHARS:
        return output[:_MAX_COMMAND_OUTPUT_CHARS] + "…"
    return output


def contract_command_specs(repo_root: Path, tmpdir: Path | None = None) -> tuple[CommandSpec, ...]:
    """Return all required executable contract surfaces."""
    root = Path(repo_root)
    safe_tmpdir = Path(tmpdir) if tmpdir is not None else Path(tempfile.gettempdir())
    return (
        CommandSpec(
            "web-api",
            ("npm", "run", "--prefix", "web", "test", "--", "src/lib/api.test.ts"),
            root,
            180,
            safe_tmpdir,
        ),
        CommandSpec(
            "native-rpc",
            (
                sys.executable,
                "-m",
                "pytest",
                "tests/tui_gateway/test_autonomy_rpc.py",
                "tests/tui_gateway/test_receipt_rpc.py",
                "tests/tui_gateway/test_transaction_rpc.py",
            ),
            root,
            180,
            safe_tmpdir,
        ),
        CommandSpec(
            "tui-slash-commands",
            (
                "npm",
                "run",
                "--prefix",
                "ui-tui",
                "test",
                "--",
                "src/__tests__/autonomyCommand.test.ts",
                "src/__tests__/receiptCommand.test.ts",
                "src/__tests__/transactionCommand.test.ts",
            ),
            root,
            180,
            safe_tmpdir,
        ),
        CommandSpec(
            "web-typecheck",
            ("npm", "run", "--prefix", "web", "typecheck"),
            root,
            180,
            safe_tmpdir,
        ),
        CommandSpec(
            "ui-tui-typecheck",
            ("npm", "run", "--prefix", "ui-tui", "typecheck"),
            root,
            180,
            safe_tmpdir,
        ),
    )


def _normalize_result(result: object) -> CommandResult:
    if isinstance(result, CommandResult):
        return result
    return CommandResult(
        returncode=getattr(result, "returncode", None),
        stdout=_output_text(getattr(result, "stdout", "")),
        stderr=_output_text(getattr(result, "stderr", "")),
    )


def _command_failure(spec: CommandSpec, result: CommandResult) -> str | None:
    command = _command_display(spec)
    prefix = f"{spec.name}: command `{command}`"
    if result.missing_executable:
        return f"{prefix} failed: missing executable"
    if result.timed_out:
        detail = _safe_diagnostic(result.stdout + result.stderr)
        suffix = f"; diagnostics: {detail}" if detail else ""
        return f"{prefix} timed out after {spec.timeout_seconds:g}s{suffix}"
    if result.returncode != 0:
        detail = _safe_diagnostic(result.stderr or result.stdout or result.error)
        suffix = f"; diagnostics: {detail}" if detail else ""
        return f"{prefix} failed with exit {result.returncode}{suffix}"
    if result.error:
        return f"{prefix} failed: {_safe_diagnostic(result.error)}"
    return None


def run_contract_checks(repo_root: Path, runner: Runner | None = None) -> list[str]:
    """Execute every required surface and return actionable failures.

    A supplied runner is intentionally a one-argument dependency injection
    seam: tests can provide completed behavioral outcomes without replacing the
    real command definitions or reading implementation source.
    """
    canonical_root, failures = _canonical_repo_root(Path(repo_root))
    if canonical_root is None:
        return failures
    actual_runner = runner or run_command
    with tempfile.TemporaryDirectory(
        prefix="hades-dashboard-contract-", dir=tempfile.gettempdir()
    ) as temporary_directory:
        specs = contract_command_specs(canonical_root, Path(temporary_directory))
        for spec in specs:
            try:
                result = _normalize_result(actual_runner(spec))
            except FileNotFoundError:
                result = CommandResult(returncode=None, missing_executable=True)
            except subprocess.TimeoutExpired:
                result = CommandResult(returncode=None, timed_out=True)
            except Exception as exc:  # keep sync diagnostics bounded and actionable
                result = CommandResult(returncode=None, error=f"runner error: {exc}")
            failure = _command_failure(spec, result)
            if failure is not None:
                failures.append(failure)
    return failures


def _heredoc_delimiters(line: str) -> list[tuple[str, bool]]:
    return [
        (match.group("delimiter"), bool(match.group("strip")))
        for match in _HEREDOC_RE.finditer(line)
    ]


def _shell_fenced_commands(prompt: str) -> _ShellScan:
    """Scan fenced prompt structure for executable shell lines."""
    commands: list[_ShellCommand] = []
    heading_lines: list[int] = []
    errors: list[str] = []
    lines = re.split(r"\r\n?|\n", prompt)
    in_block = False
    shell_block = False
    block_id = -1
    fence_marker = ""
    block_start_line: int | None = None
    pending_heredocs: list[tuple[str, bool]] = []
    for line_number, line in enumerate(lines):
        if not in_block:
            if line.strip() == _INTEGRATION_HEADING:
                heading_lines.append(line_number)
            opening = re.match(
                r"^[ \t]*(?P<marker>`{3,}|~{3,})(?P<language>[A-Za-z0-9_-]*)[ \t]*$",
                line,
            )
            if opening is not None:
                in_block = True
                block_id += 1
                fence_marker = opening.group("marker")
                block_start_line = line_number
                shell_block = (
                    fence_marker.startswith("`")
                    and opening.group("language").lower() in _CRON_SHELL_LANGUAGES
                )
                pending_heredocs = []
            continue
        if pending_heredocs:
            delimiter, strip_tabs = pending_heredocs[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                pending_heredocs.pop(0)
            continue
        if line.strip(" \t") == fence_marker:
            in_block = False
            shell_block = False
            fence_marker = ""
            block_start_line = None
            continue
        fence_like = re.match(r"^[ \t]*(?P<marker>`{3,}|~{3,}).*$", line)
        if fence_like is not None:
            expected_line = block_start_line + 1 if block_start_line is not None else line_number
            displayed_line = line.strip(" \\t")
            errors.append(
                f"malformed fenced block at line {expected_line}: expected closing {fence_marker!r}, "
                f"found {displayed_line!r} at line {line_number + 1}"
            )
            in_block = False
            shell_block = False
            fence_marker = ""
            block_start_line = None
            pending_heredocs = []
            continue
        stripped = line.strip(" \t")
        if not shell_block or not stripped or stripped.startswith("#"):
            continue
        commands.append(_ShellCommand(line_number, stripped, block_id))
        pending_heredocs.extend(_heredoc_delimiters(stripped))
    if in_block:
        expected_line = block_start_line + 1 if block_start_line is not None else len(lines)
        errors.append(
            f"malformed fenced block at line {expected_line}: unclosed fence (expected closing {fence_marker!r})"
        )
    return _ShellScan(commands, tuple(heading_lines), tuple(errors))


def _strict_shell_tokens(line: str) -> list[str] | None:
    if any(character in line for character in _SHELL_CONTROL_CHARS):
        return None
    if re.search(r"\$\s*\(", line) or "`" in line:
        return None
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return None


def _is_post_sync_command(line: str) -> bool:
    return _strict_shell_tokens(line) in (
        ["./venv/bin/python3", _POST_SYNC_MARKER],
        ["./venv/bin/python3", _POST_SYNC_MARKER, "--fix"],
    )


def _shell_blocks(commands: Iterable[_ShellCommand]) -> list[list[_ShellCommand]]:
    blocks: dict[int, list[_ShellCommand]] = {}
    for command in commands:
        blocks.setdefault(command.block_id, []).append(command)
    return sorted(blocks.values(), key=lambda block: block[0].line_number)


def _is_safe_git_ref(ref: str) -> bool:
    if _SAFE_GIT_REF_RE.fullmatch(ref) is None:
        return False
    if ref in {"@", "@{", ".."} or ".." in ref or ref.startswith("/") or ref.endswith("/") or "//" in ref:
        return False
    components = ref.split("/")
    if any(
        not component
        or component.startswith(".")
        or component.endswith(".")
        or component.endswith(".lock")
        for component in components
    ):
        return False
    return not any(
        ord(character) < 0x20
        or character.isspace()
        or character in ";|&<>$`'\"\\()"
        for character in ref
    )


def _normalize_shell_command(line: str) -> str:
    return line.strip(" \t")


def _approved_verifier_block(commands: Iterable[_ShellCommand]) -> bool:
    """Accept only fail-closed production or tracked-branch rollout gates."""
    normalized = tuple(_normalize_shell_command(command.text) for command in commands)
    if normalized == _PRODUCTION_VERIFIER_LINES:
        return True
    if len(normalized) != len(_ROLLOUT_VERIFIER_LINES):
        return False
    for index, expected in enumerate(_ROLLOUT_VERIFIER_LINES):
        if expected is not None and normalized[index] != expected:
            return False
    materialization = _ROLLOUT_GIT_SHOW_RE.fullmatch(normalized[7])
    return bool(
        materialization
        and _is_safe_git_ref(materialization.group("ref"))
        and materialization.group("ref") == _TRACKED_VERIFIER_REF
    )


def _check_prompt(prompt: str, failures: list[str]) -> None:
    shell_scan = _shell_fenced_commands(prompt)
    for shell_error in shell_scan.errors:
        failures.append(f"cron: {shell_error}")
    if len(shell_scan.heading_lines) != 1:
        failures.append(
            "cron: hades-fork-integration prompt must contain exactly one standalone "
            f"heading line {_INTEGRATION_HEADING!r} outside fenced blocks"
        )

    shell_commands = shell_scan.commands
    post_sync_positions = [
        command.line_number for command in shell_commands if _is_post_sync_command(command.text)
    ]
    if not post_sync_positions:
        failures.append(
            "cron: hades-fork-integration prompt is missing the executable post-sync-verify.py anchor"
        )

    if len(shell_scan.heading_lines) != 1:
        return
    heading_line = shell_scan.heading_lines[0]
    blocks = _shell_blocks(shell_commands)
    first_post_sync = min(post_sync_positions) if post_sync_positions else None
    blocks_under_heading = [block for block in blocks if block[0].line_number > heading_line]
    gate_blocks = [
        block
        for block in blocks_under_heading
        if first_post_sync is None or block[0].line_number < first_post_sync
    ]
    approved_blocks = [block for block in blocks_under_heading if _approved_verifier_block(block)]
    shape_error = (
        "cron: hades-fork-integration prompt must contain exactly one approved fail-closed "
        "verifier shell block under the standalone heading before the first post-sync-verify.py "
        "command; no error swallowing is allowed and rollout fallback must use the tracked verifier branch"
    )
    if len(approved_blocks) != 1 or len(gate_blocks) != 1:
        failures.append(shape_error)
    elif approved_blocks[0] != gate_blocks[0]:
        failures.append(shape_error)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _pinned_string(value: object) -> bool:
    return _nonempty_string(value) and str(value).strip().lower() not in {"auto", "default"}


def _positive_interval(value: object) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value > 0
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_positive_interval(value.get(key)) for key in ("seconds", "minutes", "hours"))
    return False


def _check_cron(repo_root: Path, cron_jobs: Path, failures: list[str]) -> None:
    del repo_root  # retained in the signature for stable, testable diagnostics
    text = _read_cron_text(Path(cron_jobs), failures)
    if text is None:
        return
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        failures.append(f"cron: invalid JSON in {cron_jobs}: {exc.msg}")
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
            f"cron: expected exactly one job named {_INTEGRATION_JOB_NAME!r}, found {len(integration_jobs)}"
        )
        return
    job = integration_jobs[0]

    if job.get("enabled") is not True:
        failures.append("cron: hades-fork-integration job must be enabled=true")
    if job.get("state") != "scheduled":
        failures.append("cron: hades-fork-integration job must be scheduled, not paused or one-shot")

    schedule = job.get("schedule")
    if not isinstance(schedule, dict):
        failures.append("cron: hades-fork-integration job must have a recurring schedule object")
    else:
        kind = schedule.get("kind")
        if kind not in {"cron", "interval"}:
            failures.append("cron: hades-fork-integration schedule must describe a recurring cron or interval")
        if kind == "cron" and not _nonempty_string(schedule.get("expr")):
            failures.append("cron: recurring cron schedule must have a nonempty expression")
        if kind == "interval":
            interval = schedule.get("interval", schedule.get("seconds", schedule.get("every")))
            if not _positive_interval(interval):
                failures.append("cron: recurring interval schedule must have a positive interval")
    if not _nonempty_string(job.get("schedule_display")):
        failures.append("cron: recurring schedule_display must be nonempty")

    repeat = job.get("repeat")
    if repeat is not None:
        if not isinstance(repeat, dict):
            failures.append("cron: repeat configuration must be an object when present")
        else:
            times = repeat.get("times")
            if times is not None and (
                not isinstance(times, int) or isinstance(times, bool) or times <= 1
            ):
                failures.append("cron: hades-fork-integration schedule must be recurring, not one-shot")

    if not _pinned_string(job.get("provider")):
        failures.append("cron: hades-fork-integration provider must be pinned and nonempty")
    if not _pinned_string(job.get("model")):
        failures.append("cron: hades-fork-integration model must be pinned and nonempty")
    skills = job.get("skills")
    if not isinstance(skills, list) or "github-operations" not in skills:
        failures.append("cron: hades-fork-integration skills must include 'github-operations'")
    if job.get("deliver") != "local":
        failures.append("cron: hades-fork-integration deliver must be 'local'")

    prompt = job.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        failures.append("cron: hades-fork-integration prompt must be a nonempty string")
        return
    _check_prompt(prompt, failures)


def verify(
    repo_root: Path,
    cron_jobs: Path,
    *,
    runner: Runner | None = None,
) -> list[str]:
    """Execute all behavioral checks and return deterministic diagnostics."""
    canonical_root, failures = _canonical_repo_root(Path(repo_root).expanduser())
    if canonical_root is None:
        return failures
    failures.extend(run_contract_checks(canonical_root, runner=runner))
    _check_cron(canonical_root, Path(cron_jobs).expanduser(), failures)
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
        failures = [f"verifier: unexpected failure while checking contract: {_safe_diagnostic(str(exc))}"]

    if failures:
        print("FAIL: Hades dashboard/TUI integration contract")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PASS: Hades dashboard/TUI integration contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
