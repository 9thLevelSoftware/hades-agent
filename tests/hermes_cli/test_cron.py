"""Tests for hades_cli.cron command handling."""

from argparse import Namespace
from types import SimpleNamespace

import pytest

from cron.jobs import create_job, get_job, list_jobs
from hades_cli import cron as cron_cli
from hades_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestCronCommandLifecycle:
    def test_pause_resume_run(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        paused = get_job(job["id"])
        assert paused["state"] == "paused"

        cron_command(Namespace(cron_command="resume", job_id=job["id"]))
        resumed = get_job(job["id"])
        assert resumed["state"] == "scheduled"

        cron_command(Namespace(cron_command="run", job_id=job["id"]))
        triggered = get_job(job["id"])
        assert triggered["state"] == "scheduled"

        out = capsys.readouterr().out
        assert "Paused job" in out
        assert "Resumed job" in out
        assert "Triggered job" in out

    def test_edit_can_replace_and_clear_skills(self, tmp_cron_dir, capsys):
        job = create_job(
            prompt="Combine skill outputs",
            schedule="every 1h",
            skill="blogwatcher",
        )

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule="every 2h",
                prompt="Revised prompt",
                name="Edited Job",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["maps", "blogwatcher"],
                clear_skills=False,
                add_skills=None,
                remove_skills=None,
                script=None,
                workdir=None,
                no_agent=None,
            )
        )
        updated = get_job(job["id"])
        assert updated["skills"] == ["maps", "blogwatcher"]
        assert updated["name"] == "Edited Job"
        assert updated["prompt"] == "Revised prompt"
        assert updated["schedule_display"] == "every 120m"

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                clear_skills=True,
                add_skills=None,
                remove_skills=None,
                script=None,
                workdir=None,
                no_agent=None,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["skills"] == []
        assert cleared["skill"] is None

        out = capsys.readouterr().out
        assert "Updated job" in out

    def test_create_with_multiple_skills(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="Use both skills",
                name="Skill combo",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["blogwatcher", "maps"],
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["skills"] == ["blogwatcher", "maps"]
        assert jobs[0]["name"] == "Skill combo"

    def test_list_does_not_crash_when_repeat_is_null(self, tmp_cron_dir, capsys):
        """A one-shot job can be persisted with ``"repeat": null``. `cron
        list` must render it as ∞ rather than crashing on .get(...)\\.get."""
        from cron.jobs import load_jobs, save_jobs

        create_job(prompt="One shot", schedule="every 1h")
        # Force the present-but-null shape that .get("repeat", {}) mishandles.
        jobs = load_jobs()
        jobs[0]["repeat"] = None
        save_jobs(jobs)

        cron_command(Namespace(cron_command="list", all=True))

        out = capsys.readouterr().out
        assert "Repeat:    ∞" in out

    def test_list_does_not_crash_when_deliver_is_null(self, tmp_cron_dir, capsys):
        """A job can be persisted with ``"deliver": null`` (present-but-null).
        `cron list` must fall back to the default channel rather than crashing
        on ``", ".join(None)`` — same dict-default pitfall as ``repeat`` (#32896).
        """
        from cron.jobs import load_jobs, save_jobs

        create_job(prompt="No deliver", schedule="every 1h")
        jobs = load_jobs()
        jobs[0]["deliver"] = None
        save_jobs(jobs)

        cron_command(Namespace(cron_command="list", all=True))

        out = capsys.readouterr().out
        assert "Deliver:   local" in out


class TestGatewayNotRunningWarning:
    """`cron create` / `cron list` must warn when the gateway (and thus the
    cron ticker) isn't running, since jobs only fire inside the gateway.
    Regression guard for #51038 — the most common cron 'jobs never fired'
    report was simply a gateway that was never started.
    """

    def test_create_warns_when_gateway_absent(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(
            Namespace(
                cron_command="create",
                schedule="0 11 * * *",
                prompt="Daily report",
                name="Daily 1130",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Gateway is not running" in out

    def test_create_silent_when_gateway_running(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [4242])
        cron_command(
            Namespace(
                cron_command="create",
                schedule="0 11 * * *",
                prompt="Daily report",
                name="Daily 1130",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Gateway is not running" not in out

    def test_list_warns_when_gateway_absent(self, tmp_cron_dir, capsys, monkeypatch):
        create_job(prompt="Daily report", schedule="0 11 * * *")
        monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="list", all=True))
        out = capsys.readouterr().out
        assert "Gateway is not running" in out


class TestExternalCronProviderStatus:
    """With an external cron provider (e.g. Chronos), jobs fire via a
    NAS-mediated webhook, NOT the in-process ticker. The ticker-heartbeat /
    gateway-process heuristics are meaningless there, so neither
    `cron status` nor the create/list warning must claim the gateway being
    absent means jobs won't fire — that was a false-negative on every healthy
    Chronos instance (the heartbeat is intentionally never written).
    """

    def test_status_reports_provider_not_ticker_for_chronos(
        self, tmp_cron_dir, capsys, monkeypatch
    ):
        create_job(prompt="Ping", schedule="every 2m")
        monkeypatch.setattr(
            "hades_cli.cron._active_cron_provider_name", lambda: "chronos"
        )
        # Even with NO gateway process and NO ticker heartbeat, Chronos status
        # must NOT report a stall / "not firing".
        monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="status"))
        out = capsys.readouterr().out
        assert "chronos" in out
        assert "managed scheduler" in out
        assert "not firing" not in out.lower()
        assert "STALLED" not in out
        assert "Gateway is not running" not in out
        # Still surfaces the active-job summary.
        assert "active job(s)" in out

    def test_status_unchanged_for_builtin(self, tmp_cron_dir, capsys, monkeypatch):
        create_job(prompt="Ping", schedule="every 2m")
        monkeypatch.setattr(
            "hades_cli.cron._active_cron_provider_name", lambda: "builtin"
        )
        monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="status"))
        out = capsys.readouterr().out
        # Built-in path is the historical ticker-based report.
        assert "Gateway is not running" in out
        assert "managed scheduler" not in out

    def test_create_silent_for_chronos_even_without_gateway(
        self, tmp_cron_dir, capsys, monkeypatch
    ):
        # The create-time "gateway not running" nag is a ticker-only concern;
        # an external provider doesn't depend on a live in-process ticker.
        monkeypatch.setattr(
            "hades_cli.cron._active_cron_provider_name", lambda: "chronos"
        )
        monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 2m",
                prompt="Ping",
                name="Ping",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Gateway is not running" not in out


