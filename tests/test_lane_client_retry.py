"""Keyed lane writes survive one transient gateway or Cloudflare failure."""

from datetime import datetime, timezone
from hashlib import sha256

import httpx

from forge_lane.client import LaneClient
from forge_lane.errors import LaneFailure
from forge_protocol import Heartbeat, LaneDispatch, LaneReport


def dispatch() -> LaneDispatch:
    return LaneDispatch(
        forge_run_id="run-1",
        attempt=1,
        generation=1,
        server_url="https://forge.example",
        provider="gitlab",
        repository="group/project",
        ref="forge/run",
        base_sha="b" * 40,
        approved_digest="c" * 64,
        driver="claude-code",
        model="glm",
        timeout_seconds=30,
    )


def client(handler) -> tuple[LaneClient, list[str]]:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return handler(len(seen), request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    lane = LaneClient(dispatch(), http)

    async def immediate(_seconds: float) -> None:
        return None

    lane._sleep = immediate
    return lane, seen


async def test_heartbeat_retries_a_cloudflare_520():
    beat = Heartbeat(
        forge_run_id="run-1",
        attempt=1,
        generation=1,
        sequence=1,
        observed_at=datetime.now(timezone.utc),
        phase="running",
    )
    body = beat.model_dump(mode="json")

    def handler(count, _request):
        if count == 1:
            return httpx.Response(520)
        return httpx.Response(200, json=body)

    lane, seen = client(handler)
    await lane.heartbeat(beat)
    assert seen == ["/lane/v1/heartbeat", "/lane/v1/heartbeat"]
    await lane.http.aclose()


async def test_blob_upload_retries_a_transport_failure():
    def handler(count, _request):
        if count == 1:
            return httpx.Response(503)
        return httpx.Response(204)

    lane, seen = client(handler)
    assert await lane.put_blob(b"payload") == sha256(b"payload").hexdigest()
    assert len(seen) == 2
    await lane.http.aclose()


async def test_report_does_not_retry_a_client_error():
    report = LaneReport(
        forge_run_id="run-1",
        attempt=1,
        generation=1,
        approved_digest="c" * 64,
        status="failed",
        classification="empty_diff",
        driver="claude-code",
        model="glm",
        terminal_reason="empty diff",
    )

    def handler(_count, _request):
        return httpx.Response(400)

    lane, seen = client(handler)
    try:
        await lane.report(report)
    except LaneFailure as exc:
        assert "HTTP 400" in exc.detail
    else:
        raise AssertionError("a client error was retried into success")
    assert seen == ["/lane/v1/report"]
    await lane.http.aclose()


async def test_model_key_is_not_retried():
    def handler(_count, _request):
        return httpx.Response(503)

    lane, seen = client(handler)
    try:
        await lane.model_key()
    except LaneFailure as exc:
        assert "HTTP 503" in exc.detail
    else:
        raise AssertionError("a model grant request was retried")
    assert seen == ["/lane/v1/model-key"]
    await lane.http.aclose()
