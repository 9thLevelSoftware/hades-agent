"""Tests for cronjob no_agent mode — script-driven jobs that skip the LLM.

Covers:

* ``create_job(no_agent=True)`` shape, validation, and serialization.
* ``cronjob(action='create', no_agent=True)`` tool-level validation.
* ``cronjob(action='update')`` flipping no_agent on/off.
* ``scheduler.run_job`` short-circuit path: success/silent/failure.
* Shell script support in ``_run_job_script`` (.sh runs via bash).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HADES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hades"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HADES_HOME", str(home))

    # Reload modules that cache get_hades_home() at import time.
    import importlib
    import hades_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


# ---------------------------------------------------------------------------
# create_job / update_job: data-layer semantics
# ---------------------------------------------------------------------------


def test_create_job_no_agent_requires_script(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="no_agent=True requires a script"):
        create_job(prompt=None, schedule="every 5m", no_agent=True)


def test_create_job_no_agent_stores_field(hermes_env):
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "watchdog.sh"
    script_path.write_text("#!/bin/bash\necho hi\n")

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="watchdog.sh",
        no_agent=True,
        deliver="local",
    )
    assert job["no_agent"] is True
    assert job["script"] == "watchdog.sh"
    # Prompt can be empty/None for no_agent jobs.
    assert job["prompt"] in {None, ""}


def test_no_agent_script_launch_claim_is_scheduler_owned_and_single_use(
    hermes_env,
    monkeypatch,
):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "claim.py"
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    script_path.write_text("# populated after job creation\n", encoding="utf-8")
    job = create_job(
        prompt=None,
        schedule="every 5m",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )
    script_path.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from cron.script_claim import consume_script_launch_claim\n"
        f"job_id = {job['id']!r}\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "first = consume_script_launch_claim(home=home, expected_job_id=job_id)\n"
        "second = consume_script_launch_claim(home=home, expected_job_id=job_id)\n"
        "raise SystemExit(0 if first and not second else 1)\n",
        encoding="utf-8",
    )

    ok, _document, _response, error = run_job(job)

    assert ok is True
    assert error is None


def test_no_agent_script_launch_claim_is_redacted_from_script_output(hermes_env):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "echo-claim.py"
    script_path.write_text(
        "import os\n"
        "print('claim-capability=' + os.environ['_CRON_INTERNAL_LAUNCH_CAPABILITY'])\n",
        encoding="utf-8",
    )
    job = create_job(
        prompt=None,
        schedule="every 5m",
        name="echo-claim",
        script="echo-claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )

    ok, _document, response, error = run_job(job)

    assert ok is True
    assert error is None
    assert not re.search(r"claim-capability=\\S+", response)


def test_script_launch_capabilities_are_independent_per_scheduler_run(
    hermes_env,
    monkeypatch,
):
    from cron.jobs import create_job
    from cron.script_claim import (
        consume_script_launch_claim,
        issue_script_launch_claim,
    )

    script_path = hermes_env / "scripts" / "claim.py"
    script_path.write_text("print('claim')\n", encoding="utf-8")
    job = create_job(
        prompt=None,
        schedule="every 5m",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )
    first = issue_script_launch_claim(
        home=hermes_env,
        job_id=job["id"],
        dispatched_job=job,
    )
    second = issue_script_launch_claim(
        home=hermes_env,
        job_id=job["id"],
        dispatched_job=job,
    )

    for name, value in first.environment.items():
        monkeypatch.setenv(name, value)
    assert consume_script_launch_claim(home=hermes_env, expected_job_id=job["id"])
    for name, value in second.environment.items():
        monkeypatch.setenv(name, value)
    assert consume_script_launch_claim(home=hermes_env, expected_job_id=job["id"])


def test_script_launch_capability_rejects_same_id_job_replacement(
    hermes_env,
    monkeypatch,
):
    from cron.jobs import create_job, update_job
    from cron.script_claim import (
        consume_script_launch_claim,
        issue_script_launch_claim,
    )

    script_path = hermes_env / "scripts" / "claim.py"
    script_path.write_text("print('claim')\n", encoding="utf-8")
    job = create_job(
        prompt=None,
        schedule="every 5m",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )
    claim = issue_script_launch_claim(
        home=hermes_env,
        job_id=job["id"],
        dispatched_job=job,
    )
    assert update_job(job["id"], {"schedule": "every 10m"}) is not None

    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    assert not consume_script_launch_claim(
        home=hermes_env,
        expected_job_id=job["id"],
    )


def test_script_launch_capability_rejects_job_changed_before_issuance(
    hermes_env,
):
    """The scheduler may authorize only the exact snapshot it dispatched."""
    from cron.jobs import create_job, update_job
    from cron.script_claim import issue_script_launch_claim

    script_path = hermes_env / "scripts" / "claim.py"
    script_path.write_text("print('claim')\n", encoding="utf-8")
    dispatched = create_job(
        prompt=None,
        schedule="every 5m",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )
    assert update_job(dispatched["id"], {"schedule": "every 10m"}) is not None

    with pytest.raises(ValueError, match="changed before launch capability issuance"):
        issue_script_launch_claim(
            home=hermes_env,
            job_id=dispatched["id"],
            dispatched_job=dispatched,
        )


def test_strict_cron_store_lock_fails_closed_without_cross_process_locking(
    hermes_env,
    monkeypatch,
):
    import cron.jobs as jobs_module
    from cron.jobs import CronStoreLockError, locked_cron_store_strict

    monkeypatch.setattr(jobs_module, "fcntl", None)
    monkeypatch.setattr(jobs_module, "msvcrt", None)

    with pytest.raises(CronStoreLockError, match="unavailable"):
        with locked_cron_store_strict(hermes_env):
            pytest.fail("strict cron mutation guard must not fail open")


def test_strict_windows_cron_store_lock_has_bounded_contention(
    hermes_env,
    monkeypatch,
):
    import cron.jobs as jobs_module
    from cron.jobs import CronStoreLockError, locked_cron_store_strict

    class ContendedMsvcrt:
        LK_NBLCK = 2
        LK_UNLCK = 0

        @staticmethod
        def locking(*_args):
            raise OSError("injected lock contention")

    monkeypatch.setattr(jobs_module, "fcntl", None)
    monkeypatch.setattr(jobs_module, "msvcrt", ContendedMsvcrt)
    monkeypatch.setattr(jobs_module, "_JOBS_LOCK_TIMEOUT_SECONDS", 0)

    with pytest.raises(CronStoreLockError, match="timed out"):
        with locked_cron_store_strict(hermes_env):
            pytest.fail("strict Windows cron mutation guard must not fail open")


def test_script_launch_capability_edit_revert_does_not_resurrect_claim(
    hermes_env,
    monkeypatch,
):
    from cron.jobs import create_job, update_job
    from cron.script_claim import (
        consume_script_launch_claim,
        issue_script_launch_claim,
    )

    script_path = hermes_env / "scripts" / "claim.py"
    script_path.write_text("print('claim')\n", encoding="utf-8")
    dispatched = create_job(
        prompt=None,
        schedule="every 5m",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )
    claim = issue_script_launch_claim(
        home=hermes_env,
        job_id=dispatched["id"],
        dispatched_job=dispatched,
    )
    assert update_job(dispatched["id"], {"schedule": "every 10m"}) is not None
    assert update_job(dispatched["id"], {"schedule": "every 5m"}) is not None
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    assert not consume_script_launch_claim(
        home=hermes_env,
        expected_job_id=dispatched["id"],
    )


@pytest.mark.parametrize("changed_field", ["schedule", "script", "workdir"])
def test_script_launch_issuance_binds_user_launch_authority(
    hermes_env,
    changed_field,
):
    from cron.jobs import create_job, update_job
    from cron.script_claim import issue_script_launch_claim

    script_path = hermes_env / "scripts" / "claim.py"
    script_path.write_text("print('claim')\n", encoding="utf-8")
    replacement_path = hermes_env / "scripts" / "replacement.py"
    replacement_path.write_text("print('replacement')\n", encoding="utf-8")
    workdir = hermes_env / "work"
    workdir.mkdir()
    dispatched = create_job(
        prompt=None,
        schedule="every 5m",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )
    updates = {
        "schedule": "every 10m",
        "script": "replacement.py",
        "workdir": str(workdir),
    }
    assert update_job(
        dispatched["id"],
        {changed_field: updates[changed_field]},
    ) is not None

    with pytest.raises(ValueError, match="changed before launch capability issuance"):
        issue_script_launch_claim(
            home=hermes_env,
            job_id=dispatched["id"],
            dispatched_job=dispatched,
        )


def test_run_one_job_accepts_finite_dispatch_bookkeeping(
    hermes_env,
    monkeypatch,
):
    from cron.jobs import create_job
    from cron.scheduler import run_one_job

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    script_path = hermes_env / "scripts" / "claim.py"
    job = create_job(
        prompt=None,
        schedule="2099-01-01T00:00:00Z",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
        repeat=1,
    )
    script_path.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from cron.script_claim import consume_script_launch_claim\n"
        f"ok = consume_script_launch_claim(home=Path(os.environ['HERMES_HOME']), expected_job_id={job['id']!r})\n"
        "raise SystemExit(0 if ok else 1)\n",
        encoding="utf-8",
    )

    assert run_one_job(job, verbose=False) is True


def test_tick_accepts_recurring_next_run_bookkeeping(
    hermes_env,
    monkeypatch,
):
    from cron.jobs import create_job
    from cron import scheduler

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    script_path = hermes_env / "scripts" / "claim.py"
    job = create_job(
        prompt=None,
        schedule="every 5m",
        name="claim",
        script="claim.py",
        no_agent=True,
        script_launch_claim=True,
        deliver="local",
    )
    script_path.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from cron.script_claim import consume_script_launch_claim\n"
        f"ok = consume_script_launch_claim(home=Path(os.environ['HERMES_HOME']), expected_job_id={job['id']!r})\n"
        "raise SystemExit(0 if ok else 1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [job])

    assert scheduler.tick(verbose=False, sync=True) == 1


def test_create_job_default_is_not_no_agent(hermes_env):
    from cron.jobs import create_job

    job = create_job(prompt="say hi", schedule="every 5m", deliver="local")
    assert job.get("no_agent") is False


def test_update_job_roundtrips_no_agent_flag(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")
    job = create_job(prompt=None, schedule="every 5m", script="w.sh", no_agent=True, deliver="local")

    update_job(job["id"], {"no_agent": False})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is False

    update_job(job["id"], {"no_agent": True})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is True


# ---------------------------------------------------------------------------
# cronjob tool: API-layer validation
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_no_agent_without_script_errors(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(action="create", schedule="every 5m", no_agent=True, deliver="local")
    )
    assert result.get("success") is False
    assert "no_agent=True requires a script" in result.get("error", "")


def test_cronjob_tool_create_no_agent_with_script_succeeds(hermes_env):
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho alert\n")

    result = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="alert.sh",
            no_agent=True,
            deliver="local",
        )
    )
    assert result.get("success") is True
    assert result["job"]["no_agent"] is True
    assert result["job"]["script"] == "alert.sh"


def test_cronjob_tool_update_toggles_no_agent(hermes_env):
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")

    created = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="w.sh",
            no_agent=True,
            deliver="local",
        )
    )
    job_id = created["job_id"]

    off = json.loads(cronjob(action="update", job_id=job_id, no_agent=False, prompt="run"))
    assert off["success"] is True
    assert off["job"].get("no_agent") in {False, None}

    on = json.loads(cronjob(action="update", job_id=job_id, no_agent=True))
    assert on["success"] is True
    assert on["job"]["no_agent"] is True


def test_cronjob_tool_update_no_agent_without_script_errors(hermes_env):
    """Flipping no_agent=True on a job that has no script must fail."""
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(action="create", schedule="every 5m", prompt="do a thing", deliver="local")
    )
    job_id = created["job_id"]

    result = json.loads(cronjob(action="update", job_id=job_id, no_agent=True))
    assert result.get("success") is False
    assert "without a script" in result.get("error", "")


def test_cronjob_tool_create_does_not_require_prompt_when_no_agent(hermes_env):
    """The 'prompt or skill required' rule is relaxed for no_agent jobs."""
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")

    result = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="w.sh",
            no_agent=True,
            deliver="local",
        )
    )
    assert result.get("success") is True


# ---------------------------------------------------------------------------
# scheduler.run_job: short-circuit behavior
# ---------------------------------------------------------------------------


def test_run_job_no_agent_success_returns_script_stdout(hermes_env):
    """Happy path: script exits 0 with output, delivered verbatim."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'RAM 92% on host'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert "RAM 92% on host" in final_response
    assert "RAM 92% on host" in doc


