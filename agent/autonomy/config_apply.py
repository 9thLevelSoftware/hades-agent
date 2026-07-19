"""Preview/hash/apply/recovery saga for the ``autonomy`` config section.

Programmatic changes to the stable authority layer never rewrite
``config.yaml`` wholesale. They flow through an exact-hash guarded saga:

1. ``preview_config_change()`` — computes the normalized rule diff, the
   before/after raw config hashes, and the before/after contract hashes
   without writing anything.
2. ``apply_config_change()`` — under a bounded cross-process file lock
   (``<home>/autonomy.config.lock``), re-verifies both the raw config
   hash and the exact current head contract hash, writes a verified
   backup plus a content-free recovery journal
   (``<home>/autonomy-apply.pending.json``, hashes only — never rule
   text), atomically replaces only the ``autonomy`` section through the
   existing guarded config writer, then materializes and hash-verifies
   the new contract version and removes the journal.
3. ``recover_config_apply()`` — after a crash, converges: completes
   materialization when the on-disk YAML matches the after hash, rolls
   back the journal when it matches the before hash, or restores the
   verified backup when it matches neither. Until recovery succeeds,
   every mutation fails closed with ``incomplete_authority_apply``.

Everything resolves from ``get_hades_home()`` — one profile, no
inheritance. Managed configuration remains a stronger boundary: the
guarded writer rejects managed installs/pinned keys outright.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from agent.autonomy.compiler import (
    InvalidStableAuthority,
    compile_draft,
    parse_stable_rules,
    stable_rule_to_config_entry,
    validate_autonomy_section,
)
from agent.autonomy.models import AutonomyRule
from agent.autonomy.store import StoredContractVersion

__all__ = [
    "INCOMPLETE_AUTHORITY_APPLY",
    "AppliedConfigChange",
    "AuthorityConflict",
    "ConfigChange",
    "ConfigChangePreview",
    "IncompleteAuthorityApply",
    "RecoveryResult",
    "apply_config_change",
    "backup_path",
    "journal_path",
    "lock_path",
    "pending_apply",
    "preview_config_change",
    "recover_config_apply",
]

INCOMPLETE_AUTHORITY_APPLY = "incomplete_authority_apply"

_JOURNAL_NAME = "autonomy-apply.pending.json"
_LOCK_NAME = "autonomy.config.lock"
_BACKUP_NAME = "autonomy-apply.backup.yaml"
_LOCK_TIMEOUT_MS = 10_000


class AuthorityConflict(RuntimeError):
    """The config or contract changed between preview and apply."""


class IncompleteAuthorityApply(RuntimeError):
    """A crashed apply has not been recovered; mutations fail closed."""

    code = INCOMPLETE_AUTHORITY_APPLY


# ── Records ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class ConfigChange:
    """A requested edit to ``autonomy.stable_rules``.

    ``set_rules`` adds or replaces (by ``rule_id``) durable
    ``user_assertion`` rules; ``remove_rule_ids`` deletes existing ones.
    """

    set_rules: tuple[AutonomyRule, ...] = ()
    remove_rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ConfigChangePreview:
    """What an apply would do, bound to exact before-state hashes."""

    profile_id: str
    before_config_hash: str
    after_config_hash: str
    before_contract_hash: str
    after_contract_hash: str
    added_rule_ids: tuple[str, ...] = ()
    removed_rule_ids: tuple[str, ...] = ()
    changed_rule_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Canonical JSON of the full autonomy section to write on apply.
    after_section_json: str
    created_at_ms: int


@dataclass(frozen=True, kw_only=True)
class AppliedConfigChange:
    """A committed section replacement plus its materialized contract."""

    config_hash: str
    contract: StoredContractVersion


@dataclass(frozen=True, kw_only=True)
class RecoveryResult:
    """Outcome of :func:`recover_config_apply`."""

    action: str  # "none" | "completed" | "rolled_back" | "restored"
    config_hash: str
    contract: Optional[StoredContractVersion] = None


# ── Profile-local paths ─────────────────────────────────────────────────────


def _home() -> Path:
    from hades_constants import get_hades_home

    return get_hades_home()


def journal_path() -> Path:
    """Content-free recovery journal for an in-flight apply."""
    return _home() / _JOURNAL_NAME


def lock_path() -> Path:
    """Cross-process exclusive lock file for the saga."""
    return _home() / _LOCK_NAME


def backup_path() -> Path:
    """Verified pre-apply copy of ``config.yaml``."""
    return _home() / _BACKUP_NAME


def pending_apply() -> bool:
    """Whether a crashed apply awaits :func:`recover_config_apply`."""
    return journal_path().exists()


def _require_no_pending_apply() -> None:
    if journal_path().exists():
        raise IncompleteAuthorityApply(
            f"{INCOMPLETE_AUTHORITY_APPLY}: a previous authority apply did "
            f"not complete ({journal_path()}); run recovery before mutating "
            "authority again"
        )


# ── Bounded portable exclusive lock ─────────────────────────────────────────


def _lock_acquire(handle, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise AuthorityConflict(
                    "could not acquire the autonomy config lock "
                    f"({lock_path()}); another authority change is in flight"
                )
            time.sleep(0.05)


def _lock_release(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _config_lock(timeout_ms: int = _LOCK_TIMEOUT_MS) -> Iterator[None]:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        _lock_acquire(handle, timeout_ms)
        try:
            yield
        finally:
            _lock_release(handle)
    finally:
        handle.close()


# ── Shared helpers ──────────────────────────────────────────────────────────


@contextmanager
def _session_db(db):
    """Yield the caller's ``SessionDB`` or open/close a profile-local one."""
    if db is not None:
        yield db
        return
    from hades_state import SessionDB

    handle = SessionDB(_home() / "state.db")
    try:
        yield handle
    finally:
        handle.close()


