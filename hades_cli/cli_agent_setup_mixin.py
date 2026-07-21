"""Agent-construction and session-resume display methods for ``HermesCLI``.

Extracted from ``cli.py`` as part of the god-file decomposition campaign
(``~/.hades/plans/god-file-decomposition.md``, Phase 4 step 2). This mixin holds
the agent lifecycle/setup cluster: runtime-credential resolution, per-turn agent
config, first-use agent construction, and resumed-session preload + history recap.

Behavior-neutral: every method is lifted verbatim from ``HermesCLI``. ``self.*``
calls resolve unchanged via the MRO. Neutral dependencies are imported at module
top level; ``cli.py``-internal helpers/constants are imported lazily inside each
method (``from cli import ...`` resolves at call time, when ``cli`` is fully
loaded) so this module never imports ``cli`` at import time -> no import cycle.
"""

from __future__ import annotations

import json
import sys

from rich.markup import escape as _escape


class CLIAgentSetupMixin:
    """Agent construction + session-resume display methods for ``HermesCLI``."""

    def _ensure_runtime_credentials(self) -> bool:
        """
        Ensure runtime credentials are resolved before agent use.
        Re-resolves provider credentials so key rotation and token refresh
        are picked up without restarting the CLI.
        Returns True if credentials are ready, False on auth failure.
        """
        from cli import ChatConsole, _cprint, logger
        from hades_cli.runtime_provider import (
            resolve_runtime_provider,
            format_runtime_provider_error,
        )

        _primary_exc = None
        runtime = None
        try:
            runtime = resolve_runtime_provider(
                requested=self.requested_provider,
                explicit_api_key=self._explicit_api_key,
                explicit_base_url=self._explicit_base_url,
            )
        except Exception as exc:
            _primary_exc = exc

        # Primary provider auth failed — try fallback providers before giving up.
        if runtime is None and _primary_exc is not None:
            from hades_cli.auth import AuthError
            if isinstance(_primary_exc, AuthError):
                _fb_chain = self._fallback_model if isinstance(self._fallback_model, list) else []
                for _fb in _fb_chain:
                    _fb_provider = (_fb.get("provider") or "").strip().lower()
                    _fb_model = (_fb.get("model") or "").strip()
                    if not _fb_provider or not _fb_model:
                        continue
                    try:
                        runtime = resolve_runtime_provider(requested=_fb_provider)
                        logger.warning(
                            "Primary provider auth failed (%s). Falling through to fallback: %s/%s",
                            _primary_exc, _fb_provider, _fb_model,
                        )
                        _cprint(f"⚠️  Primary auth failed — switching to fallback: {_fb_provider} / {_fb_model}")
                        self.requested_provider = _fb_provider
                        self.model = _fb_model
                        _primary_exc = None
                        break
                    except Exception:
                        continue

        if runtime is None:
            message = format_runtime_provider_error(_primary_exc) if _primary_exc else "Provider resolution failed."
            ChatConsole().print(f"[bold red]{message}[/]")
            return False

        api_key = runtime.get("api_key")
        base_url = runtime.get("base_url")
        resolved_provider = runtime.get("provider", "openrouter")
        resolved_api_mode = runtime.get("api_mode", self.api_mode)
        resolved_acp_command = runtime.get("command")
        resolved_acp_args = list(runtime.get("args") or [])
        resolved_credential_pool = runtime.get("credential_pool")
        # A callable api_key is a bearer-token provider (Azure Foundry
        # Entra ID — ``azure_identity_adapter.build_token_provider``).
        # The OpenAI SDK accepts ``Callable[[], str]`` for ``api_key`` and
        # invokes it before every request. Skip the string-only validation
        # and placeholder substitution for callables.
        _is_callable_provider = callable(api_key) and not isinstance(api_key, str)
        if not _is_callable_provider and (not isinstance(api_key, str) or not api_key):
            # Custom / local endpoints (llama.cpp, ollama, vLLM, etc.) often
            # don't require authentication.  When a base_url IS configured but
            # no API key was found, use a placeholder so the OpenAI SDK
            # doesn't reject the request and local servers just ignore it.
            _source = runtime.get("source", "")
            _has_custom_base = isinstance(base_url, str) and base_url and "openrouter.ai" not in base_url
            if _has_custom_base:
                api_key = "no-key-required"
                logger.debug(
                    "No API key for custom endpoint %s (source=%s), "
                    "using placeholder — local servers typically ignore auth",
                    base_url, _source,
                )
            else:
                print("\n⚠️  Provider resolver returned an empty API key. "
                      "Set OPENROUTER_API_KEY or run: hermes setup")
                return False
        if not isinstance(base_url, str) or not base_url:
            print("\n⚠️  Provider resolver returned an empty base URL. "
                  "Check your provider config or run: hermes setup")
            return False

        credentials_changed = api_key != self.api_key or base_url != self.base_url
        routing_changed = (
            resolved_provider != self.provider
            or resolved_api_mode != self.api_mode
            or resolved_acp_command != self.acp_command
            or resolved_acp_args != self.acp_args
        )
        self.provider = resolved_provider
        self.api_mode = resolved_api_mode
        self.acp_command = resolved_acp_command
        self.acp_args = resolved_acp_args
        self._credential_pool = resolved_credential_pool
        self._provider_source = runtime.get("source")
        self.api_key = api_key
        self.base_url = base_url

        # When a custom_provider entry carries an explicit `model` field,
        # use it as the effective model name.  Without this, running
        # `hermes chat --model <provider-name>` sends the provider name
        # (e.g. "my-provider") as the model string to the API instead of
        # the configured model (e.g. "qwen3.6-plus"), causing 400 errors.
        runtime_model = runtime.get("model")
        if runtime_model and isinstance(runtime_model, str):
            # Only use runtime model if: model is unset, or model equals provider name
            should_use_runtime_model = (
                not self.model or  # No model configured yet
                self.model == self.provider or  # Model is the provider slug
                self.model == runtime.get("name")  # Model matches provider display name
            )
            if should_use_runtime_model:
                self.model = runtime_model

        # If model is still empty (e.g. user ran `hermes auth add openai-codex`
        # without `hermes model`), fall back to the provider's first catalog
        # model so the API call doesn't fail with "model must be non-empty".
        if not self.model and resolved_provider:
            try:
                from hades_cli.models import get_default_model_for_provider
                _default = get_default_model_for_provider(resolved_provider)
                if _default:
                    self.model = _default
                    logger.info(
                        "No model configured — defaulting to %s for provider %s",
                        _default, resolved_provider,
                    )
            except Exception:
                pass

        # Normalize model for the resolved provider (e.g. swap non-Codex
        # models when provider is openai-codex).  Fixes #651.
        model_changed = self._normalize_model_for_provider(resolved_provider)

        # AIAgent/OpenAI client holds auth at init time, so rebuild if key,
        # routing, or the effective model changed.
        if (credentials_changed or routing_changed or model_changed) and self.agent is not None:
            self.agent = None
            self._active_agent_route_signature = None

        return True

    def _resolve_turn_agent_config(self, user_message: str) -> dict:
        """Build the effective model/runtime config for a single user turn.

        Always uses the session's primary model/provider.  If the user has
        toggled `/fast` on and the current model supports Priority
        Processing / Anthropic fast mode, attach `request_overrides` so the
        API call is marked accordingly.
        """
        from hades_cli.models import resolve_fast_mode_overrides

        runtime = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "provider": self.provider,
            "api_mode": self.api_mode,
            "command": self.acp_command,
            "args": list(self.acp_args or []),
            "credential_pool": getattr(self, "_credential_pool", None),
        }
        route = {
            "model": self.model,
            "runtime": runtime,
            "signature": (
                self.model,
                runtime["provider"],
                runtime["base_url"],
                runtime["api_mode"],
                runtime["command"],
                tuple(runtime["args"]),
            ),
        }

        service_tier = getattr(self, "service_tier", None)
        if not service_tier:
            route["request_overrides"] = None
            return route

        try:
            overrides = resolve_fast_mode_overrides(route["model"])
        except Exception:
            overrides = None
        route["request_overrides"] = overrides
        return route

    def _runtime_routing_handoff(
        self,
        *,
        initial_task,
        session_id: str,
        task_id: str,
        model_override: str | None = None,
        runtime_override: dict | None = None,
        is_resume: bool = False,
        manual_runtime_pin: bool = False,
        manual_pin_source: str | None = None,
        update_host_state: bool = True,
    ):
        """Prepare and seal the exact constructor runtime for one new agent.

        Policy runs against the requested baseline first.  A projected runtime
        is already executable, so an unavailable baseline must not veto it.
        Inherit/shadow plans retain the canonical Hermes credential resolver
        and are sealed only after it has produced the executable spec.
        """
        from agent.runtime_routing import (
            AgentRuntimeContext,
            AgentRuntimeRequest,
            RUNTIME_ROUTING_CONTRACT_VERSION,
            constructor_runtime_spec,
            finalize_prepared_agent_runtime,
            prepare_agent_runtime_for_construction,
            resolve_ordinary_hermes_runtime,
        )

        requested_runtime = runtime_override or {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "provider": self.provider,
            "api_mode": self.api_mode,
            "command": self.acp_command,
            "args": list(self.acp_args or []),
            "credential_pool": getattr(self, "_credential_pool", None),
        }
        requested_model = self.model if model_override is None else model_override
        baseline = constructor_runtime_spec(
            model=requested_model or "",
            provider=requested_runtime.get("provider"),
            base_url=requested_runtime.get("base_url"),
            api_key=requested_runtime.get("api_key"),
            api_mode=requested_runtime.get("api_mode"),
            acp_command=requested_runtime.get("command"),
            acp_args=requested_runtime.get("args"),
            credential_pool=requested_runtime.get("credential_pool"),
            reasoning_config=self.reasoning_config,
            fallback_model=self._fallback_model,
        )
        context = AgentRuntimeContext(
            scope="fresh_session",
            # Resumes replay durable/manual bindings.  Never classify the next
            # user turn as though it were a brand-new conversation.
            task=None if is_resume else initial_task,
            session_id=session_id,
            task_id=task_id,
            is_resume=is_resume,
            manual_runtime_pin=manual_runtime_pin,
            manual_pin_source=manual_pin_source if manual_runtime_pin else None,
            metadata={"platform": "cli"},
        )
        request = AgentRuntimeRequest(
            contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
            context=context,
            baseline=baseline,
        )
        prepared = prepare_agent_runtime_for_construction(
            request,
            session_store=getattr(self, "_session_db", None),
        )

        if prepared.plan.action == "project":
            effective = prepared.plan.runtime
        else:
            resolution = resolve_ordinary_hermes_runtime(
                baseline,
                owns_fallbacks=prepared.plan.owns_fallbacks,
            )
            effective = resolution.runtime
            # Keep the classic CLI's host state aligned with the sealed
            # ordinary runtime so per-turn credential refresh and /model
            # operate on what the agent actually uses.
            if update_host_state:
                self.model = effective.model
                self.provider = effective.provider
                self.api_key = effective.api_key
                self.base_url = effective.base_url
                self.api_mode = effective.api_mode
                self.acp_command = effective.acp_command
                self.acp_args = list(effective.acp_args)
                self._credential_pool = effective.credential_pool

        finalized = finalize_prepared_agent_runtime(
            prepared,
            request,
            effective,
        )
        return context, finalized, effective

    def _record_manual_runtime_transition(
        self,
        *,
        source: str,
        runtime=None,
    ) -> bool:
        """Persist canonical CLI manual intent and restore host fallbacks."""
        from agent.runtime_routing import (
            apply_manual_runtime_transition,
            constructor_runtime_spec,
            runtime_spec_has_exact_execution_binding,
        )

        # The user's choice is canonical even if credential refresh or durable
        # resolver persistence is temporarily unavailable.  A later agent
        # construction will carry this manual intent and retry the record.
        self._runtime_manual_pin = True
        self._runtime_manual_pin_source = source
        agent = getattr(self, "agent", None)
        if runtime is None:
            runtime_owner = agent if agent is not None else self
            runtime = constructor_runtime_spec(
                model=getattr(runtime_owner, "model", "") or "",
                provider=getattr(runtime_owner, "provider", None),
                base_url=getattr(runtime_owner, "base_url", None),
                api_key=getattr(runtime_owner, "api_key", None),
                api_mode=getattr(runtime_owner, "api_mode", None),
                acp_command=getattr(runtime_owner, "acp_command", None),
                acp_args=getattr(runtime_owner, "acp_args", None),
                credential_pool=getattr(runtime_owner, "_credential_pool", None),
                reasoning_config=getattr(
                    runtime_owner,
                    "reasoning_config",
                    getattr(self, "reasoning_config", None),
                ),
                fallback_model=self._fallback_model,
            )
            if (
                not runtime_spec_has_exact_execution_binding(runtime)
                and agent is None
            ):
                if not self._ensure_runtime_credentials():
                    return False
                runtime = constructor_runtime_spec(
                    model=self.model or "",
                    provider=self.provider,
                    base_url=self.base_url,
                    api_key=self.api_key,
                    api_mode=self.api_mode,
                    acp_command=self.acp_command,
                    acp_args=list(self.acp_args or []),
                    credential_pool=getattr(self, "_credential_pool", None),
                    reasoning_config=self.reasoning_config,
                    fallback_model=self._fallback_model,
                )

        apply_manual_runtime_transition(
            agent,
            session_id=self.session_id,
            source=source,
            runtime=runtime,
            fallback_model=self._fallback_model,
        )
        return True

    def _restore_invocation_runtime_baseline(self) -> None:
        """Drop session-local runtime intent at a new conversation boundary."""
        baseline = getattr(self, "_initial_runtime_baseline", None)
        if isinstance(baseline, dict):
            for field, value in baseline.items():
                if field in {"acp_args"}:
                    value = list(value or [])
                elif field == "reasoning_config" and isinstance(value, dict):
                    value = dict(value)
                setattr(self, field, value)

        initial_manual_pin = getattr(self, "_initial_runtime_manual_pin", False)
        if not isinstance(initial_manual_pin, bool):
            initial_manual_pin = False
        self._runtime_manual_pin = initial_manual_pin
        self._runtime_manual_pin_source = (
            "cli_explicit_runtime" if initial_manual_pin else None
        )
        # This note describes a switch inside the parent transcript.  Carrying
        # it to the child's first prompt would both leak state and falsely tell
        # the newly selected model that it had just been switched in place.
        self._pending_model_switch_note = None

    def _promote_persisted_runtime_baseline(self) -> bool:
        """Adopt a saved /model choice as this invocation's config baseline.

        A fresh process would read the newly persisted model/provider from
        config.yaml. Keep long-running classic CLI processes equivalent by
        refreshing the baseline used by /new and /branch after a successful
        global save. Explicit launch-time model/provider arguments remain
        authoritative for the whole invocation and therefore never promote.
        """
        if bool(getattr(self, "_initial_runtime_manual_pin", False)):
            return False

        fields = (
            "model",
            "provider",
            "requested_provider",
            "api_key",
            "base_url",
            "api_mode",
            "acp_command",
            "acp_args",
            "_credential_pool",
            "_explicit_api_key",
            "_explicit_base_url",
            "_provider_source",
            "reasoning_config",
        )
        baseline = {}
        for field in fields:
            value = getattr(self, field, None)
            if field == "acp_args":
                value = list(value or [])
            elif field == "reasoning_config" and isinstance(value, dict):
                value = dict(value)
            baseline[field] = value
        self._initial_runtime_baseline = baseline
        return True

    def _restore_session_runtime(self, session_meta: dict | None) -> bool:
        """Restore a session's non-secret runtime identity before rebuilding."""
        if not isinstance(session_meta, dict):
            return False

        raw_config = session_meta.get("model_config")
        model_config = {}
        if isinstance(raw_config, dict):
            model_config = dict(raw_config)
        elif isinstance(raw_config, str) and raw_config.strip():
            try:
                parsed = json.loads(raw_config)
                if isinstance(parsed, dict):
                    model_config = parsed
            except Exception:
                model_config = {}

        from agent.runtime_routing import (
            constructor_runtime_spec,
            session_runtime_metadata,
        )

        model = str(session_meta.get("model") or model_config.get("model") or "")
        provider = str(model_config.get("provider") or "")
        base_url = str(model_config.get("base_url") or "")
        api_mode = str(model_config.get("api_mode") or "")
        reasoning_config = model_config.get("reasoning_config")
        runtime_kwargs = {
            "model": model,
            "provider": provider,
            "base_url": base_url,
            "api_key": None,
            "api_mode": api_mode,
            "acp_command": None,
            "acp_args": (),
            "credential_pool": None,
            "reasoning_config": (
                reasoning_config if isinstance(reasoning_config, dict) else None
            ),
            "fallback_model": (),
        }
        try:
            runtime = constructor_runtime_spec(**runtime_kwargs)
        except Exception:
            # Corrupt optional reasoning metadata must not make an otherwise
            # valid model/provider identity unresumable.
            runtime_kwargs["reasoning_config"] = None
            runtime = constructor_runtime_spec(**runtime_kwargs)
        metadata = session_runtime_metadata(
            runtime,
            manual_pin_source=model_config.get("runtime_manual_pin_source"),
        )
        if not any(
            key in metadata
            for key in ("model", "provider", "base_url", "api_mode")
        ):
            return False

        if "model" in metadata:
            self.model = metadata["model"]
        restored_provider = metadata.get("provider") or None
        restored_base_url = metadata.get("base_url") or None
        self.provider = restored_provider
        self.requested_provider = restored_provider
        self.base_url = restored_base_url
        self._explicit_base_url = restored_base_url
        self.api_mode = metadata.get("api_mode") or None
        self.reasoning_config = metadata.get("reasoning_config")

        # Credentials and provider clients are deliberately not stored with a
        # session. Force the normal provider resolver to reacquire them from
        # the current profile when the replacement agent is constructed.
        self.api_key = None
        self._explicit_api_key = None
        self._credential_pool = None
        self._provider_source = None
        self.acp_command = None
        self.acp_args = []

        manual_source = metadata.get("runtime_manual_pin_source")
        self._runtime_manual_pin = bool(manual_source)
        self._runtime_manual_pin_source = manual_source
        return True

    def _repair_runtime_routing_continuations(self) -> None:
        """Replay durable compression ancestry before resolving a resume."""
        if not getattr(self, "_resumed", False) or self._session_db is None:
            return
        from cli import logger
        from agent.runtime_routing import (
            repair_runtime_session_continuations_from_store,
        )

        try:
            repair_runtime_session_continuations_from_store(
                self.session_id,
                session_store=self._session_db,
            )
        except Exception:
            # The durable SessionDB edge remains the source of truth.  A later
            # resume can replay it again once the resolver is healthy.
            logger.warning(
                "Runtime routing continuation repair failed for resumed session",
                exc_info=True,
            )

    def _active_agent_has_projected_runtime(self) -> bool:
        binding = getattr(
            getattr(self, "agent", None), "_runtime_routing_binding", None
        )
        return getattr(binding, "action", None) == "project"

    def _runtime_routing_is_resume(self) -> bool:
        """Classify routing lifecycle independently from transcript restore.

        A branch starts with copied messages, so ``_resumed`` alone cannot
        distinguish its first child prompt from a real continuation. The
        in-process marker is authoritative while the CLI is alive; after a
        restart, the durable branch boundary and session message count provide
        the same answer. Unreadable metadata fails conservatively as a resume.
        """
        if not bool(getattr(self, "_resumed", False)):
            return False

        pending = getattr(self, "_runtime_branch_pending_fresh", None)
        if pending is not None:
            return not bool(pending)

        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "session_id", None)
        if session_db is None or not session_id:
            return True
        try:
            row = session_db.get_session(session_id)
            if not isinstance(row, dict):
                return True
            raw_config = row.get("model_config")
            if isinstance(raw_config, dict):
                model_config = raw_config
            elif isinstance(raw_config, str) and raw_config.strip():
                parsed = json.loads(raw_config)
                if not isinstance(parsed, dict):
                    return True
                model_config = parsed
            else:
                return True
            if not model_config.get("_branched_from"):
                return True
            boundary = model_config.get("_branch_point_message_count")
            if isinstance(boundary, bool) or not isinstance(boundary, int):
                return True
            message_count = row.get("message_count")
            if isinstance(message_count, bool) or not isinstance(message_count, int):
                return True
            if message_count > boundary:
                return True
            # The routing handoff persists credential-free selected runtime
            # metadata before the provider call. Treat that as lifecycle
            # evidence so a failed first call cannot be reclassified after a
            # process restart while the copied-message count still equals the
            # branch boundary.
            return any(
                model_config.get(key)
                for key in (
                    "provider",
                    "base_url",
                    "api_mode",
                    "runtime_manual_pin_source",
                )
            )
        except Exception:
            return True

    def _sync_precreated_session_runtime_metadata(
        self,
        runtime,
        *,
        manual_pin_source: str | None = None,
    ) -> None:
        """Merge a sealed credential-free runtime into an existing CLI row."""
        if runtime is None:
            return
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "session_id", None)
        if session_db is None or not session_id:
            return
        try:
            from agent.runtime_routing import session_runtime_metadata

            metadata = session_runtime_metadata(
                runtime,
                manual_pin_source=manual_pin_source,
            )
            row = session_db.get_session(session_id)
            if not isinstance(row, dict):
                return
            raw_config = row.get("model_config")
            if isinstance(raw_config, dict):
                persisted = dict(raw_config)
            elif isinstance(raw_config, str) and raw_config.strip():
                parsed = json.loads(raw_config)
                if not isinstance(parsed, dict):
                    return
                persisted = dict(parsed)
            else:
                persisted = {}
            persisted.update(metadata)
            session_db.update_session_meta(
                session_id,
                json.dumps(persisted),
                model=metadata.get("model"),
            )
        except Exception:
            # The plugin's durable route binding remains authoritative. A
            # metadata mirror failure must not block the selected runtime.
            return

    def _init_agent(
        self,
        *,
        model_override: str = None,
        runtime_override: dict = None,
        request_overrides: dict | None = None,
        initial_task=None,
        runtime_routing_context=None,
        prepared_agent_runtime=None,
    ) -> bool:
        """
        Initialize the agent on first use.
        When resuming a session, restores conversation history from SQLite.
        
        Returns:
            bool: True if successful, False otherwise
        """
        from cli import AIAgent, ChatConsole, _DIM, _RST, _accent_hex, _cprint, _prepare_deferred_agent_startup, logger
        if self.agent is not None:
            return True

        _prepare_deferred_agent_startup()
        self._install_tool_callbacks()
        self._ensure_tirith_security()

        # Resume identity must be restored before credential resolution or
        # policy handoff. Otherwise the source session's provider/client can
        # be used to reconstruct the target conversation.
        if self._session_db is None:
            try:
                from hades_state import SessionDB

                self._session_db = SessionDB()
            except Exception as e:
                logger.warning(
                    "SQLite session store not available — session will NOT be indexed: %s",
                    e,
                )
        resume_session_meta = None
        if self._resumed and self._session_db:
            try:
                resume_session_meta = self._session_db.get_session(self.session_id)
            except Exception:
                logger.warning(
                    "Could not read resumed session runtime metadata",
                    exc_info=True,
                )
            if resume_session_meta:
                self._restore_session_runtime(resume_session_meta)

        from agent.runtime_routing import (
            RuntimeRoutingDeferred,
            runtime_resolver_requires_initial_task,
        )

        routing_enabled = bool(
            runtime_routing_context is not None
            or prepared_agent_runtime is not None
            or runtime_resolver_requires_initial_task("fresh_session")
        )
        if not routing_enabled and not self._ensure_runtime_credentials():
            # Preserve the ordinary CLI path when no resolver is installed.
            return False

        from hades_cli.mcp_startup import wait_for_mcp_discovery

        wait_for_mcp_discovery()
        
        # If resuming, validate the session exists and load its history.
        # _preload_resumed_session() may have already loaded it (called from
        # run() for immediate display).  In that case, conversation_history
        # is non-empty and we skip the DB round-trip.
        if self._resumed and self._session_db and not self.conversation_history:
            session_meta = resume_session_meta or self._session_db.get_session(
                self.session_id
            )
            # In quiet mode (`hermes chat -Q` / --quiet, surfaced via
            # tool_progress_mode == "off"), resume status lines go to stderr
            # so stdout stays machine-readable for automation wrappers that
            # do `$(hermes chat -Q --resume <id> -q "...")`. Without this,
            # the resume banner pollutes captured stdout. See #11793.
            _quiet_mode = getattr(self, "tool_progress_mode", "full") == "off"
            if not session_meta:
                if _quiet_mode:
                    print(f"Session not found: {self.session_id}", file=sys.stderr)
                    print(
                        "Use a session ID from a previous CLI run (hermes sessions list).",
                        file=sys.stderr,
                    )
                else:
                    _cprint(f"\033[1;31mSession not found: {self.session_id}{_RST}")
                    _cprint(f"{_DIM}Use a session ID from a previous CLI run (hermes sessions list).{_RST}")
                return False
            # If the requested session is the (empty) head of a compression
            # chain, walk to the descendant that actually holds the messages.
            # See #15000 and SessionDB.resolve_resume_session_id.
            try:
                resolved_id = self._session_db.resolve_resume_session_id(self.session_id)
            except Exception:
                resolved_id = self.session_id
            if resolved_id and resolved_id != self.session_id:
                ChatConsole().print(
                    f"[dim]Session {_escape(self.session_id)} was compressed into "
                    f"{_escape(resolved_id)}; resuming the descendant with your "
                    f"transcript.[/dim]"
                )
                self.session_id = resolved_id
                resolved_meta = self._session_db.get_session(self.session_id)
                if resolved_meta:
                    session_meta = resolved_meta
                    self._restore_session_runtime(session_meta)
            restored = self._session_db.get_messages_as_conversation(self.session_id)
            if restored:
                restored = [m for m in restored if m.get("role") != "session_meta"]
                self.conversation_history = restored
                msg_count = len([m for m in restored if m.get("role") == "user"])
                title_part = ""
                if session_meta.get("title"):
                    title_part = f" \"{session_meta['title']}\""
                if _quiet_mode:
                    print(
                        f"↻ Resumed session {self.session_id}{title_part} "
                        f"({msg_count} user message{'s' if msg_count != 1 else ''}, "
                        f"{len(restored)} total messages)",
                        file=sys.stderr,
                    )
                else:
                    ChatConsole().print(
                        f"[bold {_accent_hex()}]↻ Resumed session[/] "
                        f"[bold]{_escape(self.session_id)}[/]"
                        f"[bold {_accent_hex()}]{_escape(title_part)}[/] "
                        f"({msg_count} user message{'s' if msg_count != 1 else ''}, {len(restored)} total messages)"
                    )
                self._restore_session_cwd(session_meta, quiet=_quiet_mode)
            else:
                if _quiet_mode:
                    print(
                        f"Session {self.session_id} found but has no messages. Starting fresh.",
                        file=sys.stderr,
                    )
                else:
                    ChatConsole().print(
                        f"[bold {_accent_hex()}]Session {_escape(self.session_id)} found but has no messages. Starting fresh.[/]"
                    )
            # Re-open the session (clear ended_at so it's active again)
            try:
                self._session_db._conn.execute(
                    "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                    (self.session_id,),
                )
                self._session_db._conn.commit()
            except Exception:
                pass

        # A crash can happen after SessionDB commits the compression child but
        # before the routing resolver records its alias.  Repair that durable
        # lineage before asking policy to resolve this resumed session.
        if routing_enabled:
            self._repair_runtime_routing_continuations()
        
        try:
            requested_runtime = runtime_override or {
                "api_key": self.api_key,
                "base_url": self.base_url,
                "provider": self.provider,
                "api_mode": self.api_mode,
                "command": self.acp_command,
                "args": list(self.acp_args or []),
                "credential_pool": getattr(self, "_credential_pool", None),
            }
            requested_model = self.model if model_override is None else model_override
            if not routing_enabled:
                # ``runtime_override`` was computed by chat() before this
                # method performed the ordinary lazy credential resolution.
                # Rebuild from the now-resolved host state, matching the old
                # pre-routing call order and constructor values.
                requested_runtime = {
                    "api_key": self.api_key,
                    "base_url": self.base_url,
                    "provider": self.provider,
                    "api_mode": self.api_mode,
                    "command": self.acp_command,
                    "args": list(self.acp_args or []),
                    "credential_pool": getattr(self, "_credential_pool", None),
                }
                requested_model = self.model
            requested_signature = (
                requested_model,
                requested_runtime.get("provider"),
                requested_runtime.get("base_url"),
                requested_runtime.get("api_mode"),
                requested_runtime.get("command"),
                tuple(requested_runtime.get("args") or ()),
            )

            effective_runtime = None
            if (
                routing_enabled
                and runtime_routing_context is None
                and prepared_agent_runtime is None
            ):
                manual_pin = bool(getattr(self, "_runtime_manual_pin", False))
                manual_source = getattr(
                    self,
                    "_runtime_manual_pin_source",
                    "cli_explicit_runtime" if manual_pin else None,
                )
                handoff = self._runtime_routing_handoff(
                    initial_task=initial_task,
                    session_id=self.session_id,
                    task_id=self.session_id,
                    model_override=requested_model,
                    runtime_override=requested_runtime,
                    is_resume=self._runtime_routing_is_resume(),
                    manual_runtime_pin=manual_pin,
                    manual_pin_source=manual_source,
                )
                if handoff is None:
                    return False
                (
                    runtime_routing_context,
                    prepared_agent_runtime,
                    effective_runtime,
                ) = handoff
                runtime = {
                    "api_key": effective_runtime.api_key,
                    "base_url": effective_runtime.base_url,
                    "provider": effective_runtime.provider,
                    "api_mode": effective_runtime.api_mode,
                    "command": effective_runtime.acp_command,
                    "args": list(effective_runtime.acp_args),
                    "credential_pool": effective_runtime.credential_pool,
                }
                effective_model = effective_runtime.model
                effective_reasoning_config = (
                    dict(effective_runtime.reasoning_config)
                    if effective_runtime.reasoning_config is not None
                    else None
                )
                effective_fallback_model = [
                    dict(item) for item in effective_runtime.fallback_model
                ]
                if prepared_agent_runtime.plan.action != "project":
                    # Inherit/shadow canonicalize the host state during the
                    # two-phase resolution.  Compare later turns with that
                    # resolved host identity so turn two does not rebuild and
                    # ask policy about the same session again.
                    requested_signature = (
                        effective_runtime.model,
                        effective_runtime.provider,
                        effective_runtime.base_url,
                        effective_runtime.api_mode,
                        effective_runtime.acp_command,
                        tuple(effective_runtime.acp_args),
                    )
            else:
                runtime = requested_runtime
                effective_model = requested_model
                effective_reasoning_config = self.reasoning_config
                effective_fallback_model = self._fallback_model

            self.agent = AIAgent(
                model=effective_model,
                api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"),
                provider=runtime.get("provider"),
                api_mode=runtime.get("api_mode"),
                acp_command=runtime.get("command"),
                acp_args=runtime.get("args"),
                credential_pool=runtime.get("credential_pool"),
                max_tokens=self.max_tokens,
                max_iterations=self.max_turns,
                enabled_toolsets=self.enabled_toolsets,
                disabled_toolsets=self.disabled_toolsets,
                verbose_logging=self.verbose,
                quiet_mode=not self.verbose,
                tool_progress_mode=getattr(self, "tool_progress_mode", "all"),
                ephemeral_system_prompt=self.system_prompt if self.system_prompt else None,
                prefill_messages=self.prefill_messages or None,
                reasoning_config=effective_reasoning_config,
                service_tier=self.service_tier,
                request_overrides=request_overrides,
                providers_allowed=self._providers_only,
                providers_ignored=self._providers_ignore,
                providers_order=self._providers_order,
                provider_sort=self._provider_sort,
                provider_require_parameters=self._provider_require_params,
                provider_data_collection=self._provider_data_collection,
                openrouter_min_coding_score=self._openrouter_min_coding_score,
                session_id=self.session_id,
                platform="cli",
                session_db=self._session_db,
                clarify_callback=self._clarify_callback,
                reasoning_callback=self._current_reasoning_callback(),

                fallback_model=effective_fallback_model,
                runtime_routing_context=runtime_routing_context,
                prepared_agent_runtime=prepared_agent_runtime,
                thinking_callback=self._on_thinking,
                checkpoints_enabled=self.checkpoints_enabled,
                checkpoint_max_snapshots=self.checkpoint_max_snapshots,
                checkpoint_max_total_size_mb=self.checkpoint_max_total_size_mb,
                checkpoint_max_file_size_mb=self.checkpoint_max_file_size_mb,
                pass_session_id=self.pass_session_id,
                skip_context_files=self.ignore_rules,
                skip_memory=self.ignore_rules,
                tool_progress_callback=self._on_tool_progress,
                tool_start_callback=self._on_tool_start if self._inline_diffs_enabled else None,
                tool_complete_callback=self._on_tool_complete if self._inline_diffs_enabled else None,
                stream_delta_callback=self._stream_delta if self.streaming_enabled else None,
                tool_gen_callback=self._on_tool_gen_start if self.streaming_enabled else None,
                notice_callback=self._on_notice,
                notice_clear_callback=self._on_notice_clear,
                reaction_callback=self._on_reaction,
            )
            self._sync_precreated_session_runtime_metadata(
                effective_runtime,
                manual_pin_source=getattr(
                    runtime_routing_context, "manual_pin_source", None
                ),
            )
            # Any later rebuild in this process is a continuation. The fresh
            # branch decision (and its durable binding) has now been sealed
            # into the child agent, even if the provider call later fails.
            if getattr(self, "_runtime_branch_pending_fresh", None) is True:
                self._runtime_branch_pending_fresh = False
            # Store reference for atexit memory provider shutdown.
            # NOTE: this MUST write to the ``cli`` module's global, not a
            # local module global. ``_run_cleanup`` (in cli.py) reads
            # ``cli._active_agent_ref`` to decide whether to fire the memory
            # provider's ``on_session_end`` hook. When this code lived in
            # cli.py a bare ``global _active_agent_ref`` worked; after the
            # god-file extraction into this mixin a ``global`` here would bind
            # *this module's* namespace, leaving ``cli._active_agent_ref`` None
            # forever — so memory shutdown never ran on /exit (#49287).
            import cli as _cli
            _cli._active_agent_ref = self.agent
            # Route agent status output through prompt_toolkit so ANSI escape
            # sequences aren't garbled by patch_stdout's StdoutProxy (#2262).
            self.agent._print_fn = _cprint
            # Hydrate credits notices at session OPEN (parity with the TUI), so a
            # depletion / usage-band warning shows before the first message. The
            # notice_callback is bound above → _on_notice renders the line. Idempotent
            # + fail-open inside the helper; harmless for non-Nous providers.
            try:
                from agent.credits_tracker import seed_credits_at_session_start

                seed_credits_at_session_start(self.agent)
            except Exception:
                pass
            # Compare subsequent turns against the requested baseline, not a
            # projected runtime.  Otherwise turn two would tear down the routed
            # agent and classify the same session again.
            self._active_agent_route_signature = requested_signature

            if (
                effective_runtime is not None
                and getattr(runtime_routing_context, "manual_runtime_pin", False)
            ):
                try:
                    self._record_manual_runtime_transition(
                        source=runtime_routing_context.manual_pin_source
                        or "cli_explicit_runtime",
                        runtime=effective_runtime,
                    )
                except Exception:
                    # The explicit selection already took effect.  Keep it live
                    # and let a later transition/resume retry persistence.
                    logger.warning(
                        "Could not persist explicit CLI runtime pin",
                        exc_info=True,
                    )

            # Force-create DB row on /title intent, then apply title.
            if self._pending_title and self._session_db and self.agent:
                try:
                    self.agent._ensure_db_session()
                    if self.agent._session_db_created:
                        self._session_db.set_session_title(self.session_id, self._pending_title)
                        _cprint(f"  Session title applied: {self._pending_title}")
                        self._pending_title = None
                    # else: row creation failed transiently — keep _pending_title for retry
                except (ValueError, Exception) as e:
                    _cprint(f"  Could not apply pending title: {e}")
                    # Keep _pending_title so it can be retried after row creation succeeds
            return True
        except RuntimeRoutingDeferred:
            ChatConsole().print(
                "[bold yellow]Runtime routing is busy; retry this turn shortly.[/]"
            )
            return False
        except Exception as e:
            ChatConsole().print(f"[bold red]Failed to initialize agent: {e}[/]")
            return False

    def _preload_resumed_session(self) -> bool:
        """Load a resumed session's history from the DB early (before first chat).

        Called from run() so the conversation history is available for display
        before the user sends their first message.  Sets
        ``self.conversation_history`` and prints the one-liner status.  Returns
        True if history was loaded, False otherwise.

        The corresponding block in ``_init_agent()`` checks whether history is
        already populated and skips the DB round-trip.
        """
        from cli import _accent_hex
        if not self._resumed or not self._session_db:
            return False

        session_meta = self._session_db.get_session(self.session_id)
        if not session_meta:
            self._console_print(
                f"[bold red]Session not found: {self.session_id}[/]"
            )
            self._console_print(
                "[dim]Use a session ID from a previous CLI run "
                "(hermes sessions list).[/]"
            )
            return False

        # If the requested session is the (empty) head of a compression chain,
        # walk to the descendant that actually holds the messages. See #15000.
        try:
            resolved_id = self._session_db.resolve_resume_session_id(self.session_id)
        except Exception:
            resolved_id = self.session_id
        if resolved_id and resolved_id != self.session_id:
            self._console_print(
                f"[dim]Session {self.session_id} was compressed into "
                f"{resolved_id}; resuming the descendant with your transcript.[/]"
            )
            self.session_id = resolved_id
            resolved_meta = self._session_db.get_session(self.session_id)
            if resolved_meta:
                session_meta = resolved_meta

        restored = self._session_db.get_messages_as_conversation(self.session_id)
        if restored:
            restored = [m for m in restored if m.get("role") != "session_meta"]
            self.conversation_history = restored
            msg_count = len([m for m in restored if m.get("role") == "user"])
            title_part = ""
            if session_meta.get("title"):
                title_part = f' "{session_meta["title"]}"'
            accent_color = _accent_hex()
            self._console_print(
                f"[{accent_color}]↻ Resumed session [bold]{self.session_id}[/bold]"
                f"{title_part} "
                f"({msg_count} user message{'s' if msg_count != 1 else ''}, "
                f"{len(restored)} total messages)[/]"
            )
            self._restore_session_cwd(session_meta)
        else:
            accent_color = _accent_hex()
            self._console_print(
                f"[{accent_color}]Session {self.session_id} found but has no "
                f"messages. Starting fresh.[/]"
            )
            return False

        # Re-open the session (clear ended_at so it's active again)
        try:
            self._session_db._conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL "
                "WHERE id = ?",
                (self.session_id,),
            )
            self._session_db._conn.commit()
        except Exception:
            pass

        return True

    def _display_resumed_history(self):
        """Render a compact recap of previous conversation messages.

        Uses Rich markup with dim/muted styling so the recap is visually
        distinct from the active conversation.  Caps the display at the
        last ``MAX_DISPLAY_EXCHANGES`` user/assistant exchanges and shows
        an indicator for earlier hidden messages.
        """
        from cli import CLI_CONFIG, _record_output_history_entry, _strip_reasoning_tags, _suspend_output_history
        if not self.conversation_history:
            return

        # Check config: resume_display setting
        if self.resume_display == "minimal":
            return

        # Read limits from config (with hardcoded defaults)
        _disp = CLI_CONFIG.get("display", {})
        MAX_DISPLAY_EXCHANGES = int(_disp.get("resume_exchanges", 10))
        MAX_USER_LEN = int(_disp.get("resume_max_user_chars", 300))
        MAX_ASST_LEN = int(_disp.get("resume_max_assistant_chars", 200))
        MAX_ASST_LINES = int(_disp.get("resume_max_assistant_lines", 3))
        SKIP_TOOL_ONLY = _disp.get("resume_skip_tool_only", True)

        # Collect displayable entries (skip system, tool-result messages)
        entries = []  # list of (role, display_text)
        _last_asst_idx = None       # index of last assistant entry
        _last_asst_full = None      # un-truncated display text for last assistant
        for msg in self.conversation_history:
            role = msg.get("role", "")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []

            if role == "system":
                continue
            if role == "tool":
                continue

            if role == "user":
                text = "" if content is None else str(content)
                # Handle multimodal content (list of dicts)
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                        elif isinstance(part, dict) and part.get("type") == "image_url":
                            parts.append("[image]")
                    text = " ".join(parts)
                if len(text) > MAX_USER_LEN:
                    text = text[:MAX_USER_LEN] + "..."
                entries.append(("user", text))

            elif role == "assistant":
                text = "" if content is None else str(content)
                text = _strip_reasoning_tags(text)
                parts = []
                full_parts = []  # un-truncated version
                if text:
                    full_parts.append(text)
                    lines = text.splitlines()
                    if len(lines) > MAX_ASST_LINES:
                        text = "\n".join(lines[:MAX_ASST_LINES]) + " ..."
                    if len(text) > MAX_ASST_LEN:
                        text = text[:MAX_ASST_LEN] + "..."
                    parts.append(text)
                if tool_calls:
                    tc_count = len(tool_calls)
                    # Extract tool names
                    names = []
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        name = fn.get("name", "unknown") if isinstance(fn, dict) else "unknown"
                        if name not in names:
                            names.append(name)
                    names_str = ", ".join(names[:4])
                    if len(names) > 4:
                        names_str += ", ..."
                    noun = "call" if tc_count == 1 else "calls"
                    tc_summary = f"[{tc_count} tool {noun}: {names_str}]"
                    parts.append(tc_summary)
                    full_parts.append(tc_summary)
                if not parts:
                    # Skip pure-reasoning messages that have no visible output
                    continue
                # Skip tool-call-only entries when SKIP_TOOL_ONLY is enabled
                has_text = bool(text)
                if SKIP_TOOL_ONLY and not has_text and tool_calls:
                    continue
                entries.append(("assistant", " ".join(parts)))
                _last_asst_idx = len(entries) - 1
                _last_asst_full = " ".join(full_parts)

        if not entries:
            return

        # Determine if we need to truncate
        skipped = 0
        if len(entries) > MAX_DISPLAY_EXCHANGES * 2:
            skipped = len(entries) - MAX_DISPLAY_EXCHANGES * 2
            entries = entries[skipped:]

        # Replace last assistant entry with full (un-truncated) text
        # so the user can see where they left off without wasting tokens.
        if _last_asst_idx is not None and _last_asst_full:
            adj_idx = _last_asst_idx - skipped
            if 0 <= adj_idx < len(entries):
                entries[adj_idx] = ("assistant_last", _last_asst_full)

        # Build the display using Rich
        from rich.panel import Panel
        from rich.text import Text

        try:
            from hades_cli.skin_engine import get_active_skin
            _skin = get_active_skin()
            _history_text_c = _skin.get_color("banner_text", "#FFF8DC")
            _session_label_c = _skin.get_color("session_label", "#DAA520")
            _session_border_c = _skin.get_color("session_border", "#8B8682")
            _assistant_label_c = _skin.get_color("ui_ok", "#8FBC8F")
        except Exception:
            _history_text_c = "#FFF8DC"
            _session_label_c = "#DAA520"
            _session_border_c = "#8B8682"
            _assistant_label_c = "#8FBC8F"

        lines = Text()
        if skipped:
            lines.append(
                f"  ... {skipped} earlier messages ...\n\n",
                style="dim italic",
            )

        for i, (role, text) in enumerate(entries):
            if role == "user":
                lines.append("  ● You: ", style=f"dim bold {_session_label_c}")
                # Show first line inline, indent rest
                msg_lines = text.splitlines()
                lines.append(msg_lines[0] + "\n", style="dim")
                for ml in msg_lines[1:]:
                    lines.append(f"         {ml}\n", style="dim")
            elif role == "assistant_last":
                # Last assistant response shown in full, non-dim
                lines.append("  ◆ Hermes: ", style=f"bold {_assistant_label_c}")
                msg_lines = text.splitlines()
                lines.append(msg_lines[0] + "\n", style="")
                for ml in msg_lines[1:]:
                    lines.append(f"            {ml}\n", style="")
            else:
                lines.append("  ◆ Hermes: ", style=f"dim bold {_assistant_label_c}")
                msg_lines = text.splitlines()
                lines.append(msg_lines[0] + "\n", style="dim")
                for ml in msg_lines[1:]:
                    lines.append(f"            {ml}\n", style="dim")
            if i < len(entries) - 1:
                lines.append("")  # small gap

        panel = Panel(
            lines,
            title=f"[dim {_session_label_c}]Previous Conversation[/]",
            border_style=f"dim {_session_border_c}",
            padding=(0, 1),
            style=_history_text_c,
        )
        _record_output_history_entry(lambda: self._render_resume_history_panel_lines(panel))
        with _suspend_output_history():
            self._console_print(panel)
