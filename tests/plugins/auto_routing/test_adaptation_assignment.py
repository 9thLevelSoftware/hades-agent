from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing import profile_key as profile_key_module
from plugins.auto_routing.auto_routing.adaptation import (
    canary_eligible,
    deterministic_canary_arm,
    operation_identity_hash,
)
from plugins.auto_routing.auto_routing.profile_key import (
    PROFILE_CANARY_KEY_BYTES,
    PROFILE_CANARY_KEY_NAME,
    ProfileKeyError,
    ensure_profile_canary_key,
    profile_canary_key_path,
    read_profile_canary_key,
)


def _config(home: Path) -> Path:
    path = home / "config.yaml"
    path.write_text("plugins: {}\n", encoding="utf-8")
    return path


def test_operation_identity_is_scope_exact_and_content_free() -> None:
    fresh = operation_identity_hash(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id="ignored-a",
        task_index=1,
    )
    assert fresh == operation_identity_hash(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id="ignored-b",
        task_index=999,
    )
    assert fresh != operation_identity_hash(
        scope="fresh_session",
        session_id="session-b",
        task_id="task-a",
        operation_id="ignored-a",
        task_index=1,
    )

    delegated = operation_identity_hash(
        scope="delegation",
        session_id="ignored",
        task_id="task-a",
        operation_id="operation-a",
        task_index=0,
    )
    assert delegated != operation_identity_hash(
        scope="delegation",
        session_id="ignored",
        task_id="task-a",
        operation_id="operation-a",
        task_index=1,
    )
    assert delegated != operation_identity_hash(
        scope="delegation",
        session_id="ignored",
        task_id="task-a",
        operation_id="operation-b",
        task_index=0,
    )
    assert len(delegated) == 64


def test_canary_is_stable_and_profile_local() -> None:
    key = (5).to_bytes(32, "big")
    identity = operation_identity_hash(
        scope="fresh_session",
        session_id="s",
        task_id="t",
        operation_id="ignored",
        task_index=0,
    )
    coding = deterministic_canary_arm(key, "coding", identity, 0.05)
    assert coding == deterministic_canary_arm(key, "coding", identity, 0.05)
    assert coding != deterministic_canary_arm(key, "research", identity, 0.05)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({}, True),
        ({"is_resume": True}, False),
        ({"is_compression": True}, False),
        ({"manual_override": True}, False),
        ({"fixed_runtime": True}, False),
        ({"risk_class": "high"}, False),
        ({"risk_class": "critical"}, False),
        ({"policy_compliant": False}, False),
        ({"frozen": True}, False),
        ({"adaptation_enabled": False}, False),
        ({"challenger_available": False}, False),
        ({"canary_fraction": 0.0}, False),
        ({"risk_class": "high", "canary_high_risk_tasks": True}, True),
    ],
)
def test_canary_eligibility_is_closed_and_fail_safe(
    override: dict[str, object], expected: bool
) -> None:
    values: dict[str, object] = {
        "scope": "fresh_session",
        "is_resume": False,
        "is_compression": False,
        "manual_override": False,
        "fixed_runtime": False,
        "risk_class": "low",
        "canary_high_risk_tasks": False,
        "policy_compliant": True,
        "frozen": False,
        "adaptation_enabled": True,
        "challenger_available": True,
        "canary_fraction": 0.05,
    }
    values.update(override)
    assert canary_eligible(**values) is expected


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("is_resume", 0),
        ("is_compression", 1),
        ("manual_override", "false"),
        ("fixed_runtime", None),
        ("canary_high_risk_tasks", object()),
        ("policy_compliant", 1),
        ("frozen", []),
        ("adaptation_enabled", "yes"),
        ("challenger_available", 1.0),
        ("risk_class", "unknown"),
        ("risk_class", "LOW"),
        ("risk_class", []),
        ("scope", []),
        ("canary_fraction", True),
        ("canary_fraction", "0.05"),
        ("canary_fraction", float("inf")),
        ("canary_fraction", float("nan")),
        ("canary_fraction", -0.01),
        ("canary_fraction", 1.01),
    ],
)
def test_canary_eligibility_rejects_every_malformed_gate(
    field: str, invalid: object
) -> None:
    values: dict[str, object] = {
        "scope": "fresh_session",
        "is_resume": False,
        "is_compression": False,
        "manual_override": False,
        "fixed_runtime": False,
        "risk_class": "moderate",
        "canary_high_risk_tasks": False,
        "policy_compliant": True,
        "frozen": False,
        "adaptation_enabled": True,
        "challenger_available": True,
        "canary_fraction": 0.05,
    }
    values[field] = invalid

    assert canary_eligible(**values) is False


