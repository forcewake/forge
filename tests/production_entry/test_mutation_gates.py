"""R40-08 (#344) — the REQUIRED composed-trace set and its mutation arms.

CI greenness proved nothing about the four high-risk invariants below:
a service-level test can pass a correct grant explicitly, invoke the
reconciler directly, or use a reviewer double that ignores the budget —
it stays green while the INSTALLED command cannot reach the feature or
its guard is unchanged. Each trace here drives the same entry points a
customer invokes (real ASGI ingress, the installed worker/reconciler
loops, the real dispatch leg, the real guard over a real recording
HTTP transport) and then runs the SAME event sequence a second time
with exactly ONE mutation seeded — a patch applied to the SHIPPED
symbol the way a regression would reintroduce the defect. The mutation
arm asserts the DEFECT's observable: the baseline trace's assertions
would fail under it, which is precisely what makes the set a detector.

The four invariants (one per high-risk seam):

- **MG-A — feedback ingress** (``/fix``//``ask`` → inbox → worker →
  reconciler): already pinned by ``test_feedback_ingress.py`` — FI-1
  (the composed ASGI→inbox→restarted-worker→installed-reconciler
  trace) with FI-5's two registration-revert arms (parser off;
  correction pass neutralized). This module does not duplicate it; the
  pg-gate manifest and the docs enumerate it beside the three below.
- **MG-1 — partial-liability admission** (#339): a run whose durable
  receipts carry a settled FINAL figure AND a PARTIAL with a streamed
  subtotal inside a retained envelope admits the closing decision on
  FINALITY — settled + retained liability, never a lower bound and
  never the partial's subtotal (the P01 counterexample: cap 10,
  settled 8, partial 0.5 inside an envelope of 3 → bounded exposure
  11, the review does NOT fit). Mutation: the finality classification
  swapped back to VALUE PRESENCE (a row with any cost settles; only
  costless rows reserve) — the old math "fits" at 8.5 and the run
  wrongly stays reviewable.
- **MG-2 — guarded review amendment** (#340 / AT-04): the reviewer is
  the REAL ``LLMReviewer`` over the REAL ``LLMClient`` against a
  recording HTTP endpoint, guarded by the ACTUAL ``BudgetGuard``; a
  calls-axis amendment through ``continue_review_only`` re-opens the
  exhausted guard and EXACTLY ONE reviewer call reaches the provider.
  Mutation: the amendment reverted to EVIDENCE-ONLY (the recorded
  #340 defect — amount/reason recorded, the enforcing limits row
  untouched): the shipped pre-review capacity check still refuses and
  NOTHING reaches the provider — the reviewer double cannot bypass
  the guard.
- **MG-3 — grant persistence under concurrent evidence** (#341): the
  redemption-mode dispatch persists its operation grant (keyed
  authority row + evidence projection) while a CONCURRENT evidence
  write (a checkpoint landing) is in flight; BOTH survive. Mutation:
  the projection writer reverted to the WHOLE-DOCUMENT overwrite (the
  recorded R40-05/P02 loss) — the concurrent key is silently dropped.

Every mutation arm patches the symbol at TEST TIME (``monkeypatch``
on the shipped module attribute, resolved by ``getattr`` so a moved
seam fails loudly instead of silently not patching) — no source file
is edited.

This module is env-clean once (the module-scoped scrub).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.durable import (
    AXIS_CALLS,
    FlowStatus,
    ingest_usage_receipt,
)

from .conftest import (
    GL_BASE_BRANCH,
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    GL_PROJECT_ID,
    PE_HARNESS_MODEL,
    PE_LANE_SECRET,
    PE_LEGACY_DEADLINE,
    gl_settings,
)
from .test_credential_dispatch import (
    REF_V1,
    _bound_lab,
    _start_run,
    drive_go,
    get_run_evidence,
)
from .test_production_entry import (
    make_checkout,
    run_collector,
    run_vendor_once,
)
from .test_review_feedback import get_run

pytestmark = pytest.mark.production_entry

#: The operator's native command identity for the amendment (the inbox
#: ``source_event_id`` shape — ``run:<command>:<project>:<note id>``).
AMEND_COMMAND = "run:continue_review:42:9501"

#: The first runner's work (same shape the feedback traces use).
RUNNER_ACTIONS = [
    {"op": "write", "path": "src/app.py", "content": "def check(email):\n    return bool(email)\n"},
]

#: The evidence key a concurrent checkpoint-shaped writer merges while
#: the grant projection is in flight (MG-3).
CONCURRENT_EVIDENCE_KEY = "checkpoint_probe"
CONCURRENT_EVIDENCE_VALUE = {"slot": "wip", "bytes": 4096}

#: Where the traces write their machine-readable records when the gate
#: asks for them (unset: fully hermetic, nothing is written).
TRACE_RECORD_DIR_ENV = "FORGE_TRACE_RECORD_DIR"
#: The release-artifact digest when the run executes against a built
#: artifact (the canary's spelling) — distinguishing source main from
#: the artifact under test in the record.
RELEASE_ARTIFACT_SHA_ENV = "FORGE_RELEASE_ARTIFACT_SHA256"

TRACE_RECORD_SCHEMA = "forge.trace-record/1"
_THIS_FILE = Path(__file__).resolve()

#: Stashed at IMPORT time — the module-scoped env scrub below removes
#: every FORGE_* variable before the traces run, and the records are
#: written at teardown while the scrub is still in force.
_RECORD_DIR = os.environ.get(TRACE_RECORD_DIR_ENV, "").strip()
_ARTIFACT_SHA = os.environ.get(RELEASE_ARTIFACT_SHA_ENV, "").strip()


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module."""
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


