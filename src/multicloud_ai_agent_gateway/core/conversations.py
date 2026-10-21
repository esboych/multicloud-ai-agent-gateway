"""Conversation store: remembers which agent owns a conversation id and who
started it.

Why this exists. Every backend returns an opaque ``conversation_id`` (Foundry
``response.id``, AgentCore ``runtimeSessionId``). Without a record of which
agent produced it, two things are impossible:

* sticky AutoRouter: ``AutoRouter:continue`` has to know which concrete agent
  owns the thread, otherwise the gateway would re-classify the follow-up and
  may send it to a different agent that has never seen the conversation;
* ownership: when callers are authenticated, a conversation started by one
  tenant must not be continuable by another one that guesses the id.

The store is deliberately small: one record per conversation id, a TTL and a
size cap. ``memory`` is the default and is enough for a single replica.
``sqlite`` survives restarts on a persistent volume. Neither is a shared
store across replicas; that is listed in ``docs/roadmap.md``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    agent: str
    owner_subject: str | None
    owner_tenant: str | None
    created_at: float
    last_seen_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "agent": self.agent,
            "owner_subject": self.owner_subject,
            "owner_tenant": self.owner_tenant,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }


@runtime_checkable
class ConversationStore(Protocol):
    async def get(self, conversation_id: str) -> ConversationRecord | None: ...

    async def remember(
        self,
        conversation_id: str,
        agent: str,
        *,
        owner_subject: str | None = None,
        owner_tenant: str | None = None,
    ) -> ConversationRecord: ...

    async def forget(self, conversation_id: str) -> None: ...

    async def aclose(self) -> None: ...


Clock = Callable[[], float]


class NullConversationStore:
    """Remembers nothing. Restores the pre-store behaviour (pure pass-through)."""

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        return None

    async def remember(
        self,
        conversation_id: str,
        agent: str,
        *,
        owner_subject: str | None = None,
        owner_tenant: str | None = None,
    ) -> ConversationRecord:
        now = time.time()
        return ConversationRecord(conversation_id, agent, owner_subject, owner_tenant, now, now)

    async def forget(self, conversation_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class InMemoryConversationStore:
    """Bounded LRU with TTL. Safe for concurrent use from one event loop."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 86400.0,
        max_entries: int = 10_000,
        clock: Clock = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._now = clock
        self._lock = asyncio.Lock()
        self._records: OrderedDict[str, ConversationRecord] = OrderedDict()

    def __len__(self) -> int:
        return len(self._records)

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        async with self._lock:
            record = self._records.get(conversation_id)
            if record is None:
                return None
            if self._expired(record):
                del self._records[conversation_id]
                return None
            self._records.move_to_end(conversation_id)
            return record

    async def remember(
        self,
        conversation_id: str,
        agent: str,
        *,
        owner_subject: str | None = None,
        owner_tenant: str | None = None,
    ) -> ConversationRecord:
        if not conversation_id:
            raise ValueError("conversation_id must be non-empty")
        now = self._now()
        async with self._lock:
            previous = self._records.get(conversation_id)
            created_at = previous.created_at if previous and not self._expired(previous) else now
            # The first writer owns the conversation. Later writes (the same
            # caller continuing the thread) keep the original owner so an
            # ownership check cannot be defeated by re-remembering.
            if previous is not None and not self._expired(previous):
                owner_subject = previous.owner_subject
                owner_tenant = previous.owner_tenant
            record = ConversationRecord(
                conversation_id=conversation_id,
                agent=agent,
                owner_subject=owner_subject,
                owner_tenant=owner_tenant,
                created_at=created_at,
                last_seen_at=now,
            )
            self._records[conversation_id] = record
            self._records.move_to_end(conversation_id)
            self._evict_locked()
            return record

    async def forget(self, conversation_id: str) -> None:
        async with self._lock:
            self._records.pop(conversation_id, None)

    async def aclose(self) -> None:
        return None

    def _expired(self, record: ConversationRecord) -> bool:
        return (self._now() - record.last_seen_at) > self._ttl

    def _evict_locked(self) -> None:
        for key in [k for k, v in self._records.items() if self._expired(v)]:
            del self._records[key]
        while len(self._records) > self._max:
            self._records.popitem(last=False)