def test_profile_canary_key_first_writer_is_atomic_and_profile_local(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile-a"
    home.mkdir()
    config = _config(home)

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(
            pool.map(
                lambda _index: ensure_profile_canary_key(
                    home, config_path=config
                ),
                range(24),
            )
        )

    assert len(set(keys)) == 1
    assert len(keys[0]) == PROFILE_CANARY_KEY_BYTES == 32
    assert read_profile_canary_key(home) == keys[0]
    path = profile_canary_key_path(home)
    assert not path.is_symlink()
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    other_home = tmp_path / "profile-b"
    other_home.mkdir()
    other_key = ensure_profile_canary_key(
        other_home,
        config_path=_config(other_home),
    )
    assert other_key != keys[0]


def test_profile_canary_key_rejects_missing_and_corrupt_files(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    _config(home)

    with pytest.raises(ProfileKeyError, match="missing"):
        read_profile_canary_key(home)

    path = profile_canary_key_path(home)
    path.parent.mkdir()
    path.write_bytes(b"short")
    if os.name == "posix":
        path.chmod(0o600)
    with pytest.raises(ProfileKeyError, match="invalid length"):
        read_profile_canary_key(home)


def test_profile_canary_key_rejects_config_and_symlink_escape(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_config = _config(outside)

    with pytest.raises(ProfileKeyError, match="another profile"):
        ensure_profile_canary_key(home, config_path=outside_config)

    auto_routing = home / "auto-routing"
    try:
        auto_routing.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    with pytest.raises(ProfileKeyError, match="escaped"):
        ensure_profile_canary_key(home, config_path=home / "config.yaml")
    assert not (outside / profile_canary_key_path(home).name).exists()


def test_profile_canary_key_parent_swap_cannot_redirect_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    config = _config(home)
    parent = home / "auto-routing"
    parent.mkdir()
    parked = tmp_path / "parked-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    swap_blocked = False
    original_random = profile_key_module._generate_profile_key

    def swap_parent(key_bytes: int) -> bytes:
        nonlocal swap_blocked
        try:
            parent.rename(parked)
        except OSError:
            swap_blocked = True
        else:
            if os.name == "posix":
                parent.symlink_to(outside, target_is_directory=True)
            else:
                parent.mkdir()
        return original_random(key_bytes)

    monkeypatch.setattr(profile_key_module, "_generate_profile_key", swap_parent)

    if os.name == "posix":
        with pytest.raises(ProfileKeyError, match="changed"):
            ensure_profile_canary_key(home, config_path=config)
        assert not (parked / PROFILE_CANARY_KEY_NAME).exists()
    else:
        key = ensure_profile_canary_key(home, config_path=config)
        assert swap_blocked is True
        assert read_profile_canary_key(home) == key
    assert not (outside / PROFILE_CANARY_KEY_NAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dir-relative key read")
def test_profile_canary_key_read_fails_closed_when_pinned_parent_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    config = _config(home)
    key = ensure_profile_canary_key(home, config_path=config)
    parent = home / "auto-routing"
    parked = tmp_path / "parked-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = profile_key_module._read_protected_key_relative

    def read_then_swap(*args: object, **kwargs: object) -> bytes:
        value = original(*args, **kwargs)
        parent.rename(parked)
        parent.symlink_to(outside, target_is_directory=True)
        return value

    monkeypatch.setattr(
        profile_key_module,
        "_read_protected_key_relative",
        read_then_swap,
    )

    with pytest.raises(ProfileKeyError, match="parent changed"):
        read_profile_canary_key(home)
    assert (parked / PROFILE_CANARY_KEY_NAME).read_bytes() == key
    assert not (outside / PROFILE_CANARY_KEY_NAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dir-relative winner race")
def test_concurrent_winner_parent_swap_fails_closed_before_returning_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    config = _config(home)
    parent = home / "auto-routing"
    parent.mkdir()
    parked = tmp_path / "parked-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    winner = b"w" * PROFILE_CANARY_KEY_BYTES

    def publish_winner_then_swap(*_args: object, **_kwargs: object) -> None:
        winner_path = parent / PROFILE_CANARY_KEY_NAME
        winner_path.write_bytes(winner)
        winner_path.chmod(0o600)
        parent.rename(parked)
        parent.symlink_to(outside, target_is_directory=True)
        raise FileExistsError

    monkeypatch.setattr(profile_key_module.os, "link", publish_winner_then_swap)

    with pytest.raises(ProfileKeyError, match="parent changed"):
        ensure_profile_canary_key(home, config_path=config)
    assert (parked / PROFILE_CANARY_KEY_NAME).read_bytes() == winner
    assert not (outside / PROFILE_CANARY_KEY_NAME).exists()


def test_concurrent_winner_is_not_issued_after_pinned_parent_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SwappedParent:
        descriptor = 123

        @staticmethod
        def assert_current() -> None:
            raise ProfileKeyError("profile canary key parent changed")

    monkeypatch.setattr(
        profile_key_module,
        "_read_protected_key_relative",
        lambda *_args, **_kwargs: b"w" * PROFILE_CANARY_KEY_BYTES,
    )

    with pytest.raises(ProfileKeyError, match="parent changed"):
        profile_key_module._read_concurrent_winner(
            _SwappedParent(),
            PROFILE_CANARY_KEY_NAME,
            expected_bytes=PROFILE_CANARY_KEY_BYTES,
            description="profile canary key",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows protected-file behavior")
def test_profile_canary_key_is_readable_without_posix_permission_assumptions(
    tmp_path: Path,
) -> None:
    home = tmp_path / "windows-profile"
    home.mkdir()
    key = ensure_profile_canary_key(home, config_path=_config(home))

    assert read_profile_canary_key(home) == key
    assert profile_canary_key_path(home).is_file()


def test_invalid_key_or_fraction_never_issues_a_canary_arm() -> None:
    operation_hash = "a" * 64
    for key in (None, b"", b"x" * 31, b"x" * 33):
        with pytest.raises(ValueError, match="32-byte"):
            deterministic_canary_arm(key, "coding", operation_hash, 0.05)
    for fraction in (-0.1, 1.1, float("nan")):
        with pytest.raises(ValueError, match="fraction"):
            deterministic_canary_arm(b"x" * 32, "coding", operation_hash, fraction)