# ----------------------------------------------------------------------
# The trace-record writer (report hygiene: the executed trace, its
# evidence class, and WHICH source it executed against — source main
# or a release artifact digest, never conflated)
# ----------------------------------------------------------------------


def _source_identity() -> dict[str, Any]:
    """Who executed this trace: the source tree, or a release artifact."""
    import subprocess as _sp

    commit: str | None = None
    dirty = False
    try:
        root = _THIS_FILE.parents[2]
        commit = (
            _sp.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
            ).stdout.strip()
            or None
        )
        dirty = bool(
            _sp.run(
                ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
            ).stdout.strip()
        )
    except OSError:  # pragma: no cover — outside a checkout
        pass
    # _ARTIFACT_SHA was stashed at IMPORT time: the module-scoped env scrub
    # below removes every FORGE_* variable before the traces run, and the
    # records are written at teardown while the scrub is still in force.
    return {
        # the same "basis" spelling the pg-gate report carries — the two
        # artifacts read uniformly side by side
        "basis": "release-artifact" if _ARTIFACT_SHA else "source-main",
        "git_commit": commit,
        "tree_dirty": dirty,
        "test_source_sha256": hashlib.sha256(_THIS_FILE.read_bytes()).hexdigest(),
        "artifact_sha256": _ARTIFACT_SHA or None,
    }


def write_trace_record(
    test_id: str,
    *,
    label: str,
    mutation: str | None,
    outcome: str,
    duration_seconds: float,
) -> None:
    """One machine-readable record per executed trace (or mutation arm).

    Written ONLY when :data:`TRACE_RECORD_DIR_ENV` names a directory —
    the pg-gate/CI evidence path. The record keeps the evidence class
    separate from unit/integration/native-live/customer evidence, and
    names WHICH source executed it: the source tree (git identity +
    the test file's exact sha256) or a release artifact digest.
    """
    if not _RECORD_DIR:
        return
    directory = _RECORD_DIR
    record = {
        "schema": TRACE_RECORD_SCHEMA,
        "label": label,
        "test_id": test_id,
        "mutation": mutation,
        "outcome": outcome,
        "duration_seconds": round(duration_seconds, 3),
        "evidence_class": "production-entry (offline composed trace)",
        "executed_at": time.time(),
        "source": _source_identity(),
    }
    stem = test_id.replace("/", "_").replace("::", "__").replace("[", "_").replace("]", "")
    path = Path(directory) / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _record_the_trace(request: pytest.FixtureRequest):
    """Record every MG trace/arm execution (source-sha'd, class-separate)."""
    started = time.monotonic()
    yield
    marker = request.node.get_closest_marker("trace_record")
    if marker is None:
        return
    from .conftest import TRACE_OUTCOME_KEY

    write_trace_record(
        request.node.nodeid,
        label=str(marker.kwargs.get("label", "")),
        mutation=marker.kwargs.get("mutation"),
        outcome=request.node.stash.get(TRACE_OUTCOME_KEY, "executed"),
        duration_seconds=time.monotonic() - started,
    )


def trace_record(label: str, *, mutation: str | None = None):
    """Mark a test as one of the enumerated MG traces/arms."""
    return pytest.mark.trace_record(label=label, mutation=mutation)


