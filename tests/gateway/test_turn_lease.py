"""Behavior tests for the per-session turn lease (#64934)."""

import asyncio

import pytest

from gateway.turn_lease import SessionTurnLeaseRegistry


def _run(coro):
    return asyncio.run(coro)


def test_alias_key_turn_waits_and_order_is_preserved():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        events = []

        async def turn(owner_key, generation, hold):
            token = await registry.acquire(
                "sess-1", owner_key=owner_key, generation=generation, timeout=5
            )
            assert token is not None and not token.degraded
            events.append(f"load:{owner_key}")
            await asyncio.sleep(hold)
            events.append(f"flush:{owner_key}")
            registry.release(token)

        t1 = asyncio.create_task(turn("key-a", 1, hold=0.05))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(turn("key-b", 1, hold=0))
        await asyncio.gather(t1, t2)
        return events

    assert _run(scenario()) == ["load:key-a", "flush:key-a", "load:key-b", "flush:key-b"]


def test_distinct_sessions_do_not_contend():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        order = []

        async def turn(session_id, owner_key):
            token = await registry.acquire(session_id, owner_key=owner_key, generation=1, timeout=5)
            order.append(f"start:{session_id}")
            await asyncio.sleep(0.05)
            order.append(f"end:{session_id}")
            registry.release(token)

        await asyncio.gather(turn("sess-a", "key-a"), turn("sess-b", "key-b"))
        return order

    assert _run(scenario())[:2] == ["start:sess-a", "start:sess-b"]


def test_contention_logs_named_warning(caplog):
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        t1 = await registry.acquire("sess-w", owner_key="key-a", generation=3, timeout=5)

        async def second():
            t2 = await registry.acquire("sess-w", owner_key="key-b", generation=7, timeout=5)
            registry.release(t2)

        task = asyncio.create_task(second())
        await asyncio.sleep(0.01)
        registry.release(t1)
        await task

    with caplog.at_level("WARNING", logger="gateway.turn_lease"):
        _run(scenario())
    warnings = [r for r in caplog.records if "turn lease contention" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "sess-w" in msg and "key-a" in msg and "key-b" in msg


def test_generation_scoped_idempotent_release():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        stale = await registry.acquire("sess-g", owner_key="key-a", generation=1, timeout=5)
        assert stale is not None
        assert registry.release(stale) is True
        assert registry.release(stale) is False

        newer = await registry.acquire("sess-g", owner_key="key-a", generation=2, timeout=5)
        stale.released = False
        assert registry.release(stale) is False
        waiter = asyncio.create_task(registry.acquire("sess-g", owner_key="key-b", generation=3, timeout=5))
        await asyncio.sleep(0.02)
        assert not waiter.done()
        assert registry.release(newer) is True
        third = await waiter
        assert third is not None and not third.degraded
        registry.release(third)

    _run(scenario())


def test_release_none_and_empty_session_are_noops():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        assert registry.release(None) is False
        assert await registry.acquire("", owner_key="k", generation=1) is None

    _run(scenario())


def test_timeout_fails_open_with_degraded_token(caplog):
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        holder = await registry.acquire("sess-t", owner_key="key-stuck", generation=1, timeout=5)
        degraded = await registry.acquire("sess-t", owner_key="key-b", generation=2, timeout=0.05)
        assert degraded is not None and degraded.degraded is True
        assert registry.release(degraded) is False
        third = asyncio.create_task(registry.acquire("sess-t", owner_key="key-c", generation=3, timeout=5))
        await asyncio.sleep(0.02)
        assert not third.done()
        registry.release(holder)
        t3 = await third
        assert t3 is not None and not t3.degraded
        registry.release(t3)

    with caplog.at_level("ERROR", logger="gateway.turn_lease"):
        _run(scenario())
    errors = [r for r in caplog.records if "failing open" in r.getMessage()]
    assert len(errors) == 1
    assert "sess-t" in errors[0].getMessage()


def test_registry_bounded_and_never_evicts_live_lease():
    async def scenario():
        registry = SessionTurnLeaseRegistry(max_entries=5)
        live = await registry.acquire("live", owner_key="k", generation=1, timeout=5)
        for i in range(50):
            token = await registry.acquire(f"s{i}", owner_key="k", generation=1, timeout=5)
            registry.release(token)
        assert len(registry) <= 6
        assert registry.release(live) is True

    _run(scenario())


def test_rebind_moves_serialization_to_new_session_id():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        token = await registry.acquire("parent", owner_key="key-a", generation=1, timeout=5)
        assert registry.rebind(token, "child") is True
        assert token is not None and token.session_id == "child"
        waiter = asyncio.create_task(registry.acquire("child", owner_key="key-b", generation=1, timeout=5))
        await asyncio.sleep(0.02)
        assert not waiter.done()
        assert registry.release(token) is True
        t2 = await waiter
        assert t2 is not None and not t2.degraded
        registry.release(t2)

    _run(scenario())


def test_rebind_is_ownership_checked_and_noop_safe():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        token = await registry.acquire("s1", owner_key="k", generation=1, timeout=5)
        assert token is not None
        assert registry.rebind(token, "s1") is False
        assert registry.rebind(token, "") is False
        assert registry.rebind(None, "s2") is False
        registry.release(token)
        assert registry.rebind(token, "s2") is False

    _run(scenario())


def test_rebind_blocked_when_target_lease_is_live():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        t_a = await registry.acquire("sess-a", owner_key="key-a", generation=1, timeout=5)
        t_b = await registry.acquire("sess-b", owner_key="key-b", generation=1, timeout=5)
        assert t_a is not None and t_b is not None
        assert registry.rebind(t_a, "sess-b") is False
        assert t_a.session_id == "sess-a"
        assert registry.release(t_a) is True
        assert registry.release(t_b) is True

    _run(scenario())


def test_runner_release_turn_lease_is_token_scoped_and_bare_safe():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    assert runner._release_turn_lease("key-a", 1) is False

    async def scenario():
        runner._turn_leases = SessionTurnLeaseRegistry()
        runner._turn_lease_tokens = {}
        token = await runner._turn_leases.acquire("sess-r", owner_key="key-a", generation=1, timeout=5)
        runner._turn_lease_tokens[("key-a", 1)] = token
        assert runner._release_turn_lease("key-a", 2) is False
        assert runner._turn_leases._leases["sess-r"].holder is token
        assert runner._release_turn_lease("key-a", 1) is True
        assert runner._release_turn_lease("key-a", 1) is False
        assert runner._release_turn_lease("", 1) is False

    _run(scenario())
