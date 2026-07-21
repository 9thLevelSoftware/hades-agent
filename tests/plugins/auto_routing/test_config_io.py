"""Preview, conflict, locking, and recovery contracts for config updates."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hermes_constants import get_config_path
from plugins.auto_routing.auto_routing import config_io as config_io_module
from plugins.auto_routing.auto_routing.config import (
    ConfigError,
    authority_revision,
    parse_config,
)
from plugins.auto_routing.auto_routing.config_io import (
    AppliedConfig,
    ConfigConflict,
    ConfigRollbackError,
    ConfigVerificationError,
    ManagedConfigError,
    apply_update,
    preview_update,
)
from plugins.auto_routing.auto_routing.models import AutoRoutingConfig
from utils import fast_safe_load

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MANAGED_KEY = "plugins.entries.auto-routing"


def test_atomic_write_closes_raw_descriptor_when_fchmod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[int, Path]] = []
    real_mkstemp = config_io_module.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        descriptor, temporary = real_mkstemp(*args, **kwargs)
        opened.append((descriptor, Path(temporary)))
        return descriptor, temporary

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("injected fchmod failure")

    monkeypatch.setattr(config_io_module.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(config_io_module.os, "fchmod", fail_fchmod, raising=False)

    with pytest.raises(OSError, match="injected fchmod failure"):
        config_io_module._atomic_write_bytes(
            tmp_path / "config.yaml",
            b"display: {}\n",
            mode=0o600,
            owner=None,
        )

    assert len(opened) == 1
    descriptor, temporary = opened[0]
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not temporary.exists()


def _valid_subtree_payload() -> dict[str, Any]:
    return {
        "llm": {
            "allow_provider_override": True,
            "allowed_providers": ["openai-codex"],
            "allow_model_override": True,
            "allowed_models": ["gpt-5.4-mini"],
        },
        "activation": {"mode": "shadow"},
        "scopes": {
            "fresh_sessions": True,
            "delegation": True,
        },
        "classifier": {
            "provider": "openai-codex",
            "model": "gpt-5.4-mini",
            "reasoning_effort": "low",
            "timeout_seconds": 15,
            "disclosure": "full",
        },
        "safe_default": "inherit",
        "policy": {
            "eligible_sources": [
                "configured_providers",
                "installed_local_models",
            ],
            "uninstalled_local_models": "deny",
            "local_models": {
                "require_open_weights": True,
                "require_compatible_hardware": True,
            },
            "denied_providers": [],
            "denied_models": [],
            "max_estimated_task_cost_usd": 2.0,
            "max_estimated_latency_seconds": 120.0,
            "max_routing_overhead_usd_per_day": 1.0,
            "max_experiment_cost_usd_per_day": 2.0,
            "max_evaluator_calls_per_day": 20,
            "max_canary_fraction": 0.05,
            "max_reasoning_effort": "high",
            "allow_subscription": True,
            "allow_paid_access_probes": False,
            "allowed_licenses": [],
            "minimum_context_tokens": 0,
            "canary_high_risk_tasks": False,
        },
        "adaptation": {
            "enabled": True,
            "mode": "autonomous",
            "canary_fraction": 0.05,
            "minimum_canary_samples": 20,
            "rollback_threshold": 0.10,
        },
        "profiles": {
            "coding": {
                "profile_id": "coding",
                "description": "Tool-using software development tasks",
                "base_rank": 70,
                "match": {
                    "domains": ["coding", "debugging"],
                    "complexity": ["moderate", "hard", "extreme"],
                    "modalities": ["text"],
                    "capabilities": ["tools"],
                },
                "objectives": {
                    "quality": 0.55,
                    "reliability": 0.25,
                    "latency": 0.10,
                    "cost": 0.10,
                },
                "primary": {
                    "runtime": {
                        "provider": "openai-codex",
                        "model": "gpt-5.4",
                        "auth_identity": "subscription:default",
                        "credential_pool_identity": "pool:codex",
                        "endpoint_identity": "endpoint:codex",
                        "api_mode": "codex_responses",
                        "local_backend": "",
                        "inventory_revision": "inventory-1",
                    },
                    "reasoning": {
                        "default": "medium",
                        "min": "low",
                        "max": "high",
                    },
                    "supported_reasoning_efforts": [],
                    "revision_status": "active",
                },
                "fallbacks": [],
                "provenance": [],
            }
        },
        "economics_overrides": {},
    }


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


@pytest.fixture
def valid_subtree() -> AutoRoutingConfig:
    return parse_config(
        {
            "plugins": {
                "entries": {
                    "auto-routing": _valid_subtree_payload(),
                }
            }
        }
    )


def _parse_path(path: Path) -> AutoRoutingConfig:
    with path.open("rb") as stream:
        root = fast_safe_load(stream)
    return parse_config(root)


def _backup_paths(config_path: Path) -> list[Path]:
    return sorted(config_path.parent.glob(f"{config_path.name}.auto-routing.*.bak"))


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def _assert_alias_and_target_apply_serialize(
    alias_path: Path,
    target_path: Path,
    proposal: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias_preview = preview_update(proposal, path=alias_path)
    target_preview = preview_update(proposal, path=target_path)
    real_write = config_io_module._atomic_write_bytes
    first_writer_entered = threading.Event()
    second_writer_entered = threading.Event()
    release_first_writer = threading.Event()
    write_count = 0
    write_count_lock = threading.Lock()

    def delayed_write(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        **kwargs: Any,
    ) -> None:
        nonlocal write_count
        if Path(path) == target_path:
            with write_count_lock:
                write_count += 1
                current_write = write_count
            if current_write == 1:
                first_writer_entered.set()
                if not release_first_writer.wait(timeout=10):
                    raise TimeoutError("test did not release the alias writer")
            else:
                second_writer_entered.set()
        real_write(path, content, mode=mode, owner=owner, **kwargs)

    monkeypatch.setattr(config_io_module, "_atomic_write_bytes", delayed_write)
    outcomes: list[AppliedConfig | BaseException] = []

    def apply(path: Path, precondition: str) -> None:
        try:
            outcomes.append(
                apply_update(
                    proposal,
                    expected_precondition_sha256=precondition,
                    path=path,
                )
            )
        except BaseException as exc:  # capture worker failures for the main thread
            outcomes.append(exc)

    alias_writer = threading.Thread(
        target=apply,
        args=(alias_path, alias_preview.precondition_sha256),
        daemon=True,
    )
    target_writer = threading.Thread(
        target=apply,
        args=(target_path, target_preview.precondition_sha256),
        daemon=True,
    )
    alias_writer.start()
    assert first_writer_entered.wait(timeout=10)
    target_writer.start()
    try:
        assert not second_writer_entered.wait(timeout=1)
        assert outcomes == []
    finally:
        release_first_writer.set()
        alias_writer.join(timeout=10)
        target_writer.join(timeout=10)

    assert not alias_writer.is_alive() and not target_writer.is_alive()
    assert sum(isinstance(outcome, AppliedConfig) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ConfigConflict) for outcome in outcomes) == 1
    assert _parse_path(target_path) == proposal


def test_preview_is_read_only_and_binds_exact_round_trip_bytes(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    before = b'# keep me\ndisplay:\n  skin: "kawaii"\n'
    config_path.write_bytes(before)

    preview = preview_update(valid_subtree, path=config_path)
    repeated = preview_update(valid_subtree, path=config_path)

    assert config_path.read_bytes() == before
    assert preview.before_bytes == before
    assert preview.after_bytes != before
    assert hashlib.sha256(preview.before_bytes).hexdigest() == preview.before_sha256
    assert hashlib.sha256(preview.after_bytes).hexdigest() == preview.after_sha256
    assert preview.authority_revision == authority_revision(valid_subtree)
    assert preview.precondition_sha256 == repeated.precondition_sha256
    assert preview.after_bytes == repeated.after_bytes
    assert preview.backup_filename_pattern.startswith(
        f"{config_path.name}.auto-routing."
    )
    assert preview.backup_filename_pattern.endswith(".bak")
    assert preview.unified_diff.startswith("--- ")
    assert "+++ " in preview.unified_diff
    assert "+plugins:" in preview.unified_diff
    assert _parse_bytes(preview.after_bytes) == valid_subtree
    assert _backup_paths(config_path) == []


def _parse_bytes(content: bytes) -> AutoRoutingConfig:
    return parse_config(fast_safe_load(content))


def test_default_path_is_the_profile_local_hermes_config(
    isolated_home: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    config_path = get_config_path()
    assert config_path == isolated_home / "config.yaml"
    config_path.write_text("display:\n  skin: kawaii\n", encoding="utf-8")

    preview = preview_update(valid_subtree)

    assert preview.before_bytes == config_path.read_bytes()
    assert _parse_bytes(preview.after_bytes) == valid_subtree


def test_relative_path_is_captured_as_an_absolute_target(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative_path = Path("nested") / "config.yaml"

    preview = preview_update(valid_subtree, path=relative_path)

    assert preview.config_path == tmp_path / relative_path
    assert not relative_path.exists()


def test_precondition_binds_path_source_proposal_and_authority(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    first_path = tmp_path / "first" / "config.yaml"
    second_path = tmp_path / "second" / "config.yaml"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    source = b"display:\n  skin: kawaii\n"
    first_path.write_bytes(source)
    second_path.write_bytes(source)

    baseline = preview_update(valid_subtree, path=first_path)
    other_path = preview_update(valid_subtree, path=second_path)
    changed_proposal = valid_subtree.model_copy(
        update={"activation": {"mode": "off"}}
    )
    proposal_preview = preview_update(changed_proposal, path=first_path)
    first_path.write_bytes(source + b"# external edit\n")
    changed_source = preview_update(valid_subtree, path=first_path)

    assert baseline.precondition_sha256 != other_path.precondition_sha256
    assert baseline.precondition_sha256 != proposal_preview.precondition_sha256
    assert baseline.precondition_sha256 != changed_source.precondition_sha256
    assert baseline.authority_revision != proposal_preview.authority_revision


def test_precondition_binds_pinned_target_identity_without_symlink_privileges(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_path = tmp_path / "logical" / "config.yaml"
    first_target = tmp_path / "first-target" / "config.yaml"
    second_target = tmp_path / "second-target" / "config.yaml"
    first_target.parent.mkdir()
    second_target.parent.mkdir()
    source = b"display:\n  skin: kawaii\n"
    first_target.write_bytes(source)
    second_target.write_bytes(source)
    selected_target = {"path": first_target}
    monkeypatch.setattr(
        config_io_module,
        "_resolve_target_path",
        lambda _logical: selected_target["path"],
        raising=False,
    )

    first_preview = preview_update(valid_subtree, path=logical_path)
    selected_target["path"] = second_target
    second_preview = preview_update(valid_subtree, path=logical_path)

    assert first_preview.before_bytes == second_preview.before_bytes == source
    assert first_preview.after_bytes == second_preview.after_bytes
    assert first_preview.precondition_sha256 != second_preview.precondition_sha256


def test_apply_rejects_pinned_target_change_before_mutation_without_symlinks(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_path = tmp_path / "logical" / "config.yaml"
    first_target = tmp_path / "first-target" / "config.yaml"
    second_target = tmp_path / "second-target" / "config.yaml"
    first_target.parent.mkdir()
    second_target.parent.mkdir()
    source = b"display:\n  skin: kawaii\n"
    first_target.write_bytes(source)
    second_target.write_bytes(source)
    resolutions = iter((first_target, first_target, second_target))
    monkeypatch.setattr(
        config_io_module,
        "_resolve_target_path",
        lambda _logical: next(resolutions),
        raising=False,
    )

    preview = preview_update(valid_subtree, path=logical_path)

    with pytest.raises(ConfigConflict, match="target changed"):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=logical_path,
        )

    assert first_target.read_bytes() == source
    assert second_target.read_bytes() == source
    assert not os.path.lexists(logical_path)


def test_pre_mutation_byte_conflict_preserves_external_replacement(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"display:\n  skin: kawaii\n"
    external = b"display:\n  skin: slate\n# concurrent replacement\n"
    config_path.write_bytes(original)
    preview = preview_update(valid_subtree, path=config_path)
    real_assert = config_io_module._assert_pinned_target
    real_write = config_io_module._atomic_write_bytes
    external_inode: list[int] = []
    target_writes: list[bytes] = []

    def replace_before_guard(
        pinned_path: Any,
        *,
        before_mutation: bool,
        expected_bytes: bytes | None = None,
    ) -> None:
        if before_mutation and not external_inode:
            replacement = config_path.with_name("external-config.yaml")
            replacement.write_bytes(external)
            os.replace(replacement, config_path)
            external_inode.append(config_path.stat().st_ino)
        real_assert(
            pinned_path,
            before_mutation=before_mutation,
            expected_bytes=expected_bytes,
        )

    def record_target_writes(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        **kwargs: Any,
    ) -> None:
        if Path(path) == config_path:
            target_writes.append(content)
        real_write(path, content, mode=mode, owner=owner, **kwargs)

    monkeypatch.setattr(config_io_module, "_assert_pinned_target", replace_before_guard)
    monkeypatch.setattr(config_io_module, "_atomic_write_bytes", record_target_writes)

    with pytest.raises(ConfigConflict, match="target changed"):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )

    assert config_path.read_bytes() == external
    assert config_path.stat().st_ino == external_inode[0]
    assert target_writes == []
    assert _backup_paths(config_path) == []


def test_pre_mutation_creation_conflict_preserves_external_target(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = b"display:\n  skin: slate\n# concurrent creation\n"
    preview = preview_update(valid_subtree, path=config_path)
    real_assert = config_io_module._assert_pinned_target
    real_write = config_io_module._atomic_write_bytes
    external_inode: list[int] = []
    target_writes: list[bytes] = []

    def create_before_guard(
        pinned_path: Any,
        *,
        before_mutation: bool,
        expected_bytes: bytes | None = None,
    ) -> None:
        if before_mutation and not external_inode:
            replacement = config_path.with_name("external-config.yaml")
            replacement.write_bytes(external)
            os.replace(replacement, config_path)
            external_inode.append(config_path.stat().st_ino)
        real_assert(
            pinned_path,
            before_mutation=before_mutation,
            expected_bytes=expected_bytes,
        )

    def record_target_writes(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        **kwargs: Any,
    ) -> None:
        if Path(path) == config_path:
            target_writes.append(content)
        real_write(path, content, mode=mode, owner=owner, **kwargs)

    monkeypatch.setattr(config_io_module, "_assert_pinned_target", create_before_guard)
    monkeypatch.setattr(config_io_module, "_atomic_write_bytes", record_target_writes)

    with pytest.raises(ConfigConflict, match="target changed"):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )

    assert config_path.read_bytes() == external
    assert config_path.stat().st_ino == external_inode[0]
    assert target_writes == []
    assert _backup_paths(config_path) == []


def test_apply_preserves_unrelated_yaml_and_requires_exact_hash(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    config_path.write_text(
        "# keep me\ndisplay:\n  skin: kawaii\n",
        encoding="utf-8",
    )
    preview = preview_update(valid_subtree, path=config_path)

    with pytest.raises(ConfigConflict):
        apply_update(
            valid_subtree,
            expected_precondition_sha256="0" * 64,
            path=config_path,
        )

    changed_proposal = valid_subtree.model_copy(
        update={"activation": {"mode": "off"}}
    )
    with pytest.raises(ConfigConflict):
        apply_update(
            changed_proposal,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )
    assert config_path.read_bytes() == preview.before_bytes
    assert _backup_paths(config_path) == []

    result = apply_update(
        valid_subtree,
        expected_precondition_sha256=preview.precondition_sha256,
        path=config_path,
    )

    assert isinstance(result, AppliedConfig)
    text = config_path.read_text(encoding="utf-8")
    assert "# keep me" in text and "skin: kawaii" in text
    assert config_path.read_bytes() == preview.after_bytes
    assert _parse_path(config_path) == valid_subtree
    assert result.backup_path.read_bytes() == preview.before_bytes
    assert result.precondition_sha256 == preview.precondition_sha256
    assert result.after_sha256 == preview.after_sha256
    assert re.fullmatch(
        r"config\.yaml\.auto-routing\.\d{8}T\d{12}Z\.bak",
        result.backup_path.name,
    )


def test_dangling_symlink_creates_pinned_target_and_preserves_logical_link(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    logical_path = tmp_path / "logical" / "config.yaml"
    target_path = tmp_path / "target" / "config.yaml"
    logical_path.parent.mkdir()
    target_path.parent.mkdir()
    _symlink_or_skip(logical_path, target_path)

    preview = preview_update(valid_subtree, path=logical_path)
    result = apply_update(
        valid_subtree,
        expected_precondition_sha256=preview.precondition_sha256,
        path=logical_path,
    )

    assert logical_path.is_symlink()
    assert logical_path.resolve(strict=False) == target_path.resolve(strict=False)
    assert target_path.read_bytes() == preview.after_bytes
    assert result.backup_path.read_bytes() == b""


def test_dangling_symlink_source_absent_rollback_removes_only_created_target(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_path = tmp_path / "logical" / "config.yaml"
    target_path = tmp_path / "target" / "config.yaml"
    logical_path.parent.mkdir()
    target_path.parent.mkdir()
    _symlink_or_skip(logical_path, target_path)
    preview = preview_update(valid_subtree, path=logical_path)
    real_write = config_io_module._atomic_write_bytes

    def interrupted_write(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        mutation_state: Any = None,
    ) -> None:
        if Path(path) == target_path:
            target_path.write_bytes(b"partial target")
            mutation_state.target_replaced = True
            raise KeyboardInterrupt
        real_write(
            path,
            content,
            mode=mode,
            owner=owner,
            mutation_state=mutation_state,
        )

    monkeypatch.setattr(config_io_module, "_atomic_write_bytes", interrupted_write)

    with pytest.raises(KeyboardInterrupt):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=logical_path,
        )

    assert logical_path.is_symlink()
    assert logical_path.resolve(strict=False) == target_path.resolve(strict=False)
    assert not target_path.exists()
    backups = _backup_paths(logical_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == b""


def test_symlink_retarget_during_commit_rolls_back_only_pinned_target(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_path = tmp_path / "logical" / "config.yaml"
    first_target = tmp_path / "first-target" / "config.yaml"
    second_target = tmp_path / "second-target" / "config.yaml"
    logical_path.parent.mkdir()
    first_target.parent.mkdir()
    second_target.parent.mkdir()
    first_bytes = b"# first target\ndisplay:\n  skin: kawaii\n"
    second_bytes = b"# second target\ndisplay:\n  skin: slate\n"
    first_target.write_bytes(first_bytes)
    second_target.write_bytes(second_bytes)
    _symlink_or_skip(logical_path, first_target)
    preview = preview_update(valid_subtree, path=logical_path)
    real_replace = os.replace
    retargeted = False

    def retarget_before_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        nonlocal retargeted
        if not retargeted and Path(target) == first_target:
            logical_path.unlink()
            logical_path.symlink_to(second_target)
            retargeted = True
        real_replace(source, target)

    monkeypatch.setattr(config_io_module.os, "replace", retarget_before_replace)

    with pytest.raises(ConfigConflict, match="target changed"):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=logical_path,
        )

    assert retargeted
    assert logical_path.is_symlink()
    assert logical_path.resolve() == second_target.resolve()
    assert first_target.read_bytes() == first_bytes
    assert second_target.read_bytes() == second_bytes
    backups = _backup_paths(logical_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == first_bytes


def test_symlink_target_replace_uses_adjacent_temp_and_rejects_exdev_fallback(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_path = tmp_path / "logical" / "config.yaml"
    target_path = tmp_path / "target" / "config.yaml"
    logical_path.parent.mkdir()
    target_path.parent.mkdir()
    original = b"# target\ndisplay:\n  skin: kawaii\n"
    target_path.write_bytes(original)
    _symlink_or_skip(logical_path, target_path)
    preview = preview_update(valid_subtree, path=logical_path)
    real_replace = os.replace
    attempted_sources: list[Path] = []

    def exdev_once(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target) == target_path and not attempted_sources:
            attempted_sources.append(Path(source))
            raise OSError(errno.EXDEV, "simulated cross-device replacement")
        real_replace(source, target)

    monkeypatch.setattr(config_io_module.os, "replace", exdev_once)

    with pytest.raises(OSError) as exc_info:
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=logical_path,
        )

    assert exc_info.value.errno == errno.EXDEV
    assert attempted_sources[0].parent == target_path.parent
    assert logical_path.is_symlink()
    assert target_path.read_bytes() == original


def test_restore_uses_direct_replace_and_reports_exdev_without_copy_fallback(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"# original\ndisplay:\n  skin: kawaii\n"
    config_path.write_bytes(original)
    preview = preview_update(valid_subtree, path=config_path)
    real_write = config_io_module._atomic_write_bytes
    real_replace = os.replace
    target_replace_count = 0
    tampered = False

    def exdev_on_restore(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        nonlocal target_replace_count
        if Path(target) == config_path:
            target_replace_count += 1
            if target_replace_count == 2:
                raise OSError(errno.EXDEV, "simulated cross-device restore")
        real_replace(source, target)

    def tampering_write(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        mutation_state: Any = None,
    ) -> None:
        nonlocal tampered
        real_write(
            path,
            content,
            mode=mode,
            owner=owner,
            mutation_state=mutation_state,
        )
        if Path(path) == config_path and not tampered:
            with config_path.open("ab") as stream:
                stream.write(b"# verification tamper\n")
            tampered = True

    monkeypatch.setattr(config_io_module.os, "replace", exdev_on_restore)
    monkeypatch.setattr(config_io_module, "_atomic_write_bytes", tampering_write)

    with pytest.raises(ConfigRollbackError) as exc_info:
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )

    assert isinstance(exc_info.value.original_error, ConfigVerificationError)
    assert exc_info.value.backup_path.read_bytes() == original
    assert config_path.read_bytes() == preview.after_bytes + b"# verification tamper\n"


@pytest.mark.parametrize(
    "managed_key",
    [
        _MANAGED_KEY,
        f"{_MANAGED_KEY}.policy",
        "plugins.entries",
    ],
)
def test_managed_plugin_subtree_fails_closed(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
    managed_key: str,
) -> None:
    from hermes_cli import managed_scope

    monkeypatch.setattr(managed_scope, "managed_config_keys", lambda: {managed_key})

    with pytest.raises(ManagedConfigError, match="managed"):
        preview_update(valid_subtree, path=config_path)

    assert not config_path.exists()


def test_existing_unreadable_config_fails_before_preview(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    config_path.mkdir()

    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        preview_update(valid_subtree, path=config_path)


def test_preview_revalidates_model_copy_updates(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    config_path.write_text("display: {}\n", encoding="utf-8")
    invalid_proposal = valid_subtree.model_copy(
        update={"activation": {"mode": "active"}}
    )

    with pytest.raises(ConfigError, match="guarded activation command"):
        preview_update(invalid_proposal, path=config_path)

    assert config_path.read_text(encoding="utf-8") == "display: {}\n"


def test_only_explicit_activation_writer_may_preview_active(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
) -> None:
    config_path.write_text("display: {}\n", encoding="utf-8")
    active = valid_subtree.model_copy(
        update={"activation": {"mode": "active"}}
    )

    preview = preview_update(active, path=config_path, allow_active=True)

    assert preview.before_sha256 != preview.precondition_sha256
    assert len(preview.precondition_sha256) == 64
    assert "mode: active" in preview.after_bytes.decode("utf-8")
    assert config_path.read_text(encoding="utf-8") == "display: {}\n"


def test_alias_and_target_apply_share_pinned_target_lock_without_symlinks(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias_path = tmp_path / "alias" / "config.yaml"
    target_path = tmp_path / "target" / "config.yaml"
    target_path.parent.mkdir()
    target_path.write_text("display:\n  skin: kawaii\n", encoding="utf-8")
    real_resolve = config_io_module._resolve_target_path

    def resolve_alias(path: Path) -> Path:
        if Path(path) == alias_path:
            return target_path
        return real_resolve(path)

    monkeypatch.setattr(config_io_module, "_resolve_target_path", resolve_alias)

    _assert_alias_and_target_apply_serialize(
        alias_path,
        target_path,
        valid_subtree,
        monkeypatch,
    )


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlink semantics")
def test_real_symlink_alias_and_target_apply_share_pinned_target_lock(
    tmp_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias_path = tmp_path / "alias" / "config-link.yaml"
    target_path = tmp_path / "target" / "config.yaml"
    alias_path.parent.mkdir()
    target_path.parent.mkdir()
    target_path.write_text("display:\n  skin: kawaii\n", encoding="utf-8")
    alias_path.symlink_to(target_path)

    _assert_alias_and_target_apply_serialize(
        alias_path,
        target_path,
        valid_subtree,
        monkeypatch,
    )


def test_same_process_apply_is_serialized_and_second_writer_conflicts(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path.write_text("display:\n  skin: kawaii\n", encoding="utf-8")
    preview = preview_update(valid_subtree, path=config_path)
    real_write = config_io_module._atomic_write_bytes
    first_writer_entered = threading.Event()
    release_first_writer = threading.Event()

    def delayed_write(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        mutation_state: Any = None,
    ) -> None:
        if Path(path) == config_path:
            first_writer_entered.set()
            if not release_first_writer.wait(timeout=10):
                raise TimeoutError("test did not release the first writer")
        real_write(
            path,
            content,
            mode=mode,
            owner=owner,
            mutation_state=mutation_state,
        )

    monkeypatch.setattr(
        config_io_module,
        "_atomic_write_bytes",
        delayed_write,
    )
    outcomes: list[AppliedConfig | BaseException] = []

    def apply() -> None:
        try:
            outcomes.append(
                apply_update(
                    valid_subtree,
                    expected_precondition_sha256=preview.precondition_sha256,
                    path=config_path,
                )
            )
        except BaseException as exc:  # capture worker failures for the main thread
            outcomes.append(exc)

    first = threading.Thread(target=apply, daemon=True)
    second = threading.Thread(target=apply, daemon=True)
    first.start()
    assert first_writer_entered.wait(timeout=10)
    second.start()
    time.sleep(0.2)

    assert outcomes == []

    release_first_writer.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive() and not second.is_alive()
    assert sum(isinstance(outcome, AppliedConfig) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ConfigConflict) for outcome in outcomes) == 1
    assert _parse_path(config_path) == valid_subtree


_CROSS_PROCESS_APPLY = r"""
import json
import sys
import time
from pathlib import Path