# ----------------------------------------------------------------------
# The recording model endpoint — the reviewer's provider (real HTTP)
# ----------------------------------------------------------------------


class _ReviewCompletionHandler(BaseHTTPRequestHandler):
    """Answers every POST with one valid review verdict; records bodies."""

    def do_POST(self) -> None:  # noqa: N802 — the http.server contract
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server = self.server
        server.requests.append(json.loads(body.decode("utf-8") or "{}"))  # type: ignore[attr-defined]
        content = json.dumps(
            {
                "verdict": "ok",
                "summary": "The rename is applied and scoped.",
                "findings": [],
            }
        )
        payload = json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:  # silence the test log
        return


class RecordingModelEndpoint:
    """A REAL local HTTP server the REAL LLMClient dials — the assertion
    surface for whether (and how often) the reviewer's call reached the
    provider. Cleaned up in ``close`` (shutdown, socket, thread)."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ReviewCompletionHandler)
        self._server.requests = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="mg-model-endpoint", daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def requests(self) -> list[dict]:
        return list(self._server.requests)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def model_endpoint():
    endpoint = RecordingModelEndpoint()
    try:
        yield endpoint
    finally:
        endpoint.close()


# ----------------------------------------------------------------------
# The composed landing: real lane, real collector, real publish, REAL
# reviewer over the recording endpoint, guarded by the real budget
# ----------------------------------------------------------------------


def _budget_settings(endpoint_url: str, **overrides) -> "Any":
    """gl_settings with the LLM pointed at the recording endpoint and a
    one-call budget profile (the planner/implementer are stubs; the one
    call belongs to the closing review — the reviewer's real call)."""
    values = dict(
        LITELLM_URL=endpoint_url,
        # R13: the numeric profile the planning leg freezes into the run.
        # ONE call: the reviewer's real call — the receipts below consume
        # it first, so the guard (not the test) refuses the review.
        FORGE_BUDGET_PROFILES=json.dumps({"standard": {"max_calls": 1, "max_tokens": 1_000_000}}),
        FORGE_REQUIRED_JOBS="verify",
    )
    values.update(overrides)
    return gl_settings(**values)


class _LandedRun:
    """The composed trace's landed world (waiting_ci + Draft MR)."""

    def __init__(self, run_id: str, branch: str, mr_iid: int, service, client) -> None:
        self.run_id = run_id
        self.branch = branch
        self.mr_iid = mr_iid
        self.service = service
        self.client = client  # the REAL LLMClient (closed by the fixture)


@pytest.fixture()
async def real_reviewer(pe_db, model_endpoint):
    """The REAL LLMClient the traces' services share, pointed at the
    recording endpoint — closed on teardown (no leaked HTTP pool)."""
    from forge.factory.llm import LLMClient

    client = LLMClient(
        settings=_budget_settings(model_endpoint.url), session_factory=pe_db.worker_factory()
    )
    client.endpoint = model_endpoint  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        await client.close()


async def _land_guarded_run(
    pe_db,
    gitlab_native,
    gitlab_client,
    monkeypatch,
    tmp_path,
    real_reviewer,
    *,
    settings,
) -> _LandedRun:
    """Issue → /go → the lane's work → the shipped collector → the native
    publish: a Draft-MR run in ``waiting_ci`` whose reviewer is the REAL
    LLMReviewer over the recording endpoint."""
    from forge.config import ForgeConfig
    from forge.factory.reviewer import LLMReviewer
    from forge.runs.service import RunService
    from forge.runs.stubs import StubImplementer, StubPlanner

    store_dir = tmp_path / "mg-store"
    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
    monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
    monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)

    factory = pe_db.worker_factory()
    reviewer = LLMReviewer(real_reviewer, gitlab_client)
    service = RunService(
        factory,
        gitlab=gitlab_client,
        settings=settings,
        config=ForgeConfig(),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=reviewer,
    )
    gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
    checkout, base_oid = make_checkout(tmp_path, "mg-workspace")
    gitlab_native.seed_commit(GL_BASE_BRANCH, base_oid, "frozen base")
    for name in ("README.md", "src/app.py", "run.sh"):
        gitlab_native.seed_file(name, (checkout / name).read_text())
    gitlab_native.seed_file(".forge.yml", "implement:\n  paths:\n    - src/**\n")

    run_id = await service.start_run(
        GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
    )
    await service.handle_command_note(
        GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
    )
    (dispatch,) = gitlab_native.dispatches()
    branch = dispatch["ref"]
    job = gitlab_native.jobs(dispatch["pipeline_id"])[0]

    run_vendor_once(checkout, RUNNER_ACTIONS, tmp_path / "mg-vendor-events.jsonl")
    outcome = run_collector(checkout, work_id=run_id, attempt_base=base_oid)
    assert outcome.returncode == 0, outcome.stderr
    reported = json.loads(outcome.stdout)
    diff = Path(reported["diff_path"]).read_bytes()
    meta = json.dumps(
        {
            "attempt_base": base_oid,
            "driver": "claude-code",
            "model": PE_HARNESS_MODEL,
            "exit": "completed",
            "usage": None,
        }
    ).encode("utf-8")
    gitlab_native.seed_artifact(job["id"], ".forge/candidate.diff", diff)
    gitlab_native.seed_artifact(job["id"], ".forge/candidate.meta.json", meta)
    gitlab_native.mark_job(job["id"], "success")

    await service.evaluate_waiting_harness()
    published = await get_run(factory, run_id)
    assert published.status == FlowStatus.WAITING_CI.value
    assert published.mr_iid is not None
    return _LandedRun(run_id, branch, published.mr_iid, service, real_reviewer)


