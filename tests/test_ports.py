"""Port contracts preserve scope, uncertainty, full recovery and manifest identity."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from forge.domain.budget import Observation, Resources
from forge.domain.candidate import CandidateBundle, CreateFile, validate_candidate_bundle
from forge.domain.plan import Plan, PlanStep, RepositoryIdentity
from forge.domain.verdicts import Committed, Conflict, Unknown
from forge.port_values import (
    Applied,
    BranchRef,
    CommentRef,
    DefinitelyAbsent,
    Found,
    GatewayKeyRef,
    Inconclusive,
    LabelState,
    LaneHandle,
    ModelReply,
    NeighborSnapshot,
    Outcome,
    PlanningSnapshot,
    ProbeResult,
    ReadUnavailable,
    RepoRef,
    ReviewRef,
    SubjectRef,
    TaskSnapshot,
)
from forge.ports import (
    ActorPort,
    BlobPort,
    BudgetGateway,
    CIPort,
    Clock,
    EffectProbe,
    ModelClient,
    Planner,
    RepoPort,
    Reviewer,
    ReviewPort,
    SubjectPort,
    WritePort,
)
from scripts.render_ports import render
from tests.fakes.ports import FixedClock, MemoryBlobs, ScriptedPorts, ScriptedProbe

SHA = "a" * 40
NOW = datetime(2026, 10, 23, tzinfo=UTC)
REPO = RepoRef(
    identity=RepositoryIdentity(provider="github", connection_id="install-1", repository_id="42"),
    full_name="owner/project",
)
SUBJECT = SubjectRef(repo=REPO, number=1)


@pytest.mark.asyncio
async def test_frozen_identity_and_read_scope() -> None:
    explicit = NeighborSnapshot(repo=REPO, sha=SHA, allowed_globs=())
    snapshot = PlanningSnapshot(primary=explicit)
    assert snapshot.primary.allowed_globs == () and snapshot.neighbors == ()
    assert snapshot.primary.repo.identity == REPO.identity
    for pattern in (
        "../private/*",
        "/private/*",
        "src/../private/*",
        "src\\private/*",
        "src/\x00*",
    ):
        with pytest.raises(ValidationError):
            NeighborSnapshot(repo=REPO, sha=SHA, allowed_globs=(pattern,))
    with pytest.raises(ValidationError):
        PlanningSnapshot(primary=explicit, neighbors=(explicit,))
    with pytest.raises(ValidationError):
        setattr(explicit, "sha", "b" * 40)
    port: RepoPort = ScriptedPorts((("read_blob", b"frozen"), ("head", None)))
    assert await port.read_blob(REPO, SHA, "src/a.py") == b"frozen"
    assert await port.head(REPO, "absent") is None
    assert isinstance(port, ScriptedPorts)
    assert port.calls == [("read_blob", (REPO, SHA, "src/a.py")), ("head", (REPO, "absent"))]
    with pytest.raises(ValidationError):
        RepoRef.model_validate(
            {"identity": REPO.identity, "full_name": "name", "credential": "forbidden"}
        )


@pytest.mark.asyncio
async def test_recovery_preserves_complete_outcome_and_uncertainty() -> None:
    native = '{"request":"r1","parent":"' + SHA + '"}'
    outcomes: tuple[Outcome, ...] = (
        Committed(sha="b" * 40, evidence=native),
        Conflict(actual_head="c" * 40, evidence=native),
        Unknown(evidence=native),
        Applied[CommentRef](
            value=CommentRef(subject=ReviewRef(repo=REPO, number=2), native_id="3"), evidence=native
        ),
        Applied[BranchRef](
            value=BranchRef(repo=REPO, branch="forge/run", sha=SHA), evidence=native
        ),
        Applied[ReviewRef](value=ReviewRef(repo=REPO, number=2), evidence=native),
        Applied[LabelState](
            value=LabelState(subject=SUBJECT, label="forge", present=False), evidence=native
        ),
        Applied[LaneHandle](
            value=LaneHandle(repo=REPO, native_id="9", attempt=2, correlation_id="nonce"),
            evidence=native,
        ),
        Applied[GatewayKeyRef](
            value=GatewayKeyRef(
                alias="run/1", model="model", allowance=Resources(), expires_at=NOW, blocked=True
            ),
            evidence=native,
        ),
    )
    adapter: TypeAdapter[ProbeResult[Outcome]] = TypeAdapter(ProbeResult[Outcome])
    for outcome in outcomes:
        found = Found[Outcome](value=outcome, evidence="recovered")
        restored = adapter.validate_json(adapter.dump_json(found))
        assert isinstance(restored, Found) and restored.value == outcome
        assert type(restored.value) is type(outcome)
        assert restored.value.evidence == native
        callback: EffectProbe[Outcome] = ScriptedProbe[Outcome](restored)
        assert await callback() == restored
    absent = DefinitelyAbsent(evidence="authoritative absence")
    inconclusive = Inconclusive(reason="settling", evidence="eventually consistent negative")
    assert adapter.validate_json(adapter.dump_json(absent)) == absent
    assert adapter.validate_json(adapter.dump_json(inconclusive)) == inconclusive
    provider: RepoPort = ScriptedPorts((("read_blob", ReadUnavailable("timeout", "request-r2")),))
    with pytest.raises(ReadUnavailable, match="timeout") as failure:
        await provider.read_blob(REPO, SHA, "src/a.py")
    assert failure.value.evidence == "request-r2"


@pytest.mark.asyncio
async def test_every_protocol_has_a_deterministic_fake() -> None:
    # These assignments are also checked by strict mypy, not just attribute inspection.
    scripted = ScriptedPorts()
    subject: SubjectPort = scripted
    repo: RepoPort = scripted
    review: ReviewPort = scripted
    ci: CIPort = scripted
    actor: ActorPort = scripted
    writer: WritePort = scripted
    planner: Planner = scripted
    reviewer: Reviewer = scripted
    gateway: BudgetGateway = scripted
    model: ModelClient = scripted
    clock: Clock = FixedClock(NOW)
    blobs: BlobPort = MemoryBlobs()
    assert all((subject, repo, review, ci, actor, writer, planner, reviewer, gateway, model))
    with pytest.raises(AssertionError, match="unscripted call: post_comment"):
        await writer.post_comment(SUBJECT, "body", "marker")
    candidate = validate_candidate_bundle(
        CandidateBundle(
            attempt_base_oid=SHA,
            driver_exit="success",
            entries=(CreateFile("tool.sh", "echo ok\n", mode="100755"),),
        ),
        base_contents={},
        branch="forge/run",
        commit_message="Validated change",
    )
    result = Committed(sha="b" * 40, evidence="native-cas")
    scripted.script.append(("commit", result))
    assert await writer.commit(REPO, candidate, SHA, "marker") == result
    passed = scripted.calls[-1][1][1]
    assert passed is candidate and candidate.manifest[0].mode == "100755"
    plan = Plan(
        summary="Inspect frozen source",
        steps=(PlanStep(step_id="read", title="Read", instructions="Read the allowed path."),),
    )
    scripted.script.append(("plan", plan))
    snapshot = PlanningSnapshot(primary=NeighborSnapshot(repo=REPO, sha=SHA))
    task = TaskSnapshot(subject=SUBJECT, title="Task", description="Body", version="1")
    assert await planner.plan(task, snapshot) is plan
    unknown_usage = Observation(usd=None, calls=None, tokens=None)
    scripted.script.append(("read_spend", unknown_usage))
    assert await gateway.read_spend("run/1") == unknown_usage and not unknown_usage.complete
    scripted.script.append(
        ("complete", ModelReply(text="answer", usage=unknown_usage, evidence="local"))
    )
    reply = await model.complete(
        model="test", system="scope", prompt="read", credential_alias="run/1", max_output_tokens=64
    )
    assert reply.text == "answer" and reply.usage == unknown_usage
    assert clock.now() == NOW
    digest = await blobs.put(b"exact\x00bytes")
    assert await blobs.get(digest) == b"exact\x00bytes"
    assert await blobs.get("0" * 64) is None
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs/reference/ports.md").read_text() == render()