def _profile_id_for(home: Path) -> str:
    """Profile identity from the home layout (``<root>/profiles/<name>``)."""
    return home.name if home.parent.name == "profiles" else "default"


def _now_ms(now_ms: Optional[int]) -> int:
    return now_ms if now_ms is not None else int(time.time() * 1000)


def _read_raw_config(config_path: Path) -> dict:
    """Fresh, fail-closed raw config read (no cache, no default merge).

    Unlike ``hades_cli.config.read_raw_config()`` this refuses to treat a
    present-but-unparseable file as ``{}`` — compiling authority from a
    silently-emptied config would drop deny rules.
    """
    if not config_path.exists():
        return {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidStableAuthority(
            f"invalid_stable_authority: cannot read {config_path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise InvalidStableAuthority(
            f"invalid_stable_authority: {config_path} is not a mapping"
        )
    return data


def _config_hash(config: dict) -> str:
    from hades_cli.config import raw_config_hash

    return raw_config_hash(config)


def _autonomy_section_hash(config: dict) -> str:
    """Hash only the autonomy section for source fingerprinting."""
    import hashlib, json as _json

    section = config.get("autonomy") or {}
    return hashlib.sha256(
        _json.dumps(section, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _active_mandates(db) -> tuple:
    return db.autonomy.list_runtime_rules(
        source="temporary_mandate", states=("active",)
    )


def _sync_head(db, config: dict, profile_id: str, now_ms: int) -> StoredContractVersion:
    """Materialize the current contract head if (and only if) it changed.

    The comparison ignores ``compiled_at_ms`` so repeated reads of an
    unchanged config/mandate set converge on one immutable version
    instead of minting a new one per read.
    """
    from agent.autonomy.canonical import rule_to_dict

    draft = compile_draft(
        config,
        _active_mandates(db),
        profile_id=profile_id,
        now_ms=now_ms,
        source_fingerprint=f"config:{_autonomy_section_hash(config)}",
    )
    head = db.autonomy.get_head()
    if (
        head is not None
        and head.source_fingerprint == draft.source_fingerprint
        and head.contract.profile_id == draft.profile_id
        and [rule_to_dict(r) for r in head.contract.rules]
        == [rule_to_dict(r) for r in draft.rules]
    ):
        return head
    return db.autonomy.materialize_contract(draft, now_ms=now_ms)


def _write_json_fsync(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _write_bytes_fsync(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _remove_saga_files() -> None:
    for path in (journal_path(), backup_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# ── Preview ─────────────────────────────────────────────────────────────────


def preview_config_change(
    change: ConfigChange,
    *,
    db=None,
    now_ms: Optional[int] = None,
) -> ConfigChangePreview:
    """Compute the exact effect of *change* without writing anything."""
    if not isinstance(change, ConfigChange):
        raise ValueError("change must be a ConfigChange")
    home = _home()
    profile_id = _profile_id_for(home)
    now = _now_ms(now_ms)

    with _config_lock():
        _require_no_pending_apply()
        raw = _read_raw_config(home / "config.yaml")
        before_config_hash = _config_hash(raw)
        section = dict(validate_autonomy_section(raw.get("autonomy")))
        current_rules = parse_stable_rules(section)
        by_id: dict[str, AutonomyRule] = {r.rule_id: r for r in current_rules}

        for rule in change.set_rules:
            # Round-trip through the config-entry serializer so anything a
            # stable rule may not carry (runtime counters, task scopes,
            # non-user sources, secret-looking keys) is rejected here.
            entry = stable_rule_to_config_entry(rule)
            by_id[rule.rule_id] = parse_stable_rules({"stable_rules": [entry]})[0]
        for rule_id in change.remove_rule_ids:
            if rule_id not in by_id:
                raise AuthorityConflict(
                    f"no stable rule {rule_id!r} exists to remove"
                )
            del by_id[rule_id]

        new_rules = tuple(sorted(by_id.values(), key=lambda r: r.rule_id))
        new_section = dict(section)
        new_section.setdefault("schema_version", 1)
        new_section["stable_rules"] = [
            stable_rule_to_config_entry(rule) for rule in new_rules
        ]
        # The written section must reparse to exactly the intended rules.
        parse_stable_rules(new_section)

        new_raw = dict(raw)
        new_raw["autonomy"] = new_section
        after_config_hash = _config_hash(new_raw)

        before_by_id = {r.rule_id: r for r in current_rules}
        after_by_id = {r.rule_id: r for r in new_rules}
        added = tuple(sorted(set(after_by_id) - set(before_by_id)))
        removed = tuple(sorted(set(before_by_id) - set(after_by_id)))
        changed = tuple(
            sorted(
                rule_id
                for rule_id in set(before_by_id) & set(after_by_id)
                if before_by_id[rule_id] != after_by_id[rule_id]
            )
        )

        warnings = []
        for rule in new_rules:
            if rule.expires_at_ms is not None and rule.expires_at_ms <= now:
                warnings.append(
                    f"stable rule {rule.rule_id!r} is already expired "
                    f"(expires_at_ms={rule.expires_at_ms} <= now={now})"
                )

        with _session_db(db) as sdb:
            head = _sync_head(sdb, raw, profile_id, now)
            after_draft = compile_draft(
                new_raw,
                _active_mandates(sdb),
                profile_id=profile_id,
                now_ms=now,
                source_fingerprint=f"config:{_autonomy_section_hash(new_raw)}",
            )

        return ConfigChangePreview(
            profile_id=profile_id,
            before_config_hash=before_config_hash,
            after_config_hash=after_config_hash,
            before_contract_hash=head.content_hash,
            after_contract_hash=after_draft.content_hash(),
            added_rule_ids=added,
            removed_rule_ids=removed,
            changed_rule_ids=changed,
            warnings=tuple(warnings),
            after_section_json=json.dumps(new_section, sort_keys=True),
            created_at_ms=now,
        )


# ── Apply ───────────────────────────────────────────────────────────────────


def apply_config_change(
    preview: ConfigChangePreview,
    *,
    expected_contract_hash: str,
    db=None,
    now_ms: Optional[int] = None,
    _crash_hook=None,
) -> AppliedConfigChange:
    """Commit a previewed change with exact-hash compare-and-set.

    Fails with :class:`AuthorityConflict` (writing nothing) when the raw
    config or the current head contract no longer matches the preview.
    ``_crash_hook`` is a test-only checkpoint callback used to prove
    crash recovery; it must never be passed in production code.
    """
    hook = _crash_hook if _crash_hook is not None else (lambda _point: None)
    home = _home()
    profile_id = _profile_id_for(home)
    config_path = home / "config.yaml"
    now = _now_ms(now_ms)

    with _config_lock():
        _require_no_pending_apply()
        if not isinstance(preview, ConfigChangePreview):
            raise ValueError("preview must be a ConfigChangePreview")

        raw = _read_raw_config(config_path)
        if _config_hash(raw) != preview.before_config_hash:
            raise AuthorityConflict(
                "config changed since preview: refusing to apply a stale "
                "authority change"
            )

        with _session_db(db) as sdb:
            head = _sync_head(sdb, raw, profile_id, now)
            if (
                expected_contract_hash != preview.before_contract_hash
                or head.content_hash != expected_contract_hash
            ):
                raise AuthorityConflict(
                    "authority changed since preview: expected contract hash "
                    f"{expected_contract_hash}, current head is "
                    f"{head.content_hash}"
                )

            new_section = json.loads(preview.after_section_json)

            # Verified backup of the exact before-state, then the
            # content-free journal (hashes only), both fsynced before the
            # config file is touched.
            has_backup = config_path.exists()
            if has_backup:
                _write_bytes_fsync(backup_path(), config_path.read_bytes())
            _write_json_fsync(
                journal_path(),
                {
                    "schema": 1,
                    "profile_id": profile_id,
                    "before_config_hash": preview.before_config_hash,
                    "after_config_hash": preview.after_config_hash,
                    "before_contract_hash": preview.before_contract_hash,
                    "after_contract_hash": preview.after_contract_hash,
                    "has_backup": has_backup,
                    "created_at_ms": now,
                },
            )
            hook("after_journal_write")

            from hades_cli.config import ConfigSectionConflict, replace_config_section

            try:
                replace_config_section(
                    "autonomy",
                    new_section,
                    expected_raw_hash=preview.before_config_hash,
                )
            except ConfigSectionConflict as exc:
                _remove_saga_files()
                raise AuthorityConflict(
                    "config changed since preview: refusing to apply a stale "
                    "authority change"
                ) from exc
            hook("after_config_replace")

            new_raw = _read_raw_config(config_path)
            new_config_hash = _config_hash(new_raw)
            if new_config_hash != preview.after_config_hash:
                # Leave the journal in place: recovery will converge.
                raise IncompleteAuthorityApply(
                    f"{INCOMPLETE_AUTHORITY_APPLY}: written config hash "
                    f"{new_config_hash} does not match previewed "
                    f"{preview.after_config_hash}"
                )

            stored = _sync_head(sdb, new_raw, profile_id, now)
            hook("after_materialize")

            _remove_saga_files()
            return AppliedConfigChange(config_hash=new_config_hash, contract=stored)


# ── Recovery ────────────────────────────────────────────────────────────────


def recover_config_apply(
    *,
    db=None,
    now_ms: Optional[int] = None,
) -> RecoveryResult:
    """Converge a crashed apply; idempotent and safe to call at startup."""
    home = _home()
    profile_id = _profile_id_for(home)
    config_path = home / "config.yaml"
    now = _now_ms(now_ms)

    with _config_lock():
        if not journal_path().exists():
            raw = _read_raw_config(config_path)
            return RecoveryResult(action="none", config_hash=_config_hash(raw))

        try:
            journal = json.loads(journal_path().read_text(encoding="utf-8"))
            before_hash = journal["before_config_hash"]
            after_hash = journal["after_config_hash"]
            has_backup = bool(journal.get("has_backup"))
        except (OSError, ValueError, KeyError) as exc:
            raise IncompleteAuthorityApply(
                f"{INCOMPLETE_AUTHORITY_APPLY}: recovery journal "
                f"{journal_path()} is unreadable ({exc}); authority "
                "mutations stay blocked until it is resolved"
            ) from exc

        raw = _read_raw_config(config_path)
        current_hash = _config_hash(raw)

        with _session_db(db) as sdb:
            if current_hash == after_hash:
                stored = _sync_head(sdb, raw, profile_id, now)
                action = "completed"
            elif current_hash == before_hash:
                stored = _sync_head(sdb, raw, profile_id, now)
                action = "rolled_back"
            else:
                backup_cfg: dict = {}
                if has_backup:
                    backup_cfg = _read_raw_config(backup_path())
                if _config_hash(backup_cfg) != before_hash:
                    raise IncompleteAuthorityApply(
                        f"{INCOMPLETE_AUTHORITY_APPLY}: config.yaml matches "
                        "neither the before nor the after state and the "
                        "backup does not verify; refusing to guess"
                    )
                from hades_cli.config import atomic_config_write

                atomic_config_write(config_path, backup_cfg)
                stored = _sync_head(sdb, backup_cfg, profile_id, now)
                current_hash = before_hash
                action = "restored"

        _remove_saga_files()
        return RecoveryResult(
            action=action, config_hash=current_hash, contract=stored
        )