async def _green_pipeline_for(gitlab_native, landed: _LandedRun) -> None:
    """The candidate's independent CI goes green (the required job)."""
    candidate = (await get_run(landed.service._session_factory, landed.run_id)).candidate_shas[-1]
    gitlab_native.seed_pipeline(
        ref=landed.branch,
        sha=candidate,
        status="success",
        jobs=[{"id": 77001, "name": "verify", "status": "success"}],
    )


async def _ingest(pe_db, run_id: str, usage: SimpleNamespace) -> None:
    """The durable receipt front door — exactly as the lane reconciler
    persists a candidate's spend (idempotent, cost-lineage'd)."""
    factory = pe_db.worker_factory()
    async with factory() as session:
        _, created = await ingest_usage_receipt(session, run_id=run_id, usage=usage)
        await session.commit()
        assert created, f"the receipt {usage.receipt_id} was not ingested"


def _final_receipt(*, receipt_id: str, cost_usd: float) -> SimpleNamespace:
    """A settled FINAL receipt: the provider reported the exact figure."""
    return SimpleNamespace(
        attempt_id="a-1",
        receipt_id=receipt_id,
        driver="claude-code",
        model=PE_HARNESS_MODEL,
        input_tokens=100,
        output_tokens=20,
        completeness="exact",
        source="forge-lab",
        final=True,
        cost_usd=cost_usd,
        cost_basis="provider-reported",
        raw={"cost_usd": cost_usd, "cost_basis": "provider-reported", "final": True},
    )


def _partial_receipt(
    *, receipt_id: str, subtotal_usd: float, envelope_usd: float
) -> SimpleNamespace:
    """A PARTIAL receipt: the provider streamed a subtotal that is NOT
    settled, inside the worst-case envelope still fenced for it."""
    return SimpleNamespace(
        attempt_id="a-1",
        receipt_id=receipt_id,
        driver="claude-code",
        model=PE_HARNESS_MODEL,
        input_tokens=80,
        output_tokens=10,
        completeness="partial",
        source="forge-lab",
        final=False,
        cost_usd=subtotal_usd,
        cost_basis="estimated",
        raw={
            "cost_usd": subtotal_usd,
            "cost_upper_bound_usd": envelope_usd,
            "final": False,
        },
    )


async def _review_budget_block(pe_db, run_id: str) -> dict:
    run = await get_run(pe_db.worker_factory(), run_id)
    block = (run.evidence or {}).get("review_budget_block")
    assert isinstance(block, dict), "the reviewer-leg budget decision was never recorded"
    return block


# ----------------------------------------------------------------------
# MG-1 — partial-liability admission (#339: finality settles liability)
# ----------------------------------------------------------------------


