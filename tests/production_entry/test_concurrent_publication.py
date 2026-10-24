"""Real SQL journal and native HTTP clients collapse racing Draft creators.

Deterministic barriers reproduce native creation while its response or visibility
is unavailable. An in-flight result stays unknown until the existing receipt can
be proved; a second creator never infers permission for another native POST.
"""

import asyncio
from datetime import timedelta
import json

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.domain.verdicts import Unknown
from forge.engine.effects import Effects
from forge.port_values import Actor, Applied
from forge.store.effects import EffectStore
from forge.store.models_execution import EngineEffect
from forge.store.records import StoreConflict
from tests.test_engine_delivery import (
    advance,
    case as case,
    completed,
    delivery as delivery,
    env as env,
    settings as settings,
    started,
)
from tests.test_github_reads import app_key as app_key

pytestmark = pytest.mark.production_entry


class NativeDraft:
    def __init__(self, env, run):
        self.env, self.run = env, run
        self.repo, self.branch = env.intent.task.subject.repo, "forge/" + run.id
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.posts, self.rows, self.hide, self.lose_response = 0, [], False, False
        self.bot = Actor(
            provider=env.fake.provider,
            connection_id="test",
            native_id="99",
            login="forge[bot]" if env.fake.provider == "github" else "project_42_bot_forge",
            is_bot=True,
        )

    def row(self):
        if self.env.fake.provider == "github":
            return {
                "id": 80,
                "number": 8,
                "state": "open",
                "draft": True,
                "user": {"id": 99, "login": self.bot.login, "type": "Bot"},
                "head": {"ref": self.branch, "sha": "a" * 40, "repo": {"id": 42}},
                "base": {"ref": "main", "sha": "b" * 40, "repo": {"id": 42}},
            }
        return {
            "id": 80,
            "iid": 8,
            "project_id": 42,
            "source_project_id": 42,
            "target_project_id": 42,
            "source_branch": self.branch,
            "target_branch": "main",
            "sha": "a" * 40,
            "draft": True,
            "state": "opened",
            "author": {"id": 99, "username": self.bot.login, "bot": True},
        }

    async def __call__(self, request):
        path, method = request.url.path, request.method
        if path == "/app/installations/900/access_tokens":
            body = json.loads(request.content)
            assert body["repository_ids"] == [42]
            return httpx.Response(
                201,
                json={
                    "token": "synthetic-token",
                    "repositories": [{"id": 42}],
                    "repository_selection": "selected",
                    "permissions": body["permissions"],
                    "expires_at": (self.env.clock.now() + timedelta(hours=1)).isoformat(),
                },
            )
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "repositories": [
                        {
                            "id": 42,
                            "full_name": "owner/project",
                            "default_branch": "main",
                            "archived": False,
                            "disabled": False,
                        }
                    ],
                },
            )
        if path == "/api/v4/user":
            assert request.headers["private-token"] == "synthetic-write"
            return httpx.Response(200, json={"id": 99, "username": self.bot.login, "bot": True})
        if path == "/api/v4/projects/42":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "path_with_namespace": "owner/project",
                    "default_branch": "main",
                    "archived": False,
                },
            )
        prefix = (
            "/repos/owner/project/"
            if self.env.fake.provider == "github"
            else "/api/v4/projects/42/"
        )
        assert path.startswith(prefix)
        resource = path.removeprefix(prefix)
        target = "pulls" if self.env.fake.provider == "github" else "merge_requests"
        if method == "GET":
            if resource in ("protected_branches", "rules/branches/" + self.branch):
                return httpx.Response(200, json=[])
            if resource in ("branches/" + self.branch, "repository/branches/" + self.branch):
                return httpx.Response(200, json={"name": self.branch, "protected": False})
            assert resource == target, resource
            return httpx.Response(200, json=[] if self.hide else self.rows)
        assert method == "POST" and resource == target
        self.posts += 1
        async with self.env.factory() as session:
            row = await session.scalar(
                select(EngineEffect).where(EngineEffect.key == self.run.id + ":ensure_draft")
            )
            assert row.state == "dispatched" and row.payload_json and row.payload_digest
        self.entered.set()
        await self.release.wait()
        result = self.row()
        self.rows.append(result)
        if self.lose_response:
            raise httpx.ReadTimeout("synthetic lost native reply", request=request)
        return httpx.Response(201, json=result)


class NoCommitter:
    async def commit(self, *args):
        raise AssertionError("Draft creation cannot synthesize a commit")


@pytest.fixture
async def native(delivery, app_key):
    env = delivery["env"]
    run, _, pending = await started(delivery)
    await completed(delivery, pending)
    (lease,) = await env.steps.claim_due("publisher", env.clock.now(), 60)
    wire = NativeDraft(env, run)
    if env.fake.provider == "github":
        from forge.providers.github.auth import AppAuth
        from forge.providers.github.client import API
        from forge.providers.github.repo import GitHubRepo
        from forge.providers.github.review import GitHubReview
        from forge.providers.github.transport import Transport
        from forge.providers.github.write import GitHubWrites

        transport = Transport(transport=httpx.MockTransport(wire))
        api = API(AppAuth("test", "app-9", app_key, {"42": 900}, env.clock, transport), transport)
        repo, review = GitHubRepo(api), GitHubReview(api, bot=wire.bot)
        writer = GitHubWrites(
            api,
            bot=wire.bot,
            repositories=repo,
            reviews=review,
            ci=env.fake,
            committer=NoCommitter(),
            requirements_url="https://release.example/requirements.txt",
            requirements_sha256="1" * 64,
        )
    else:
        from forge.providers.gitlab.client import API, Credentials
        from forge.providers.gitlab.repo import GitLabRepo
        from forge.providers.gitlab.review import GitLabReview
        from forge.providers.gitlab.transport import Endpoint, Transport
        from forge.providers.gitlab.write import GitLabWrites

        transport = Transport(Endpoint("https://ce.example"), transport=httpx.MockTransport(wire))
        api = API(
            "test",
            transport,
            {
                "42": Credentials(
                    SecretStr("synthetic-read"), SecretStr("synthetic-write"), wire.bot
                )
            },
        )
        repo, review = GitLabRepo(api), GitLabReview(api)
        writer = GitLabWrites(
            api,
            repositories=repo,
            reviews=review,
            ci=env.fake,
            committer=NoCommitter(),
            requirements_url="https://release.example/requirements.txt",
            requirements_sha256="1" * 64,
        )

    def effects(factory=None, store=None):
        return Effects(
            store or EffectStore(factory or env.factory),
            writer,
            repo,
            env.fake,
            review,
            env.fake,
            env.effects._gateway,
            env.clock,
        )

    delivery.update(root=run, lease=lease, wire=wire, fresh_effects=effects)
    try:
        yield delivery
    finally:
        wire.release.set()
        await transport.close()


