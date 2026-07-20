"""Regression test for #48879.

When a turn is interrupted via ``/stop`` right after a tool completes — but
before the assistant streams any final text — the transcript tail is a raw
``tool`` message. Persisting that tail unmodified means the next user message
lands as ``... tool → user``, a role-alternation violation that strict
providers (Gemini, Claude) react to by hallucinating a continuation of the
user's message before transitioning into the assistant persona.

``finalize_turn`` closes the tool-call sequence on interrupt by appending a
synthetic ``assistant`` message before persistence. ``final_response`` is
typically empty on an interrupt, so the placeholder text is used rather than
an empty-content assistant turn.
"""

import pytest

from agent.turn_finalizer import finalize_turn


class _StubBudget:
    used = 1
    max_total = 90
    remaining = 89


class _StubCompressor:
    last_prompt_tokens = 0


class _StubAgent:
    """Minimal agent surface that ``finalize_turn`` reads from."""

    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = _StubBudget()
        self.context_compressor = _StubCompressor()
        self.model = "stub/model"
        self.provider = "stub"
        self.base_url = "http://stub"
        self.session_id = "sess-1"
        self.quiet_mode = True
        self.platform = "cli"
        self._interrupt_requested = False
        self._interrupt_message = None
        self._tool_guardrail_halt_decision = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        # The invariants test exercises capture-mode issuance explicitly;
        # the shipped config default is receipts.mode: off (Task 7).
        self._receipts_mode = "capture"
        for attr in (
            "session_input_tokens",
            "session_output_tokens",
            "session_cache_read_tokens",
            "session_cache_write_tokens",
            "session_reasoning_tokens",
            "session_prompt_tokens",
            "session_completion_tokens",
            "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, attr, 0)
        self.session_cost_status = "ok"
        self.session_cost_source = "stub"
        self.persisted_messages = None

    # --- fallible cleanup surfaces (all succeed here) ------------------
    def _save_trajectory(self, *a, **k):
        pass

    def _cleanup_task_resources(self, *a, **k):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        # A clean interrupt sets no empty-response scaffolding flags, so
        # the real method returns early and leaves the tool tail in place.
        # Model that here as a no-op.
        pass

    def _persist_session(self, messages, conversation_history):
        # Snapshot the role sequence at the moment of persistence.
        self.persisted_messages = [dict(m) for m in messages]

    # --- harmless no-ops ------------------------------------------------
    def _emit_status(self, *a, **k):
        pass

    def _safe_print(self, *a, **k):
        pass

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **k):
        pass


def _interrupted_tool_tail():
    """A transcript interrupted after a successful tool, before any
    assistant text — the exact #48879 shape."""
    return [
        {"role": "user", "content": "edit the file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "patch", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok edited"},
    ]


def _finalize(agent, messages, *, interrupted, final_response=None):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=1,
        interrupted=interrupted,
        failed=False,
        messages=messages,
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="edit the file",
        original_user_message="edit the file",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
    )


def _assert_no_tool_then_user(messages):
    for i in range(len(messages) - 1):
        if messages[i].get("role") == "tool":
            assert messages[i + 1].get("role") != "user", (
                f"role-alternation violation: tool → user at index {i}"
            )


def test_interrupt_after_tool_closes_sequence_with_placeholder():
    agent = _StubAgent()
    messages = _interrupted_tool_tail()
    _finalize(agent, messages, interrupted=True, final_response=None)

    # Tail must now be an assistant message, not a raw tool result.
    assert messages[-1]["role"] == "assistant"
    # Empty final_response falls back to the explicit placeholder rather
    # than persisting an empty-content assistant turn.
    assert messages[-1]["content"] == "Operation interrupted."

    # The persisted snapshot is alternation-safe: appending a new user
    # message would follow an assistant, not an orphan tool.
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1]["role"] == "assistant"
    follow_on = agent.persisted_messages + [{"role": "user", "content": "forget it"}]
    _assert_no_tool_then_user(follow_on)


def test_interrupt_after_tool_keeps_delivered_text_when_present():
    agent = _StubAgent()
    messages = _interrupted_tool_tail()
    _finalize(agent, messages, interrupted=True, final_response="Partial answer so far")

    assert messages[-1]["role"] == "assistant"
    # Real delivered text is preserved, not clobbered by the placeholder.
    assert messages[-1]["content"] == "Partial answer so far"


