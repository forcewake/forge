"""Tests for the thin LiteLLM HTTP client and the ADR-0013 usage ledger."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import LLMCall
from forge.factory.llm import (
    LLMClient,
    LLMError,
    LLMResponseError,
    parse_json,
    truncate_chars,
)
from forge.models.base import Base


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def make_client(db) -> LLMClient:
    return LLMClient(settings=_settings(), session_factory=db)


def _settings():
    from forge.config import Settings

    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        LITELLM_URL="http://litellm.test",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )


def completion(text: str, prompt_tokens=None, completion_tokens=None) -> dict:
    usage = {}
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage["completion_tokens"] = completion_tokens
    body = {"choices": [{"message": {"content": text}}]}
    if usage:
        body["usage"] = usage
    return body


async def llm_rows(db) -> list[LLMCall]:
    async with db() as session:
        return (await session.execute(select(LLMCall).order_by(LLMCall.id))).scalars().all()


class TestComplete:
    async def test_ok_call_journals_tokens_and_role(self, db, httpx_mock):
        httpx_mock.add_response(json=completion("hello", prompt_tokens=12, completion_tokens=34))
        client = make_client(db)
        result = await client.complete(
            tier="strong",
            system="be brief",
            user="hi",
            role="planner",
            flow_run_id="run-123",
        )
        await client.close()

        assert result.text == "hello"
        assert result.input_tokens == 12
        assert result.output_tokens == 34

        # The wire request names the proxy tier (bare name over raw HTTP).
        (request,) = httpx_mock.get_requests()
        import json as _json

        assert _json.loads(request.content)["model"] == "strong"
        assert request.url.path == "/v1/chat/completions"

        (row,) = await llm_rows(db)
        assert row.role == "planner"
        assert row.provider == "litellm-proxy"
        assert row.model == "strong"  # the tier as configured, not the wire name
        assert row.status == "ok"
        assert row.input_tokens == 12
        assert row.output_tokens == 34
        assert row.duration_ms is not None
        assert row.error is None

    async def test_http_error_journals_failed_row_and_raises(self, db, httpx_mock):
        httpx_mock.add_response(status_code=500, text="boom")
        client = make_client(db)
        with pytest.raises(LLMError):
            await client.complete(
                tier="code", system="s", user="u", role="implementer", flow_run_id="r1"
            )
        await client.close()

        (row,) = await llm_rows(db)
        assert row.status == "failed"
        assert row.role == "implementer"
        assert "500" in row.error
        # Unknown usage stays NULL — never zero (ADR-0013).
        assert row.input_tokens is None
        assert row.output_tokens is None

    async def test_unknown_usage_is_null_not_zero(self, db, httpx_mock):
        httpx_mock.add_response(json=completion("no usage block"))
        client = make_client(db)
        result = await client.complete(
            tier="fast", system="s", user="u", role="reviewer", flow_run_id="r1"
        )
        await client.close()

        assert result.input_tokens is None
        assert result.output_tokens is None
        (row,) = await llm_rows(db)
        assert row.status == "ok"
        assert row.input_tokens is None
        assert row.output_tokens is None

    async def test_json_mode_400_retries_without_response_format(self, db, httpx_mock):
        httpx_mock.add_response(status_code=400, text="response_format not supported")
        httpx_mock.add_response(json=completion('{"ok": true}'))
        client = make_client(db)
        result = await client.complete(
            tier="strong",
            system="be brief",
            user="u",
            role="reviewer",
            flow_run_id="r1",
            json_mode=True,
        )
        await client.close()

        assert '"ok": true' in result.text.replace(" ", " ").replace(": ", ": ")
        first, second = httpx_mock.get_requests()
        assert b"response_format" in first.content
        assert b"response_format" not in second.content
        # The fallback asks for raw JSON in the system prompt instead.
        assert b"Respond with ONLY a JSON object" in second.content

        # Both HTTP attempts, one ledger row: the successful call.
        (row,) = await llm_rows(db)
        assert row.status == "ok"

    async def test_json_mode_invalid_json_fails_row_and_raises(self, db, httpx_mock):
        httpx_mock.add_response(json=completion("this is not json at all"))
        client = make_client(db)
        with pytest.raises(LLMResponseError):
            await client.complete(
                tier="strong",
                system="s",
                user="u",
                role="planner",
                flow_run_id="r1",
                json_mode=True,
            )
        await client.close()

        (row,) = await llm_rows(db)
        assert row.status == "failed"
        assert "invalid_json" in row.error

    async def test_malformed_body_journals_failed(self, db, httpx_mock):
        httpx_mock.add_response(json={"unexpected": "shape"})
        client = make_client(db)
        with pytest.raises(LLMError):
            await client.complete(
                tier="fast", system="s", user="u", role="planner", flow_run_id=None
            )
        await client.close()

        (row,) = await llm_rows(db)
        assert row.status == "failed"
        assert row.flow_run_id is None


class TestParseJson:
    def test_plain_object(self):
        assert parse_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        text = '```json\n{"a": 1, "b": [2, 3]}\n```'
        assert parse_json(text) == {"a": 1, "b": [2, 3]}

    def test_prose_around_object(self):
        text = 'Sure! Here is the plan:\n{"summary": "do it", "n": 2}\nHope that helps.'
        assert parse_json(text) == {"summary": "do it", "n": 2}

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        text = '{"code": "x = {1: {a}}", "n": 1}'
        assert parse_json(text) == {"code": "x = {1: {a}}", "n": 1}

    def test_escaped_quotes_inside_strings(self):
        text = '{"text": "said \\"hi\\" { ok", "n": 1}'
        assert parse_json(text)["n"] == 1

    def test_no_object_raises(self):
        with pytest.raises(LLMResponseError):
            parse_json("no json here")

    def test_unbalanced_object_raises(self):
        with pytest.raises(LLMResponseError):
            parse_json('{"a": 1')

    def test_non_object_json_raises(self):
        with pytest.raises(LLMResponseError):
            parse_json("[1, 2, 3]")

    def test_invalid_json_raises(self):
        with pytest.raises(LLMResponseError):
            parse_json('{"a": not-json}')


class TestTruncateChars:
    def test_short_text_untouched(self):
        assert truncate_chars("abc", 10) == "abc"

    def test_long_text_cut_deterministically(self):
        assert truncate_chars("abcdef", 3) == "abc"

    def test_zero_limit_empty(self):
        assert truncate_chars("abc", 0) == ""


class TestReviewWithRetry:
    """The reviewer model is stochastic (LIVE: prose instead of JSON on 2 of
    4 calls blocked otherwise-green runs) — one bounded re-ask, then raise."""

    def _verdict_json(self) -> str:
        import json

        return json.dumps({"verdict": "ok", "summary": "clean", "findings": []})

    async def _run(self, fake_llm, parse=None):
        from forge.factory.reviewer import LLMReviewer, review_with_retry

        return await review_with_retry(
            fake_llm,
            system="sys",
            user="user",
            parse=parse or LLMReviewer._parse,
        )

    async def test_unparseable_review_is_reasked_once(self, db):
        from tests.fixtures.fake_llm import FakeLLM

        llm = FakeLLM(script=["Sorry, I cannot comply.", self._verdict_json()])
        verdict = await self._run(llm)

        assert verdict.verdict == "ok"
        assert len(llm.calls) == 2  # both attempts journaled

    async def test_both_attempts_unparseable_raises(self, db):
        from tests.fixtures.fake_llm import FakeLLM

        llm = FakeLLM(script=["nope", "still nope"])
        with pytest.raises(LLMResponseError):
            await self._run(llm)
        assert len(llm.calls) == 2  # bounded, not an infinite loop

    async def test_first_call_clean_makes_exactly_one_call(self, db):
        from tests.fixtures.fake_llm import FakeLLM

        llm = FakeLLM(script=[self._verdict_json()])
        verdict = await self._run(llm)
        assert verdict.verdict == "ok"
        assert len(llm.calls) == 1
