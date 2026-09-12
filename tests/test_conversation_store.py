"""Tests for ConversationStore."""

import pytest
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from forge.models.base import Base
from forge.models.conversation import Conversation
from forge.stores.conversation import ConversationStore


@pytest.fixture()
async def session_factory():
    """In-memory SQLite with conversations table."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def store(session_factory) -> ConversationStore:
    return ConversationStore(session_factory)


class TestGetHistory:
    async def test_returns_empty_for_nonexistent(self, store: ConversationStore):
        result = await store.get_history(1, "MergeRequest", 10, "disc-1")
        assert result == []

    async def test_returns_messages_after_append(self, store: ConversationStore):
        await store.append(1, "MergeRequest", 10, "disc-1", "user", "hello")
        result = await store.get_history(1, "MergeRequest", 10, "disc-1")
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "hello"}


class TestAppend:
    async def test_creates_new_row(self, store: ConversationStore, session_factory):
        await store.append(1, "MergeRequest", 10, "disc-1", "user", "first")
        async with session_factory() as session:
            row = (await session.execute(Conversation.__table__.select())).first()
            assert row is not None

    async def test_appends_to_existing(self, store: ConversationStore):
        await store.append(1, "MergeRequest", 10, "disc-1", "user", "hello")
        await store.append(1, "MergeRequest", 10, "disc-1", "assistant", "hi there")
        result = await store.get_history(1, "MergeRequest", 10, "disc-1")
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "hi there"

    async def test_multiple_messages_accumulate(self, store: ConversationStore):
        for i in range(5):
            await store.append(1, "MergeRequest", 10, "disc-1", "user", f"msg {i}")
        result = await store.get_history(1, "MergeRequest", 10, "disc-1")
        assert len(result) == 5


class TestThreadIsolation:
    async def test_different_discussions_independent(self, store: ConversationStore):
        await store.append(1, "MergeRequest", 10, "disc-1", "user", "thread 1")
        await store.append(1, "MergeRequest", 10, "disc-2", "user", "thread 2")

        h1 = await store.get_history(1, "MergeRequest", 10, "disc-1")
        h2 = await store.get_history(1, "MergeRequest", 10, "disc-2")

        assert len(h1) == 1
        assert h1[0]["content"] == "thread 1"
        assert len(h2) == 1
        assert h2[0]["content"] == "thread 2"

    async def test_different_projects_independent(self, store: ConversationStore):
        await store.append(1, "MergeRequest", 10, "disc-1", "user", "project 1")
        await store.append(2, "MergeRequest", 10, "disc-1", "user", "project 2")

        h1 = await store.get_history(1, "MergeRequest", 10, "disc-1")
        h2 = await store.get_history(2, "MergeRequest", 10, "disc-1")

        assert h1[0]["content"] == "project 1"
        assert h2[0]["content"] == "project 2"

    async def test_different_noteables_independent(self, store: ConversationStore):
        await store.append(1, "MergeRequest", 10, "disc-1", "user", "MR 10")
        await store.append(1, "Issue", 10, "disc-1", "user", "Issue 10")

        h1 = await store.get_history(1, "MergeRequest", 10, "disc-1")
        h2 = await store.get_history(1, "Issue", 10, "disc-1")

        assert h1[0]["content"] == "MR 10"
        assert h2[0]["content"] == "Issue 10"


class TestCleanupExpired:
    async def test_removes_old_conversations(self, store: ConversationStore, session_factory):
        # Insert a conversation and manually backdate it
        await store.append(1, "MergeRequest", 10, "disc-old", "user", "old msg")
        async with session_factory() as session:
            from sqlalchemy import update

            stmt = (
                update(Conversation)
                .where(Conversation.discussion_id == "disc-old")
                .values(updated_at=datetime.now(timezone.utc) - timedelta(days=60))
            )
            await session.execute(stmt)
            await session.commit()

        # Insert a fresh conversation
        await store.append(1, "MergeRequest", 10, "disc-new", "user", "new msg")

        deleted = await store.cleanup_expired(max_age_days=30)
        assert deleted == 1

        # Old one is gone, new one remains
        assert await store.get_history(1, "MergeRequest", 10, "disc-old") == []
        assert len(await store.get_history(1, "MergeRequest", 10, "disc-new")) == 1

    async def test_cleanup_returns_zero_when_nothing_expired(self, store: ConversationStore):
        await store.append(1, "MergeRequest", 10, "disc-1", "user", "recent")
        deleted = await store.cleanup_expired(max_age_days=30)
        assert deleted == 0
