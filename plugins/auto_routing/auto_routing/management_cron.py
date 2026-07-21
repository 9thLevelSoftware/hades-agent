"""Profile-local no-agent cron installation for autonomous management."""

from __future__ import annotations

import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cron.jobs import (
    create_job,
    get_job,
    list_jobs,
    parse_schedule,
    remove_job,
    restore_job_snapshot,
    update_job,
    use_cron_store,
)
from cron.script_claim import consume_script_launch_claim
from utils import atomic_replace


MANAGEMENT_CRON_NAME = "auto-routing-management"
MANAGEMENT_SCRIPT_NAME = "auto-routing-management.py"
_SCRIPT_SOURCE = (
    "import shutil\n"
    "import subprocess\n"
    "import sys\n"
    "hermes = shutil.which('hermes')\n"
    "if hermes is None:\n"
    "    raise SystemExit('hermes executable not found')\n"
    "result = subprocess.run([hermes, 'auto-routing', 'manage', 'reconcile', "
    "'--scheduled', '--json'], check=False, text=True, stdout=sys.stdout, "
    "stderr=sys.stderr)\n"
    "raise SystemExit(result.returncode)\n"
)


@dataclass(frozen=True, slots=True)
class ManagementCronInstall:
    """Identity of the one installed profile-local management job."""

    job_id: str
    script_path: Path
    created: bool
    prior_job: dict | None
    prior_script: bytes | None
    prior_script_exists: bool


def _validated_home(home: Path) -> Path:
    resolved = Path(home).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError("management cron home must be a directory")
    return resolved


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        atomic_replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_private_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        atomic_replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def assert_management_scheduled_invocation(
    *,
    home: Path,
    expected_job_id: str,
) -> None:
    """Consume the scheduler-owned one-run launch claim for this local job."""
    if not consume_script_launch_claim(
        home=Path(home),
        expected_job_id=expected_job_id,
    ):
        raise RuntimeError("scheduled invocation claim is invalid")


def _existing_management_job(previous_job_id: str | None) -> dict | None:
    named = [
        job
        for job in list_jobs(include_disabled=True)
        if job.get("name") == MANAGEMENT_CRON_NAME
    ]
    if len(named) > 1:
        raise RuntimeError("multiple auto-routing management cron jobs require repair")
    if previous_job_id:
        previous = get_job(previous_job_id)
        if previous is not None and previous.get("name") != MANAGEMENT_CRON_NAME:
            raise RuntimeError("stored management cron job identity changed")
        if previous is not None:
            if named and named[0]["id"] != previous["id"]:
                raise RuntimeError(
                    "stored management cron job conflicts with the named job"
                )
            return previous
    return named[0] if named else None


def _repeat_for_schedule(schedule: dict) -> dict[str, int | None]:
    return {
        "times": 1 if schedule.get("kind") == "once" else None,
        "completed": 0,
    }


def _restore_existing_job(previous: dict) -> None:
    job_id = str(previous["id"])
    updates = {
        key: deepcopy(value)
        for key, value in previous.items()
        if key != "id"
    }
    try:
        restored = restore_job_snapshot(job_id, previous)
    except BaseException as error:
        restored = get_job(job_id)
        if restored is None or not _job_matches_snapshot(restored, previous):
            raise error
    if restored is None or not _job_matches_snapshot(restored, previous):
        raise RuntimeError("management cron job could not be restored")


def _job_matches_snapshot(current: dict, expected: dict) -> bool:
    fields = (
        "schedule",
        "repeat",
        "script",
        "no_agent",
        "script_launch_claim",
        "prompt",
        "deliver",
        "enabled",
        "state",
        "paused_at",
        "paused_reason",
        "next_run_at",
    )
    return all(current.get(field) == expected.get(field) for field in fields)