class SqliteConversationStore:
    """Same contract as the in-memory store, persisted in one SQLite file.

    Single-writer by design (a ``threading.Lock`` around every statement);
    the gateway runs one replica. SQLite calls run on a worker thread so the
    event loop is never blocked.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        agent           TEXT NOT NULL,
        owner_subject   TEXT,
        owner_tenant    TEXT,
        created_at      REAL NOT NULL,
        last_seen_at    REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS conversations_last_seen ON conversations(last_seen_at);
    """

    def __init__(
        self,
        path: str,
        *,
        ttl_seconds: float = 86400.0,
        max_entries: int = 100_000,
        clock: Clock = time.time,
    ) -> None:
        if not path:
            raise ValueError("path must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._path = path
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._now = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        return await asyncio.to_thread(self._get_sync, conversation_id)

    async def remember(
        self,
        conversation_id: str,
        agent: str,
        *,
        owner_subject: str | None = None,
        owner_tenant: str | None = None,
    ) -> ConversationRecord:
        if not conversation_id:
            raise ValueError("conversation_id must be non-empty")
        return await asyncio.to_thread(
            self._remember_sync, conversation_id, agent, owner_subject, owner_tenant
        )

    async def forget(self, conversation_id: str) -> None:
        await asyncio.to_thread(self._forget_sync, conversation_id)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_sync)

    # -- sync internals ---------------------------------------------------

    def _get_sync(self, conversation_id: str) -> ConversationRecord | None:
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT conversation_id, agent, owner_subject, owner_tenant, created_at, last_seen_at "
                "FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            record = ConversationRecord(*row)
            if (now - record.last_seen_at) > self._ttl:
                self._conn.execute(
                    "DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,)
                )
                self._conn.commit()
                return None
            return record

    def _remember_sync(
        self,
        conversation_id: str,
        agent: str,
        owner_subject: str | None,
        owner_tenant: str | None,
    ) -> ConversationRecord:
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_subject, owner_tenant, created_at, last_seen_at "
                "FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            created_at = now
            if row is not None and (now - row[3]) <= self._ttl:
                owner_subject, owner_tenant, created_at = row[0], row[1], row[2]
            self._conn.execute(
                "INSERT INTO conversations (conversation_id, agent, owner_subject, owner_tenant, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET agent=excluded.agent, "
                "owner_subject=excluded.owner_subject, owner_tenant=excluded.owner_tenant, "
                "created_at=excluded.created_at, last_seen_at=excluded.last_seen_at",
                (conversation_id, agent, owner_subject, owner_tenant, created_at, now),
            )
            self._conn.execute(
                "DELETE FROM conversations WHERE last_seen_at < ?", (now - self._ttl,)
            )
            self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id IN ("
                "  SELECT conversation_id FROM conversations ORDER BY last_seen_at DESC "
                "  LIMIT -1 OFFSET ?)",
                (self._max,),
            )
            self._conn.commit()
            return ConversationRecord(
                conversation_id, agent, owner_subject, owner_tenant, created_at, now
            )

    def _forget_sync(self, conversation_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,)
            )
            self._conn.commit()

    def _close_sync(self) -> None:
        with self._lock:
            self._conn.close()


def build_conversation_store(
    kind: str,
    *,
    path: str = "",
    ttl_seconds: float = 86400.0,
    max_entries: int = 10_000,
) -> ConversationStore:
    """Factory used by app.py: ``none`` | ``memory`` | ``sqlite``."""
    normalized = (kind or "memory").strip().lower()
    if normalized == "none":
        return NullConversationStore()
    if normalized == "memory":
        return InMemoryConversationStore(ttl_seconds=ttl_seconds, max_entries=max_entries)
    if normalized == "sqlite":
        if not path:
            raise ValueError("conversation store 'sqlite' requires a file path")
        return SqliteConversationStore(path, ttl_seconds=ttl_seconds, max_entries=max_entries)
    raise ValueError(f"Unknown conversation store kind {normalized!r}; use none, memory or sqlite")


__all__ = [
    "ConversationRecord",
    "ConversationStore",
    "InMemoryConversationStore",
    "NullConversationStore",
    "SqliteConversationStore",
    "build_conversation_store",
]