def test_cron_list_warns_when_gateway_not_running(monkeypatch, capsys):
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [
            {
                "id": "job-1",
                "name": "Nightly docs",
                "schedule_display": "every day",
                "state": "scheduled",
                "enabled": True,
                "next_run_at": "2026-06-01T00:00:00Z",
                "deliver": ["local"],
            }
        ],
    )
    monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")

    cron_cli.cron_list()

    out = capsys.readouterr().out
    assert "Gateway is not running" in out
    assert "Nightly docs" in out


def test_cron_status_reports_running_gateway(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr("hades_cli.gateway.find_gateway_pids", lambda: [1234, 5678])
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [
            {"next_run_at": "2026-06-01T00:00:00Z"},
            {"next_run_at": "2026-05-31T12:00:00Z"},
        ],
    )

    cron_cli.cron_status()

    out = capsys.readouterr().out
    assert "Gateway is running" in out
    assert "1234, 5678" in out
    assert "2 active job(s)" in out
    assert "2026-05-31T12:00:00Z" in out


def test_cron_tick_invokes_scheduler_tick_with_verbose(monkeypatch):
    calls = []
    monkeypatch.setattr("cron.scheduler.tick", lambda verbose=False: calls.append(verbose))

    cron_cli.cron_tick()

    assert calls == [True]