def test_non_interrupted_tool_tail_is_left_untouched():
    # A turn that ends on a tool tail WITHOUT an interrupt (mid-progress
    # tool loop) must not get a synthetic close — that is normal dialog
    # state handled elsewhere.
    agent = _StubAgent()
    messages = _interrupted_tool_tail()
    _finalize(agent, messages, interrupted=False, final_response=None)
    assert messages[-1]["role"] == "tool"


def test_interrupt_without_tool_tail_adds_nothing():
    # Interrupt while the tail is already an assistant/user message: no
    # synthetic close needed.
    agent = _StubAgent()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "partial reply"},
    ]
    before = len(messages)
    _finalize(agent, messages, interrupted=True, final_response="partial reply")
    assert len(messages) == before
    assert messages[-1]["role"] == "assistant"


def test_multi_turn_receipt_lifecycle_preserves_cache_and_role_invariants(
    tmp_path,
):
    """Task 11: a multi-turn receipt-enabled fixture never disturbs the
    conversation-cache identity.

    The system prompt, effective tool definitions, provider, model, and
    normalized role sequence are hashed at four checkpoints — before
    source capture, after issue, after artifact recheck, and after the
    observation append — and must be byte-identical throughout, with
    strict user/assistant alternation preserved.
    """
    import hashlib
    import json

    from agent.receipt_artifacts import ArtifactCatalog
    from agent.receipt_ingest import build_receipt_issuer
    from agent.receipt_store import ReceiptStore
    from agent.receipts import ReceiptSourceKey
    from agent.turn_ledger import record_turn_outcome_and_receipt
    from hades_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent = _StubAgent()
        agent._session_db = db
        system_message = {"role": "system", "content": "You are Hermes."}
        tool_definitions = [
            {
                "type": "function",
                "function": {"name": "patch", "parameters": {"type": "object"}},
            }
        ]
        messages = [
            system_message,
            {"role": "user", "content": "edit the file"},
            {"role": "assistant", "content": "Edited."},
            {"role": "user", "content": "now write the report"},
            {"role": "assistant", "content": "Report written."},
        ]

        def _fingerprint() -> str:
            return hashlib.sha256(
                json.dumps(
                    {
                        "system": system_message,
                        "tools": tool_definitions,
                        "provider": agent.provider,
                        "model": agent.model,
                        "roles": [m.get("role") for m in messages],
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()

        def _assert_alternation() -> None:
            roles = [m["role"] for m in messages if m["role"] != "system"]
            for index in range(len(roles) - 1):
                assert roles[index] != roles[index + 1], (
                    f"role alternation broken at index {index}: {roles}"
                )

        before_capture = _fingerprint()

        # Two receipt-enabled turns issue receipts through the finalizer
        # seam without touching the conversation.
        for turn_id in ("turn-a", "turn-b"):
            agent._current_turn_id = turn_id
            record_turn_outcome_and_receipt(
                agent,
                outcome="completed_unverified",
                outcome_reason="response completed without verification",
                turn_exit_reason="text_response(finish_reason=stop)",
                api_calls=1,
                tool_iterations=0,
                messages=messages,
            )
        after_issue = _fingerprint()
        assert after_issue == before_capture

        # A real artifact registration and read-only recheck.
        artifact_root = tmp_path / "artifacts"
        artifact_root.mkdir()
        artifact_path = artifact_root / "report.txt"
        artifact_path.write_text("report contents")
        catalog = ArtifactCatalog(db)
        digest = catalog.register_path(
            artifact_path,
            source_kind="execute_code",
            source_ref="sess-1:turn-a:call-1",
            allowed_roots=(artifact_root,),
        )
        catalog.recheck(digest.artifact_id, allowed_roots=(artifact_root,))
        after_artifact_recheck = _fingerprint()
        assert after_artifact_recheck == before_capture

        # Observation append via the public issuer.
        store = ReceiptStore(db)
        receipt = store.find_by_source(ReceiptSourceKey("turn", "sess-1:turn-a"))
        assert receipt is not None
        issuer = build_receipt_issuer(db)
        observation = issuer.recheck(receipt.receipt_id)
        assert observation.receipt_id == receipt.receipt_id
        after_observation = _fingerprint()
        assert after_observation == before_capture

        # Provider/model identity and strict alternation held throughout.
        assert (agent.provider, agent.model) == ("stub", "stub/model")
        assert len(messages) == 5
        _assert_alternation()
        # Both turns have exactly one receipt each — no duplicates.
        for turn_id in ("turn-a", "turn-b"):
            assert store.find_by_source(
                ReceiptSourceKey("turn", f"sess-1:{turn_id}")
            ) is not None
    finally:
        db.close()


def test_receipt_disabled_ordinary_turn_writes_no_receipt_or_artifact_rows(
    tmp_path,
):
    """Task 11: with ``receipts.mode: off`` an ordinary turn behaves
    exactly as before — the ledger row lands, but no receipt, source
    link, or artifact-catalog row is ever written."""
    from agent.receipts import ReceiptQuery
    from agent.receipt_store import ReceiptStore
    from agent.turn_ledger import record_turn_outcome_and_receipt
    from hades_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent = _StubAgent()
        agent._session_db = db
        agent._current_turn_id = "turn-off"
        agent._receipts_mode = "off"
        record, projection = record_turn_outcome_and_receipt(
            agent,
            outcome="completed_unverified",
            outcome_reason="response completed without verification",
            turn_exit_reason="text_response(finish_reason=stop)",
            api_calls=1,
            tool_iterations=0,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert projection is None
        assert record.outcome == "completed_unverified"
        # The ordinary ledger row landed.
        from agent.turn_ledger import fetch_turn_outcome

        assert fetch_turn_outcome(db, "sess-1", "turn-off") is not None
        # No receipt rows, no source links, no artifact-catalog rows.
        assert ReceiptStore(db).list(ReceiptQuery()) == []

        def _counts(conn):
            return (
                conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
                conn.execute(
                    "SELECT COUNT(*) FROM artifact_digests"
                ).fetchone()[0],
                conn.execute(
                    "SELECT COUNT(*) FROM artifact_locations"
                ).fetchone()[0],
            )

        assert db._execute_read(_counts) == (0, 0, 0)
    finally:
        db.close()


def test_receipt_issue_and_recheck_preserve_conversation_invariants(tmp_path):
    """Receipt issue/recheck never touches the conversation or prompt cache.

    Hash the system message, effective tool definitions, provider, model,
    and normalized role sequence immediately before and after receipt
    issue and recheck: the receipt path appends no message, resets no
    cached prompt field, and mutates no history.
    """
    import hashlib
    import json

    from agent.receipt_ingest import build_receipt_issuer
    from agent.receipt_store import ReceiptStore
    from agent.receipts import ReceiptSourceKey
    from agent.turn_ledger import record_turn_outcome_and_receipt
    from hades_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent = _StubAgent()
        agent._session_db = db
        agent._current_turn_id = "turn-inv"
        system_message = {"role": "system", "content": "You are Hermes."}
        tool_definitions = [
            {
                "type": "function",
                "function": {"name": "patch", "parameters": {"type": "object"}},
            }
        ]
        messages = [
            system_message,
            {"role": "user", "content": "edit the file"},
            {"role": "assistant", "content": "Done."},
        ]

        def _fingerprint() -> str:
            return hashlib.sha256(
                json.dumps(
                    {
                        "system": system_message,
                        "tools": tool_definitions,
                        "provider": agent.provider,
                        "model": agent.model,
                        "roles": [m.get("role") for m in messages],
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()

        before_issue = _fingerprint()
        _record, projection = record_turn_outcome_and_receipt(
            agent,
            outcome="completed_unverified",
            outcome_reason="response completed without verification",
            turn_exit_reason="text_response(finish_reason=stop)",
            api_calls=1,
            tool_iterations=0,
            messages=messages,
        )
        assert _fingerprint() == before_issue
        assert len(messages) == 3
        # Capture mode exposes no receipt projection.
        assert projection is None

        receipt = ReceiptStore(db).find_by_source(
            ReceiptSourceKey("turn", "sess-1:turn-inv")
        )
        assert receipt is not None

        issuer = build_receipt_issuer(db)
        before_recheck = _fingerprint()
        observation = issuer.recheck(receipt.receipt_id)
        assert _fingerprint() == before_recheck
        assert len(messages) == 3
        assert observation.receipt_id == receipt.receipt_id
        # The original receipt is byte-identical after the recheck.
        assert ReceiptStore(db).get(receipt.receipt_id) == receipt
    finally:
        db.close()
