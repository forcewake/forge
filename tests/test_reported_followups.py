"""Regression tests for the second end-to-end verification."""

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path

import pytest

from forge.command_values import CommandResult, blocked_comment, human_status, render
from forge_protocol import canonical_json
from forge.engine.dispatch import startup_grace_seconds
from forge.engine.effects import probe_delay, probe_due
from forge.engine.model_services import planning_window
from forge.planning.planner import SYSTEM, decode_reply
from forge.store.commands import command_retry_budget
from forge.store.records import EffectRecord


def test_reconcile_reply_names_effects_in_prose():
    result = CommandResult(
        status="observed",
        reason="effects_observed",
        run_id="a" * 32,
        data_json=canonical_json({"effects": [{"key": "run:commit", "state": "unknown"}]}).decode(),
    )
    text = render(result)
    assert "run:commit" in text and "unknown" in text
    assert "{" not in text and "effects_observed" not in text


def test_correction_headline_differs_from_the_original_implementation():
    from forge.engine.publish import commit_headline

    assert commit_headline("Parser", correction=False) == "Implement Parser"
    assert commit_headline("Parser", correction=True) == "Correct Parser"


def test_provider_credential_env_and_lane_parallelism_are_configurable(monkeypatch):
    from forge.budget.gateway import parallel_request_limit
    from tools.litellm.bootstrap import provider_credential_env

    assert provider_credential_env({}) == "ANTHROPIC_API_KEY"
    assert provider_credential_env({"FORGE_GATEWAY_PROVIDER_KEY_ENV": "ZAI_API_KEY"}) == (
        "ZAI_API_KEY"
    )
    with pytest.raises(ValueError):
        provider_credential_env({"FORGE_GATEWAY_PROVIDER_KEY_ENV": "lower"})
    monkeypatch.delenv("FORGE_GATEWAY_MAX_PARALLEL_REQUESTS", raising=False)
    assert parallel_request_limit() > 1
    monkeypatch.setenv("FORGE_GATEWAY_MAX_PARALLEL_REQUESTS", "8")
    assert parallel_request_limit() == 8


def test_planner_call_budget_follows_the_allowance_and_reasks_are_configurable(monkeypatch):
    from forge.engine.model_services import planner_reasks, planning_model_calls

    assert planning_model_calls(6, 4) == 6
    assert planning_model_calls(0, 4) == 4
    assert planning_model_calls(100, 4) == 32
    assert planner_reasks(2) == 2
    monkeypatch.setenv("FORGE_PLANNER_REASKS", "5")
    assert planner_reasks(2) == 5


def test_planner_accepts_fences_trailing_text_and_a_slash_path():
    from forge.domain.plan import _repo_path
    from forge.planning.planner import decode_reply, extract_model_json

    fenced = '```json\n{"kind":"blocked","question":"Which file?"}\n```\n</invoke_result>'
    assert extract_model_json(fenced).startswith("{")
    assert decode_reply(fenced, 100_000).kind == "blocked"
    assert _repo_path("src/") == "src"
    assert "pytest and ruff are not installed" in SYSTEM
    assert "may run pytest" not in SYSTEM
    assert "git" in SYSTEM


def test_usd_quantizes_float_noise_and_renders_plainly():
    from decimal import Decimal

    from forge.budget.gateway import quantize_usd
    from forge.domain.budget import Pool, Reservation, Resources

    noisy = Decimal("0.001543900000000001")
    assert quantize_usd(noisy) == Decimal("0.001543")
    assert quantize_usd(Decimal("18.358456100000002")) == quantize_usd(
        Decimal("18.358456099999998")
    )
    assert Resources(usd=Decimal("20")).model_dump(mode="json")["usd"] == "20"
    pool = Pool(
        identity="run",
        capacity=Resources(usd=Decimal("5")),
        reservations=(
            Reservation(
                identity="implementation:1:1",
                requested=Resources(usd=Decimal("5")),
                allowance=Resources(usd=Decimal("5")),
                minimum=Resources(usd=Decimal("1")),
                status="unknown",
            ),
        ),
    )
    released = pool.resolve_unknown()
    assert released.balance.available.usd == Decimal("5")
    assert released.reservations[0].status == "settled"


