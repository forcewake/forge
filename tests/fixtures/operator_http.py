"""Small HTTP fixtures over the current application and actual SQLite store."""

from datetime import datetime, timezone
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.api_operator import operator_subject_scope_token
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun
from forge.main import create_app

SECRET = "read-scope-fixture"
SUBJECT_A = CanonicalSubject(provider_family="github", connection="", native_id="acme/alpha")
SUBJECT_B = CanonicalSubject(provider_family="github", connection="", native_id="other/beta")


def scope(subjects=(SUBJECT_A,), *, signed=None):
    return {"subject": [subject.subject_id() for subject in subjects]}, {
        "Authorization": "Bearer " + operator_subject_scope_token(SECRET, signed or subjects)
    }


@pytest.fixture()
async def operator_app(tmp_path):
    reset_engine()
    settings = Settings(
        _env_file=None,
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="fixture-token",
        GITLAB_WEBHOOK_SECRET="gitlab-hook-secret",
        FORGE_GITHUB_ENABLED=True,
        FORGE_GITHUB_WEBHOOK_SECRET="github-hook-secret",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/operator.db",
        REDIS_URL=None,
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            yield app
    finally:
        reset_engine()


@pytest.fixture()
async def operator_client(operator_app):
    async with AsyncClient(
        transport=ASGITransport(app=operator_app), base_url="http://forge.test"
    ) as client:
        yield client


async def seed_run(app, run_id="a" * 32, subject=SUBJECT_A, **changes):
    values = dict(
        id=run_id,
        project_id=42,
        issue_iid=int(run_id[:5], 16),
        provider=subject.provider_family,
        github_repo_full_name=subject.native_id,
        status="waiting_ci",
        base_sha="b" * 40,
        candidate_shas=["c" * 40],
        evidence={},
        created_at=datetime.now(timezone.utc),
    )
    values.update(changes)
    async with app.state.session_factory() as session:
        session.add(FlowRun(**values))
        await session.commit()
    return run_id