class TestMG1PartialLiabilityAdmission:
    @trace_record("MG-1 partial-liability admission (baseline)")
    async def test_a_partial_subtotal_never_settles_the_closing_admission(
        self,
        pe_db,
        gitlab_native,
        gitlab_client,
        monkeypatch,
        tmp_path,
        real_reviewer,
    ):
        """Settled 8.00 FINAL + a PARTIAL at 0.50 inside a 3.00 envelope,
        cap 10.00: the bounded exposure is 11.00 — the closing review
        does NOT fit and the run is BLOCKED with the shortage named,
        having NEVER contacted the provider."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "10.00")
        landed = await _land_guarded_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            monkeypatch,
            tmp_path,
            real_reviewer,
            settings=_budget_settings(real_reviewer.endpoint.url),
        )
        await _green_pipeline_for(gitlab_native, landed)
        await _ingest(pe_db, landed.run_id, _final_receipt(receipt_id="mg1-final", cost_usd=8.0))
        await _ingest(
            pe_db,
            landed.run_id,
            _partial_receipt(receipt_id="mg1-partial", subtotal_usd=0.5, envelope_usd=3.0),
        )

        await landed.service.evaluate_waiting_ci()

        run = await get_run(pe_db.worker_factory(), landed.run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("budget_exhausted: reviewer refused")
        block = await _review_budget_block(pe_db, landed.run_id)
        budget = block["budget"]
        # FINALITY settles: the partial's 0.50 rides INSIDE the retained
        # envelope — it never settles, never releases, never adds on top.
        assert budget["settled_usd"] == pytest.approx(8.0)
        assert budget["accrued_unsettled_usd"] == pytest.approx(0.5)
        assert budget["retained_liability_usd"] == pytest.approx(3.0)
        assert budget["unbounded_intervals"] == 0
        assert budget["requires_bounded_policy"] is False
        # the P01 counterexample verdict: 8 + 3 > 10 — the review does
        # not fit, and the reserve is eaten (known 8 + retained 3 vs the
        # 9.40 coder ceiling).
        assert budget["closing_review_fits"] is False
        assert budget["reserve_intact"] is False
        # the guard refused BEFORE the provider: zero reviewer requests
        assert reviewer_requests(real_reviewer) == 0

    @trace_record("MG-1 mutation: value-presence finality", mutation="value-presence-fold")
    async def test_the_value_presence_regression_fits_the_unfittable(
        self,
        pe_db,
        gitlab_native,
        gitlab_client,
        monkeypatch,
        tmp_path,
        real_reviewer,
    ):
        """Seed the #339 defect back (``exposure_fold`` reverted to
        value-presence: any cost settles, only costless rows reserve) and
        run the SAME event sequence: the partial's 0.50 is counted as
        SETTLED, the 3.00 envelope vanishes, exposure reads 8.50 — the
        run wrongly stays REVIEWING. The baseline trace's assertions
        (BLOCKED, settled 8.00, retained 3.00) all fail under this
        patch — the mutant is killed."""
        import forge.adaptive.usage_ingestion as usage_ingestion

        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "10.00")

        def _value_presence_fold(rows, *, unknown_interval_ceiling_usd=None):  # noqa: ANN001
            """The pre-#339 math: value presence settles; only costless
            intervals reserve — exactly how a regression would
            reintroduce the defect."""
            settled = accrued = lowers = retained = 0.0
            unresolved = unbounded = 0
            for row in rows:
                cost = row.cost_usd
                if cost is not None and isinstance(cost, (int, float)) and cost >= 0:
                    settled += float(cost)  # presence settles — the defect
                    continue
                unresolved += 1
                lower = row.cost_lower_bound_usd or 0.0
                lowers += lower
                bound = row.cost_upper_bound_usd
                if bound is None:
                    bound = unknown_interval_ceiling_usd
                if bound is None:
                    unbounded += 1
                    continue
                retained += max(float(bound), float(lower))
            return usage_ingestion.SpendExposure(
                settled_usd=settled,
                accrued_unsettled_usd=accrued,
                unknown_lower_bound=lowers,
                retained_liability_usd=retained,
                settlement_release_usd=0.0,
                unresolved_intervals=unresolved,
                unbounded_intervals=unbounded,
                incoherent_bounds=0,
                requires_bounded_policy=unbounded > 0,
                findings=(),
            )

        # Bind to the symbol at test time: the shipped seam spend_cap_check
        # resolves exposure_fold as a module global on every call.
        monkeypatch.setattr(usage_ingestion, "exposure_fold", _value_presence_fold)

        landed = await _land_guarded_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            monkeypatch,
            tmp_path,
            real_reviewer,
            settings=_budget_settings(real_reviewer.endpoint.url),
        )
        await _green_pipeline_for(gitlab_native, landed)
        await _ingest(pe_db, landed.run_id, _final_receipt(receipt_id="mg1m-final", cost_usd=8.0))
        await _ingest(
            pe_db,
            landed.run_id,
            _partial_receipt(receipt_id="mg1m-partial", subtotal_usd=0.5, envelope_usd=3.0),
        )

        await landed.service.evaluate_waiting_ci()

        # THE DEFECT'S OBSERVABLE — the exact opposite of the baseline:
        run = await get_run(pe_db.worker_factory(), landed.run_id)
        assert run.status == FlowStatus.REVIEWING.value  # wrongly reviewable
        block = await _review_budget_block(pe_db, landed.run_id)
        budget = block["budget"]
        assert budget["settled_usd"] == pytest.approx(8.5)  # the partial "settled"
        assert budget["retained_liability_usd"] == pytest.approx(0.0)  # envelope gone
        assert budget["closing_review_fits"] is True  # 8.5 "fits"
        assert reviewer_requests(real_reviewer) == 0


def reviewer_requests(real_reviewer) -> int:
    """How many reviewer requests reached the recording endpoint — the
    provider-side proof the guard refused BEFORE the wire."""
    return len(real_reviewer.endpoint.requests())  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# MG-2 — guarded review amendment (#340 / AT-04)
# ----------------------------------------------------------------------


class TestMG2GuardedReviewAmendment:
    @trace_record("MG-2 guarded review amendment (baseline)")
    async def test_a_calls_amendment_reopens_the_real_guard_for_exactly_one_review(
        self,
        pe_db,
        gitlab_native,
        gitlab_client,
        monkeypatch,
        tmp_path,
        real_reviewer,
    ):
        """The one-call budget is consumed by the lane's receipt; the REAL
        reviewer's call is refused by the REAL guard (the block records
        with the reserve intact); a calls-axis amendment through
        ``continue_review_only`` re-opens the enforcement row and
        EXACTLY ONE reviewer call reaches the provider — the run goes
        ready_for_human with zero coder dispatches."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        landed = await _land_guarded_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            monkeypatch,
            tmp_path,
            real_reviewer,
            settings=_budget_settings(real_reviewer.endpoint.url),
        )
        await _green_pipeline_for(gitlab_native, landed)
        dispatches_before = len(gitlab_native.dispatches())

        # The lane's OWN artifact receipt (the meta the collector shipped)
        # already consumed the one call through the real harness ingestion
        # path — the guard, not the test, refuses the review.
        await landed.service.evaluate_waiting_ci()

        run = await get_run(pe_db.worker_factory(), landed.run_id)
        assert run.status == FlowStatus.REVIEWING.value  # held, not blocked
        block = await _review_budget_block(pe_db, landed.run_id)
        assert block["budget"]["closing_review_fits"] is True  # reserve intact
        assert reviewer_requests(real_reviewer) == 0  # the guard refused first

        outcome = await landed.service.continue_review_only(
            landed.run_id,
            operator="human:alice",
            command_id=AMEND_COMMAND,
            axis=AXIS_CALLS,
            amount=1,
            reason="close the promised review",
        )
        assert outcome["allowed"] is True, outcome
        assert outcome["amendment"]["applied"] is True
        assert outcome["amendment"]["limit_after"]["max_calls"] == 2  # the row moved

        # EXACTLY ONE reviewer call reached the provider, and the run is
        # ready for the human — the amendment opened the REAL guard.
        [request] = real_reviewer.endpoint.requests()  # type: ignore[attr-defined]
        assert request["model"] == "strong"  # the reviewer tier
        run = await get_run(pe_db.worker_factory(), landed.run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        review = (run.evidence or {})["review"]
        assert review["verdict"] == "ok"
        # review-only: zero coder dispatches, zero new commits
        assert len(gitlab_native.dispatches()) == dispatches_before

        # a redelivery of the SAME command after the review completed is
        # refused honestly (the released block is closed) — and it added
        # NOTHING: the amendment ledger still holds exactly ONE applied
        # row for the command, and the provider stayed quiet.
        replay = await landed.service.continue_review_only(
            landed.run_id,
            operator="human:alice",
            command_id=AMEND_COMMAND,
            axis=AXIS_CALLS,
            amount=1,
            reason="close the promised review",
        )
        assert replay["allowed"] is False
        assert replay["reason"] == "no_review_budget_block"
        assert reviewer_requests(real_reviewer) == 1  # the provider stays quiet
        from forge.durable import budget_amendments_for_run

        factory = pe_db.worker_factory()
        async with factory() as session:
            ledger = await budget_amendments_for_run(session, landed.run_id)
        applied = [row.command_id for row in ledger if row.status == "applied"]
        assert applied == [AMEND_COMMAND]  # the command applied EXACTLY once

    @trace_record("MG-2 mutation: evidence-only amendment", mutation="evidence-only-amendment")
    async def test_an_evidence_only_amendment_cannot_buy_a_reviewer_call(
        self,
        pe_db,
        gitlab_native,
        gitlab_client,
        monkeypatch,
        tmp_path,
        real_reviewer,
    ):
        """Seed the #340 defect back (``apply_budget_amendment`` reverted
        to an evidence-only annotation — the enforcing limits row is
        never touched) and run the SAME event sequence: the shipped
        pre-review capacity check still refuses, NOTHING reaches the
        provider and the run stays held. A reviewer double (a handler
        lying that it applied) cannot bypass the BudgetGuard; the
        baseline trace's assertions (allowed, one provider call,
        ready_for_human) all fail under this patch."""
        import forge.runs.service as runs_service
        from forge.durable import AppliedBudgetAmendment, BudgetAmendmentCommand
        from forge.durable.models import FlowRun

        assert hasattr(runs_service, "apply_budget_amendment"), (
            "the #338 lifecycle refactor moved the amendment seam — update "
            "this patch point to the shipped symbol"
        )

        async def _evidence_only_amendment(session, command: BudgetAmendmentCommand):
            """The recorded defect: amount/reason/operator recorded into
            run EVIDENCE, the enforcing ``run_budgets`` limits untouched."""
            run = await session.get(FlowRun, command.run_id)
            assert run is not None
            evidence = dict(run.evidence or {})
            ledger = list(evidence.get("budget_amendments") or [])
            ledger.append({**command.to_json(), "status": "applied"})
            run.evidence = {**evidence, "budget_amendments": ledger}
            return AppliedBudgetAmendment(
                command=command, applied=True, replayed=False, refusal_reason=None
            )

        monkeypatch.setattr(runs_service, "apply_budget_amendment", _evidence_only_amendment)

        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        landed = await _land_guarded_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            monkeypatch,
            tmp_path,
            real_reviewer,
            settings=_budget_settings(real_reviewer.endpoint.url),
        )
        await _green_pipeline_for(gitlab_native, landed)
        await landed.service.evaluate_waiting_ci()
        run = await get_run(pe_db.worker_factory(), landed.run_id)
        assert run.status == FlowStatus.REVIEWING.value  # held at the block

        outcome = await landed.service.continue_review_only(
            landed.run_id,
            operator="human:alice",
            command_id=AMEND_COMMAND,
            axis=AXIS_CALLS,
            amount=1,
            reason="close the promised review",
        )

        # THE DEFECT'S OBSERVABLE — the guard was never opened:
        from forge.adaptive.closing_budget import OBSERVABLE_REFUSED_AXIS

        assert outcome["allowed"] is False
        assert outcome["reason"] == "review_capacity_refused"
        assert outcome[OBSERVABLE_REFUSED_AXIS] in ("calls", "status")
        assert reviewer_requests(real_reviewer) == 0  # nothing reached the provider
        run = await get_run(pe_db.worker_factory(), landed.run_id)
        assert run.status == FlowStatus.REVIEWING.value  # still held
        # the evidence-only annotation DID land (the defect's shape) —
        # and bought nothing:
        evidence = dict(run.evidence or {})
        assert evidence.get("budget_amendments")


