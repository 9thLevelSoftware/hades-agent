"""Private scheduler-to-script capability transport for no-agent cron jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cron.jobs import (
    _script_launch_job_revision,
    consume_script_launch_capability,
    issue_script_launch_capability,
    revoke_script_launch_capability,
    use_cron_store,
)


_CAPABILITY_ENV = "_CRON_INTERNAL_LAUNCH_CAPABILITY"


@dataclass(frozen=True, slots=True)
class ScriptLaunchClaim:
    """One opaque capability issued by the scheduler for one script process."""

    home: Path
    job_id: str
    capability: str

    @property
    def environment(self) -> dict[str, str]:
        return {_CAPABILITY_ENV: self.capability}

    def revoke(self) -> bool:
        with use_cron_store(self.home):
            return revoke_script_launch_capability(self.job_id, self.capability)


def issue_script_launch_claim(
    *,
    home: Path,
    job_id: str,
    dispatched_job: Mapping[str, Any],
) -> ScriptLaunchClaim:
    """Issue a claim only if storage still matches the dispatched snapshot."""
    profile_home = Path(home).expanduser().resolve()
    if str(dispatched_job.get("id") or "") != str(job_id):
        raise ValueError("dispatched cron job identity does not match job_id")
    expected_revision = _script_launch_job_revision(dispatched_job)
    with use_cron_store(profile_home):
        capability = issue_script_launch_capability(
            str(job_id),
            expected_job_revision_sha256=expected_revision,
        )
    return ScriptLaunchClaim(
        home=profile_home,
        job_id=str(job_id),
        capability=capability,
    )


def consume_script_launch_claim(*, home: Path, expected_job_id: str) -> bool:
    """Atomically consume this child process's capability for one exact job."""
    capability = os.environ.pop(_CAPABILITY_ENV, "")
    if not capability:
        return False
    with use_cron_store(Path(home).expanduser().resolve()):
        return consume_script_launch_capability(str(expected_job_id), capability)


__all__ = [
    "ScriptLaunchClaim",
    "consume_script_launch_claim",
    "issue_script_launch_claim",
]
