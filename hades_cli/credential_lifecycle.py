"""Canonical provider-credential lifecycle across Hades' durable stores.

Provider API keys may be represented in three profile-scoped files:

* ``.env`` is the canonical secret store.
* ``auth.json`` may contain credential-pool entries seeded from an env var.
* ``config.yaml`` may contain value-matched inline mirrors for custom
  endpoints and auxiliary models.

Save and remove operations must keep those representations coherent while
preserving unrelated credentials. In particular, removing an env key only
prunes pool entries whose source is exactly ``env:<VAR>``; OAuth, device-code,
manual, and borrowed entries survive unchanged.

Each file mutation uses that store's existing atomic writer. A profile-local
compensation boundary snapshots the exact prior bytes and mode before the
first mutation, then atomically restores every store if a later write fails.
The lifecycle is deliberately fail-loud: an I/O failure propagates to the
caller instead of reporting a successful reconciliation. Result dictionaries
and rollback errors contain key/provider/store names only, never credential
values.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterable, Iterator

__all__ = [
    "is_provider_env_credential",
    "save_provider_env_credential",
    "remove_provider_env_credential",
    "purge_env_credential_references",
]

_MISSING_ENV_VALUE = object()


@dataclass(frozen=True)
class _FileSnapshot:
    """In-memory rollback state for one profile-owned credential store."""

    path: Path
    existed: bool
    content: bytes
    mode: int | None
    owner: tuple[int, int] | None


def _snapshot_file(path: Path) -> _FileSnapshot:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return _FileSnapshot(
            path=path,
            existed=False,
            content=b"",
            mode=None,
            owner=None,
        )

    try:
        file_stat = path.stat()
    except OSError:
        mode = None
        owner = None
    else:
        mode = stat.S_IMODE(file_stat.st_mode)
        owner = (
            (file_stat.st_uid, file_stat.st_gid)
            if hasattr(os, "chown")
            else None
        )
    return _FileSnapshot(
        path=path,
        existed=True,
        content=content,
        mode=mode,
        owner=owner,
    )


def _restore_file(snapshot: _FileSnapshot) -> None:
    """Restore one snapshot without parsing or exposing secret-bearing bytes."""
    path = snapshot.path
    if not snapshot.existed:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return

    from utils import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".credential_rollback_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as file_handle:
            file_handle.write(snapshot.content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        restored_path = Path(atomic_replace(tmp_path, path))
        if snapshot.owner is not None:
            try:
                os.chown(restored_path, *snapshot.owner)
            except OSError:
                pass
        if snapshot.mode is not None:
            try:
                os.chmod(restored_path, snapshot.mode)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _invalidate_restored_config_caches(config_path: Path) -> None:
    """Ensure the process cannot retain a value from a compensated write."""
    from hades_cli import config

    config.invalidate_env_cache()
    path_key = str(config_path)
    config._LOAD_CONFIG_CACHE.pop(path_key, None)
    config._RAW_CONFIG_CACHE.pop(path_key, None)
    config._LAST_EXPANDED_CONFIG_BY_PATH.pop(path_key, None)


@contextmanager
def _credential_store_transaction(env_var: str) -> Iterator[None]:
    """Compensate all profile credential stores after any failed mutation."""
    from hades_cli.auth import _auth_file_path, _auth_store_lock
    from hades_cli.config import get_config_path, get_env_path
    from hades_constants import get_hades_home

    env_path = get_env_path()
    auth_path = _auth_file_path()
    config_path = get_config_path()
    models_cache_path = get_hades_home() / "provider_models_cache.json"

    # The auth lock is profile-path-specific and reentrant. Holding it across
    # the lifecycle prevents a rollback from overwriting a concurrent auth
    # update while nested auth helpers retain their existing locking contract.
    with _auth_store_lock():
        snapshots = (
            ("env", _snapshot_file(env_path)),
            ("auth", _snapshot_file(auth_path)),
            ("config", _snapshot_file(config_path)),
            ("models_cache", _snapshot_file(models_cache_path)),
        )
        process_env_value = os.environ.get(env_var, _MISSING_ENV_VALUE)
        try:
            yield
        except BaseException as operation_error:
            rollback_failures: list[str] = []
            for store_name, snapshot in reversed(snapshots):
                try:
                    _restore_file(snapshot)
                except BaseException:
                    rollback_failures.append(store_name)

            if process_env_value is _MISSING_ENV_VALUE:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = str(process_env_value)
            _invalidate_restored_config_caches(config_path)

            if rollback_failures:
                stores = ", ".join(sorted(rollback_failures))
                raise RuntimeError(
                    f"credential lifecycle rollback incomplete for stores: {stores}"
                ) from operation_error
            raise


def _canonical_provider_id(provider_id: str) -> str:
    """Return the current canonical identity for a provider or legacy alias."""
    try:
        from hades_cli.providers import normalize_provider

        return normalize_provider(provider_id) or provider_id
    except Exception:
        return provider_id


def _require_env_key_writable(env_var: str, action: str) -> None:
    """Fail loudly before a managed env writer can return a silent no-op."""
    from hades_cli import managed_scope
    from hades_cli.config import is_managed

    if is_managed() or managed_scope.is_env_managed(env_var):
        raise RuntimeError(
            f"credential env key {env_var} is managed and cannot be {action}"
        )


def _providers_for_env_var(env_var: str) -> list[str]:
    """Return canonical providers whose credential vars include *env_var*.

    ``PROVIDER_REGISTRY`` remains the primary credential authority. The
    unified provider catalog is the fallback for Keys-tab API-key providers
    intentionally omitted from that registry (currently OpenRouter). Evaluate
    both at call time so profile/plugin-scoped catalogs are never frozen into
    process-global dispatch state.
    """
    providers: set[str] = set()
    try:
        from hades_cli.auth import PROVIDER_REGISTRY
    except Exception:
        PROVIDER_REGISTRY = {}

    for provider_id, provider_config in PROVIDER_REGISTRY.items():
        try:
            if env_var in (provider_config.api_key_env_vars or ()):
                providers.add(_canonical_provider_id(provider_id))
        except (AttributeError, TypeError):
            continue

    try:
        from hades_cli.provider_catalog import provider_catalog

        catalog = provider_catalog()
    except Exception:
        catalog = ()
    for descriptor in catalog:
        try:
            if (
                descriptor.tab == "keys"
                and env_var in (descriptor.api_key_env_vars or ())
            ):
                providers.add(_canonical_provider_id(descriptor.slug))
        except (AttributeError, TypeError):
            continue
    return sorted(providers)


def is_provider_env_credential(env_var: str) -> bool:
    """Return whether *env_var* is a registered provider credential key."""
    return bool(_providers_for_env_var(env_var))


def _prune_env_pool_entries(
    env_var: str,
) -> tuple[list[str], tuple[str, ...]]:
    """Prune exact env entries and briefly retain their stale mirror values."""
    from hades_cli.auth import (
        _auth_store_lock,
        _load_auth_store,
        _save_auth_store,
    )

    source = f"env:{env_var}"
    pruned: list[str] = []
    stale_values: list[str] = []
    with _auth_store_lock():
        auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            return pruned, ()

        changed = False
        for provider_id in list(pool):
            entries = pool[provider_id]
            if not isinstance(entries, list):
                continue
            kept: list[Any] = []
            for entry in entries:
                if (
                    isinstance(entry, dict)
                    and entry.get("source") == source
                ):
                    access_token = entry.get("access_token")
                    if isinstance(access_token, str) and access_token:
                        stale_values.append(access_token)
                    continue
                kept.append(entry)
            if len(kept) == len(entries):
                continue

            changed = True
            pruned.append(provider_id)
            if kept:
                pool[provider_id] = kept
            else:
                del pool[provider_id]

        if changed:
            _save_auth_store(auth_store)
    return pruned, tuple(stale_values)


def _scrub_config_yaml_mirrors(
    old_values: str | Iterable[str],
    new_value: str | None,
) -> list[str]:
    """Update value-matched inline credential mirrors in raw ``config.yaml``."""
    if isinstance(old_values, str):
        matched_values = {old_values} if old_values else set()
    else:
        matched_values = {
            value
            for value in old_values
            if isinstance(value, str) and value
        }
    if not matched_values:
        return []

    from hades_cli.config import (
        atomic_config_write,
        get_config_path,
        require_readable_config_before_write,
    )
    from utils import fast_safe_load

    config_path = get_config_path()
    if not config_path.exists():
        return []

    # Validate readability before parsing and again at the atomic write
    # boundary. Do not silently claim success while a stale higher-precedence
    # config mirror remains on disk.
    require_readable_config_before_write(config_path)
    with open(config_path, encoding="utf-8") as config_file:
        user_config = fast_safe_load(config_file) or {}
    if not isinstance(user_config, dict):
        return []

    touched: list[str] = []

    def reconcile(section: Any, path: str) -> None:
        if not isinstance(section, dict):
            return
        # ``api`` is the legacy alias retained by older configurations.
        for field in ("api_key", "api"):
            if section.get(field) not in matched_values:
                continue
            if new_value:
                section[field] = new_value
            else:
                section.pop(field, None)
            touched.append(f"{path}.{field}")

    reconcile(user_config.get("model"), "model")

    auxiliary = user_config.get("auxiliary")
    if isinstance(auxiliary, dict):
        for task_name, task_config in auxiliary.items():
            reconcile(task_config, f"auxiliary.{task_name}")

    custom_providers = user_config.get("custom_providers")
    if isinstance(custom_providers, list):
        for index, provider_config in enumerate(custom_providers):
            reconcile(provider_config, f"custom_providers.{index}")
    elif isinstance(custom_providers, dict):
        for provider_name, provider_config in custom_providers.items():
            reconcile(provider_config, f"custom_providers.{provider_name}")

    if touched:
        atomic_config_write(config_path, user_config, sort_keys=False)
    return touched


def _suppression_provider_ids(env_var: str) -> list[str]:
    """Return canonical plus already-stored alias ids for one env source."""
    canonical_ids = set(_providers_for_env_var(env_var))
    provider_ids = set(canonical_ids)
    source = f"env:{env_var}"

    # Older builds could persist suppression markers under an alias. Clear
    # those on an explicit re-save too, without creating new alias markers.
    try:
        from hades_cli.auth import _load_auth_store

        suppressed = _load_auth_store().get("suppressed_sources")
        if isinstance(suppressed, dict):
            for provider_id, sources in suppressed.items():
                if (
                    isinstance(provider_id, str)
                    and isinstance(sources, list)
                    and source in sources
                    and _canonical_provider_id(provider_id) in canonical_ids
                ):
                    provider_ids.add(provider_id)
    except Exception:
        pass
    return sorted(provider_ids)


def _purge_env_credential_references(
    env_var: str,
    *,
    clear_models_cache: bool = True,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Remove references and return private stale values for config scrubbing."""
    pruned, stale_values = _prune_env_pool_entries(env_var)
    affected_providers = sorted(
        set(pruned) | set(_providers_for_env_var(env_var))
    )
    source = f"env:{env_var}"

    from hades_cli.auth import suppress_credential_source

    # Seeders resolve provider aliases to canonical identities. Suppress only
    # those canonical ids so a lingering process/shell export cannot recreate
    # the removed pool entry.
    suppression_ids = sorted(
        {
            _canonical_provider_id(provider_id)
            for provider_id in affected_providers
        }
    )
    for provider_id in suppression_ids:
        suppress_credential_source(provider_id, source)

    if clear_models_cache and affected_providers:
        from hades_cli.models import clear_provider_models_cache

        # Cache cleanup is intentionally best-effort inside the cache helper;
        # durable credential mutations remain authoritative.
        for provider_id in affected_providers:
            clear_provider_models_cache(provider_id)

    return (
        {
            "pool_pruned": pruned,
            "providers": affected_providers,
        },
        stale_values,
    )


