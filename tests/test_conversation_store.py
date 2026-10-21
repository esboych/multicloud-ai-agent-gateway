"""Conversation store contract: memory and sqlite behave identically."""

from __future__ import annotations

import pytest

from multicloud_ai_agent_gateway.core.conversations import (
    InMemoryConversationStore,
    NullConversationStore,
    SqliteConversationStore,
    build_conversation_store,
)


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(params=["memory", "sqlite"])
def store_and_clock(request, tmp_path):
    clock = _Clock()
    if request.param == "memory":
        store = InMemoryConversationStore(ttl_seconds=100, max_entries=3, clock=clock)
    else:
        store = SqliteConversationStore(
            str(tmp_path / "conversations.db"), ttl_seconds=100, max_entries=3, clock=clock
        )
    yield store, clock


async def test_remember_then_get(store_and_clock) -> None:
    store, clock = store_and_clock
    record = await store.remember("c1", "AgentA", owner_subject="u1", owner_tenant="t1")
    assert record.agent == "AgentA"
    assert record.created_at == clock.now
    fetched = await store.get("c1")
    assert fetched is not None
    assert fetched.agent == "AgentA"
    assert fetched.owner_subject == "u1"
    assert fetched.owner_tenant == "t1"


async def test_unknown_conversation_is_none(store_and_clock) -> None:
    store, _ = store_and_clock
    assert await store.get("nope") is None


async def test_first_writer_keeps_ownership(store_and_clock) -> None:
    store, clock = store_and_clock
    await store.remember("c1", "AgentA", owner_subject="u1", owner_tenant="t1")
    clock.now += 10
    record = await store.remember("c1", "AgentA", owner_subject="u2", owner_tenant="t2")
    assert record.owner_subject == "u1"
    assert record.owner_tenant == "t1"
    assert record.created_at == 1_000.0
    assert record.last_seen_at == 1_010.0


async def test_ttl_expires_records(store_and_clock) -> None:
    store, clock = store_and_clock
    await store.remember("c1", "AgentA")
    clock.now += 101
    assert await store.get("c1") is None
    # An expired id can be re-owned by a new writer.
    record = await store.remember("c1", "AgentB", owner_subject="u9")
    assert record.owner_subject == "u9"
    assert record.agent == "AgentB"


async def test_max_entries_evicts_least_recent(store_and_clock) -> None:
    store, clock = store_and_clock
    for i in range(4):
        clock.now += 1
        await store.remember(f"c{i}", "AgentA")
    assert await store.get("c0") is None
    assert await store.get("c3") is not None


async def test_forget(store_and_clock) -> None:
    store, _ = store_and_clock
    await store.remember("c1", "AgentA")
    await store.forget("c1")
    assert await store.get("c1") is None


async def test_rejects_empty_conversation_id(store_and_clock) -> None:
    store, _ = store_and_clock
    with pytest.raises(ValueError):
        await store.remember("", "AgentA")


async def test_sqlite_persists_across_instances(tmp_path) -> None:
    path = str(tmp_path / "persist.db")
    first = SqliteConversationStore(path)
    await first.remember("c1", "AgentA", owner_subject="u1")
    await first.aclose()
    second = SqliteConversationStore(path)
    record = await second.get("c1")
    assert record is not None and record.agent == "AgentA" and record.owner_subject == "u1"
    await second.aclose()


async def test_null_store_remembers_nothing() -> None:
    store = NullConversationStore()
    await store.remember("c1", "AgentA")
    assert await store.get("c1") is None


def test_factory(tmp_path) -> None:
    assert isinstance(build_conversation_store("none"), NullConversationStore)
    assert isinstance(build_conversation_store("memory"), InMemoryConversationStore)
    assert isinstance(
        build_conversation_store("sqlite", path=str(tmp_path / "x.db")), SqliteConversationStore
    )
    with pytest.raises(ValueError):
        build_conversation_store("sqlite")
    with pytest.raises(ValueError):
        build_conversation_store("redis")


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        InMemoryConversationStore(ttl_seconds=0)
    with pytest.raises(ValueError):
        InMemoryConversationStore(max_entries=0)