def rollback_management_cron_install(
    *,
    home: Path,
    installed: ManagementCronInstall,
) -> bool:
    """Undo only the exact cron mutation described by ``installed``."""
    profile_home = _validated_home(home)
    with use_cron_store(profile_home):
        current = get_job(installed.job_id)
        if installed.created:
            if current is None:
                return True
            if current.get("name") != MANAGEMENT_CRON_NAME:
                return False
            removed = remove_job(installed.job_id)
            if installed.prior_script_exists:
                _write_private_bytes(installed.script_path, installed.prior_script or b"")
            else:
                installed.script_path.unlink(missing_ok=True)
            return removed
        prior = installed.prior_job
        if prior is None or current is None:
            return False
        if current.get("name") != MANAGEMENT_CRON_NAME:
            return False
        _restore_existing_job(prior)
        if installed.prior_script_exists:
            _write_private_bytes(installed.script_path, installed.prior_script or b"")
        else:
            installed.script_path.unlink(missing_ok=True)
        restored = get_job(installed.job_id)
        return restored is not None and _job_matches_snapshot(restored, prior)


def install_management_cron(
    *,
    home: Path,
    schedule: str,
    previous_job_id: str | None,
    on_installed: Callable[[ManagementCronInstall], None] | None = None,
) -> ManagementCronInstall:
    """Install or update one job and publish its identity before return.

    ``on_installed`` runs inside the compensation boundary.  Callers that need
    to recover from interruption after this function returns can capture the
    exact job identity there, before any interruptible return boundary.
    """
    # Validate through the existing cron parser before either durable side effect.
    parsed_schedule = parse_schedule(schedule)
    profile_home = _validated_home(home)
    scripts = profile_home / "scripts"
    script = scripts / MANAGEMENT_SCRIPT_NAME
    prior_script_exists = script.exists()
    prior_script = script.read_bytes() if prior_script_exists else None
    relative_script = script.relative_to(scripts).as_posix()
    with use_cron_store(profile_home):
        existing = _existing_management_job(previous_job_id)
        prior = None if existing is None else deepcopy(existing)
        _write_private_text(script, _SCRIPT_SOURCE)
        (scripts / ".auto-routing-management.proof").unlink(missing_ok=True)
        before_ids = {str(item["id"]) for item in list_jobs(include_disabled=True)}
        try:
            if existing is None:
                job = create_job(
                    prompt="",
                    schedule=schedule,
                    name=MANAGEMENT_CRON_NAME,
                    script=relative_script,
                    no_agent=True,
                    deliver="local",
                    repeat=1 if parsed_schedule.get("kind") == "once" else None,
                    script_launch_claim=True,
                )
            else:
                job = update_job(
                    str(existing["id"]),
                    {
                        "schedule": schedule,
                        "script": relative_script,
                        "no_agent": True,
                        "prompt": "",
                        "deliver": "local",
                        "enabled": True,
                        "state": "scheduled",
                        "repeat": _repeat_for_schedule(parsed_schedule),
                        "script_launch_claim": True,
                    },
                )
                if job is None:
                    raise RuntimeError("management cron job disappeared during update")
            installed = ManagementCronInstall(
                job_id=str(job["id"]),
                script_path=script,
                created=prior is None,
                prior_job=prior,
                prior_script=prior_script,
                prior_script_exists=prior_script_exists,
            )
            if on_installed is not None:
                on_installed(installed)
        except BaseException:
            if prior is not None:
                _restore_existing_job(prior)
            else:
                created = [
                    item
                    for item in list_jobs(include_disabled=True)
                    if (
                        item.get("name") == MANAGEMENT_CRON_NAME
                        and str(item.get("id")) not in before_ids
                    )
                ]
                if len(created) != 1 or not remove_job(str(created[0]["id"])):
                    raise RuntimeError("management cron create rollback is incomplete")
            if prior_script_exists:
                _write_private_bytes(script, prior_script or b"")
            else:
                script.unlink(missing_ok=True)
            raise
    return installed


def remove_management_cron(*, home: Path, job_id: str | None) -> bool:
    """Idempotently remove only the recorded named management cron job."""
    if not job_id:
        return True
    profile_home = _validated_home(home)
    with use_cron_store(profile_home):
        job = get_job(job_id)
        if job is None:
            return True
        if job.get("name") != MANAGEMENT_CRON_NAME:
            return False
        return remove_job(job_id)


__all__ = [
    "MANAGEMENT_CRON_NAME",
    "MANAGEMENT_SCRIPT_NAME",
    "ManagementCronInstall",
    "assert_management_scheduled_invocation",
    "install_management_cron",
    "remove_management_cron",
    "rollback_management_cron_install",
]