def purge_env_credential_references(
    env_var: str,
    *,
    clear_models_cache: bool = True,
) -> dict[str, Any]:
    """Remove auth-pool and model-cache references without exposing secrets."""
    references, _stale_values = _purge_env_credential_references(
        env_var,
        clear_models_cache=clear_models_cache,
    )
    return references


def save_provider_env_credential(
    env_var: str,
    value: str,
) -> dict[str, Any]:
    """Save an env credential and rotate matching config mirrors."""
    from hades_cli.config import _save_env_value_raw, load_env

    _require_env_key_writable(env_var, "saved")
    with _credential_store_transaction(env_var):
        old_value = load_env().get(env_var)
        if _save_env_value_raw(env_var, value) is not True:
            raise RuntimeError(
                f"credential env store did not persist key {env_var}"
            )
        persisted_env = load_env()
        if env_var not in persisted_env:
            raise RuntimeError(
                f"credential env store did not persist key {env_var}"
            )
        persisted_value = persisted_env[env_var]

        config_updates: list[str] = []
        if old_value is not None and old_value != persisted_value:
            config_updates = _scrub_config_yaml_mirrors(
                old_value,
                persisted_value,
            )

        # A deliberate save is an explicit re-add. Lift canonical and legacy
        # alias suppression markers so the credential pool may seed this source
        # again. The transaction restores them if any later alias write fails.
        from hades_cli.auth import unsuppress_credential_source

        for provider_id in _suppression_provider_ids(env_var):
            unsuppress_credential_source(provider_id, f"env:{env_var}")

        return {
            "ok": True,
            "key": env_var,
            "config_updates": config_updates,
        }


def remove_provider_env_credential(env_var: str) -> dict[str, Any]:
    """Remove an env credential and every exact durable reference to it."""
    from hades_cli.config import _remove_env_value_raw, load_env

    _require_env_key_writable(env_var, "removed")
    with _credential_store_transaction(env_var):
        old_value = load_env().get(env_var)
        removed_from_env = _remove_env_value_raw(env_var)
        if old_value is not None and not removed_from_env:
            raise RuntimeError(
                f"credential env store did not remove key {env_var}"
            )
        references, stale_values = _purge_env_credential_references(env_var)
        mirror_values = list(stale_values)
        if old_value:
            mirror_values.append(old_value)
        config_scrubbed = _scrub_config_yaml_mirrors(
            mirror_values,
            None,
        )
        stale_values = ()
        mirror_values.clear()

        return {
            "ok": True,
            "key": env_var,
            "removed": removed_from_env,
            "pool_pruned": references["pool_pruned"],
            "providers": references["providers"],
            "config_scrubbed": config_scrubbed,
            "found": bool(
                removed_from_env
                or references["pool_pruned"]
                or config_scrubbed
            ),
        }
