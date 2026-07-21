"""ACP session manager — maps ACP sessions to Hades AIAgent instances.

Sessions are persisted to the shared SessionDB (``~/.hades/state.db``) so they
survive process restarts and appear in ``session_search``.  When the editor
reconnects after idle/restart, the ``load_session`` / ``resume_session`` calls
find the persisted session in the database and restore the full conversation
history.
"""
from __future__ import annotations

from hades_constants import get_hades_home

import copy
import inspect
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _translate_acp_cwd(cwd: str) -> str:
    """Translate Windows ACP cwd values when Hermes itself is running in WSL.

    Windows ACP clients can launch ``hermes acp`` inside WSL while still sending
    editor workspaces as Windows drive paths (``E:\\Projects``) or
    ``\\\\wsl.localhost\\`` UNC paths. Store and execute against the POSIX form so
    agents, tools, and persisted ACP sessions all agree on the usable workspace.
    Native Linux/macOS keeps the original cwd unchanged.
    """
    from hades_constants import translate_cwd_for_wsl_backend

    return translate_cwd_for_wsl_backend(str(cwd))


def _normalize_cwd_for_compare(cwd: str | None) -> str:
    raw = str(cwd or ".").strip()
    if not raw:
        raw = "."
    expanded = os.path.expanduser(raw)

    # Normalize Windows drive paths into the equivalent WSL mount form so
    # ACP history filters match the same workspace across Windows and WSL.
    from hades_constants import windows_path_to_wsl

    translated = windows_path_to_wsl(expanded)
    if translated is not None:
        expanded = translated
    elif re.match(r"^/mnt/[A-Za-z]/", expanded):
        expanded = f"/mnt/{expanded[5].lower()}/{expanded[7:]}"

    return os.path.normpath(expanded)


def _build_session_title(title: Any, preview: Any, cwd: str | None) -> str:
    explicit = str(title or "").strip()
    if explicit:
        return explicit
    preview_text = str(preview or "").strip()
    if preview_text:
        return preview_text
    leaf = os.path.basename(str(cwd or "").rstrip("/\\"))
    return leaf or "New thread"


