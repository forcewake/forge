import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.models import AgentRun, Base, ReviewState


@pytest.fixture()
async def db_session():
    """Create an in-memory SQLite database and yield a session."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_and_query_agent_run(db_session: AsyncSession):
    run = AgentRun(
        project_id=1,
        event_type="merge_request",
        target_iid=42,
        agent_name="code_review",
        model_used="strong",
        input_tokens=500,
        output_tokens=200,
        duration_ms=1500,
        status="success",
    )
    db_session.add(run)
    await db_session.commit()

    result = await db_session.execute(select(AgentRun).where(AgentRun.project_id == 1))
    fetched = result.scalar_one()
    assert fetched.agent_name == "code_review"
    assert fetched.status == "success"
    assert fetched.target_iid == 42


@pytest.mark.asyncio
async def test_review_state_unique_constraint(db_session: AsyncSession):
    state1 = ReviewState(project_id=1, mr_iid=10, last_reviewed_sha="abc123")
    db_session.add(state1)
    await db_session.commit()

    # Inserting a duplicate (same project_id + mr_iid) should raise
    state2 = ReviewState(project_id=1, mr_iid=10, last_reviewed_sha="def456")
    db_session.add(state2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