def test_run_job_no_agent_empty_output_is_silent(hermes_env):
    """Empty stdout → SILENT_MARKER, which suppresses delivery downstream."""
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    script_path = hermes_env / "scripts" / "quiet.sh"
    script_path.write_text("#!/bin/bash\n# nothing to say\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="quiet.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER


def test_run_job_no_agent_wake_gate_is_silent(hermes_env):
    """wakeAgent=false gate in stdout triggers a silent run."""
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    script_path = hermes_env / "scripts" / "gated.sh"
    script_path.write_text('#!/bin/bash\necho \'{"wakeAgent": false}\'\n')

    job = create_job(
        prompt=None, schedule="every 5m", script="gated.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert final_response == SILENT_MARKER


def test_run_job_no_agent_script_failure_delivers_error(hermes_env):
    """Non-zero exit → success=False, error alert is the delivered message."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "broken.sh"
    script_path.write_text("#!/bin/bash\necho oops >&2\nexit 3\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="broken.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is False
    assert error is not None
    assert "oops" in final_response or "exited with code 3" in final_response
    assert "Cron watchdog" in final_response  # alert header


def test_run_job_no_agent_never_invokes_aiagent(hermes_env):
    """no_agent jobs must NOT import/construct the AIAgent."""
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho alert\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )

    with patch("run_agent.AIAgent") as ai_mock:
        from cron.scheduler import run_job

        run_job(job)

    ai_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _run_job_script: shell-script support
# ---------------------------------------------------------------------------


def test_run_job_script_shell_script_runs_via_bash(hermes_env):
    """.sh files should execute under /bin/bash even without a shebang line."""
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "shelly.sh"
    # No shebang — relies on the interpreter-by-extension rule.
    script_path.write_text('echo "shell: $BASH_VERSION" | head -c 7\n')

    ok, output = _run_job_script("shelly.sh")
    assert ok is True
    assert output.startswith("shell:")


def test_run_job_script_bash_extension_also_runs_via_bash(hermes_env):
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "thing.bash"
    script_path.write_text('printf "via bash\\n"\n')

    ok, output = _run_job_script("thing.bash")
    assert ok is True
    assert output == "via bash"


def test_run_job_script_python_still_runs_via_python(hermes_env):
    """Regression: .py files must keep running via sys.executable."""
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "py.py"
    script_path.write_text("import sys\nprint(f'python {sys.version_info.major}')\n")

    ok, output = _run_job_script("py.py")
    assert ok is True
    assert output.startswith("python ")


def test_run_job_script_path_traversal_still_blocked(hermes_env):
    """Security regression: shell-script support must NOT loosen containment."""
    from cron.scheduler import _run_job_script

    # Absolute path outside the scripts dir should be rejected.
    ok, output = _run_job_script("/etc/passwd")
    assert ok is False
    assert "Blocked" in output or "outside" in output
