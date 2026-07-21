"""Profile-local no-agent scheduler lifecycle for Stage 5 management."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from cron.jobs import get_job, list_jobs, use_cron_store


def _management_cron():
    return importlib.import_module(
        "plugins.auto_routing.auto_routing.management_cron"
    )


def test_install_creates_one_profile_local_no_agent_python_job(
    tmp_path: Path,
) -> None:
    module = _management_cron()

    installed = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )

    with use_cron_store(tmp_path):
        job = get_job(installed.job_id)
    assert job is not None
    assert job["name"] == "auto-routing-management"
    assert job["no_agent"] is True
    assert job["prompt"] == ""
    assert job["deliver"] == "local"
    assert job["script"].endswith("auto-routing-management.py")
    assert installed.script_path == tmp_path / "scripts" / job["script"]


def test_installed_script_invokes_only_content_free_scheduled_reconcile(
    tmp_path: Path,
) -> None:
    module = _management_cron()
    installed = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )

    source = installed.script_path.read_text(encoding="utf-8")

    assert "shutil.which('hermes')" in source
    assert "'auto-routing', 'manage', 'reconcile', '--scheduled', '--json'" in source
    for forbidden in (
        "api_key",
        "credential",
        "ranking",
        "config.yaml",
        str(tmp_path),
    ):
        assert forbidden not in source


def test_normal_profile_files_cannot_supply_a_scheduled_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _management_cron()
    installed = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )
    proof_path = installed.script_path.with_name(
        ".auto-routing-management.proof"
    )

    assert not proof_path.exists()
    monkeypatch.delenv("_HERMES_AUTO_ROUTING_SCHEDULED_PROOF", raising=False)
    with pytest.raises(RuntimeError, match="claim is invalid"):
        module.assert_management_scheduled_invocation(
            home=tmp_path,
            expected_job_id=installed.job_id,
        )
    source = installed.script_path.read_text(encoding="utf-8")
    assert "_HERMES_AUTO_ROUTING_SCHEDULED_PROOF" not in source


def test_fake_claim_endpoint_and_environment_cannot_authorize_scheduled_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _management_cron()
    monkeypatch.setenv("_HERMES_CRON_SCRIPT_CLAIM_ADDR", "127.0.0.1:1")
    monkeypatch.setenv("_HERMES_CRON_SCRIPT_CLAIM_NONCE", "a" * 64)
    monkeypatch.setenv("_HERMES_CRON_SCRIPT_CLAIM_JOB", "forged-job")
    monkeypatch.setenv("_CRON_INTERNAL_LAUNCH_CAPABILITY", "forged")

    with pytest.raises(RuntimeError, match="claim is invalid"):
        module.assert_management_scheduled_invocation(
            home=tmp_path,
            expected_job_id="forged-job",
        )


def test_reinstall_removes_the_legacy_profile_static_proof(tmp_path: Path) -> None:
    module = _management_cron()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    legacy_proof = scripts / ".auto-routing-management.proof"
    legacy_proof.write_text("retired-static-bearer", encoding="utf-8")

    module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )

    assert not legacy_proof.exists()


def test_scheduler_claim_is_local_single_use_and_not_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    module = _management_cron()
    installed = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )
    with use_cron_store(tmp_path):
        dispatched = get_job(installed.job_id)
    assert dispatched is not None
    claim = issue_script_launch_claim(
        home=tmp_path,
        job_id=installed.job_id,
        dispatched_job=dispatched,
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    module.assert_management_scheduled_invocation(
        home=tmp_path,
        expected_job_id=installed.job_id,
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="claim is invalid"):
        module.assert_management_scheduled_invocation(
            home=tmp_path,
            expected_job_id=installed.job_id,
        )


def test_reinstall_updates_the_same_named_job_without_duplicates(
    tmp_path: Path,
) -> None:
    module = _management_cron()
    first = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )

    second = module.install_management_cron(
        home=tmp_path,
        schedule="23 */4 * * *",
        previous_job_id=first.job_id,
    )

    assert second.job_id == first.job_id
    with use_cron_store(tmp_path):
        jobs = list_jobs(include_disabled=True)
    assert [job["id"] for job in jobs] == [first.job_id]
    assert jobs[0]["schedule"]["expr"] == "23 */4 * * *"


def test_install_publishes_created_or_adopted_mutation_provenance(
    tmp_path: Path,
) -> None:
    module = _management_cron()
    created = module.install_management_cron(
        home=tmp_path,
        schedule="23 */4 * * *",
        previous_job_id=None,
    )
    assert created.created is True
    assert created.prior_job is None
    with use_cron_store(tmp_path):
        before = get_job(created.job_id)
    assert before is not None

    adopted = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )
    assert adopted.job_id == created.job_id
    assert adopted.created is False
    assert adopted.prior_job == before

    assert module.rollback_management_cron_install(home=tmp_path, installed=adopted)
    with use_cron_store(tmp_path):
        restored = get_job(created.job_id)
    assert restored is not None
    # Rollback restores user-visible launch configuration, but every edit
    # rotates the internal CAS incarnation so a pre-edit dispatched worker
    # cannot regain authority through edit/revert ABA.
    assert restored["_launch_revision"] != before["_launch_revision"]
    assert {
        key: value
        for key, value in restored.items()
        if key != "_launch_revision"
    } == {
        key: value
        for key, value in before.items()
        if key != "_launch_revision"
    }


def test_adopted_rollback_restores_script_bytes_and_interval_clock(
    tmp_path: Path,
) -> None:
    module = _management_cron()
    first = module.install_management_cron(
        home=tmp_path,
        schedule="every 30m",
        previous_job_id=None,
    )
    prior_script = b"# user-owned prior script\n"
    first.script_path.write_bytes(prior_script)
    with use_cron_store(tmp_path):
        prior_job = get_job(first.job_id)
    assert prior_job is not None
    prior_next_run = prior_job["next_run_at"]

    adopted = module.install_management_cron(
        home=tmp_path,
        schedule="every 2h",
        previous_job_id=first.job_id,
    )
    assert module.rollback_management_cron_install(
        home=tmp_path,
        installed=adopted,
    )

    assert first.script_path.read_bytes() == prior_script
    with use_cron_store(tmp_path):
        restored = get_job(first.job_id)
    assert restored is not None
    assert restored["schedule"] == prior_job["schedule"]
    assert restored["next_run_at"] == prior_next_run


def test_install_rolls_back_a_new_job_when_create_raises_after_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _management_cron()
    real_create = module.create_job

    def create_then_raise(*args, **kwargs):
        real_create(*args, **kwargs)
        raise RuntimeError("post-create failure")

    monkeypatch.setattr(module, "create_job", create_then_raise)

    with pytest.raises(RuntimeError, match="post-create failure"):
        module.install_management_cron(
            home=tmp_path,
            schedule="17 */6 * * *",
            previous_job_id=None,
        )

    with use_cron_store(tmp_path):
        assert list_jobs(include_disabled=True) == []


def test_install_restores_existing_job_when_update_raises_after_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _management_cron()
    first = module.install_management_cron(
        home=tmp_path,
        schedule="2099-01-01T00:00:00Z",
        previous_job_id=None,
    )
    with use_cron_store(tmp_path):
        before = get_job(first.job_id)
    assert before is not None
    real_update = module.update_job

    def update_then_raise(job_id, updates):
        real_update(job_id, updates)
        raise RuntimeError("post-update failure")

    monkeypatch.setattr(module, "update_job", update_then_raise)

    with pytest.raises(RuntimeError, match="post-update failure"):
        module.install_management_cron(
            home=tmp_path,
            schedule="17 */6 * * *",
            previous_job_id=first.job_id,
        )

    with use_cron_store(tmp_path):
        restored = get_job(first.job_id)
    assert restored is not None
    assert restored["schedule"] == before["schedule"]
    assert restored.get("repeat") == before.get("repeat")


def test_schedule_update_normalizes_one_shot_repeat_semantics(tmp_path: Path) -> None:
    module = _management_cron()
    first = module.install_management_cron(
        home=tmp_path,
        schedule="2099-01-01T00:00:00Z",
        previous_job_id=None,
    )
    with use_cron_store(tmp_path):
        one_shot = get_job(first.job_id)
    assert one_shot is not None
    assert one_shot.get("repeat", {}).get("times") == 1

    recurring = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=first.job_id,
    )
    with use_cron_store(tmp_path):
        recurring_job = get_job(recurring.job_id)
    assert recurring_job is not None
    assert recurring_job["schedule"]["kind"] == "cron"
    assert recurring_job.get("repeat", {}).get("times") is None

    one_shot_again = module.install_management_cron(
        home=tmp_path,
        schedule="2099-01-02T00:00:00Z",
        previous_job_id=recurring.job_id,
    )
    with use_cron_store(tmp_path):
        final_job = get_job(one_shot_again.job_id)
    assert final_job is not None
    assert final_job["schedule"]["kind"] == "once"
    assert final_job.get("repeat", {}).get("times") == 1


def test_invalid_schedule_changes_neither_script_nor_job(tmp_path: Path) -> None:
    module = _management_cron()

    with pytest.raises(ValueError):
        module.install_management_cron(
            home=tmp_path,
            schedule="definitely not cron",
            previous_job_id=None,
        )

    assert not (tmp_path / "scripts" / "auto-routing-management.py").exists()
    with use_cron_store(tmp_path):
        assert list_jobs(include_disabled=True) == []


def test_script_install_uses_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _management_cron()
    calls: list[tuple[Path, Path]] = []
    original = module.atomic_replace

    def recording_replace(source, target):
        calls.append((Path(source), Path(target)))
        return original(source, target)

    monkeypatch.setattr(module, "atomic_replace", recording_replace)

    installed = module.install_management_cron(
        home=tmp_path,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )

    destinations = [destination for _source, destination in calls]
    assert destinations.count(installed.script_path) == 1
    assert len(destinations) == 1
    assert all(source.parent == installed.script_path.parent for source, _ in calls)


def test_cron_stores_are_isolated_by_profile_home(tmp_path: Path) -> None:
    module = _management_cron()
    first_home = tmp_path / "first"
    second_home = tmp_path / "second"

    first = module.install_management_cron(
        home=first_home,
        schedule="17 */6 * * *",
        previous_job_id=None,
    )
    second = module.install_management_cron(
        home=second_home,
        schedule="23 */4 * * *",
        previous_job_id=None,
    )

    with use_cron_store(first_home):
        assert [job["id"] for job in list_jobs()] == [first.job_id]
    with use_cron_store(second_home):
        assert [job["id"] for job in list_jobs()] == [second.job_id]