# ----------------------------------------------------------------------
# MG-3 — grant persistence under concurrent evidence (#341)
# ----------------------------------------------------------------------


async def _merge_concurrent_evidence(pe_db, run_id: str) -> None:
    """A checkpoint-shaped concurrent evidence write (the same
    read-merge-commit a checkpoint/native-handle writer performs)."""
    from forge.durable.models import FlowRun

    factory = pe_db.worker_factory()
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        evidence = dict(run.evidence or {})
        evidence[CONCURRENT_EVIDENCE_KEY] = dict(CONCURRENT_EVIDENCE_VALUE)
        run.evidence = evidence
        await session.commit()


async def _write_concurrent_evidence_once_the_authority_lands(pe_db, run_id: str, done) -> None:
    """Watch for the keyed authority row (committed BEFORE the evidence
    projection runs), then land the concurrent evidence write — hitting
    whatever side of the projection's read/write window is live."""
    from sqlalchemy import select

    from forge.durable.models import OperationGrant

    factory = pe_db.worker_factory()
    deadline = time.monotonic() + 30.0
    while not done.is_set() and time.monotonic() < deadline:
        async with factory() as session:
            row = (
                await session.execute(
                    select(OperationGrant.id).where(OperationGrant.work_id == run_id)
                )
            ).scalar_one_or_none()
        if row is not None:
            await _merge_concurrent_evidence(pe_db, run_id)
            return
        await asyncio.sleep(0.005)


