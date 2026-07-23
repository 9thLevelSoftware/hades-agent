"""Behavioral contract tests for the dashboard/TUI sync verifier.

The verifier must execute the real web, native RPC, TUI, and typecheck
surfaces.  These tests inject completed-process outcomes to exercise the
orchestration and keep cron checks focused on operational configuration data;
no Python or TypeScript source is read or synthesized for matching.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import scripts.verify_hades_dashboard_contract as verifier
from scripts.verify_hades_dashboard_contract import verify


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_hades_dashboard_contract.py"
TRACKED_REF = "fix/dashboard-api-contract"

VALID_PROMPT = (
    "## Integration Manifest + Handler Verification\n"
    "```bash\n"
    "set -euo pipefail\n"
    "cd ~/.hermes/hermes-agent\n"
    "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
    "```\n"
    "```bash\n"
    "./venv/bin/python3 scripts/post-sync-verify.py --fix\n"
    "./venv/bin/python3 scripts/post-sync-verify.py\n"
    "```\n"
)

VALID_ROLLOUT_PROMPT = (
    "## Integration Manifest + Handler Verification\n"
    "```bash\n"
    "set -euo pipefail\n"
    "cd ~/.hermes/hermes-agent\n"
    "if [ -f scripts/verify_hades_dashboard_contract.py ]; then\n"
    "  ./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
    "else\n"
    "  verifier_tmp=$(mktemp)\n"
    "  trap 'rm -f \"$verifier_tmp\"' EXIT\n"
    f"  if ! git show {TRACKED_REF}:scripts/verify_hades_dashboard_contract.py > \"$verifier_tmp\"; then\n"
    "    echo \"dashboard verifier materialization failed\" >&2\n"
    "    exit 1\n"
    "  fi\n"
    "  if [ ! -s \"$verifier_tmp\" ]; then\n"
    "    echo \"dashboard verifier materialized empty\" >&2\n"
    "    exit 1\n"
    "  fi\n"
    "  ./venv/bin/python3 \"$verifier_tmp\" --repo-root \"$PWD\" --cron-jobs ~/.hermes/cron/jobs.json\n"
    "fi\n"
    "```\n"
    "```bash\n"
    "./venv/bin/python3 scripts/post-sync-verify.py --fix\n"
    "./venv/bin/python3 scripts/post-sync-verify.py\n"
    "```\n"
)


@dataclass
class FakeRunner:
    outcomes: dict[str, verifier.CommandResult] = field(default_factory=dict)
    calls: list[verifier.CommandSpec] = field(default_factory=list)

    def __call__(self, spec: verifier.CommandSpec) -> verifier.CommandResult:
        self.calls.append(spec)
        return self.outcomes.get(spec.name, verifier.CommandResult(returncode=0))


def _write_cron(path: Path, *, prompt: str = VALID_PROMPT, jobs: list[dict] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if jobs is None:
        jobs = [
            {
                "id": "integration",
                "name": "hades-fork-integration",
                "prompt": prompt,
                "skills": ["github-operations"],
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "schedule": {"kind": "cron", "expr": "17 * * * *"},
                "schedule_display": "hourly at :17",
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "deliver": "local",
            },
            {"id": "other", "name": "other-job", "prompt": "noop"},
        ]
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _failures(root: Path, cron_jobs: Path, runner: FakeRunner | None = None) -> str:
    return "\n".join(verify(root, cron_jobs, runner=runner or FakeRunner()))


def test_contract_checks_execute_every_required_surface(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner()

    assert verifier.run_contract_checks(root, runner=runner) == []

    assert [call.name for call in runner.calls] == [
        "web-api",
        "native-rpc",
        "tui-slash-commands",
        "web-typecheck",
        "ui-tui-typecheck",
    ]
    assert all(call.cwd == root for call in runner.calls)


def test_command_specs_use_real_commands_and_current_python(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    specs = verifier.contract_command_specs(root, tmp_path / "safe-tmp")
    by_name = {spec.name: spec for spec in specs}

    assert by_name["web-api"].argv == (
        "npm",
        "run",
        "--prefix",
        "web",
        "test",
        "--",
        "src/lib/api.test.ts",
    )
    assert by_name["native-rpc"].argv == (
        sys.executable,
        "-m",
        "pytest",
        "tests/tui_gateway/test_autonomy_rpc.py",
        "tests/tui_gateway/test_receipt_rpc.py",
        "tests/tui_gateway/test_transaction_rpc.py",
    )
    assert by_name["tui-slash-commands"].argv == (
        "npm",
        "run",
        "--prefix",
        "ui-tui",
        "test",
        "--",
        "src/__tests__/autonomyCommand.test.ts",
        "src/__tests__/receiptCommand.test.ts",
        "src/__tests__/transactionCommand.test.ts",
    )
    assert by_name["web-typecheck"].argv == (
        "npm",
        "run",
        "--prefix",
        "web",
        "typecheck",
    )
    assert by_name["ui-tui-typecheck"].argv == (
        "npm",
        "run",
        "--prefix",
        "ui-tui",
        "typecheck",
    )
    assert all(".venv" not in spec.argv for spec in specs)
    assert all(spec.timeout_seconds > 0 for spec in specs)


def test_contract_checks_do_not_stop_after_a_failed_surface(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(
        outcomes={
            "web-api": verifier.CommandResult(
                returncode=1, stderr="expected behavioral failure\n"
            ),
            "native-rpc": verifier.CommandResult(timed_out=True),
        }
    )

    failures = verifier.run_contract_checks(root, runner=runner)

    assert [call.name for call in runner.calls] == [
        "web-api",
        "native-rpc",
        "tui-slash-commands",
        "web-typecheck",
        "ui-tui-typecheck",
    ]
    assert "web-api" in "\n".join(failures)
    assert "exit 1" in "\n".join(failures)
    assert "native-rpc" in "\n".join(failures)
    assert "timed out" in "\n".join(failures)


def test_command_diagnostics_are_bounded_and_redacted(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(
        outcomes={
            "web-api": verifier.CommandResult(
                returncode=9,
                stderr=("API_KEY=do-not-print " + "x" * 20_000),
            )
        }
    )

    failures = verifier.run_contract_checks(root, runner=runner)
    diagnostic = "\n".join(failures)

    assert "web-api" in diagnostic
    assert "exit 9" in diagnostic
    assert "do-not-print" not in diagnostic
    assert len(diagnostic) < 8_000


def test_missing_executable_is_fail_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(
        outcomes={"web-api": verifier.CommandResult(missing_executable=True)}
    )

    failures = verifier.run_contract_checks(root, runner=runner)

    assert "web-api" in "\n".join(failures)
    assert "missing executable" in "\n".join(failures)


def test_missing_or_unusable_repo_does_not_run_commands(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    runner = FakeRunner()

    failures = verifier.run_contract_checks(missing, runner=runner)

    assert failures
    assert "repo" in failures[0]
    assert runner.calls == []


def test_real_runner_never_uses_a_shell_and_captures_success(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    spec = verifier.CommandSpec(
        name="probe",
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=root,
        timeout_seconds=5,
        tmpdir=tmp_path / "tmp",
    )

    result = verifier.run_command(spec)

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_timeout_is_bounded_by_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    spec = verifier.CommandSpec(
        name="probe",
        argv=(sys.executable, "-c", "import time; time.sleep(10)"),
        cwd=root,
        timeout_seconds=0.05,
        tmpdir=tmp_path / "tmp",
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(spec.argv, spec.timeout_seconds)

    monkeypatch.setattr(verifier.subprocess, "run", timeout)
    result = verifier.run_command(spec)

    assert result.timed_out is True
    assert result.returncode is None


def test_valid_cron_manifest_passes_behavioral_gate(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json")

    assert verify(root, cron_jobs, runner=FakeRunner()) == []


def test_valid_rollout_fallback_passes_with_tracked_ref(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json", prompt=VALID_ROLLOUT_PROMPT)

    assert verify(root, cron_jobs, runner=FakeRunner()) == []


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ("missing", "exactly one"),
        ("duplicate", "exactly one"),
        ("disabled", "enabled"),
        ("unscheduled", "scheduled"),
        ("one_shot", "recurring"),
        ("missing_provider", "provider"),
        ("auto_provider", "provider"),
        ("empty_model", "model"),
        ("auto_model", "model"),
    ],
)
def test_cron_operational_invariants_fail_closed(
    tmp_path: Path, mutation: str, needle: str
) -> None:
    root = _repo(tmp_path)
    if mutation == "missing":
        jobs = [{"name": "other", "prompt": VALID_PROMPT}]
        cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json", jobs=jobs)
    else:
        cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json")
        payload = json.loads(cron_jobs.read_text(encoding="utf-8"))
        job = payload["jobs"][0]
        if mutation == "duplicate":
            payload["jobs"].append(dict(job))
        elif mutation == "disabled":
            job["enabled"] = False
        elif mutation == "unscheduled":
            job["state"] = "paused"
        elif mutation == "one_shot":
            job["repeat"] = {"times": 1, "completed": 1}
        elif mutation == "missing_provider":
            job.pop("provider")
        elif mutation == "auto_provider":
            job["provider"] = "auto"
        elif mutation == "empty_model":
            job["model"] = "  "
        elif mutation == "auto_model":
            job["model"] = "default"
        cron_jobs.write_text(json.dumps(payload), encoding="utf-8")

    failures = _failures(root, cron_jobs)

    assert "cron" in failures.lower()
    assert needle.lower() in failures.lower()


@pytest.mark.parametrize(
    "prompt",
    [
        VALID_PROMPT.replace("scripts/verify_hades_dashboard_contract.py", "scripts/other.py"),
        VALID_PROMPT.replace("## Integration Manifest + Handler Verification", "## Wrong"),
        VALID_PROMPT.replace("```bash", "```text", 1),
        VALID_PROMPT.replace(
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py",
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py || true",
            1,
        ),
        VALID_PROMPT.replace(
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n",
            "./venv/bin/python3 scripts/post-sync-verify.py\n"
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n",
            1,
        ),
    ],
)
def test_prompt_must_fail_closed_before_post_sync_checks(
    tmp_path: Path, prompt: str
) -> None:
    root = _repo(tmp_path)
    cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json", prompt=prompt)

    failures = _failures(root, cron_jobs)

    assert "cron" in failures.lower()
    assert any(term in failures.lower() for term in ("verifier", "heading", "approved", "before"))


def test_fallback_must_use_tracked_branch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    prompt = VALID_ROLLOUT_PROMPT.replace(TRACKED_REF, "other-branch")
    cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json", prompt=prompt)

    failures = _failures(root, cron_jobs)

    assert "cron" in failures.lower()
    assert "tracked" in failures.lower() or "ref" in failures.lower()


def test_prompt_rejects_unclosed_fence_and_missing_post_sync_anchor(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    prompt = (
        "## Integration Manifest + Handler Verification\n"
        "```bash\n"
        "set -euo pipefail\n"
        "cd ~/.hermes/hermes-agent\n"
        "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
    )
    cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json", prompt=prompt)

    failures = _failures(root, cron_jobs)

    assert "fence" in failures.lower()
    assert "post-sync" in failures.lower()


def test_cron_json_must_be_valid_and_jobs_a_list(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    cron_jobs = tmp_path / "cron" / "jobs.json"
    cron_jobs.parent.mkdir()
    cron_jobs.write_text("{", encoding="utf-8")

    failures = _failures(root, cron_jobs)

    assert "cron" in failures.lower()
    assert "json" in failures.lower()


def test_external_cron_final_symlink_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    real = _write_cron(tmp_path / "outside" / "jobs.json")
    linked = tmp_path / "cron" / "jobs.json"
    linked.parent.mkdir()
    linked.symlink_to(real)

    failures = _failures(root, linked)

    assert "cron" in failures.lower()
    assert "symlink" in failures.lower() or "no-follow" in failures.lower()


def test_external_cron_intermediate_symlink_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    real = _write_cron(tmp_path / "outside" / "jobs.json")
    linked_dir = tmp_path / "cron"
    linked_dir.symlink_to(real.parent, target_is_directory=True)

    failures = _failures(root, linked_dir / real.name)

    assert "cron" in failures.lower()
    assert "symlink" in failures.lower() or "path component" in failures.lower()


def test_external_cron_intermediate_symlink_swap_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    cron_dir = tmp_path / "cron-race"
    cron_dir.mkdir()
    cron_path = cron_dir / "jobs.json"
    _write_cron(cron_path)
    outside = tmp_path / "outside-race"
    outside.mkdir()
    _write_cron(outside / "jobs.json")
    moved_dir = tmp_path / "cron-race-original"
    original_open = verifier.os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("dir_fd") is not None and path == "cron-race":
            swapped = True
            cron_dir.rename(moved_dir)
            cron_dir.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(verifier.os, "open", racing_open)
    failures = _failures(root, cron_path)

    assert swapped
    assert "cron" in failures.lower()
    assert "symlink" in failures.lower() or "path component" in failures.lower()


def test_missing_descriptor_safety_primitives_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(verifier, "_dirfd_safety_available", lambda: False)

    failures = _failures(root, cron_jobs)

    assert "cron" in failures.lower()
    assert "safety" in failures.lower()


def test_external_cron_fifo_is_rejected_without_hanging(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    fifo = tmp_path / "cron" / "jobs.json"
    fifo.parent.mkdir()
    os.mkfifo(fifo)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(root),
            "--cron-jobs",
            str(fifo),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )

    assert completed.returncode == 1
    output = completed.stdout + completed.stderr
    assert "cron" in output.lower()
    assert "regular file" in output.lower()


def test_cli_reports_behavioral_failure_without_traceback(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    cron_jobs = _write_cron(tmp_path / "cron" / "jobs.json")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(root),
            "--cron-jobs",
            str(cron_jobs),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    output = completed.stdout + completed.stderr
    assert "FAIL" in output
    assert "web-api" in output
    assert "Traceback" not in output