def test_planner_accepts_a_long_read_and_states_the_line_limit():
    reply = json.dumps(
        {
            "kind": "tools",
            "calls": [
                {
                    "tool": "read_file",
                    "repo": "primary",
                    "path": "src/app.py",
                    "line_count": 200,
                }
            ],
        }
    )
    decoded = decode_reply(reply, 100_000)
    assert decoded.kind == "tools" and decoded.calls[0].line_count == 200
    assert "100" in SYSTEM


def test_retry_clears_an_expired_planning_window():
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = created + timedelta(hours=2)
    assert planning_window(created, None, 600, now) == created + timedelta(seconds=600)
    renewed = now + timedelta(seconds=600)
    assert planning_window(created, renewed, 600, now) == renewed
    assert planning_window(now, None, 600, now) == now + timedelta(seconds=600)


def test_unknown_effect_waits_out_backoff_and_rate_limit():
    assert probe_delay(1, None, "ab") >= 5
    assert probe_delay(4, None, "ab") >= 40
    assert probe_delay(1, 90, "ab") >= 90
    later = datetime.now(timezone.utc) + timedelta(seconds=30)
    record = EffectRecord(
        id="ab",
        key="effect",
        kind="post_comment",
        run_id=None,
        step_id=None,
        lease_epoch=1,
        payload_digest="a" * 64,
        payload_json="{}",
        state="unknown",
        version=2,
        outcome_json=json.dumps(
            {"kind": "unknown", "evidence": json.dumps({"next_probe_at": later.isoformat()})}
        ),
        observations=(),
        repo_key=None,
        head_branch=None,
        dispatched_at=None,
        dispatch_deadline_at=None,
    )
    assert probe_due(record, datetime.now(timezone.utc)) is False
    assert probe_due(record, later + timedelta(seconds=1)) is True


def test_why_blocked_is_prose_and_a_block_names_the_reason():
    snapshot = json.dumps(
        {
            "state": "blocked",
            "reason": "planning_deadline_expired",
            "failed_handler": "plan",
            "run_version": 4,
        }
    )
    text = human_status(snapshot)
    assert "State: blocked." in text
    assert human_status("not-json") == "The current status could not be read."
    assert human_status("[]") == "The current status could not be read."
    assert "planning deadline expired" in text
    assert "run_version" not in text
    assert "{" not in text
    comment = blocked_comment("invalid_planner_output")
    assert "invalid planner output" in comment and "/why-blocked" in comment and "/retry" in comment
    raw = canonical_json(json.loads(snapshot)).decode()
    body = render(
        CommandResult(
            status="observed",
            reason="planning_deadline_expired",
            run_id="a" * 32,
            data_json=raw,
        )
    )
    assert "State: blocked." in body and "run_version" not in body and "{" not in body


def test_command_retry_budget_outlasts_a_fifteen_second_blip(monkeypatch):
    monkeypatch.delenv("FORGE_COMMAND_RETRY_ATTEMPTS", raising=False)
    monkeypatch.delenv("FORGE_COMMAND_RETRY_BASE_SECONDS", raising=False)
    monkeypatch.delenv("FORGE_COMMAND_RETRY_CAP_SECONDS", raising=False)
    attempts, base, cap = command_retry_budget()
    assert attempts >= 8 and base >= 5 and cap >= 60
    waits = [min(cap, base * (2 ** (n - 1))) for n in range(1, attempts)]
    assert sum(waits) > 15


def test_lane_startup_grace_rejects_a_non_numeric_value(monkeypatch):
    monkeypatch.setenv("FORGE_LANE_STARTUP_GRACE_SECONDS", "nope")
    assert startup_grace_seconds() == 600


def test_lane_startup_grace_defaults_above_two_minutes(monkeypatch):
    monkeypatch.delenv("FORGE_LANE_STARTUP_GRACE_SECONDS", raising=False)
    assert startup_grace_seconds() >= 600
    monkeypatch.setenv("FORGE_LANE_STARTUP_GRACE_SECONDS", "900")
    assert startup_grace_seconds() == 900