def test_cron_create_success_prints_job_details(monkeypatch, capsys):
    monkeypatch.setattr(
        cron_cli,
        "_cron_api",
        lambda **kwargs: {
            "success": True,
            "job_id": "job-1",
            "name": "Nightly docs",
            "schedule": "every day",
            "skills": ["docs"],
            "next_run_at": "2026-06-01T00:00:00Z",
            "job": {
                "script": "scripts/build_docs.py",
                "no_agent": True,
                "workdir": "/tmp/repo",
            },
        },
    )
    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)

    args = SimpleNamespace(
        schedule="every day",
        prompt="refresh docs",
        name="Nightly docs",
        deliver=None,
        repeat=None,
        skill="docs",
        skills=None,
        script="scripts/build_docs.py",
        workdir="/tmp/repo",
        no_agent=True,
    )

    rc = cron_cli.cron_create(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Created job: job-1" in out
    assert "Skills: docs" in out
    assert "Script: scripts/build_docs.py" in out
    assert "Mode: no-agent" in out
    assert "Workdir: /tmp/repo" in out
    assert "Next run: 2026-06-01T00:00:00Z" in out


def test_cron_create_failure_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_cron_api", lambda **kwargs: {"success": False, "error": "boom"})

    args = SimpleNamespace(
        schedule="every day",
        prompt="refresh docs",
        name=None,
        deliver=None,
        repeat=None,
        skill=None,
        skills=None,
        script=None,
        workdir=None,
        no_agent=False,
    )

    rc = cron_cli.cron_create(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Failed to create job: boom" in out


# =============================================================================
# Task5: Cron state mutation service (real-store TDD tests)
# =============================================================================

import copy
import json
import hashlib

from cron.jobs import (
    apply_mutation,
    canonical_revision,
    canonical_snapshot,
    CronStateMutation,
    load_jobs,
    prepare_create,
    prepare_disable,
    prepare_update,
    restore_mutation,
    use_cron_store,
    verify_mutation,
    _jobs_lock,
    _save_jobs_unlocked,
)


@pytest.fixture()
def cron_home(tmp_path):
    """Isolated cron store via use_cron_store."""
    home = tmp_path / "profile"
    home.mkdir()
    with use_cron_store(home):
        yield home


def _make_raw_job(job_id="raw001", **overrides):
    """Construct a raw job dict as if read from storage."""
    base = {
        "id": job_id,
        "name": "Test",
        "prompt": "do stuff",
        "skills": [],
        "skill": None,
        "model": None,
        "provider": None,
        "base_url": None,
        "script": None,
        "no_agent": False,
        "context_from": None,
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": "2026-01-01T00:00:00",
        "next_run_at": "2026-01-01T01:00:00",
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": "local",
        "origin": None,
        "enabled_toolsets": None,
        "workdir": None,
        "provider_snapshot": None,
        "model_snapshot": None,
    }
    base.update(overrides)
    return base


class TestCanonicalSnapshot:
    def test_omits_runtime_keys(self, cron_home):
        job = create_job(prompt="test", schedule="every 1h")
        snap = canonical_snapshot(job)
        assert "next_run_at" not in snap
        assert "last_run_at" not in snap
        assert "last_status" not in snap
        assert "last_error" not in snap
        assert "last_delivery_error" not in snap

    def test_keeps_stable_keys(self, cron_home):
        job = create_job(prompt="stable", schedule="every 2h", deliver="local")
        snap = canonical_snapshot(job)
        assert snap["id"] == job["id"]
        assert snap["prompt"] == "stable"
        assert snap["deliver"] == "local"
        assert snap["schedule"] == job["schedule"]
        assert snap["enabled"] is True

    def test_repeat_omits_completed(self, cron_home):
        job = create_job(prompt="repeat test", schedule="every 1h")
        snap = canonical_snapshot(job)
        assert "completed" not in snap["repeat"]
        assert snap["repeat"]["times"] is None

    def test_defensive_copy(self, cron_home):
        job = create_job(prompt="copy test", schedule="every 1h")
        snap = canonical_snapshot(job)
        snap["prompt"] = "mutated"
        assert job["prompt"] == "copy test"

    def test_revision_deterministic(self, cron_home):
        job = create_job(prompt="det", schedule="every 1h")
        r1 = canonical_revision(job)
        r2 = canonical_revision(job)
        assert r1 == r2
        assert isinstance(r1, str) and len(r1) == 64  # sha256 hex

    def test_absent_revision_is_none(self):
        assert canonical_revision(None) is None


class TestCronStateMutationDataclass:
    def test_frozen(self, cron_home):
        job = create_job(prompt="frozen", schedule="every 1h")
        m = CronStateMutation(
            resource=job["id"], action="update",
            expected_revision="abc", before={"x": 1}, after={"x": 2},
        )
        with pytest.raises(AttributeError):
            m.action = "delete"

    def test_defensive_copy_on_init(self, cron_home):
        inner = {"x": 1}
        m = CronStateMutation(
            resource="r", action="create",
            expected_revision=None, before=None, after=inner,
        )
        inner["x"] = 999
        assert m.after["x"] == 1


class TestPrepareMutations:
    def test_prepare_create(self, cron_home):
        job = _make_raw_job("new01")
        m = prepare_create(job)
        assert m.action == "create"
        assert m.resource == "new01"
        assert m.before is None
        assert m.expected_revision is None
        assert m.after["id"] == "new01"

    def test_prepare_update(self, cron_home):
        job = create_job(prompt="orig", schedule="every 1h")
        m = prepare_update(job["id"], job, {"prompt": "changed"})
        assert m.action == "update"
        assert m.resource == job["id"]
        assert m.before is not None
        assert m.expected_revision is not None
        assert m.before["prompt"] == "orig"
        assert m.after["prompt"] == "changed"
        # Stable fields preserved in after
        assert m.after["schedule"] == canonical_snapshot(job)["schedule"]

    def test_prepare_disable(self, cron_home):
        job = create_job(prompt="disable me", schedule="every 1h")
        m = prepare_disable(job["id"], job)
        assert m.action == "disable"
        assert m.before["enabled"] is True
        assert m.after["enabled"] is False
        assert m.after["state"] == "paused"


class TestApplyMutation:
    def test_create_and_read(self, cron_home):
        job = _make_raw_job("cr01")
        m = prepare_create(job)
        apply_mutation(m)
        stored = get_job("cr01")
        assert stored is not None
        assert stored["prompt"] == "do stuff"

    def test_update_changes_fields(self, cron_home):
        job = create_job(prompt="orig", schedule="every 1h")
        m = prepare_update(job["id"], job, {"prompt": "updated"})
        apply_mutation(m)
        stored = get_job(job["id"])
        assert stored["prompt"] == "updated"
        assert stored["schedule"] == job["schedule"]

    def test_disable_sets_paused(self, cron_home):
        job = create_job(prompt="dis", schedule="every 1h")
        m = prepare_disable(job["id"], job)
        apply_mutation(m)
        stored = get_job(job["id"])
        assert stored["enabled"] is False
        assert stored["state"] == "paused"

    def test_create_duplicate_raises(self, cron_home):
        job = _make_raw_job("dup01")
        m = prepare_create(job)
        apply_mutation(m)
        with pytest.raises(RuntimeError, match="already exists"):
            apply_mutation(m)

    def test_revision_mismatch_blocks_update(self, cron_home):
        job = create_job(prompt="conflict", schedule="every 1h")
        m = prepare_update(job["id"], job, {"prompt": "mine"})
        # Concurrent write
        with _jobs_lock():
            jobs = load_jobs()
            for j in jobs:
                if j["id"] == job["id"]:
                    j["prompt"] = "theirs"
            _save_jobs_unlocked(jobs)
        with pytest.raises(RuntimeError, match="revision mismatch"):
            apply_mutation(m)


class TestDurableVerify:
    def test_verify_create(self, cron_home):
        job = _make_raw_job("ver01")
        m = prepare_create(job)
        apply_mutation(m)
        assert verify_mutation(m) is True

    def test_verify_update(self, cron_home):
        job = create_job(prompt="verify", schedule="every 1h")
        m = prepare_update(job["id"], job, {"prompt": "verified"})
        apply_mutation(m)
        assert verify_mutation(m) is True

    def test_verify_disable(self, cron_home):
        job = create_job(prompt="vdis", schedule="every 1h")
        m = prepare_disable(job["id"], job)
        apply_mutation(m)
        assert verify_mutation(m) is True

    def test_verify_fails_after_concurrent_overwrite(self, cron_home):
        job = create_job(prompt="verconflict", schedule="every 1h")
        m = prepare_update(job["id"], job, {"prompt": "mine"})
        apply_mutation(m)
        # Overwrite after apply
        with _jobs_lock():
            jobs = load_jobs()
            for j in jobs:
                if j["id"] == job["id"]:
                    j["prompt"] = "theirs"
            _save_jobs_unlocked(jobs)
        assert verify_mutation(m) is False


class TestRestoreMutation:
    def test_restore_create_removes_job(self, cron_home):
        job = _make_raw_job("rest01")
        m = prepare_create(job)
        apply_mutation(m)
        assert get_job("rest01") is not None
        restore_mutation(m)
        assert get_job("rest01") is None

    def test_restore_update_restores_exact_before(self, cron_home):
        job = create_job(
            prompt="original", schedule="every 1h", deliver="local", skill="sk",
        )
        orig_snap = canonical_snapshot(job)
        m = prepare_update(job["id"], job, {"prompt": "changed", "deliver": "telegram"})
        apply_mutation(m)
        changed = get_job(job["id"])
        assert changed["prompt"] == "changed"
        assert changed["deliver"] == "telegram"
        restore_mutation(m)
        restored = get_job(job["id"])
        restored_snap = canonical_snapshot(restored)
        # All stable fields match original
        for key in orig_snap:
            assert restored_snap[key] == orig_snap[key], f"field {key} not restored"

    def test_restore_disable_restores_schedule_and_delivery(self, cron_home):
        job = create_job(prompt="rdis", schedule="every 30m", deliver="local")
        orig_schedule = copy.deepcopy(job["schedule"])
        orig_deliver = job["deliver"]
        m = prepare_disable(job["id"], job)
        apply_mutation(m)
        assert get_job(job["id"])["enabled"] is False
        restore_mutation(m)
        restored = get_job(job["id"])
        assert restored["enabled"] is True
        assert restored["state"] == "scheduled"
        assert restored["deliver"] == orig_deliver
        assert restored["schedule"] == orig_schedule

    def test_restore_refuses_to_clobber_concurrent_change(self, cron_home):
        job = create_job(prompt="original", schedule="every 1h", deliver="local")
        mutation = prepare_update(job["id"], job, {"prompt": "mission"})
        apply_mutation(mutation)
        with _jobs_lock():
            jobs = load_jobs()
            for current in jobs:
                if current["id"] == job["id"]:
                    current["prompt"] = "human"
            _save_jobs_unlocked(jobs)

        with pytest.raises(RuntimeError, match="revision mismatch"):
            restore_mutation(mutation)
        assert get_job(job["id"])["prompt"] == "human"
