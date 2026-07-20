"""Regression tests for issue #17335.

The ``quiet_mode=True`` fast path in :func:`model_tools.get_tool_definitions`
memoizes results to avoid re-walking the registry on every Gateway call. The
cached object must NOT be aliased into callers' return values \u2014 long-lived
Gateway processes mutate the returned list (``run_agent`` appends memory and
LCM context-engine tool schemas to ``self.tools``), and a shared list would
poison subsequent agent inits with duplicate tool names. Providers that
enforce uniqueness (DeepSeek, Xiaomi MiMo, Moonshot/Kimi) then reject the
API call with HTTP 400.

These tests pin:
- the cache-hit path returns a fresh list (existing #17098 behavior)
- the first uncached call also returns a fresh list (the fix)
- every call returns a list that is not the cached one, even after mutation
"""
from __future__ import annotations

import pytest

import model_tools


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty quiet_mode cache."""
    model_tools._tool_defs_cache.clear()
    yield
    model_tools._tool_defs_cache.clear()


class TestQuietModeCacheIsolation:

    def test_first_uncached_call_returns_fresh_list(self):
        """The first quiet_mode call must not alias the cached object \u2014
        otherwise a caller mutating the returned list mutates the cache."""
        first = model_tools.get_tool_definitions(quiet_mode=True)
        assert isinstance(first, list)
        # Find the cached value to compare identity.
        assert len(model_tools._tool_defs_cache) == 1
        cached = next(iter(model_tools._tool_defs_cache.values()))
        assert first is not cached, (
            "issue #17335: first quiet_mode call returned the cached list "
            "by reference \u2014 mutations will leak into subsequent calls."
        )

    def test_cache_hit_returns_fresh_list(self):
        """The cache-hit path already returned a copy pre-fix; pin it."""
        first = model_tools.get_tool_definitions(quiet_mode=True)
        second = model_tools.get_tool_definitions(quiet_mode=True)
        assert first is not second
        cached = next(iter(model_tools._tool_defs_cache.values()))
        assert second is not cached

    def test_caller_mutation_does_not_poison_cache(self):
        """Simulate run_agent appending LCM tool schemas to the returned
        list. A second call must NOT see those appended entries."""
        first = model_tools.get_tool_definitions(quiet_mode=True)
        baseline_len = len(first)
        # Caller mutates the returned list (this is what run_agent does
        # when it injects memory + context-engine tool schemas).
        first.append({"type": "function", "function": {"name": "lcm_grep"}})
        first.append({"type": "function", "function": {"name": "lcm_expand"}})

        second = model_tools.get_tool_definitions(quiet_mode=True)
        # Length must match the original \u2014 cache pollution would make
        # second 2 entries longer.
        assert len(second) == baseline_len, (
            f"issue #17335: cache was polluted by caller mutation. "
            f"first len={baseline_len}, mutated len={len(first)}, "
            f"second-call len={len(second)} \u2014 expected {baseline_len}."
        )
        names = [t.get("function", {}).get("name") for t in second]
        assert "lcm_grep" not in names
        assert "lcm_expand" not in names

    def test_repeated_caller_mutation_does_not_accumulate(self):
        """The original Gateway symptom: every agent init in a long-lived
        process appends LCM schemas, accumulating duplicates over time."""
        baseline = len(model_tools.get_tool_definitions(quiet_mode=True))
        for _ in range(5):
            tools = model_tools.get_tool_definitions(quiet_mode=True)
            tools.append({"type": "function", "function": {"name": "lcm_grep"}})
        final = model_tools.get_tool_definitions(quiet_mode=True)
        assert len(final) == baseline, (
            f"Cache accumulated mutations across {5} agent inits: "
            f"baseline={baseline}, final={len(final)}."
        )

    def test_cache_bounded_by_eviction(self):
        """The cache evicts the oldest entry when it reaches the cap,
        keeping the cache bounded instead of growing unbounded over a
        long-lived Gateway's lifetime (#19251)."""
        cap = model_tools._TOOL_DEFS_CACHE_MAX
        # Fill cache to the cap with distinct keys by varying enabled_toolsets.
        for i in range(cap):
            model_tools.get_tool_definitions(
                enabled_toolsets=[f"fake_toolset_{i}"], quiet_mode=True,
            )
        assert len(model_tools._tool_defs_cache) == cap

        # Adding one more must evict the oldest, not clear everything and
        # not grow past the cap.
        model_tools.get_tool_definitions(
            enabled_toolsets=["fake_toolset_overflow"], quiet_mode=True,
        )
        assert len(model_tools._tool_defs_cache) == cap, (
            "Eviction should keep the cache at the cap, not clear it or grow"
        )

    def test_non_quiet_mode_does_not_use_cache(self):
        """Sanity: quiet_mode=False (TUI path) skips the cache entirely \u2014
        explains why the bug only hit Gateway."""
        model_tools.get_tool_definitions(quiet_mode=False)
        assert len(model_tools._tool_defs_cache) == 0


class TestReceiptToolSchemaIsolation:
    """Task 11: receipts add no model-visible tool and never disturb the
    effective tool-definition cache or schema hashes."""

    def test_receipt_issue_and_recheck_keep_tool_schema_byte_identical(
        self, tmp_path
    ):
        import hashlib
        import json

        from agent.receipt_ingest import build_receipt_issuer
        from agent.receipt_store import ReceiptStore
        from agent.receipts import ReceiptSourceKey
        from agent.turn_ledger import TurnOutcomeRecord
        from hades_state import SessionDB

        def _schema_hash() -> str:
            defs = model_tools.get_tool_definitions(quiet_mode=True)
            return hashlib.sha256(
                json.dumps(defs, sort_keys=True).encode("utf-8")
            ).hexdigest()

        before_hash = _schema_hash()
        cache_size_before = len(model_tools._tool_defs_cache)

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.record_turn_outcome(
                TurnOutcomeRecord(
                    session_id="s1",
                    turn_id="t1",
                    created_at=1752660000.0,
                    outcome="completed_unverified",
                    outcome_reason="response completed without verification",
                    turn_exit_reason="text_response(finish_reason=stop)",
                    api_calls=1,
                    tool_iterations=0,
                    retry_count=0,
                    guardrail_halt=None,
                    cost_usd_delta=0.0,
                    input_tokens_delta=1,
                    output_tokens_delta=1,
                    cache_read_tokens_delta=0,
                    skills_loaded=(),
                    model="cache-test-model",
                )
            )
            issuer = build_receipt_issuer(db)
            receipt = issuer.issue(ReceiptSourceKey("turn", "s1:t1"))
            issuer.recheck(receipt.receipt_id)
            assert ReceiptStore(db).get(receipt.receipt_id) == receipt
        finally:
            db.close()

        # The effective tool schema hash is unchanged and receipt code
        # touched neither the cache nor the definitions themselves.
        assert _schema_hash() == before_hash
        assert len(model_tools._tool_defs_cache) >= cache_size_before

    def test_no_receipt_named_tool_is_model_visible(self):
        definitions = model_tools.get_tool_definitions(quiet_mode=True)
        names = [
            tool.get("function", {}).get("name", "") for tool in definitions
        ]
        assert all("receipt" not in name.lower() for name in names), (
            "receipts are Footprint Ladder rung 1: no model-visible core "
            f"tool may be added \u2014 found {names}"
        )