from plugins.auto_routing.auto_routing import config_io
from plugins.auto_routing.auto_routing.config import parse_config

config_path = Path(sys.argv[1])
proposal_path = Path(sys.argv[2])
ready_path = Path(sys.argv[3])
release_path = Path(sys.argv[4])
expected = sys.argv[5]
payload = json.loads(proposal_path.read_text(encoding="utf-8"))
proposal = parse_config({"plugins": {"entries": {"auto-routing": payload}}})
real_write = config_io._atomic_write_bytes

def delayed_write(path, content, *, mode, owner, mutation_state=None):
    if Path(path) == config_path:
        ready_path.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not release_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("parent did not release child writer")
            time.sleep(0.02)
    real_write(
        path,
        content,
        mode=mode,
        owner=owner,
        mutation_state=mutation_state,
    )

config_io._atomic_write_bytes = delayed_write
result = config_io.apply_update(
    proposal,
    expected_precondition_sha256=expected,
    path=config_path,
)
print(json.dumps({"backup_path": str(result.backup_path)}))
"""


def test_cross_process_apply_holds_the_lock_through_replacement(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    tmp_path: Path,
) -> None:
    config_path.write_text("display:\n  skin: kawaii\n", encoding="utf-8")
    preview = preview_update(valid_subtree, path=config_path)
    proposal_path = tmp_path / "proposal.json"
    ready_path = tmp_path / "child-ready"
    release_path = tmp_path / "release-child"
    proposal_path.write_text(
        json.dumps(
            valid_subtree.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
        ),
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CROSS_PROCESS_APPLY,
            str(config_path),
            str(proposal_path),
            str(ready_path),
            str(release_path),
            preview.precondition_sha256,
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    outcome: list[AppliedConfig | BaseException] = []

    try:
        deadline = time.monotonic() + 10
        while not ready_path.exists() and child.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("child writer did not reach the guarded replacement")
            time.sleep(0.02)
        assert child.poll() is None

        def apply_second() -> None:
            try:
                outcome.append(
                    apply_update(
                        valid_subtree,
                        expected_precondition_sha256=preview.precondition_sha256,
                        path=config_path,
                    )
                )
            except BaseException as exc:
                outcome.append(exc)

        second = threading.Thread(target=apply_second, daemon=True)
        second.start()
        time.sleep(0.2)
        assert outcome == []

        release_path.write_text("release", encoding="utf-8")
        stdout, stderr = child.communicate(timeout=10)
        second.join(timeout=10)

        assert child.returncode == 0, stderr
        assert json.loads(stdout)["backup_path"]
        assert not second.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], ConfigConflict)
        assert _parse_path(config_path) == valid_subtree
    finally:
        release_path.write_text("release", encoding="utf-8")
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_interrupted_replacement_restores_exact_original_bytes(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"# exact original\ndisplay:\n  skin: kawaii\n"
    config_path.write_bytes(original)
    preview = preview_update(valid_subtree, path=config_path)
    real_write = config_io_module._atomic_write_bytes
    interrupted = False

    def interrupted_write(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        mutation_state: Any = None,
    ) -> None:
        nonlocal interrupted
        if Path(path) == config_path and not interrupted:
            interrupted = True
            config_path.write_bytes(b"partial replacement")
            mutation_state.target_replaced = True
            raise KeyboardInterrupt
        real_write(
            path,
            content,
            mode=mode,
            owner=owner,
            mutation_state=mutation_state,
        )

    monkeypatch.setattr(
        config_io_module,
        "_atomic_write_bytes",
        interrupted_write,
    )

    with pytest.raises(KeyboardInterrupt):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )

    backups = _backup_paths(config_path)
    assert config_path.read_bytes() == original
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_interrupt_after_committed_replace_reconciles_and_rolls_back(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"# exact original\ndisplay:\n  skin: kawaii\n"
    config_path.write_bytes(original)
    preview = preview_update(valid_subtree, path=config_path)
    real_replace = os.replace
    target_replace_count = 0

    def replace_then_interrupt(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
    ) -> None:
        nonlocal target_replace_count
        if Path(target) == config_path:
            target_replace_count += 1
            real_replace(source, target)
            if target_replace_count == 1:
                raise KeyboardInterrupt
            return
        real_replace(source, target)

    monkeypatch.setattr(config_io_module.os, "replace", replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )

    backups = _backup_paths(config_path)
    assert target_replace_count == 2
    assert config_path.read_bytes() == original
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_indeterminate_post_replace_state_is_not_clobbered(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"# exact original\ndisplay:\n  skin: kawaii\n"
    external = b"display:\n  skin: slate\n# external after replace\n"
    config_path.write_bytes(original)
    preview = preview_update(valid_subtree, path=config_path)
    real_replace = os.replace
    target_replace_count = 0
    external_inode: list[int] = []

    def replace_then_external_change(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
    ) -> None:
        nonlocal target_replace_count
        if Path(target) == config_path:
            target_replace_count += 1
            real_replace(source, target)
            if target_replace_count == 1:
                replacement = config_path.with_name("external-after-replace.yaml")
                replacement.write_bytes(external)
                real_replace(replacement, target)
                external_inode.append(config_path.stat().st_ino)
                raise KeyboardInterrupt
            return
        real_replace(source, target)

    monkeypatch.setattr(
        config_io_module.os,
        "replace",
        replace_then_external_change,
    )

    with pytest.raises(ConfigConflict, match="indeterminate") as exc_info:
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )

    assert isinstance(exc_info.value.__cause__, KeyboardInterrupt)
    assert target_replace_count == 1
    assert config_path.read_bytes() == external
    assert config_path.stat().st_ino == external_inode[0]
    backups = _backup_paths(config_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_on_disk_verification_failure_rolls_back_atomically(
    config_path: Path,
    valid_subtree: AutoRoutingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"# original\ndisplay:\n  skin: kawaii\n"
    config_path.write_bytes(original)
    preview = preview_update(valid_subtree, path=config_path)
    real_write = config_io_module._atomic_write_bytes
    tampered = False

    def tampering_write(
        path: Path,
        content: bytes,
        *,
        mode: int | None,
        owner: tuple[int, int] | None,
        mutation_state: Any = None,
    ) -> None:
        nonlocal tampered
        real_write(
            path,
            content,
            mode=mode,
            owner=owner,
            mutation_state=mutation_state,
        )
        if Path(path) == config_path and not tampered:
            with config_path.open("ab") as stream:
                stream.write(b"# concurrent tamper\n")
            tampered = True

    monkeypatch.setattr(
        config_io_module,
        "_atomic_write_bytes",
        tampering_write,
    )

    with pytest.raises(ConfigVerificationError, match="verification"):
        apply_update(
            valid_subtree,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )

    backups = _backup_paths(config_path)
    assert config_path.read_bytes() == original
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