def _format_updated_at(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _updated_at_sort_key(value: Any) -> float:
    if value is None:
        return float("-inf")
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return float("-inf")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        try:
            return float(raw)
        except Exception:
            return float("-inf")


def _acp_stderr_print(*args, **kwargs) -> None:
    """Best-effort human-readable output sink for ACP stdio sessions.

    ACP reserves stdout for JSON-RPC frames, so any incidental CLI/status output
    from AIAgent must be redirected away from stdout. Route it to stderr instead.
    """
    kwargs = dict(kwargs)
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def _register_task_cwd(task_id: str, cwd: str) -> None:
    """Bind a task/session id to the editor's working directory for tools.

    Zed can launch Hermes from a Windows workspace while the ACP process runs
    inside WSL. In that case ACP sends cwd as e.g. ``E:\\Projects\\POTI``;
    local tools need the WSL mount equivalent or subprocess creation fails
    before the command can run.
    """
    if not task_id:
        return
    try:
        from tools.terminal_tool import register_task_env_overrides
        register_task_env_overrides(task_id, {"cwd": _translate_acp_cwd(cwd)})
    except Exception:
        logger.debug("Failed to register ACP task cwd override", exc_info=True)


def _expand_acp_enabled_toolsets(
    toolsets: List[str] | None = None,
    mcp_server_names: List[str] | None = None,
) -> List[str]:
    """Return ACP toolsets plus explicit MCP server toolsets for this session."""
    expanded: List[str] = []
    for name in list(toolsets or ["hades-acp"]):
        if name and name not in expanded:
            expanded.append(name)

    for server_name in list(mcp_server_names or []):
        toolset_name = f"mcp-{server_name}"
        if server_name and toolset_name not in expanded:
            expanded.append(toolset_name)

    return expanded


def _clear_task_cwd(task_id: str) -> None:
    """Remove task-specific cwd overrides for an ACP session."""
    if not task_id:
        return
    try:
        from tools.terminal_tool import clear_task_env_overrides
        clear_task_env_overrides(task_id)
    except Exception:
        logger.debug("Failed to clear ACP task cwd override", exc_info=True)


@dataclass
class SessionState:
    """Tracks per-session state for an ACP-managed Hades agent."""

    session_id: str
    agent: Any | None = None  # Built on the first model prompt.
    cwd: str = "."
    model: str = ""
    requested_provider: str | None = None
    base_url: str | None = None
    api_mode: str | None = None
    manual_runtime_pin: bool = False
    manual_pin_source: str | None = None
    mcp_servers: List[Any] = field(default_factory=list, repr=False)
    mcp_servers_registered: bool = False
    history: List[Dict[str, Any]] = field(default_factory=list)
    cancel_event: Any = None  # threading.Event
    is_running: bool = False
    queued_prompts: List[str] = field(default_factory=list)
    runtime_lock: Any = field(default_factory=Lock)
    current_prompt_text: str = ""
    interrupted_prompt_text: str = ""
    is_resume: bool = False


class SessionManager:
    """Thread-safe manager for ACP sessions backed by Hades AIAgent instances.

    Sessions are held in-memory for fast access **and** persisted to the
    shared SessionDB so they survive process restarts and are searchable
    via ``session_search``.
    """

    def __init__(self, agent_factory=None, db=None):
        """
        Args:
            agent_factory: Optional callable that creates an AIAgent-like object.
                           Used by tests. When omitted, a real AIAgent is created
                           using the current Hermes runtime provider configuration.
            db:            Optional SessionDB instance. When omitted, the default
                           SessionDB (``~/.hades/state.db``) is lazily created.
        """
        self._sessions: Dict[str, SessionState] = {}
        self._lock = Lock()
        self._agent_factory = agent_factory
        self._db_instance = db  # None → lazy-init on first use

    # ---- public API ---------------------------------------------------------

    def create_session(self, cwd: str = ".") -> SessionState:
        """Allocate and persist a session without constructing a provider client."""
        import threading

        cwd = _translate_acp_cwd(cwd)
        session_id = str(uuid.uuid4())
        state = SessionState(
            session_id=session_id,
            cwd=cwd,
            cancel_event=threading.Event(),
        )
        with self._lock:
            self._sessions[session_id] = state
        _register_task_cwd(session_id, cwd)
        self._persist(state)
        logger.info("Created ACP session %s (cwd=%s)", session_id, cwd)
        return state

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Return the session for *session_id*, or ``None``.

        If the session is not in memory but exists in the database (e.g. after
        a process restart), it is transparently restored.
        """
        with self._lock:
            state = self._sessions.get(session_id)
        if state is not None:
            return state
        # Attempt to restore from database.
        return self._restore(session_id)

    def remove_session(self, session_id: str) -> bool:
        """Remove a session from memory and database. Returns True if it existed."""
        with self._lock:
            existed = self._sessions.pop(session_id, None) is not None
        db_existed = self._delete_persisted(session_id)
        if existed or db_existed:
            _clear_task_cwd(session_id)
        return existed or db_existed

    def fork_session(self, session_id: str, cwd: str = ".") -> Optional[SessionState]:
        """Deep-copy session state while deferring the fork's agent construction."""
        import threading

        cwd = _translate_acp_cwd(cwd)
        original = self.get_session(session_id)  # checks DB too
        if original is None:
            return None

        new_id = str(uuid.uuid4())
        state = SessionState(
            session_id=new_id,
            cwd=cwd,
            model=original.model,
            requested_provider=original.requested_provider,
            base_url=original.base_url,
            api_mode=original.api_mode,
            manual_runtime_pin=original.manual_runtime_pin,
            manual_pin_source=original.manual_pin_source,
            history=copy.deepcopy(original.history),
            cancel_event=threading.Event(),
        )
        with self._lock:
            self._sessions[new_id] = state
        _register_task_cwd(new_id, cwd)
        self._persist(state)
        logger.info("Forked ACP session %s -> %s", session_id, new_id)
        return state

    def list_sessions(self, cwd: str | None = None) -> List[Dict[str, Any]]:
        """Return lightweight info dicts for all sessions (memory + database)."""
        normalized_cwd = _normalize_cwd_for_compare(cwd) if cwd else None
        db = self._get_db()
        persisted_rows: dict[str, dict[str, Any]] = {}

        if db is not None:
            try:
                for row in db.list_sessions_rich(source="acp", limit=1000):
                    persisted_rows[str(row["id"])] = dict(row)
            except Exception:
                logger.debug("Failed to load ACP sessions from DB", exc_info=True)

        # Collect in-memory sessions first.
        with self._lock:
            seen_ids = set(self._sessions.keys())
            results = []
            for s in self._sessions.values():
                history_len = len(s.history)
                if history_len <= 0:
                    continue
                if normalized_cwd and _normalize_cwd_for_compare(s.cwd) != normalized_cwd:
                    continue
                persisted = persisted_rows.get(s.session_id, {})
                preview = next(
                    (
                        str(msg.get("content") or "").strip()
                        for msg in s.history
                        if msg.get("role") == "user" and str(msg.get("content") or "").strip()
                    ),
                    persisted.get("preview") or "",
                )
                results.append(
                    {
                        "session_id": s.session_id,
                        "cwd": s.cwd,
                        "model": s.model,
                        "history_len": history_len,
                        "title": _build_session_title(persisted.get("title"), preview, s.cwd),
                        "updated_at": _format_updated_at(
                            persisted.get("last_active") or persisted.get("started_at") or time.time()
                        ),
                    }
                )

        # Merge any persisted sessions not currently in memory.
        for sid, row in persisted_rows.items():
            if sid in seen_ids:
                continue
            message_count = int(row.get("message_count") or 0)
            if message_count <= 0:
                continue
            # Extract cwd from model_config JSON.
            session_cwd = "."
            mc = row.get("model_config")
            if mc:
                try:
                    session_cwd = json.loads(mc).get("cwd", ".")
                except (json.JSONDecodeError, TypeError):
                    pass
            if normalized_cwd and _normalize_cwd_for_compare(session_cwd) != normalized_cwd:
                continue
            results.append({
                "session_id": sid,
                "cwd": session_cwd,
                "model": row.get("model") or "",
                "history_len": message_count,
                "title": _build_session_title(row.get("title"), row.get("preview"), session_cwd),
                "updated_at": _format_updated_at(row.get("last_active") or row.get("started_at")),
            })

        results.sort(key=lambda item: _updated_at_sort_key(item.get("updated_at")), reverse=True)
        return results

    def update_cwd(self, session_id: str, cwd: str) -> Optional[SessionState]:
        """Update the working directory for a session and its tool overrides."""
        cwd = _translate_acp_cwd(cwd)
        state = self.get_session(session_id)  # checks DB too
        if state is None:
            return None
        state.cwd = cwd
        _register_task_cwd(session_id, cwd)
        self._persist(state)
        return state

    def cleanup(self) -> None:
        """Remove all sessions (memory and database) and clear task-specific cwd overrides."""
        with self._lock:
            session_ids = list(self._sessions.keys())
            self._sessions.clear()
        for session_id in session_ids:
            _clear_task_cwd(session_id)
            self._delete_persisted(session_id)
        # Also remove any DB-only ACP sessions not currently in memory.
        db = self._get_db()
        if db is not None:
            try:
                rows = db.search_sessions(source="acp", limit=10000)
                for row in rows:
                    sid = row["id"]
                    _clear_task_cwd(sid)
                    db.delete_session(sid)
            except Exception:
                logger.debug("Failed to cleanup ACP sessions from DB", exc_info=True)

    def save_session(self, session_id: str) -> None:
        """Persist the current state of a session to the database.

        Called by the server after prompt completion, slash commands that
        mutate history, and model switches.
        """
        with self._lock:
            state = self._sessions.get(session_id)
        if state is not None:
            self._persist(state)

    def ensure_agent(self, state: SessionState, *, task: Any) -> Any:
        """Construct the canonical ACP agent once the first model task is known."""
        if state.agent is not None:
            return state.agent

        from agent.runtime_routing import (
            AgentRuntimeContext,
            apply_manual_runtime_transition,
            constructor_runtime_spec,
            runtime_resolver_requires_initial_task,
        )
        route_first_prompt = runtime_resolver_requires_initial_task("fresh_session")
        context = None
        if route_first_prompt:
            context = AgentRuntimeContext(
                scope="fresh_session",
                task=task,
                session_id=state.session_id,
                task_id=state.session_id,
                is_resume=state.is_resume,
                manual_runtime_pin=state.manual_runtime_pin,
                manual_pin_source=state.manual_pin_source,
                metadata={"platform": "acp"},
            )
        fallback_model: list[dict[str, Any]] = []
        if route_first_prompt or state.manual_runtime_pin:
            from hermes_cli.config import load_config
            from hermes_cli.fallback_config import get_fallback_chain

            fallback_model = get_fallback_chain(load_config())
        agent = self._make_agent(
            session_id=state.session_id,
            cwd=state.cwd,
            model=state.model or None,
            requested_provider=state.requested_provider,
            base_url=state.base_url,
            api_mode=state.api_mode,
            fallback_model=fallback_model or None,
            mcp_server_names=[server.name for server in state.mcp_servers],
            runtime_routing_context=context,
        )
        state.agent = agent
        state.model = getattr(agent, "model", state.model) or state.model
        if state.manual_runtime_pin:
            runtime = constructor_runtime_spec(
                model=state.model,
                provider=getattr(agent, "provider", None) or state.requested_provider,
                base_url=getattr(agent, "base_url", None),
                api_key=getattr(agent, "api_key", None),
                api_mode=getattr(agent, "api_mode", None),
                acp_command=getattr(agent, "acp_command", None),
                acp_args=getattr(agent, "acp_args", None),
                credential_pool=getattr(
                    agent,
                    "credential_pool",
                    getattr(agent, "_credential_pool", None),
                ),
                reasoning_config=getattr(agent, "reasoning_config", None),
                fallback_model=fallback_model,
            )
            apply_manual_runtime_transition(
                agent,
                session_id=state.session_id,
                source=state.manual_pin_source or "acp_model_selection",
                runtime=runtime,
                fallback_model=fallback_model,
            )
        self._persist(state)
        return agent

    # ---- persistence via SessionDB ------------------------------------------

    def _get_db(self):
        """Lazily initialise and return the SessionDB instance.

        Returns ``None`` if the DB is unavailable (e.g. import error in a
        minimal test environment).

        Note: we resolve ``HADES_HOME`` dynamically rather than relying on
        the module-level ``DEFAULT_DB_PATH`` constant, because that constant
        is evaluated at import time and won't reflect env-var changes made
        later (e.g. by the test fixture ``_isolate_hermes_home``).
        """
        if self._db_instance is not None:
            return self._db_instance
        try:
            from hades_state import SessionDB
            hermes_home = get_hades_home()
            self._db_instance = SessionDB(db_path=hermes_home / "state.db")
            return self._db_instance
        except Exception:
            logger.debug("SessionDB unavailable for ACP persistence", exc_info=True)
            return None

    def _persist(self, state: SessionState) -> None:
        """Write session state to the database.

        Creates the session record if it doesn't exist, then replaces all
        stored messages with the current in-memory history.
        """
        db = self._get_db()
        if db is None:
            return

        # Ensure model is a plain string (not a MagicMock or other proxy).
        model_str = str(state.model) if state.model else None
        session_meta = {"cwd": state.cwd}
        provider = getattr(state.agent, "provider", None) or state.requested_provider
        base_url = getattr(state.agent, "base_url", None) or state.base_url
        api_mode = getattr(state.agent, "api_mode", None) or state.api_mode
        if isinstance(provider, str) and provider.strip():
            session_meta["provider"] = provider.strip()
        if isinstance(base_url, str) and base_url.strip():
            session_meta["base_url"] = base_url.strip()
        if isinstance(api_mode, str) and api_mode.strip():
            session_meta["api_mode"] = api_mode.strip()
        if state.manual_runtime_pin:
            session_meta["manual_runtime_pin"] = True
            if state.manual_pin_source:
                session_meta["manual_pin_source"] = state.manual_pin_source
        cwd_json = json.dumps(session_meta)

        try:
            # Ensure the session record exists.
            existing = db.get_session(state.session_id)
            if existing is None:
                db.create_session(
                    session_id=state.session_id,
                    source="acp",
                    model=model_str,
                    model_config=session_meta,
                )
            else:
                # Update model_config (contains cwd) if changed.
                try:
                    db.update_session_meta(state.session_id, cwd_json, model_str)
                except Exception:
                    logger.debug("Failed to update ACP session metadata", exc_info=True)

            # When the agent owns persistence to this same SessionDB it has
            # already flushed the live transcript incrementally during
            # run_conversation (append_message), and it preserves pre-compaction
            # turns non-destructively via archive_and_compact() — keeping them on
            # disk as searchable active=0/compacted=1 rows. Calling
            # replace_messages() here would then be a redundant double-write that
            # DELETEs exactly those archived rows (and, after a compression-driven
            # id rotation where agent.session_id no longer equals
            # state.session_id, clobbers the ended parent transcript) — silent
            # data loss for any ACP conversation long enough to compress.
            #
            # Only fall back to the destructive atomic replace when the agent is
            # NOT persisting itself to this DB (e.g. a test agent factory, or a
            # fresh create/fork whose copied history the agent has not flushed
            # yet). That path still rolls back on a mid-rewrite failure so the
            # previously persisted conversation survives (salvaged from #13675).
            agent = state.agent
            agent_db = getattr(agent, "_session_db", None)
            agent_owns_persistence = (
                agent_db is not None
                and agent_db is db
                and bool(getattr(agent, "_session_db_created", False))
            )
            if not agent_owns_persistence:
                # Even when the current agent doesn't "own" persistence, the
                # session on disk may already carry compaction-archived rows —
                # e.g. after a model switch or a /restore, both of which mint a
                # fresh agent with _session_db_created=False (so the check above
                # is False) yet leave the durable archived transcript in place.
                # A full-history replace would DELETE those archived rows just
                # like the owned-agent case. Guard against it: when archived
                # rows exist, replace ONLY the live (active=1) set and leave the
                # archived turns untouched; otherwise the destructive replace is
                # safe (fresh create/fork with no archived history to lose).
                try:
                    has_archived = db.has_archived_messages(state.session_id)
                except Exception:
                    has_archived = False
                db.replace_messages(
                    state.session_id, state.history, active_only=has_archived
                )
        except Exception:
            logger.warning("Failed to persist ACP session %s", state.session_id, exc_info=True)

    def _restore(self, session_id: str) -> Optional[SessionState]:
        """Load persisted ACP state while keeping agent construction deferred."""
        import threading

        db = self._get_db()
        if db is None:
            return None

        try:
            row = db.get_session(session_id)
        except Exception:
            logger.debug("Failed to query DB for ACP session %s", session_id, exc_info=True)
            return None

        if row is None:
            return None

        # Only restore ACP sessions.
        if row.get("source") != "acp":
            return None

        # Extract cwd from model_config.
        cwd = "."
        requested_provider = row.get("billing_provider")
        restored_base_url = row.get("billing_base_url")
        restored_api_mode = None
        manual_runtime_pin = False
        manual_pin_source = None
        mc = row.get("model_config")
        if mc:
            try:
                meta = json.loads(mc)
                if isinstance(meta, dict):
                    cwd = meta.get("cwd", ".")
                    requested_provider = meta.get("provider") or requested_provider
                    restored_base_url = meta.get("base_url") or restored_base_url
                    restored_api_mode = meta.get("api_mode") or restored_api_mode
                    manual_runtime_pin = meta.get("manual_runtime_pin") is True
                    manual_pin_source = meta.get("manual_pin_source")
            except (json.JSONDecodeError, TypeError):
                pass

        model = row.get("model") or None

        # Load conversation history. repair_alternation: this restore feeds
        # LIVE REPLAY — the loaded list becomes the resumed agent's working
        # conversation. A durable ``user;user`` violation left in state.db would
        # otherwise re-fire the pre-request defensive repair on every request
        # for the rest of the session (see hades_state.get_messages_as_conversation).
        try:
            history = db.get_messages_as_conversation(session_id)
        except Exception:
            logger.warning("Failed to load messages for ACP session %s", session_id, exc_info=True)
            history = []

        state = SessionState(
            session_id=session_id,
            cwd=cwd,
            model=model or "",
            requested_provider=requested_provider,
            base_url=restored_base_url,
            api_mode=restored_api_mode,
            manual_runtime_pin=manual_runtime_pin,
            manual_pin_source=manual_pin_source,
            history=history,
            cancel_event=threading.Event(),
            is_resume=True,
        )
        with self._lock:
            self._sessions[session_id] = state
        _register_task_cwd(session_id, cwd)
        logger.info("Restored ACP session %s from DB (%d messages)", session_id, len(history))
        return state

    def _delete_persisted(self, session_id: str) -> bool:
        """Delete a session from the database. Returns True if it existed."""
        db = self._get_db()
        if db is None:
            return False
        try:
            return db.delete_session(session_id)
        except Exception:
            logger.debug("Failed to delete ACP session %s from DB", session_id, exc_info=True)
            return False

    # ---- internal -----------------------------------------------------------

    def _make_agent(
        self,
        *,
        session_id: str,
        cwd: str,
        model: str | None = None,
        requested_provider: str | None = None,
        base_url: str | None = None,
        api_mode: str | None = None,
        fallback_model: Any = None,
        mcp_server_names: List[str] | None = None,
        runtime_routing_context: Any = None,
    ):
        if self._agent_factory is not None:
            factory_kwargs = {
                "session_id": session_id,
                "cwd": cwd,
                "model": model,
                "requested_provider": requested_provider,
                "base_url": base_url,
                "api_mode": api_mode,
                "fallback_model": fallback_model,
                "mcp_server_names": mcp_server_names,
                "runtime_routing_context": runtime_routing_context,
            }
            try:
                parameters = inspect.signature(self._agent_factory).parameters
            except (TypeError, ValueError):
                return self._agent_factory()
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                return self._agent_factory(**factory_kwargs)
            accepted = {
                key: value for key, value in factory_kwargs.items() if key in parameters
            }
            return self._agent_factory(**accepted)

        from run_agent import AIAgent
        from hades_cli.config import load_config
        from hades_cli.runtime_provider import resolve_runtime_provider

        config = load_config()
        model_cfg = config.get("model")
        default_model = ""
        config_provider = None
        if isinstance(model_cfg, dict):
            default_model = str(model_cfg.get("default") or default_model)
            config_provider = model_cfg.get("provider")
        elif isinstance(model_cfg, str) and model_cfg.strip():
            default_model = model_cfg.strip()

        configured_mcp_servers = [
            name
            for name, cfg in (config.get("mcp_servers") or {}).items()
            if not isinstance(cfg, dict) or cfg.get("enabled", True) is not False
        ]

        kwargs = {
            "platform": "acp",
            "enabled_toolsets": _expand_acp_enabled_toolsets(
                ["hades-acp"],
                mcp_server_names=[
                    *configured_mcp_servers,
                    *(mcp_server_names or []),
                ],
            ),
            "quiet_mode": True,
            "session_id": session_id,
            "session_db": self._get_db(),
            "model": model or default_model,
        }
        if fallback_model is not None:
            kwargs["fallback_model"] = fallback_model

        if runtime_routing_context is not None:
            kwargs.update(
                {
                    "provider": requested_provider or config_provider,
                    "api_mode": api_mode,
                    "base_url": base_url,
                    "runtime_routing_context": runtime_routing_context,
                }
            )

        if runtime_routing_context is None:
            try:
                runtime = resolve_runtime_provider(requested=requested_provider or config_provider)
                kwargs.update(
                    {
                        "provider": runtime.get("provider"),
                        "api_mode": api_mode or runtime.get("api_mode"),
                        "base_url": base_url or runtime.get("base_url"),
                        "api_key": runtime.get("api_key"),
                        "command": runtime.get("command"),
                        "args": list(runtime.get("args") or []),
                    }
                )
            except Exception:
                logger.debug("ACP session falling back to default provider resolution", exc_info=True)

        _register_task_cwd(session_id, cwd)
        agent = AIAgent(**kwargs)
        # Codex app-server sessions are spawned lazily on the first turn. Stamp
        # the ACP workspace onto the agent so the Codex runtime starts from the
        # editor/session cwd instead of the Hades daemon's process cwd.
        agent.session_cwd = cwd
        # ACP stdio transport requires stdout to remain protocol-only JSON-RPC.
        # Route any incidental human-readable agent output to stderr instead.
        agent._print_fn = _acp_stderr_print
        return agent