async def test_worker_logs_a_failed_tick(caplog):
    import asyncio
    from types import SimpleNamespace

    from forge.runtime.worker import serve

    class Boom:
        def __init__(self):
            self.calls = 0

        async def tick(self, limit=20):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("github read failed")
            return 0

    class Quiet:
        async def tick(self):
            return 0

    class Engine:
        async def tick(self):
            stop.set()
            return SimpleNamespace(
                execution=SimpleNamespace(claimed=1), recovery=SimpleNamespace(next_delay=None)
            )

    class Listen:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Wake:
        def listen(self, url):
            return Listen()

    stop = asyncio.Event()
    runtime = SimpleNamespace(
        engine=SimpleNamespace(
            url=SimpleNamespace(render_as_string=lambda hide_password: "sqlite://")
        ),
        wakeups=Wake(),
        reactive=Quiet(),
        commands=Boom(),
        worker=Engine(),
        lane=SimpleNamespace(reap_terminal_blobs=_async_zero),
    )
    with caplog.at_level(logging.ERROR, logger="forge.worker"):
        await serve(runtime, stop)
    assert any(
        record.exc_info and "github read failed" in str(record.exc_info[1])
        for record in caplog.records
    )


async def _async_zero():
    return 0


def test_schema_cli_refuses_a_password_on_the_command_line(tmp_path, capsys):
    from forge.store.schema.__main__ import main

    url = "postgresql+asyncpg://forge:secret-pass@127.0.0.1/forge"
    assert main(["check", "--database-url", url]) == 1
    err = capsys.readouterr().err
    assert "secret-pass" not in err
    assert "DATABASE_URL" in err or "url-file" in err
    path = tmp_path / "url"
    path.write_text("sqlite+aiosqlite:///" + str(tmp_path / "from-file.db"))
    assert main(["check", "--url-file", str(path)]) == 1


def test_guard_compares_expiry_in_utc():
    source = Path("tools/litellm/guard_ledger.py").read_text()
    assert "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'" in source
    assert "expires>CURRENT_TIMESTAMP " not in source


async def test_a_refused_hold_is_settled_at_zero_cost():
    from decimal import Decimal

    from tools.litellm.guard_ledger import NativeLedger

    seen: dict[str, object] = {}

    async def settle(token, request_id, cost, calls, tokens):
        seen.update(token=token, request_id=request_id, cost=cost, calls=calls, tokens=tokens)

    ledger = NativeLedger(None)
    ledger.settle = settle  # type: ignore[method-assign]
    await ledger.release("a" * 64, "b" * 32)
    assert seen == {
        "token": "a" * 64,
        "request_id": "b" * 32,
        "cost": Decimal(0),
        "calls": 0,
        "tokens": 0,
    }


def test_model_request_omits_temperature_by_default():
    source = Path("src/forge/budget/server_models.py").read_text()
    assert '"temperature": 0' not in source


@pytest.mark.skipif(not os.environ.get("FORGE_PG_TEST_URL"), reason="PostgreSQL is not configured")
async def test_guard_accepts_a_utc_expiry_when_the_session_zone_is_amsterdam():
    from decimal import Decimal

    from tools.litellm.guard_ledger import NativeLedger

    class Conn:
        def __init__(self, raw):
            self.raw = raw

        async def execute_raw(self, sql, *args):
            await self.raw.execute(sql, *args)

        async def query_raw(self, sql, *args):
            rows = await self.raw.fetch(sql, *args)
            return [dict(row) for row in rows]

        def tx(self):
            return _Tx(self.raw)

    class _Tx:
        def __init__(self, raw):
            self.raw = raw
            self._tx = None

        async def __aenter__(self):
            self._tx = self.raw.transaction()
            await self._tx.start()
            return self

        async def __aexit__(self, *args):
            if args[0] is None:
                await self._tx.commit()
            else:
                await self._tx.rollback()

        async def execute_raw(self, sql, *args):
            await self.raw.execute(sql, *args)

        async def query_raw(self, sql, *args):
            rows = await self.raw.fetch(sql, *args)
            return [dict(row) for row in rows]

    import asyncpg

    raw = await asyncpg.connect(
        os.environ["FORGE_PG_TEST_URL"].replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        await raw.execute("SET TIME ZONE 'Europe/Amsterdam'")
        await raw.execute(
            'CREATE TEMP TABLE "LiteLLM_VerificationToken" (token TEXT, key_alias TEXT, blocked BOOLEAN, expires TIMESTAMP, max_budget DOUBLE PRECISION)'
        )
        expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
        await raw.execute(
            'INSERT INTO "LiteLLM_VerificationToken" VALUES ($1,$2,FALSE,$3,$4)',
            "a" * 64,
            "alias",
            expiry.replace(tzinfo=None),
            10.0,
        )
        ledger = NativeLedger(Conn(raw))
        await ledger.initialize()
        await ledger.reserve("a" * 64, "alias", "b" * 32, Decimal("10"), Decimal("1"))
    finally:
        await raw.close()
