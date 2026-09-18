"""GitHub Actions execution adapter (ADR-0020, Stage E3b).

The second execution adapter (ADR-0019): the coding harness runs in the
TARGET repository's ephemeral Actions runner — no write token, no forge
secret — and the candidate comes back as an Actions artifact that the
trusted publisher validates and publishes (ADR-0016). Contract per ADR-0020
§1: ``launch`` (workflow_dispatch) → ``poll`` (run status + artifact
collection, mapped onto :class:`~forge.runs.backends.HarnessOutcome`) →
``cancel`` → ``reconcile_launch``, over a typed, JSON-serializable
:class:`ActionsHandle` journaled in the run's evidence.

Correlation (research github-actions-executor.md §1): the dispatch response
carries the run id since 2026-02-19 — used directly. Legacy/GHES answers an
empty 202: the run is discovered via
``GET .../workflows/{wf}/runs?event=workflow_dispatch`` filtered by
``head_sha`` (the attempt base the factory branch was cut from) plus a
created window — ordering alone is never trusted.

Failure classification mirrors the GitLab lane
(:mod:`forge.runs.backends`): auth/quota/connectivity patterns in the job
log mean infrastructure; the workflow conclusion classifies the rest;
deadline breaches are ``harness_timeout`` — never the code's fault.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from forge.integrations.github import GitHubAPIError, GitHubClient
from forge.runs.backends import (
    CANDIDATE_DIFF_PATH,
    CANDIDATE_META_PATH,
    _HARNESS_INFRASTRUCTURE_PATTERNS,
    HarnessOutcome,
)
from forge.runs.candidate import CandidateError, HarnessUsage, parse_unified_diff

logger = logging.getLogger(__name__)

#: Artifact name the workflow template uploads
#: (``ci/templates/forge-harness.github.yml``, upload-artifact@v4).
ARTIFACT_NAME_PREFIX = "forge-candidate-"

#: Log-tail bound when classifying a failed harness job.
LOG_TAIL_CHARS = 8000

#: R16 artifact-validation caps. forge POLICY — GitHub documents no
#: per-artifact byte cap (only the shared storage quota): the compressed
#: download is bounded before any parse (and against the REST
#: ``size_in_bytes`` declaration before downloading at all), the total
#: uncompressed content is bounded via ``ZipInfo.file_size`` BEFORE any
#: ``read()`` (zip-bomb guard), and the archive may carry only a handful of
#: entries.
MAX_ARTIFACT_ZIP_BYTES = 50 * 1024 * 1024
MAX_ARTIFACT_CONTENT_BYTES = 200 * 1024 * 1024
MAX_ARTIFACT_ENTRIES = 4

#: The exact basenames the candidate archive may carry (template contract).
#: Entries match by basename — upload-artifact@v4 stores the searched paths
#: under their least-common-ancestor prefix — but each EXACTLY ONCE: a
#: duplicate basename must never shadow the real meta (R16).
_CANDIDATE_BASENAMES = frozenset({"candidate.diff", "candidate.meta.json"})

#: Meta schema versions the control plane accepts: 1 is the historical
#: schemaless shape (no ``schema_version`` key), 2 adds attempt identity,
#: the ``manifest_digest`` bind, and the usage receipt. Anything else —
#: including junk strings — fails closed.
_META_SCHEMA_VERSIONS = frozenset({1, 2})

#: Run statuses that mean "keep waiting" (Actions lifecycle values).
_ACTIVE_RUN_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


def artifact_name_for(run_id: str) -> str:
    """The candidate artifact name for *run_id* (template contract)."""
    return f"{ARTIFACT_NAME_PREFIX}{run_id}"


@dataclass(frozen=True)
class ActionsHandle:
    """The journaled execution handle (ADR-0020 §1: the opaque JSON string
    the caller stores in ``flow_runs.evidence`` so a run survives restarts).

    ``run_id`` is the ACTIONS run id — 0 until correlation succeeds
    (directly from the dispatch response, or via
    :meth:`GitHubActionsExecutor.reconcile_launch` discovery for legacy
    empty-202 responses). ``forge_run_id`` is forge's own run id — the
    value dispatched as ``inputs.run_id`` and embedded in the candidate
    artifact name.
    """

    provider: str
    owner: str
    repo: str
    workflow: str
    run_id: int
    branch: str
    attempt_base: str
    run_spec_digest: str
    driver: str
    forge_run_id: str
    started_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> ActionsHandle:
        data = json.loads(raw)
        return cls(
            provider=str(data["provider"]),
            owner=str(data["owner"]),
            repo=str(data["repo"]),
            workflow=str(data["workflow"]),
            run_id=int(data.get("run_id") or 0),
            branch=str(data["branch"]),
            attempt_base=str(data["attempt_base"]),
            run_spec_digest=str(data.get("run_spec_digest") or ""),
            driver=str(data.get("driver") or ""),
            forge_run_id=str(data.get("forge_run_id") or ""),
            started_at=str(data["started_at"]),
        )

    def with_run_id(self, run_id: int) -> ActionsHandle:
        return replace(self, run_id=run_id)


class GitHubActionsExecutor:
    """launch / poll / cancel / reconcile_launch over the Actions REST API.

    Duck-types the ADR-0015 backend seam's ``poll`` result type
    (:class:`HarnessOutcome`) so the reconciler treats both CI lanes alike;
    the handle it consumes is the Actions-shaped one above.
    """

    def __init__(self, client: GitHubClient, settings: Any) -> None:
        self._client = client
        self._settings = settings

    # -- launch -------------------------------------------------------------

    async def launch(
        self,
        handle: ActionsHandle,
        *,
        inputs: dict[str, str] | None = None,
    ) -> ActionsHandle:
        """Dispatch the harness workflow on the factory branch; correlate.

        The dispatch fires exactly once (non-idempotent upstream). When the
        response carries no run id (legacy), the run is discovered by
        ``head_sha=attempt_base`` within the dispatch-created window.
        """
        dispatch_inputs = {
            "run_id": "",
            "attempt_base_oid": handle.attempt_base,
            "driver": handle.driver,
            "model": "",
            "issue_number": "",
            **(inputs or {}),
        }
        response = await self._client.dispatch_workflow(
            handle.owner,
            handle.repo,
            handle.workflow,
            handle.branch,
            dispatch_inputs,
        )
        run_id = int(response.get("run_id") or 0)
        if run_id:
            return handle.with_run_id(run_id)
        return await self._discover(handle, since=datetime.now(timezone.utc))

    async def reconcile_launch(self, handle: ActionsHandle) -> ActionsHandle:
        """Recover correlation after a lost/ambiguous launch (ADR-0005).

        Idempotent re-entry: a handle that already carries a run id is
        verified to still exist (404 → stays, the reconciler's deadline
        handles it); an uncorrelated handle re-runs discovery against a
        generous window. Never dispatches again — one launch per intent.
        """
        if handle.run_id:
            try:
                await self._client.get_workflow_run(handle.owner, handle.repo, handle.run_id)
            except GitHubAPIError as exc:
                logger.warning(
                    "Actions run %d for %s no longer readable (%s) — keeping the handle",
                    handle.run_id,
                    handle.branch,
                    exc,
                )
            return handle
        started = _parse_started_at(handle.started_at)
        return await self._discover(handle, since=started)

    async def _discover(self, handle: ActionsHandle, *, since: datetime | None) -> ActionsHandle:
        """Find the dispatch run by head_sha + created window (newest first).

        ``since`` (the journaled dispatch time) bounds the search below; a
        handle whose journaled timestamp is unparseable re-discovers without
        a lower bound rather than crashing on the window arithmetic.
        """
        try:
            runs = await self._client.list_workflow_dispatch_runs(
                handle.owner,
                handle.repo,
                handle.workflow,
                head_branch=handle.branch,
                head_sha=handle.attempt_base,
                created_after=(since - timedelta(minutes=5)) if since else None,
            )
        except GitHubAPIError:
            logger.warning(
                "Actions run discovery failed for %s (%s) — returning the handle uncorrelated",
                handle.branch,
                handle.workflow,
                exc_info=True,
            )
            return handle
        if not runs:
            logger.info(
                "No workflow_dispatch run found for %s on %s yet — discovery retries later",
                handle.workflow,
                handle.branch,
            )
            return handle
        newest = runs[0]
        # ADR-0020 §1: verify head_sha == attempt_base — never trust ordering.
        if str(newest.get("head_sha") or "") != handle.attempt_base:
            logger.warning(
                "Discovered Actions run %s has head %s, expected %s — refusing correlation",
                newest.get("id"),
                str(newest.get("head_sha") or "")[:8],
                handle.attempt_base[:8],
            )
            return handle
        correlated = handle.with_run_id(int(newest["id"]))
        logger.info(
            "Actions harness run %d discovered for branch %s", correlated.run_id, handle.branch
        )
        return correlated

    # -- poll -----------------------------------------------------------------

    async def poll(self, handle: ActionsHandle, *, now: datetime | None = None) -> HarnessOutcome:
        """One poll pass: run status → running / candidate / classified failure."""
        if not handle.run_id:
            return HarnessOutcome.running()  # not yet correlated — keep waiting
        now = now or datetime.now(timezone.utc)
        try:
            run = await self._client.get_workflow_run(handle.owner, handle.repo, handle.run_id)
        except GitHubAPIError:
            logger.warning(
                "Actions run %d read failed — keeping the run waiting", handle.run_id, exc_info=True
            )
            return HarnessOutcome.running()

        status = str(run.get("status") or "").lower()
        if status in _ACTIVE_RUN_STATUSES or status == "":
            return self._deadline_outcome(handle, now)

        conclusion = str(run.get("conclusion") or "").lower()
        if conclusion == "success":
            return await self._collect_candidate(handle)
        if conclusion in {"cancelled", "skipped", "stale"}:
            # External cancellation is not the code's fault (ADR-0015).
            return HarnessOutcome.failed("infrastructure", f"harness run {conclusion}")
        if conclusion == "timed_out":
            return HarnessOutcome.failed("infrastructure", "harness_timeout")
        if conclusion == "startup_failure":
            # The lane never started (workflow file broken, runner image
            # unavailable) — infrastructure by definition.
            return HarnessOutcome.failed("infrastructure", "harness startup_failure")
        return await self._classify_failure(handle)

    def _deadline_outcome(self, handle: ActionsHandle, now: datetime) -> HarnessOutcome:
        """running before the durable deadline; harness_timeout past it.

        Deadline = journaled ``started_at`` + ``FORGE_HARNESS_TIMEOUT_SECONDS``
        (ADR-0013: budgets are enforced by forge, not by the runner — the
        workflow's own 60-minute ``timeout-minutes`` is only the first line).
        """
        timeout = int(getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800)
        started = _parse_started_at(handle.started_at)
        if started is not None and _aware(now) > _aware(started) + timedelta(seconds=timeout):
            return HarnessOutcome.failed("infrastructure", "harness_timeout")
        return HarnessOutcome.running()

    async def _collect_candidate(self, handle: ActionsHandle) -> HarnessOutcome:
        """Artifact → CandidateBundle (ADR-0016 parse path) → change_candidate.

        The attempt base compared against the artifact's meta comes from the
        TRUSTED handle (what forge pinned), never from the artifact alone;
        the publisher re-checks it at the write boundary. Download-side
        validation (R16) is fail-closed: REST-declared size/expiry checks
        before downloading, then a bounded download→extract with exactly one
        retry before the candidate is blocked.
        """
        artifacts = await self._client.list_workflow_run_artifacts(
            handle.owner, handle.repo, handle.run_id
        )
        wanted = artifact_name_for(handle.forge_run_id or str(handle.run_id))
        artifact = next(
            (a for a in artifacts if str(a.get("name") or "") == wanted),
            None,
        )
        if artifact is None:
            # Retention expired before collection, or the upload never ran —
            # both mean the candidate is gone (research §2).
            return HarnessOutcome.failed("code", "harness_artifact_missing")
        if artifact.get("expired") is True:
            return HarnessOutcome.failed("infrastructure", "harness_candidate_expired")
        declared = artifact.get("size_in_bytes")
        if isinstance(declared, int) and declared > MAX_ARTIFACT_ZIP_BYTES:
            return HarnessOutcome.failed(
                "infrastructure",
                f"harness_artifact_invalid: artifact declares {declared} bytes, over the cap",
            )
        try:
            diff_text, meta = await self._download_and_extract(handle, artifact)
        except CandidateArchiveError:
            # One bounded retry — a truncated/corrupted download can be
            # transient. A second invalid read BLOCKS the candidate as
            # infrastructure (the artifact delivery broke, which is not the
            # code's fault) instead of re-downloading forever on every poll.
            try:
                diff_text, meta = await self._download_and_extract(handle, artifact)
            except CandidateArchiveError as retry_exc:
                return HarnessOutcome.failed(
                    "infrastructure", f"harness_artifact_invalid: {retry_exc}"
                )

        # The template writes attempt_base_oid (contracts naming); accept
        # the short historical key too.
        reported_base = str(meta.get("attempt_base_oid") or meta.get("attempt_base") or "")
        if reported_base != handle.attempt_base:
            return HarnessOutcome.failed(
                "code",
                "harness_attempt_base_mismatch: artifact base "
                f"{reported_base[:8] or '<none>'}, expected {handle.attempt_base[:8] or '<none>'}",
            )

        driver_exit = str(meta.get("exit") or "completed").strip() or "completed"
        usage = HarnessUsage.from_meta(
            meta.get("usage"),
            driver=str(meta.get("driver") or handle.driver),
            model=str(meta.get("model") or ""),
        )
        try:
            bundle = parse_unified_diff(
                diff_text,
                attempt_base_oid=handle.attempt_base,
                driver_exit=driver_exit,
                usage=usage,
            )
        except CandidateError as exc:
            return HarnessOutcome.failed("code", f"harness_candidate_invalid: {exc.reason}: {exc}")

        if driver_exit != "completed":
            # The driver itself reported failure: never adopt a possibly
            # partial working tree, whatever it managed to change.
            detail = " with no changes" if bundle.is_empty else ""
            return HarnessOutcome.failed(
                "code", f"harness_driver_failed (exit={driver_exit}{detail})"
            )
        if bundle.is_empty:
            return HarnessOutcome.failed("code", "harness_no_changes")
        return HarnessOutcome.change_candidate(bundle, summary=str(meta.get("summary") or ""))

    async def _download_and_extract(
        self, handle: ActionsHandle, artifact: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """One bounded download→extract pass (raises :class:`CandidateArchiveError`)."""
        payload = await self._client.download_artifact_zip(
            handle.owner, handle.repo, int(artifact["id"])
        )
        return _extract_candidate(payload)

    async def _classify_failure(self, handle: ActionsHandle) -> HarnessOutcome:
        """Failed workflow → code vs infrastructure, GitLab-lane patterns.

        Auth/quota/connectivity substrings in the failed job's log mean the
        *environment* failed (ADR-0015). No failed job readable, or a log
        with no blame at all, is treated as infrastructure too — unknown
        means the evidence does not blame the code (the GitLab lane's rule
        for an empty ``failure_reason``). A failed job whose log is clean
        means the driver itself reported failure → code.
        """
        detail = "harness workflow failed"
        try:
            jobs = await self._client.get_workflow_run_jobs(
                handle.owner, handle.repo, handle.run_id
            )
        except GitHubAPIError:
            jobs = []
        failed_jobs = [j for j in jobs if str(j.get("conclusion") or "").lower() == "failure"]
        if not failed_jobs:
            return HarnessOutcome.failed("infrastructure", detail)
        job = failed_jobs[0]
        detail = f"harness job {job.get('name') or job.get('id')} failed"
        try:
            log = await self._client.get_job_log(handle.owner, handle.repo, int(job["id"]))
        except GitHubAPIError:
            log = ""
        lowered = log[-LOG_TAIL_CHARS:].lower()
        if any(pattern in lowered for pattern in _HARNESS_INFRASTRUCTURE_PATTERNS):
            return HarnessOutcome.failed("infrastructure", detail)
        return HarnessOutcome.failed("code", detail)

    # -- cancel -----------------------------------------------------------------

    async def cancel(self, handle: ActionsHandle) -> None:
        """Best-effort cancel of the Actions run (research §4).

        Tolerates 409/404 (already finished / never started): a finished run
        has nothing left to cancel, and a cancelled run's earlier artifact
        uploads survive (``if: always()`` steps ran).
        """
        if not handle.run_id:
            return
        try:
            await self._client.cancel_workflow_run(handle.owner, handle.repo, handle.run_id)
        except GitHubAPIError as exc:
            if exc.status_code in (404, 409):
                return  # nothing left to cancel
            raise


class CandidateArchiveError(Exception):
    """The artifact zip does not carry the candidate contract files."""


def _extract_candidate(payload: bytes) -> tuple[str, dict[str, Any]]:
    """Extract (candidate.diff text, meta dict) from the artifact zip bytes.

    Validation posture (R16, fail closed before any parse): the compressed
    zip is capped, the uncompressed content and entry count are capped via
    ``ZipInfo`` BEFORE any ``read()`` (zip-bomb guard), and the entry
    allowlist is CLOSED — entries match by basename (upload-artifact@v4
    stores the searched paths under their least-common-ancestor prefix, so
    they appear with or without the ``.forge/`` prefix) but each exactly
    once, with duplicate basenames and everything else rejected. The meta
    must then pass the schema gate (:func:`_check_meta_schema`).
    """
    if len(payload) > MAX_ARTIFACT_ZIP_BYTES:
        raise CandidateArchiveError(
            f"artifact zip is {len(payload)} bytes, over the {MAX_ARTIFACT_ZIP_BYTES}-byte cap"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise CandidateArchiveError(f"artifact is not a valid zip: {exc}") from exc
    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if len(entries) > MAX_ARTIFACT_ENTRIES:
            raise CandidateArchiveError(
                f"artifact archive carries {len(entries)} entries, over the "
                f"{MAX_ARTIFACT_ENTRIES}-entry cap"
            )
        uncompressed = sum(info.file_size for info in entries)
        if uncompressed > MAX_ARTIFACT_CONTENT_BYTES:
            raise CandidateArchiveError(
                f"artifact content is {uncompressed} bytes uncompressed, over the "
                f"{MAX_ARTIFACT_CONTENT_BYTES}-byte cap"
            )
        by_basename: dict[str, zipfile.ZipInfo] = {}
        for info in entries:
            name = info.filename
            if name.startswith("/") or ".." in name.split("/"):
                raise CandidateArchiveError(f"unsafe entry path {name!r} in the artifact archive")
            base = name.rsplit("/", 1)[-1]
            if base not in _CANDIDATE_BASENAMES:
                raise CandidateArchiveError(f"unexpected entry {name!r} in the artifact archive")
            if base in by_basename:
                raise CandidateArchiveError(f"duplicate entry for {base!r} in the artifact archive")
            by_basename[base] = info
        for base, contract_path in (
            ("candidate.diff", CANDIDATE_DIFF_PATH),
            ("candidate.meta.json", CANDIDATE_META_PATH),
        ):
            if base not in by_basename:
                raise CandidateArchiveError(f"{contract_path} not in the artifact archive")
        diff_bytes = archive.read(by_basename["candidate.diff"])
        try:
            meta = json.loads(archive.read(by_basename["candidate.meta.json"]).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateArchiveError(f"meta JSON unparseable: {exc}") from exc
    if not isinstance(meta, dict):
        raise CandidateArchiveError("meta JSON is not an object")
    _check_meta_schema(meta, diff_bytes)
    return diff_bytes.decode("utf-8", errors="replace"), meta


def _check_meta_schema(meta: dict[str, Any], diff_bytes: bytes) -> None:
    """Gate the meta schema (v1/v2) and, for v2, the manifest digest bind.

    v1 is the historical schemaless shape (no ``schema_version`` key). v2
    must carry ``manifest_digest`` — the sha256 of the exact candidate.diff
    bytes — so a substituted archive entry cannot pass silently (R16).
    Unknown versions reject: fail closed.
    """
    raw: Any = meta.get("schema_version", 1)
    if isinstance(raw, str):
        try:
            raw = int(raw)
        except ValueError:
            pass  # junk strings reject below with the original value
    if isinstance(raw, bool) or raw not in _META_SCHEMA_VERSIONS:
        raise CandidateArchiveError(f"unknown meta schema_version {meta.get('schema_version')!r}")
    if raw == 1:
        return
    digest = hashlib.sha256(diff_bytes).hexdigest()
    claimed = str(meta.get("manifest_digest") or "")
    if claimed != f"sha256:{digest}":
        raise CandidateArchiveError(
            f"manifest_digest mismatch: meta claims {claimed or '<none>'}, "
            f"diff hashes to sha256:{digest}"
        )


def _parse_started_at(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
