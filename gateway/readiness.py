"""Bounded, non-destructive readiness probes for authenticated health surfaces."""

from __future__ import annotations

import math
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home


_DISK_DEGRADED_PERCENT = 90.0


def _check(status: str, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if detail:
        result["detail"] = detail
    result.update(extra)
    return result


def _probe_state_db(home: Path) -> dict[str, Any]:
    path = home / "state.db"
    if not path.exists():
        return _check("ok", "not initialized")
    try:
        # A readiness probe must never compete with normal state writers. A
        # read-only schema query still catches unreadable/corrupt databases
        # without taking a write reservation on every health poll.
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return _check("ok")
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if not path.exists():
        return _check("ok", "using defaults")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            return _check("degraded", "top level is not a mapping")
        return _check("ok")
    except Exception as exc:
        return _check("degraded", f"invalid config ({type(exc).__name__})")


def _probe_disk(home: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(home)
        used_pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
        status = "degraded" if used_pct >= _DISK_DEGRADED_PERCENT else "ok"
        return _check(status, used_percent=used_pct, free_bytes=usage.free)
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _live_platform_health() -> dict[str, dict[str, Any]]:
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        status = runner._runtime_health_status() if runner is not None else {}
        return status if isinstance(status, dict) else {}
    except Exception:
        return {}


def _probe_gateway(runtime_status: dict[str, Any]) -> dict[str, Any]:
    state = str(runtime_status.get("gateway_state") or "unknown")
    platforms = runtime_status.get("platforms")
    live_health = _live_platform_health()
    connected = 0
    configured = 0
    degraded = 0
    platform_health: dict[str, dict[str, Any]] = {}
    if isinstance(platforms, dict):
        configured = len(platforms)
        for name, value in platforms.items():
            if not isinstance(value, dict):
                degraded += 1
                continue
            platform_state = str(value.get("state") or value.get("status") or "").lower()
            if platform_state in {"connected", "running", "ok"}:
                connected += 1
            live = live_health.get(str(name), {})
            health_state = str(live.get("health_state") or value.get("health_state") or "").lower()
            if health_state not in {"healthy", "degraded", "open_circuit", "probing"}:
                health_state = "healthy" if platform_state in {"connected", "running", "ok"} else "degraded"
            elif health_state == "healthy" and platform_state not in {"connected", "running", "ok"}:
                # Lifecycle wins over a stale healthy snapshot: readiness must
                # fail closed while a platform is retrying or otherwise down.
                health_state = "degraded"
            try:
                next_probe_at = float(live.get("next_probe_at", value.get("next_probe_at", 0.0)))
                if not math.isfinite(next_probe_at) or next_probe_at < 0:
                    next_probe_at = 0.0
            except (TypeError, ValueError):
                next_probe_at = 0.0
            try:
                suppressed_failures = max(
                    0, int(live.get("suppressed_failures", value.get("suppressed_failures", 0)))
                )
            except (TypeError, ValueError):
                suppressed_failures = 0
            platform_health[str(name)] = {
                "health_state": health_state,
                "next_probe_at": next_probe_at,
                "suppressed_failures": suppressed_failures,
            }
            degraded += health_state != "healthy"
    status = "ok" if state in {"running", "draining"} and not degraded else "degraded"
    return _check(
        status,
        state=state,
        connected_platforms=connected,
        platforms=configured,
        configured=configured,
        connected=connected,
        degraded=degraded,
        configured_platforms=configured,
        degraded_platforms=degraded,
        platform_health=platform_health,
    )


def collect_runtime_readiness(
    *,
    configured_model: str,
    runtime_status: dict[str, Any] | None,
    active_api_runs: int = 0,
    process_completion_queue_depth: int = 0,
    active_delegations: int = 0,
) -> dict[str, Any]:
    """Return bounded readiness diagnostics without mutating runtime state.

    The detailed health endpoint is authenticated. Even there, probes expose
    status and counts only: never config values, credentials, paths, commands,
    queue payloads, or exception messages.
    """
    home = get_hermes_home()
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    checks = {
        "state_db": _probe_state_db(home),
        "config": _probe_config(home),
        "model": _check("ok" if str(configured_model or "").strip() else "degraded"),
        "disk": _probe_disk(home),
        "gateway": _probe_gateway(runtime),
        "background_queues": _check(
            "ok",
            active_api_runs=max(0, int(active_api_runs)),
            process_completions=max(0, int(process_completion_queue_depth)),
            active_delegations=max(0, int(active_delegations)),
        ),
    }
    overall = "ok" if all(item.get("status") == "ok" for item in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


__all__ = ["collect_runtime_readiness"]