async def create(native, effects=None, lease=None, *, root=None, body="Exact frozen body"):
    return await (effects or native["fresh_effects"]()).ensure_draft(
        lease or native["lease"],
        root or native["root"].id,
        native["wire"].repo,
        native["wire"].branch,
        "Frozen title",
        body,
    )


async def assert_one(native):
    assert native["wire"].posts == len(native["wire"].rows) == 1
    async with native["env"].factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EngineEffect)
                .where(EngineEffect.kind == "ensure_draft")
            )
            == 1
        )


async def test_the_scanner_racing_the_leg_creates_exactly_one_mr(native):
    wire = native["wire"]
    first = asyncio.create_task(create(native))
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        scanner = native["fresh_effects"]()
        row = await scanner.store.get(native["root"].id + ":ensure_draft")
        assert row.state == "dispatched" and not wire.rows
        observed = await scanner.recover(row)
        assert observed.state == "unknown"
        assert isinstance(await create(native, scanner), Unknown)
        assert wire.posts == 1 and not wire.rows
    finally:
        wire.release.set()
    result = await first
    assert isinstance(result, Applied)
    assert (await create(native, scanner)).value == result.value
    await assert_one(native)


class Simultaneous:
    def __init__(self):
        self.count, self.ready = 0, asyncio.Event()

    async def wait(self):
        self.count += 1
        if self.count == 2:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), 5)


class CoordinatedStore(EffectStore):
    def __init__(self, factory, barrier):
        super().__init__(factory)
        self.barrier = barrier

    async def request_once(self, *args, **kwargs):
        row = await super().request_once(*args, **kwargs)
        await self.barrier.wait()
        return row


async def test_two_direct_concurrent_creators_collapse_to_one_mr(native):
    barrier, wire = Simultaneous(), native["wire"]
    effects = [
        native["fresh_effects"](store=CoordinatedStore(native["env"].factory, barrier))
        for _ in range(2)
    ]
    calls = [asyncio.create_task(create(native, effect)) for effect in effects]
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
    finally:
        wire.release.set()
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert any(isinstance(result, Applied) for result in results), results
    assert all(isinstance(result, (Applied, Unknown, StoreConflict)) for result in results)
    first, second = await create(native), await create(native)
    assert (
        isinstance(first, Applied) and isinstance(second, Applied) and first.value == second.value
    )
    await assert_one(native)


async def test_lost_response_reopens_real_database_and_adopts_without_a_second_native_post(native):
    wire, env = native["wire"], native["env"]
    wire.lose_response = True
    wire.release.set()
    assert isinstance(await create(native), Unknown)
    # A fresh engine and session factory see committed authority, not process state.
    engine = create_async_engine(env.factory.kw["bind"].url)
    try:
        recovery = native["fresh_effects"](
            factory=async_sessionmaker(engine, expire_on_commit=False)
        )
        row = await recovery.store.get(native["root"].id + ":ensure_draft")
        assert (await recovery.recover(row)).state == "adopted"
        assert isinstance(await create(native, recovery), Applied)
        await assert_one(native)
    finally:
        await engine.dispose()


async def test_late_native_visibility_never_authorizes_a_repost_even_after_deadline(native):
    wire = native["wire"]
    wire.lose_response = True
    wire.release.set()
    assert isinstance(await create(native), Unknown)
    wire.hide = True
    advance(native, 61)
    recovery = native["fresh_effects"]()
    row = await recovery.store.get(native["root"].id + ":ensure_draft")
    assert (await recovery.recover(row)).state == "unknown"
    assert wire.posts == 1
    wire.hide = False
    row = await recovery.store.get(row.key)
    assert (await recovery.recover(row)).state == "adopted"
    await assert_one(native)


async def test_reclaimed_step_cannot_duplicate_an_older_inflight_native_create(native):
    wire, env = native["wire"], native["env"]
    first = asyncio.create_task(create(native))
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        advance(native, 61)
        (replacement,) = await env.steps.claim_due("replacement", env.clock.now(), 60)
        assert replacement.epoch > native["lease"].epoch
        assert isinstance(await create(native, lease=replacement), Unknown)
        assert wire.posts == 1 and not wire.rows
    finally:
        wire.release.set()
    result = await first
    assert isinstance(result, Applied)
    assert (await create(native, lease=replacement)).value == result.value
    await assert_one(native)


async def test_changed_inputs_or_second_operation_key_cannot_create_another_draft(native):
    native["wire"].release.set()
    assert isinstance(await create(native), Applied)
    with pytest.raises(StoreConflict):
        await create(native, body="different frozen body")
    with pytest.raises(StoreConflict):
        await create(native, root="f" * 32)
    await assert_one(native)
