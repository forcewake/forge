"""Bounded transport, closed native failure classes and proof-free negatives."""

import httpx
import pytest

from forge.port_values import Inconclusive, ReadUnavailable
from forge.providers.gitlab.ci import failure_kind
from forge.providers.gitlab.transport import Endpoint, Failure, Transport
from tests import gitlab_engine_fixture as f


@pytest.mark.parametrize(
    "url",
    [
        "http://ce.example",
        "https://user:secret@ce.example",
        "https://ce.example/a/../b",
        "https://ce.example/%2e%2e",
        "https://ce.example?token=x",
        "https://ce.example/#frag",
    ],
)
def test_endpoint_requires_explicit_https_origin_without_authority_rewriting(url):
    with pytest.raises(ValueError):
        Endpoint(url)


@pytest.mark.parametrize(
    "body,headers,limit,reason",
    [
        (b'{"id":1,"id":2}', {}, 100, "invalid_json"),
        (b'{"value":NaN}', {}, 100, "invalid_json"),
        (b"x" * 101, {}, 100, "response_too_large"),
        (b"", {"content-encoding": "gzip"}, 100, "encoded_response_refused"),
        (b"[]", {"x-next-page": "2https://evil.example"}, 100, "invalid_pagination"),
    ],
)
async def test_json_and_stream_bounds_refuse_ambiguous_or_compressed_responses(
    body, headers, limit, reason
):
    transport = Transport(
        Endpoint("https://ce.example"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body, headers=headers)),
    )
    try:
        with pytest.raises(Failure) as caught:
            await transport.request("GET", "/projects/123", "synthetic-secret", limit=limit)
        assert caught.value.reason == reason
    finally:
        await transport.close()


async def test_request_id_echo_cannot_leak_the_explicit_credential():
    transport = Transport(
        Endpoint("https://ce.example"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, headers={"x-request-id": "synthetic-secret"})
        ),
    )
    try:
        with pytest.raises(Failure) as caught:
            await transport.request("GET", "/projects/123", "synthetic-secret")
        assert "synthetic-secret" not in caught.value.evidence and caught.value.request_id == ""
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("runner_system_failure", "infrastructure"),
        ("api_failure", "infrastructure"),
        ("runner_configuration_error", "configuration"),
        ("unmet_prerequisites", "configuration"),
        ("script_failure", "unknown"),
        ("unknown_failure", "unknown"),
        (None, "unknown"),
    ],
)
def test_failure_reason_is_native_evidence_and_generic_script_failure_is_not_a_code_fault(
    reason, expected
):
    assert failure_kind({"status": "failed", "failure_reason": reason}) == expected
    assert failure_kind({"status": "success", "failure_reason": reason}) == "unknown"


async def test_dispatch_ambiguity_and_old_generation_payload_are_inconclusive():
    wire = f.Wire()
    api, _, _, _, ci, _ = f.adapters(wire)
    try:
        wire.routes["pipelines/700/variables"][1]["value"] = f.DISPATCH.model_copy(
            update={"generation": 1}
        ).model_dump_json()
        assert isinstance(await ci.probe_dispatch(f.REPO, f.DISPATCH.nonce), Inconclusive)
        wire.routes["pipelines/700/variables"] = f.variables()
        wire.routes["pipelines"].append(f.pipeline(800))
        wire.routes["pipelines/800"] = f.pipeline(800)
        wire.routes["pipelines/800/variables"] = f.variables()
        assert isinstance(await ci.probe_dispatch(f.REPO, f.DISPATCH.nonce), Inconclusive)
    finally:
        await api.transport.close()


async def test_foreign_native_pipeline_job_or_ci_configuration_never_becomes_verification():
    wire = f.Wire()
    api, _, _, _, ci, _ = f.adapters(wire)
    try:
        wire.metadata["ci_config_path"] = "https://elsewhere.example/ci.yml"
        with pytest.raises(ReadUnavailable):
            await ci.checks_for(f.REPO, f.SHA)
        wire.metadata["ci_config_path"] = None
        wire.routes["pipelines/700"]["project_id"] = 999
        with pytest.raises(ReadUnavailable):
            await ci.checks_for(f.REPO, f.SHA)
    finally:
        await api.transport.close()