class TestMG3GrantPersistenceUnderConcurrentEvidence:
    @trace_record("MG-3 grant persistence under concurrent evidence (baseline)")
    async def test_the_projection_preserves_a_concurrent_evidence_write(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        """The redemption-mode dispatch persists its operation grant while
        a checkpoint-shaped evidence write lands in the window: BOTH the
        keyed authority row and the concurrent key survive in the run
        evidence — no interleaving loses anyone's write."""
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
            delivery="runner-redemption",
        )

        done = asyncio.Event()
        watcher = asyncio.create_task(
            _write_concurrent_evidence_once_the_authority_lands(pe_db, run_id, done)
        )
        try:
            await drive_go(service, run_id)
        finally:
            done.set()
            await asyncio.wait_for(watcher, timeout=10)

        evidence = await get_run_evidence(factory, run_id)
        grants = evidence.get("credential_operation_grants")
        assert grants and set(grants) == {"0:anthropic-gateway"}
        assert grants["0:anthropic-gateway"]["credential_ref"] == REF_V1
        # the concurrent checkpoint-shaped write SURVIVED the projection
        assert evidence.get(CONCURRENT_EVIDENCE_KEY) == CONCURRENT_EVIDENCE_VALUE
        # the keyed authority row stands beside the projection
        from sqlalchemy import select

        from forge.durable.models import OperationGrant

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(OperationGrant).where(OperationGrant.work_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1 and rows[0].status == "active"

    @trace_record("MG-3 mutation: whole-document grant write", mutation="whole-document-write")
    async def test_the_whole_document_writer_drops_the_concurrent_evidence(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        """Seed the #341 defect back (the projection writer reverted to
        the whole-document overwrite — read evidence, write the ENTIRE
        stale document back) and run the SAME event sequence with the
        concurrent write landing inside the reverted writer's window:
        the checkpoint key is silently DROPPED. The baseline trace's
        assertion (both keys present) fails under this patch — the
        mutant is killed."""
        import forge.api_lane_control as lane_control
        from forge.adaptive.credential_broker import (
            EVIDENCE_OPERATION_GRANTS_KEY,
            CredentialOperationGrant,
        )
        from sqlalchemy import select

        from forge.durable.models import FlowRun, OperationGrant

        assert hasattr(lane_control, "_project_operation_grant"), (
            "the projection seam moved — update this patch point to the shipped symbol"
        )
        read_done = asyncio.Event()
        release = asyncio.Event()

        async def _whole_document_projection(session_factory, effective) -> None:
            """The pre-#341 writer: ONE read, then the ENTIRE (now stale)
            document written back — the R40-05/P02 loss."""
            async with session_factory() as session:
                run = await session.get(FlowRun, effective.work_id)
                assert run is not None
                stale = dict(run.evidence or {})  # the read everything races with
                standing = (
                    await session.execute(
                        select(OperationGrant).where(
                            OperationGrant.work_id == effective.work_id,
                            OperationGrant.attempt_generation == int(effective.attempt_generation),
                            OperationGrant.provider == effective.provider,
                        )
                    )
                ).scalar_one()
                current = CredentialOperationGrant.from_document(dict(standing.document or {}))
                grants = dict(stale.get(EVIDENCE_OPERATION_GRANTS_KEY) or {})
                grants[current.key()] = current.as_document()
                read_done.set()  # the window opens: the concurrent write lands
                await release.wait()
                run.evidence = {**stale, EVIDENCE_OPERATION_GRANTS_KEY: grants}
                await session.commit()

        monkeypatch.setattr(lane_control, "_project_operation_grant", _whole_document_projection)

        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
            delivery="runner-redemption",
        )

        async def _drive_and_write_the_concurrent_key() -> None:
            drive = asyncio.create_task(drive_go(service, run_id))
            await asyncio.wait_for(read_done.wait(), timeout=30)  # the reverted window
            await _merge_concurrent_evidence(pe_db, run_id)
            release.set()
            await drive

        await _drive_and_write_the_concurrent_key()

        evidence = await get_run_evidence(factory, run_id)
        grants = evidence.get("credential_operation_grants")
        assert grants and set(grants) == {"0:anthropic-gateway"}  # the grant survived
        # THE DEFECT'S OBSERVABLE — the concurrent key is GONE:
        assert CONCURRENT_EVIDENCE_KEY not in evidence
