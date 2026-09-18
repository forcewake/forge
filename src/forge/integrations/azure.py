"""Azure DevOps integration: PAT client, CAS writes, PR threads, reader.

First Azure DevOps slice (ADR-0024, milestone AZ-1). Ground truth for every
endpoint, field and error shape is ``docs/research/azure-devops.md`` (tagged
[documented] / [community-documented] / [inference] there); anything not
covered by that document is called out in the docstring of the method that
relies on it. The official ``azure-devops`` Python package is stale at
7.1.0b and sync-only — not adopted (ADR-0024 §1).

Both Azure DevOps Services (``https://dev.azure.com/{org}``) and Server
(``https://{instance}/{collection}``) are served by the same client: the
constructor takes the org/collection URL and every path is appended under
it (research §1.1).

Error taxonomy (mirrors :mod:`forge.gitlab.client` and
:mod:`forge.integrations.github`):

- :class:`AzureDevOpsError` — any non-2xx REST failure carrying the ``TF…``
  error envelope message (research §1.4).
- :class:`AzureDevOpsAuthError` — 401 (expired/revoked PAT, or
  ``TF400813: Resource not available for anonymous access``), or a
  redirect/HTML login page where JSON was expected. The 203
  Non-Authoritative + HTML shape is the documented symptom of a wrongly
  encoded PAT (research §1.2) — never parse errors off such bodies.
- :class:`AzureDevOpsNotFoundError` — 404, which on Azure DevOps means
  "missing OR no permission" (research §1.4) — callers must not treat it
  as proof of absence.
- :class:`AzureDevOpsRateLimited` — 429 (TSTU throttle, research §8.1).
  Retried at most once honoring ``Retry-After`` on idempotent calls, then
  surfaced with ``retry_after`` seconds; the caller owns further waiting.
- :class:`AzureDevOpsDriftError` — a ref update rejected with
  ``updateStatus != "succeeded"`` (``staleObjectId``, ``forcePushRequired``,
  ``createBranchPermissionRequired``, …). Azure returns these under HTTP
  200 — Git ref-update failures do not surface as HTTP errors (research
  §3.3) — so both failure paths are typed.

Retries: 500/502/503/504 (and one 429 honoring ``Retry-After``) are retried
with exponential backoff for idempotent calls only — non-idempotent writes
(pushes, PR creation, threads, comments, dispatch, subscriptions) pass
``retry=False`` so a single HTTP failure cannot duplicate a side effect,
exactly like the GitLab and GitHub transports (the F06 lesson). There is no
token re-mint: a PAT is static, so a 401 is always raised.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from forge.gitlab.blob_reads import BlobReadResult, decode_blob_content
from forge.gitlab.events import UserInfo
from forge.gitlab.schemas import Issue, RepositoryFile, TreeEntry

logger = logging.getLogger(__name__)

#: Pinned REST API version (research §1.3: "API version must be specified
#: with every request"). Server 2022 = API 7.1; pin per connection, not
#: globally — the constructor takes ``default_api_version``.
DEFAULT_API_VERSION = "7.1"

#: The only two preview stripes forge depends on (research §1.3): WIT
#: comments have no GA stripe at 7.1; everything else stays on GA versions.
WIT_COMMENTS_API_VERSION = "7.1-preview.4"

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1  # seconds

#: The 40-zero commit id: "branch does not exist yet" in every ref update —
#: branch creation and the CAS baseline of the first push (research §3.1).
ZERO_COMMIT_SHA = "0" * 40

#: Numeric thread enums for REQUEST bodies (research §4.4: requests take
#: numbers — ``commentType: 1`` = text, ``status: 1`` = active — while
#: responses come back as strings; parse both). Only ``1`` values are
#: pinned by the research doc.
COMMENT_TYPE_TEXT = 1
THREAD_STATUS_ACTIVE = 1

#: Valid push change types (research §3.1 — the enum has more members;
#: forge uses these three).
_CHANGE_TYPES = frozenset({"add", "edit", "delete"})
#: Content encodings for ``newContent`` (research §3.1).
_CONTENT_ENCODINGS = frozenset({"rawtext", "base64"})


class AzureDevOpsError(Exception):
    """Raised when an Azure DevOps REST request fails."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response: httpx.Response | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.response = response
        super().__init__(f"Azure DevOps API error {status_code}: {message}")


class AzureDevOpsAuthError(AzureDevOpsError):
    """401/TF400813, or HTML/redirect where the JSON envelope was expected.

    The 203-Non-Authoritative-plus-login-page shape means the credential is
    mis-encoded or unacceptable (research §1.2) — surfaced as auth config
    error, never parsed for a payload.
    """


class AzureDevOpsNotFoundError(AzureDevOpsError):
    """404 — on Azure DevOps this is "missing OR no permission" (§1.4)."""


class AzureDevOpsRateLimited(AzureDevOpsError):
    """HTTP 429 (TSTU throttle, research §8.1) after the one allowed retry.

    ``retry_after`` is the seconds-to-wait the server asked for (or 60 when
    it sent none) — the caller owns any further waiting.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        response: httpx.Response | None = None,
        retry_after: int = 0,
    ) -> None:
        super().__init__(status_code, message, response)
        self.retry_after = retry_after


class AzureDevOpsDriftError(AzureDevOpsError):
    """A ref update was rejected under HTTP 200 (``GitRefUpdateStatus``).

    The BranchDrift analog for the Azure write path (ADR-0024 §4). ``status``
    is the raw updateStatus string (``staleObjectId``, ``forcePushRequired``,
    ``createBranchPermissionRequired``, …) so callers can distinguish
    "drifted, re-base" from "misconfigured PAT/permission" (research §3.3).
    """

    def __init__(self, ref_name: str, status: str, message: str = "") -> None:
        self.ref_name = ref_name
        self.status = status
        super().__init__(200, message or f"ref {ref_name!r} update rejected: {status}")


@dataclass(frozen=True)
class AzurePullRequest:
    """The normalized PR surface forge consumes (research §4.1/§4.2)."""

    id: int
    title: str
    source_ref_name: str
    target_ref_name: str
    is_draft: bool
    status: str
    #: ``lastMergeCommit.commitId`` — a PLAIN GitCommitRef: the merge-result
    #: commit, NOT the three-way triple (research correction #2).
    last_merge_commit_id: str
    created_by_display_name: str
    #: ``uniqueName`` is the e-mail-like stable identity everywhere (§2.8).
    created_by_unique_name: str
    web_url: str | None


@dataclass(frozen=True)
class PrIteration:
    """One PR iteration — carries the three-way SHAs (research §4.3)."""

    id: int
    #: Head of the source branch AT this iteration (the "after" SHA).
    source_ref_commit: str | None
    #: Head of the target branch at this iteration.
    target_ref_commit: str | None
    #: Merge base of source and target at this iteration.
    common_ref_commit: str | None


@dataclass(frozen=True)
class PipelineRun:
    """Normalized Pipelines Runs API run (research §6.2)."""

    run_id: int
    state: str
    result: str | None
    url: str | None


@dataclass(frozen=True)
class FileChange:
    """One file mutation inside a pushed commit (research §3.1).

    ``path`` is repository-absolute (``/src/app.py``). ``content`` is
    required for ``add``/``edit`` and must be None for ``delete``.
    ``encoding`` is the forge-side spelling; it maps to the API's
    ``contentType`` (``rawtext`` | ``base64encoded``).
    """

    path: str
    change_type: str  # "add" | "edit" | "delete"
    content: str | None = None
    encoding: str = "rawtext"  # "rawtext" | "base64"

    def __post_init__(self) -> None:
        if self.change_type not in _CHANGE_TYPES:
            raise ValueError(
                f"change_type must be one of {sorted(_CHANGE_TYPES)}, got {self.change_type!r}"
            )
        if self.change_type == "delete":
            if self.content is not None:
                raise ValueError("delete changes carry no content")
            return
        if self.content is None:
            raise ValueError(f"{self.change_type} changes require content")
        if self.encoding not in _CONTENT_ENCODINGS:
            raise ValueError(
                f"encoding must be one of {sorted(_CONTENT_ENCODINGS)}, got {self.encoding!r}"
            )


@dataclass(frozen=True)
class CommitPayload:
    """One commit in a push: a comment plus its file changes (§3.1)."""

    comment: str
    changes: list[FileChange]


def _basic_auth_header(token: str) -> str:
    """The exact Basic credential bytes: ``base64(":" + PAT)`` (research §1.2).

    Empty username, colon prefix — a PAT without the colon does not reliably
    401; it silently yields the login page (the 203 trap this header shape
    avoids).
    """
    encoded = base64.b64encode(f":{token}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def _tf_error_message(response: httpx.Response) -> str:
    """Extract the ``message`` field of the TF error envelope (research §1.4).

    Match on the TF code inside the message, never on prose; fall back to
    the raw body when the envelope is absent or not JSON.
    """
    try:
        data = response.json()
    except Exception:
        return response.text
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) and message else response.text


def _looks_like_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


def _ref_update_results(payload: Any) -> list[dict[str, Any]]:
    """Extract ``GitRefUpdateResult`` entries defensively (research §10.11).

    The documented responses are a bare array (Refs - Update) while the
    pushes endpoint's wrapping object is NOT shown in documented samples —
    accept a bare list, a ``value``-wrapped array, and the ``Push`` shape
    (``refUpdates``) so the AZ-4 lab run cannot break the parser.
    """
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("value", "refUpdates"):
            wrapped = payload.get(key)
            if isinstance(wrapped, list):
                return [entry for entry in wrapped if isinstance(entry, dict)]
    return []


def _raise_on_failed_ref_update(payload: Any) -> None:
    """Type the per-ref failure taxonomy (research §3.3): 200 ≠ success."""
    for result in _ref_update_results(payload):
        status = result.get("updateStatus")
        if status is not None and status != "succeeded":
            raise AzureDevOpsDriftError(
                ref_name=str(result.get("name") or ""),
                status=str(status),
                message=str(result.get("customMessage") or ""),
            )


def _commit_id(commit: Any) -> str | None:
    """Pull ``commitId`` out of a GitCommitRef defensively."""
    if isinstance(commit, dict):
        value = commit.get("commitId")
        if isinstance(value, str):
            return value
    return None


class AzureDevOpsClient:
    """Async client for the Azure DevOps REST API 7.1 (ADR-0024 slice)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        default_api_version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )
        self._token = token
        self._default_api_version = default_api_version

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AzureDevOpsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- transport --------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        api_version: str | None = None,
    ) -> httpx.Response:
        """Request with Basic auth + api-version injection and typed errors.

        - The auth header is computed PER REQUEST from the PAT
          (``base64(":" + PAT)``, research §1.2) — never baked into the
          shared client, so a rotated token takes effect immediately.
        - ``api-version`` is injected centrally (research §1.3: every
          request must carry it); *api_version* overrides for the two
          preview stripes forge pins.
        - Retries: 500/502/503/504 with exponential backoff, idempotent
          calls only (``retry=False`` marks non-idempotent writes); 429 is
          retried AT MOST ONCE honoring ``Retry-After`` (research §8.1),
          then raised as :class:`AzureDevOpsRateLimited`.
        - A redirect or an HTML body where the JSON envelope was expected is
          raised as :class:`AzureDevOpsAuthError` (research §1.2: the 203
          login-page trap) — never returned for parsing.
        """
        merged_params: dict[str, Any] = {"api-version": api_version or self._default_api_version}
        merged_params.update(params or {})
        request_headers = {
            "Authorization": _basic_auth_header(self._token),
            "Accept": "application/json",
            **(headers or {}),
        }
        max_attempts = _MAX_RETRIES if retry else 1
        for attempt in range(max_attempts):
            try:
                response = await self._client.request(
                    method, path, json=json, params=merged_params, headers=request_headers
                )
            except httpx.HTTPError as exc:
                if attempt < max_attempts - 1:
                    delay = _BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "HTTP error on %s %s (attempt %d): %s — retrying in %ds",
                        method,
                        path,
                        attempt + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            status = response.status_code
            if 300 <= status < 400 or "text/html" in response.headers.get("content-type", ""):
                raise AzureDevOpsAuthError(
                    status,
                    "received an HTML/redirect response instead of the API envelope "
                    "(mis-encoded or unacceptable credential, research §1.2)",
                    response=response,
                )
            if status < 400:
                return response
            if status == 401:
                # A PAT is static: no re-mint, no retry — always raised.
                raise AzureDevOpsAuthError(status, _tf_error_message(response), response=response)
            if status == 404:
                raise AzureDevOpsNotFoundError(
                    status, _tf_error_message(response), response=response
                )
            if status == 429:
                wait = _retry_after_seconds(response)
                if retry:
                    logger.warning(
                        "%s %s throttled (429) — honoring Retry-After (%ds) once",
                        method,
                        path,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    retry = False  # one Retry-After retry, then surface
                    continue
                raise AzureDevOpsRateLimited(
                    status, _tf_error_message(response), response=response, retry_after=wait
                )
            if status in _RETRYABLE_STATUSES and attempt < max_attempts - 1:
                delay = _BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "%s %s returned %d (attempt %d) — retrying in %ds",
                    method,
                    path,
                    status,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise AzureDevOpsError(status, _tf_error_message(response), response=response)
        raise AzureDevOpsError(0, "unreachable: retry loop exited")  # pragma: no cover

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, *, retry: bool = False, **kwargs: Any) -> httpx.Response:
        """POST with explicit retry semantics.

        Non-idempotent by default here (the F06 lesson): every write this
        client performs is creation/dispatch — a lost response must be
        reconciled downstream, never replayed. Pass ``retry=True`` only for
        genuinely idempotent POSTs.
        """
        return await self._request("POST", path, retry=retry, **kwargs)

    # -- REST: projects / repositories / refs (read surface) ----------------------

    async def get_project(self, project: str) -> dict[str, Any]:
        """One project by name or GUID (``GET /_apis/projects/{project}``)."""
        from urllib.parse import quote

        response = await self._get(f"/_apis/projects/{quote(project, safe='')}")
        return dict(response.json())

    async def get_repository(self, project: str, repo: str) -> dict[str, Any]:
        """One repository by name or GUID.

        The payload carries ``id`` (GUID), ``default_branch``
        (``refs/heads/…``) and the ``project`` ref — the identity triple
        every other call keying off (research §2.1).
        """
        response = await self._get(f"/{quote(project)}/_apis/git/repositories/{quote(repo)}")
        return dict(response.json())

    async def get_refs(
        self, project: str, repo: str, filter: str | None = None
    ) -> list[dict[str, Any]]:
        """Repository refs, optionally filtered (``filter=heads/{branch}``).

        Each ref carries ``objectId`` — the CAS ``oldObjectId`` of the next
        push (research §3.2).
        """
        params: dict[str, Any] = {}
        if filter is not None:
            params["filter"] = filter
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/refs", params=params
        )
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        return [ref for ref in value or [] if isinstance(ref, dict)]

    async def get_branch_head(self, project: str, repo: str, branch: str) -> str:
        """The current tip commit SHA of *branch* — one filtered refs read.

        Head/drift checks must never paginate history; the ref's
        ``objectId`` is the push CAS (research §3.2). 404 propagates as
        :class:`AzureDevOpsNotFoundError`.
        """
        refs = await self.get_refs(project, repo, filter=f"heads/{branch}")
        if refs:
            object_id = refs[0].get("objectId")
            if object_id:
                return str(object_id)
        raise AzureDevOpsNotFoundError(404, f"branch head not found for {branch!r}")

    async def list_commits(
        self, project: str, repo: str, branch: str, top: int = 30
    ) -> list[dict[str, Any]]:
        """List commits on *branch*, newest first (the R11 probe read).

        ``GET .../commits?searchCriteria.itemVersion.version=<branch>`` —
        the branch-scoped listing the research doc §5.1 prescribes for
        reconciling a lost push response. Returns client-normalized
        ``{"commit_id", "comment", "parents"}`` dicts so the
        publication-intent probe's matcher
        (:func:`forge.durable.intents.commit_matches`) works identically
        across providers.
        """
        params: dict[str, Any] = {
            "searchCriteria.itemVersion.version": branch,
            "searchCriteria.itemVersion.versionType": "branch",
            "$top": top,
        }
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/commits", params=params
        )
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        commits: list[dict[str, Any]] = []
        for raw in value or []:
            if not isinstance(raw, dict):
                continue
            parents = raw.get("parents") or raw.get("parentCommitIds") or []
            commits.append(
                {
                    "commit_id": str(raw.get("commitId") or ""),
                    "comment": str(raw.get("comment") or ""),
                    "parents": [str(parent) for parent in parents],
                }
            )
        return commits

    async def get_item(
        self,
        project: str,
        repo: str,
        path: str,
        *,
        version: str | None = None,
        version_type: str | None = None,
    ) -> dict[str, Any]:
        """One item's metadata + inline content at an explicit version.

        Items API with ``includeContent=true`` (research §1 table);
        *version* pins ``versionDescriptor.version`` (a commit SHA in
        forge's usage) and *version_type* the descriptor type
        (``commit`` | ``branch`` | ``tag``, default ``commit``). No version
        descriptor = the default branch tip.
        """
        params: dict[str, Any] = {"path": path, "includeContent": "true"}
        if version is not None:
            params["versionDescriptor.version"] = version
            params["versionDescriptor.versionType"] = version_type or "commit"
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/items", params=params
        )
        return dict(response.json())

    async def get_tree(
        self, project: str, repo: str, tree_id: str, *, recursive: bool = False
    ) -> dict[str, Any]:
        """The git tree at *tree_id* (a commit SHA; ``truncated`` possible).

        Entries carry ``objectId``/``gitObjectType``/``relativePath``. A
        ``truncated: true`` response MUST be refused by callers needing
        completeness (AuthoritativeReader semantics) — the flag is surfaced,
        never swallowed.
        """
        params: dict[str, Any] = {}
        if recursive:
            params["recursive"] = "true"
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/trees/{quote(tree_id)}",
            params=params,
        )
        return dict(response.json())

    # -- REST: git CAS write surface (research §3) ---------------------------------

    async def create_branch_from(
        self, project: str, repo: str, branch: str, base_sha: str
    ) -> dict[str, Any]:
        """Create ``refs/heads/{branch}`` pointing AT *base_sha*.

        Uses the Refs-Update API (``POST …/refs``) with the 40-zero
        ``oldObjectId`` and ``newObjectId = base_sha`` — the documented
        "create branch at an existing commit" primitive (research §3.1: the
        pushes API cannot point a branch at an existing commit without also
        adding a commit). "You must specify both the old and new commit to
        avoid race conditions." Non-idempotent: a 200-with-rejection
        (``createBranchPermissionRequired``) raises
        :class:`AzureDevOpsDriftError`; callers treat an already-exists
        rejection as idempotent re-entry.
        """
        # LIVE-found (ADR-0024 lab): the Refs-Update body is the BARE
        # array — an object wrapper makes AzDO read "refUpdates": null
        # (400 "Value cannot be null. Parameter name: refUpdates").
        response = await self._post(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/refs",
            json=[
                {
                    "name": f"refs/heads/{branch}",
                    "oldObjectId": ZERO_COMMIT_SHA,
                    "newObjectId": base_sha,
                }
            ],
        )
        payload = response.json()
        _raise_on_failed_ref_update(payload)
        # Documented response is a bare GitRefUpdateResult array.
        return {"value": payload} if isinstance(payload, list) else dict(payload)

    async def push_commits(
        self,
        project: str,
        repo: str,
        branch: str,
        *,
        expected_old_sha: str,
        commits: list[CommitPayload],
    ) -> dict[str, Any]:
        """Push one or more commits to *branch* with a CAS ref update.

        Pushes API (research §3.1): ``refUpdates[0].oldObjectId`` is the
        expected parent tip — a moved head fails with
        ``updateStatus: staleObjectId`` under HTTP 200, surfaced as
        :class:`AzureDevOpsDriftError` (never retried, never force-pushed;
        ADR-0024 §4). Non-idempotent: a lost response must be reconciled by
        re-reading the ref, never replayed. *branch* is the bare name; the
        ``refs/heads/`` prefix is applied here.
        """
        response = await self._post(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/pushes",
            json={
                "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": expected_old_sha}],
                "commits": [_commit_to_json(commit) for commit in commits],
            },
        )
        _raise_on_failed_ref_update(response.json())
        return dict(response.json())

    # -- REST: pull requests (research §4) -------------------------------------------

    async def create_draft_pr(
        self,
        project: str,
        repo: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Open a Draft PR (``isDraft: true``, research §4.1).

        The only kind of PR forge ever creates (ADR-0024 §5) — the bot
        identity cannot complete or vote on it. Non-idempotent: callers
        find-by-head first and adopt, never replay. Work-item linking is
        deliberately NOT sent at create time (``workItemRefs`` is unreliable
        programmatically, research §4.6) — the reliable link is the WIT
        ArtifactLink PATCH, owned by the AZ-2 run service.
        """
        response = await self._post(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/pullrequests",
            json={
                "sourceRefName": f"refs/heads/{source_branch}",
                "targetRefName": f"refs/heads/{target_branch}",
                "title": title,
                "description": description,
                "isDraft": True,
            },
        )
        return dict(response.json())

    async def get_pr(self, project: str, repo: str, pr_id: int) -> AzurePullRequest:
        """One PR, normalized onto :class:`AzurePullRequest` (research §4.2)."""
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/pullRequests/{pr_id}"
        )
        return _map_pull_request(response.json())

    async def get_pr_iterations(self, project: str, repo: str, pr_id: int) -> list[PrIteration]:
        """The PR's iterations — the ONLY source of the three-way SHAs.

        ``lastMergeCommit`` is a plain GitCommitRef; the reactive lane's
        before/after/base triple lives on the iteration objects
        (``sourceRefCommit``/``targetRefCommit``/``commonRefCommit``,
        research correction #2 and §4.3).
        """
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}"
            f"/pullRequests/{pr_id}/iterations"
        )
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        return [
            PrIteration(
                id=int(iteration.get("id") or 0),
                source_ref_commit=_commit_id(iteration.get("sourceRefCommit")),
                target_ref_commit=_commit_id(iteration.get("targetRefCommit")),
                common_ref_commit=_commit_id(iteration.get("commonRefCommit")),
            )
            for iteration in value or []
            if isinstance(iteration, dict)
        ]

    async def list_pr_threads(self, project: str, repo: str, pr_id: int) -> list[dict[str, Any]]:
        """The PR's comment threads (string status/commentType on parse)."""
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/pullRequests/{pr_id}/threads"
        )
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        return [thread for thread in value or [] if isinstance(thread, dict)]

    async def create_pr_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        content: str,
        *,
        status: int = THREAD_STATUS_ACTIVE,
        file_path: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        offset_start: int = 1,
        offset_end: int = 1,
        change_tracking_id: int | None = None,
        first_comparing_iteration: int | None = None,
        second_comparing_iteration: int | None = None,
    ) -> dict[str, Any]:
        """Create one PR discussion thread (review surface, research §4.4).

        Request enums are NUMERIC (``commentType: 1``, ``status: 1``);
        responses come back as strings. *file_path* (+ optional 1-based
        *line_start*/*line_end* and *offset_start*/*offset_end*) builds
        ``threadContext`` (right side = source/head version);
        *change_tracking_id* + *first/second_comparing_iteration* build
        ``pullRequestThreadContext`` — the sticky-thread mechanism that
        re-tracks the comment across iterations server-side (do NOT
        re-derive positions client-side per push, research §4.4).
        Non-idempotent: a lost thread is reconciled by the caller scanning
        :meth:`list_pr_threads`, never replayed.
        """
        payload: dict[str, Any] = {
            "comments": [
                {"parentCommentId": 0, "content": content, "commentType": COMMENT_TYPE_TEXT}
            ],
            "status": status,
        }
        if file_path is not None:
            thread_context: dict[str, Any] = {"filePath": file_path}
            if line_start is not None:
                thread_context["rightFileStart"] = {"line": line_start, "offset": offset_start}
            if line_end is not None:
                thread_context["rightFileEnd"] = {"line": line_end, "offset": offset_end}
            payload["threadContext"] = thread_context
        if change_tracking_id is not None:
            pr_context: dict[str, Any] = {"changeTrackingId": change_tracking_id}
            if first_comparing_iteration is not None or second_comparing_iteration is not None:
                pr_context["iterationContext"] = {
                    "firstComparingIteration": first_comparing_iteration or 1,
                    "secondComparingIteration": second_comparing_iteration
                    or first_comparing_iteration
                    or 1,
                }
            payload["pullRequestThreadContext"] = pr_context
        response = await self._post(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/pullRequests/{pr_id}/threads",
            json=payload,
        )
        return dict(response.json())

    async def reply_pr_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        thread_id: int,
        content: str,
        *,
        parent_comment_id: int = 0,
    ) -> dict[str, Any]:
        """Reply inside an existing thread (research §4.4; ≤500 comments)."""
        response = await self._post(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}"
            f"/pullRequests/{pr_id}/threads/{thread_id}/comments",
            json={
                "content": content,
                "parentCommentId": parent_comment_id,
                "commentType": COMMENT_TYPE_TEXT,
            },
        )
        return dict(response.json())

    async def update_thread_status(
        self, project: str, repo: str, pr_id: int, thread_id: int, status: int
    ) -> dict[str, Any]:
        """Update a thread's status — the sticky-progress mechanism.

        *status* is the numeric ``CommentThreadStatus`` enum (requests take
        numbers, research §4.4; documented members: unknown/active/fixed/
        wontFix/closed/byDesign). Setting a status is idempotent-by-value,
        so unlike the POSTs this PATCH may retry.
        """
        response = await self._request(
            "PATCH",
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}"
            f"/pullRequests/{pr_id}/threads/{thread_id}",
            json={"status": status},
        )
        return dict(response.json())

    # -- REST: work items (plan/gate comment surface, research §5) -------------------

    async def get_work_item(self, project: str, work_item_id: int) -> dict[str, Any]:
        """One work item with fields (``System.Title``/``State``/``History``…)."""
        response = await self._get(f"/{quote(project)}/_apis/wit/workItems/{work_item_id}")
        return dict(response.json())

    async def add_work_item_comment(
        self, project: str, work_item_id: int, text: str
    ) -> dict[str, Any]:
        """Post a markdown comment (the plan-comment surface, research §5.1).

        Pins the only preview stripe forge depends on
        (``7.1-preview.4``) with ``format=markdown``. The response's
        ``commentId`` is the plan-comment provenance identity — persist it.
        Non-idempotent: forge's own comment is deduplicated downstream via
        the bot-loop guard, never by replaying the POST.
        """
        response = await self._post(
            f"/{quote(project)}/_apis/wit/workItems/{work_item_id}/comments",
            params={"format": "markdown"},
            api_version=WIT_COMMENTS_API_VERSION,
            json={"text": text},
        )
        return dict(response.json())

    async def get_work_item_comments(self, project: str, work_item_id: int) -> dict[str, Any]:
        """The work item's comment batch (``{comments: [...], count: N}``)."""
        response = await self._get(
            f"/{quote(project)}/_apis/wit/workItems/{work_item_id}/comments",
            api_version=WIT_COMMENTS_API_VERSION,
        )
        return dict(response.json())

    # -- REST: pipelines / builds (execution adapter surface, research §6) -----------

    async def run_pipeline(
        self,
        project: str,
        pipeline_id: int,
        *,
        ref_name: str,
        template_parameters: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
    ) -> PipelineRun:
        """Dispatch a pipeline run; correlate via the returned run id.

        Runs API (research §6.2): the response CARRIES the run id — the
        correlation handle (no list-and-guess). *ref_name* is the full
        branch ref; template parameters cross the REST boundary as strings
        (the lane template takes everything as strings, research §6.2).
        Non-idempotent: a replay would start a SECOND harness run — a lost
        response is reconciled by discovery, never retried.
        """
        body: dict[str, Any] = {"resources": {"repositories": {"self": {"refName": ref_name}}}}
        if template_parameters:
            body["templateParameters"] = template_parameters
        if variables:
            body["variables"] = variables
        response = await self._post(
            f"/{quote(project)}/_apis/pipelines/{pipeline_id}/runs", json=body
        )
        return _map_run(response.json())

    async def get_run(self, project: str, pipeline_id: int, run_id: int) -> PipelineRun:
        """Poll one pipeline run (``state``/``result``, research §6.2)."""
        response = await self._get(f"/{quote(project)}/_apis/pipelines/{pipeline_id}/runs/{run_id}")
        return _map_run(response.json())

    async def get_build(self, project: str, build_id: int) -> dict[str, Any]:
        """One Build object (``sourceVersion``/``result``/``reason`` —
        runId == buildId for YAML pipeline runs, research §6.3)."""
        response = await self._get(f"/{quote(project)}/_apis/build/builds/{build_id}")
        return dict(response.json())

    async def cancel_build(self, project: str, build_id: int) -> dict[str, Any]:
        """Request cancellation (research §6.5).

        The Runs area has NO cancel — cancellation goes through the Builds
        area with ``PATCH {"status": "cancelling"}``. Poll to
        ``result == "canceled"`` afterwards: queued jobs may survive the
        PATCH (research §6.5 caveat).
        """
        response = await self._request(
            "PATCH",
            f"/{quote(project)}/_apis/build/builds/{build_id}",
            json={"status": "cancelling"},
        )
        return dict(response.json())

    async def list_builds_by_repository(
        self,
        project: str,
        repo_id: str,
        *,
        definitions: list[int] | None = None,
        min_time: datetime | None = None,
        top: int = 25,
    ) -> list[dict[str, Any]]:
        """Builds for a repository, newest queue time first.

        DOCUMENTED QUERY PARAMS ONLY (research §6.6 correction #1):
        ``repositoryId``, ``definitions``, ``minTime``, ``queryOrder``,
        ``$top``. There is NO ``sourceVersion`` filter — it exists only as a
        response field; the commit correlation happens client-side (or via
        the ``build.complete`` webhook, which carries ``sourceVersion``).
        """
        params: dict[str, Any] = {
            "repositoryId": repo_id,
            "queryOrder": "queueTimeDescending",
            "$top": top,
        }
        if definitions:
            params["definitions"] = ",".join(str(d) for d in definitions)
        if min_time is not None:
            params["minTime"] = _format_utc(min_time)
        response = await self._get(f"/{quote(project)}/_apis/build/builds", params=params)
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        return [build for build in value or [] if isinstance(build, dict)]

    async def get_timeline(self, project: str, build_id: int) -> dict[str, Any]:
        """The build timeline (records with ``result``/``log`` refs, §6.4).

        The debug lane picks ``type == "Task"`` + ``result == "failed"``
        records and fetches ``record.log.id`` via :meth:`get_task_log`.
        """
        response = await self._get(f"/{quote(project)}/_apis/build/builds/{build_id}/timeline")
        return dict(response.json())

    async def get_task_log(self, project: str, build_id: int, log_id: int) -> str:
        """One task log's plain-text body (``record.log.id``, research §6.4)."""
        response = await self._get(f"/{quote(project)}/_apis/build/builds/{build_id}/logs/{log_id}")
        return response.text

    async def get_run_artifact_signed_url(
        self, project: str, pipeline_id: int, run_id: int, artifact_name: str
    ) -> str:
        """The expiring signed download URL of one run artifact (§6.3).

        ``$expand=signedContent`` returns ``signedContent.url`` — a
        limited-time anonymous URL: download promptly, never persist it.
        """
        response = await self._get(
            f"/{quote(project)}/_apis/pipelines/{pipeline_id}/runs/{run_id}/artifacts",
            params={"artifactName": artifact_name, "$expand": "signedContent"},
        )
        data = response.json()
        signed = data.get("signedContent") if isinstance(data, dict) else None
        url = signed.get("url") if isinstance(signed, dict) else None
        if not url:
            raise AzureDevOpsNotFoundError(
                404,
                f"artifact {artifact_name!r} has no signedContent url "
                "(not published, or the run has not produced it yet)",
            )
        return str(url)

    # -- REST: service hooks (onboarding, research §2.0) ------------------------------

    async def create_hook_subscription(
        self,
        event_type: str,
        filters: dict[str, str],
        url: str,
        username: str,
        password: str,
        *,
        http_headers: str | None = None,
    ) -> dict[str, Any]:
        """Provision one webhook subscription (org-level endpoint).

        Body per the documented contract (research §2.0): publisher ``tfs``,
        consumer ``webHooks``, action ``httpRequest``, ``resourceVersion``
        ``1.0``, forge's Basic credentials in ``consumerInputs`` (HTTPS
        required — Azure webhooks have NO HMAC; those credentials ARE the
        authenticator, ADR-0024 §3). Non-idempotent: onboarding owns
        find-then-create; a replay would double-deliver events.
        """
        consumer_inputs: dict[str, Any] = {
            "url": url,
            "basicAuthUsername": username,
            "basicAuthPassword": password,
            "resourceDetailsToSend": "all",
        }
        if http_headers is not None:
            consumer_inputs["httpHeaders"] = http_headers
        response = await self._post(
            "/_apis/hooks/subscriptions",
            json={
                "publisherId": "tfs",
                "eventType": event_type,
                "resourceVersion": "1.0",
                "consumerId": "webHooks",
                "consumerActionId": "httpRequest",
                "publisherInputs": dict(filters),
                "consumerInputs": consumer_inputs,
            },
        )
        return dict(response.json())

    async def list_hook_subscriptions(self) -> list[dict[str, Any]]:
        """All org-level service-hook subscriptions."""
        response = await self._get("/_apis/hooks/subscriptions")
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        return [sub for sub in value or [] if isinstance(sub, dict)]

    async def delete_hook_subscription(self, subscription_id: str) -> None:
        """Remove one subscription (offboarding / reprovision)."""
        await self._request("DELETE", f"/_apis/hooks/subscriptions/{subscription_id}")

    # -- REST: AZ-3 additions (reactive review + debug + executor seams) ----------
    # Everything below was added additively in AZ-3 (ADR-0024); the methods
    # above are the AZ-1 surface and must not be modified.

    async def get_pr_iteration_changes(
        self,
        project: str,
        repo: str,
        pr_id: int,
        iteration_id: int,
        *,
        compare_to: int | None = None,
        top: int | None = None,
    ) -> list[dict[str, Any]]:
        """The file changes of one PR iteration (research §4.3).

        ``GET .../pullRequests/{prId}/iterations/{iterationId}/changes`` —
        *compare_to* pins ``$compareTo`` (the iteration to diff against;
        omitted = the iteration's full change set). Returns the raw
        ``changeEntries`` (``GitPullRequestChange``: ``changeType``,
        ``item.path``, ``originalPath``) defensively — entries carry PATHS,
        not patch content, so the reactive reviewer renders diffs itself
        from item contents.
        """
        params: dict[str, Any] = {}
        if compare_to is not None:
            params["$compareTo"] = compare_to
        if top is not None:
            params["$top"] = top
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}"
            f"/pullRequests/{pr_id}/iterations/{iteration_id}/changes",
            params=params,
        )
        data = response.json()
        entries = data.get("changeEntries") if isinstance(data, dict) else None
        if entries is None:
            value = data.get("value") if isinstance(data, dict) else data
            entries = value
        return [entry for entry in entries or [] if isinstance(entry, dict)]

    async def list_pull_requests(
        self, project: str, repo: str, *, status: str = "active", top: int = 50
    ) -> list[dict[str, Any]]:
        """The repository's pull requests, filtered (research §4.1).

        ``GET .../pullrequests?searchCriteria.status={status}`` — the
        client-side join key for the CI debug lane (a failed build's
        ``sourceVersion`` matched against each PR's merge SHAs, research
        §6.6). Returns the raw GitPullRequest payloads.
        """
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/pullrequests",
            params={"searchCriteria.status": status, "$top": top},
        )
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        return [pr for pr in value or [] if isinstance(pr, dict)]

    async def download_run_artifact(
        self, project: str, pipeline_id: int, run_id: int, artifact_name: str
    ) -> bytes:
        """One run artifact's zip bytes via its signed URL (research §6.3).

        Composes :meth:`get_run_artifact_signed_url` with a plain GET. The
        download deliberately carries NO Authorization header: the signed
        URL may point at storage outside the Azure DevOps host, and forge
        never sends its PAT to a third-party host. Callers must download
        promptly — the signed URL expires (minutes, not hours).
        """
        url = await self.get_run_artifact_signed_url(project, pipeline_id, run_id, artifact_name)
        response = await self._client.get(url)
        if response.status_code >= 400:
            raise AzureDevOpsError(
                response.status_code,
                f"artifact download failed for {artifact_name!r}: HTTP {response.status_code}",
            )
        return response.content

    async def get_commit(self, project: str, repo: str, commit_id: str) -> dict[str, Any]:
        """One commit's metadata (includes ``treeId`` — the trees API
        rejects commit SHAs directly, LIVE-found in the ADR-0024 lab)."""
        response = await self._get(
            f"/{quote(project)}/_apis/git/repositories/{quote(repo)}/commits/{quote(commit_id)}",
        )
        return dict(response.json())

    # -- REST: AZ-4 additions (work-item link, repo resolution, PR dedupe) --------
    # Everything below was added additively in AZ-4 (ADR-0024); the methods
    # above are the AZ-1/AZ-3 surface and must not be modified.

    async def update_work_item(
        self, project: str, work_item_id: int, patch_ops: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Apply a JSON-Patch to a work item (WIT PATCH, research §4.6).

        ``PATCH /{project}/_apis/wit/workItems/{id}`` with
        ``Content-Type: application/json-patch+json`` and *patch_ops* as the
        raw json-patch operation list — the documented work-item mutation
        surface (the only one forge uses: the ArtifactLink relation add).
        Retry-less like the other writes: a replayed ``relations/-`` add
        would duplicate the relation, so a lost response is reconciled by
        re-reading the work item, never by resending.
        """
        response = await self._request(
            "PATCH",
            f"/{quote(project)}/_apis/wit/workItems/{work_item_id}",
            headers={"Content-Type": "application/json-patch+json"},
            json=patch_ops,
            retry=False,
        )
        return dict(response.json())

    async def link_work_item_to_pr(
        self, project: str, work_item_id: int, project_id: str, repo_id: str, pr_id: int
    ) -> dict[str, Any]:
        """Link a work item to a PR via the WIT ArtifactLink PATCH (§4.6).

        The reliable programmatic link — the PR-create body's
        ``workItemRefs`` is unreliable (research correction #4): a
        json-patch add of an ``ArtifactLink`` relation whose vstfs URL
        embeds the project/repository/PR ids (``%2F``-separated) and whose
        ``attributes.name`` is the CASE-SENSITIVE ``"Pull Request"`` — the
        exact artifactId template or the link renders one-way. Non-idempotent
        through :meth:`update_work_item`'s retry posture.
        """
        artifact_url = f"vstfs:///Git/PullRequestId/{project_id}%2F{repo_id}%2F{pr_id}"
        return await self.update_work_item(
            project,
            work_item_id,
            [
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "ArtifactLink",
                        "url": artifact_url,
                        "attributes": {"name": "Pull Request"},
                    },
                }
            ],
        )

    async def list_repositories(self, project: str) -> list[dict[str, Any]]:
        """The project's git repositories (``GET /{project}/_apis/git/repositories``).

        Each payload carries ``id`` (GUID), ``name`` and the ``project``
        ref — the resolution surface for repo-less work-item commands (the
        documented replacement for the AZ-2 name-guessing fallback, which
        assumed the default repository shares the project's name).
        """
        response = await self._get(f"/{quote(project)}/_apis/git/repositories")
        data = response.json()
        value = data.get("value") if isinstance(data, dict) else data
        return [repo for repo in value or [] if isinstance(repo, dict)]

    async def find_draft_pr_by_head(
        self, project: str, repo: str, source_branch: str
    ) -> dict[str, Any] | None:
        """The newest OPEN Draft PR from *source_branch*, or None (research §4.1).

        The find-by-head-first dedupe for PR creation: :meth:`create_draft_pr`
        is non-idempotent, so callers look BEFORE they create and ADOPT the
        existing open draft on the same source branch instead of forking a
        second PR. Matches the active-PR list (client-side join — the list
        API has no head-branch equality beyond ``searchCriteria``) on the
        bare branch name (payloads carry full ``refs/heads/…`` refs);
        newest first by creation date, id breaking ties. A 404/empty list is
        a legitimate "none" here — listing is not the ambiguity-prone 404
        of :meth:`get_pr`.
        """
        pulls = await self.list_pull_requests(project, repo, status="active")
        wanted = source_branch.removeprefix("refs/heads/")
        matches = [
            pr
            for pr in pulls
            if bool(pr.get("isDraft"))
            and str(pr.get("sourceRefName") or "").removeprefix("refs/heads/") == wanted
        ]
        matches.sort(
            key=lambda pr: (str(pr.get("creationDate") or ""), int(pr.get("pullRequestId") or 0)),
            reverse=True,
        )
        return matches[0] if matches else None


def _commit_to_json(commit: CommitPayload) -> dict[str, Any]:
    """Map a :class:`CommitPayload` onto the push-API changes shape (§3.1)."""
    changes: list[dict[str, Any]] = []
    for change in commit.changes:
        entry: dict[str, Any] = {
            "changeType": change.change_type,
            "item": {"path": change.path},
        }
        if change.content is not None:
            # API spelling is "base64encoded" (research §3.1), forge's is "base64".
            content_type = "base64encoded" if change.encoding == "base64" else "rawtext"
            entry["newContent"] = {"content": change.content, "contentType": content_type}
        changes.append(entry)
    return {"comment": commit.comment, "changes": changes}


def _map_pull_request(data: dict[str, Any]) -> AzurePullRequest:
    """Map a GitPullRequest onto the normalized DTO (research §4.2)."""
    created_by = data.get("createdBy") or {}
    web_url = None
    links = data.get("_links")
    if isinstance(links, dict):
        web = links.get("web")
        if isinstance(web, dict) and isinstance(web.get("href"), str):
            web_url = web["href"]
    return AzurePullRequest(
        id=int(data.get("pullRequestId") or 0),
        title=str(data.get("title") or ""),
        source_ref_name=str(data.get("sourceRefName") or ""),
        target_ref_name=str(data.get("targetRefName") or ""),
        is_draft=bool(data.get("isDraft")),
        status=str(data.get("status") or ""),
        last_merge_commit_id=_commit_id(data.get("lastMergeCommit")) or "",
        created_by_display_name=str(created_by.get("displayName") or ""),
        created_by_unique_name=str(created_by.get("uniqueName") or ""),
        web_url=web_url,
    )


def _map_run(data: dict[str, Any]) -> PipelineRun:
    """Map a Pipelines Run onto the normalized DTO (research §6.2)."""
    result = data.get("result")
    return PipelineRun(
        run_id=int(data.get("id") or 0),
        state=str(data.get("state") or ""),
        result=str(result) if result is not None else None,
        url=data.get("url") if isinstance(data.get("url"), str) else None,
    )


def _retry_after_seconds(response: httpx.Response) -> int:
    """The server's ``Retry-After`` wait; 60 s when the header is absent.

    An explicit ``0`` is honored verbatim (server says "go again now") —
    only the missing-header case gets the defensive default.
    """
    raw = response.headers.get("retry-after")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return 60


def _format_utc(value: datetime) -> str:
    """ISO-8601 UTC for ``minTime``-style query params (naive = assume UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AzureRepositoryReader:
    """Authoritative repository reads over the items/trees APIs.

    Bound to one project + repository; duck-types the same read surface as
    :class:`~forge.gitlab.client.GitLabClient` and
    :class:`~forge.integrations.github.GitHubRepositoryReader`
    (``get_file`` / ``get_tree`` / ``get_issue`` — the
    :class:`~forge.factory.implementer.RepositoryReader` protocol), so
    :class:`~forge.factory.implementer.LLMImplementer` and
    ``load_project_config`` run against Azure DevOps unchanged. AZ-2 wiring
    TODO: widen the ``load_project_config`` union for this class.

    AuthoritativeReader semantics (ADR-0016 §2): reads at explicit SHAs
    (the frozen attempt base), full content never truncated, symlinks
    rejected explicitly, and a truncated tree listing raises instead of
    silently returning a partial snapshot.
    """

    def __init__(self, client: AzureDevOpsClient, project: str, repo: str) -> None:
        self._client = client
        self._project = project
        self._repo = repo

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD") -> RepositoryFile:
        """Complete file content at *ref*, base64-encoded like GitLab's schema.

        ``project_id`` is accepted for signature compatibility with the
        shared read surface and ignored — the reader is repo-bound. ``ref``
        is a commit SHA (forge's usage: the frozen attempt base), a branch
        name, or HEAD (the default branch tip, resolved server-side by
        omitting the version descriptor).
        """
        data = await self._client.get_item(
            self._project,
            self._repo,
            file_path,
            version=None if ref == "HEAD" else ref,
            version_type=None
            if ref == "HEAD"
            else ("commit" if _looks_like_sha(ref) else "branch"),
        )
        if data.get("isSymLink"):
            raise AzureDevOpsError(422, f"{file_path!r} is a symlink, not a regular file")
        content = data.get("content")
        if content is None:
            # Honesty over silent truncation: the items API delivered no
            # inline content — refuse rather than return an empty file.
            raise AzureDevOpsError(
                200,
                f"{file_path!r} at {ref!r}: items API returned no inline content",
            )
        text = str(content)
        metadata = data.get("contentMetadata") or {}
        size = metadata.get("size") if isinstance(metadata, dict) else None
        return RepositoryFile.model_validate(
            {
                "file_name": file_path.rsplit("/", 1)[-1],
                "file_path": file_path,
                "size": size if isinstance(size, int) else len(text.encode("utf-8")),
                "encoding": "base64",
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "ref": ref,
            }
        )

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        """Decoded full text of *file_path* at *ref*."""
        repo_file = await self.get_file(0, file_path, ref)
        try:
            return base64.b64decode(repo_file.content).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AzureDevOpsError(200, f"{file_path!r}: undecodable content") from exc

    async def read_blob(
        self, project_id: int, file_path: str, ref: str = "HEAD"
    ) -> BlobReadResult:
        """One AUTHORITATIVE blob read as a typed result (R14).

        Provider-verified outcome for the create-vs-update existence
        policy: a DevOps 404 is ``not_found`` (NOTE §1.4: Azure conflates
        "missing OR no permission" in 404 — it is still the provider's only
        absence signal, and everything ambiguous AROUND it stays honest:
        401/HTML-envelope auth errors are ``forbidden``, throttles/5xx/
        transport errors ``unavailable``, and the reader's own
        no-inline-content and symlink refusals ``incomplete``). Strict
        UTF-8 decode — never ``errors="replace"``-mangled.
        """
        try:
            repo_file = await self.get_file(project_id, file_path, ref)
        except AzureDevOpsRateLimited as exc:
            return BlobReadResult.unavailable(
                f"{file_path!r} at {ref!r}: throttled "
                f"(retry after {exc.retry_after}s): {exc.message[:200]}"
            )
        except AzureDevOpsAuthError as exc:
            return BlobReadResult.forbidden(
                f"azure devops auth error {exc.status_code}: {exc.message[:200]}"
            )
        except AzureDevOpsError as exc:
            status = exc.status_code
            if status == 404:
                return BlobReadResult.not_found(
                    f"azure devops 404: {exc.message[:200]}"
                )
            if status in (200, 422):
                # The reader's own honesty refusals (no inline content,
                # symlink) — the path exists but delivered nothing usable.
                return BlobReadResult.incomplete(
                    f"{file_path!r} at {ref!r}: {exc.message[:200]}"
                )
            if status in (401, 403):
                return BlobReadResult.forbidden(
                    f"azure devops error {status}: {exc.message[:200]}"
                )
            return BlobReadResult.unavailable(
                f"azure devops error {status}: {exc.message[:200]}"
            )
        except httpx.HTTPError as exc:
            return BlobReadResult.unavailable(f"azure devops transport error: {exc}")
        return decode_blob_content(repo_file.content, repo_file.encoding, path=file_path, ref=ref)

    async def get_tree(
        self,
        project_id: int,
        path: str = "",
        ref: str = "HEAD",
        recursive: bool = False,
    ) -> list[TreeEntry]:
        """Repository tree at *ref*, never a truncated view (research §6.4 analog).

        ``ref`` resolves to a tree id first: a commit SHA passes through, a
        branch name (or HEAD → the default branch) is resolved through the
        refs API — the trees endpoint takes a tree/commit id, not a ref name.
        """
        tree_id = await self._resolve_tree_id(ref)
        data = await self._client.get_tree(self._project, self._repo, tree_id, recursive=recursive)
        if data.get("truncated"):
            raise AzureDevOpsError(
                200,
                f"tree listing for {ref!r} is truncated by Azure DevOps — "
                "refusing a partial snapshot",
            )
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries: list[TreeEntry] = []
        for item in data.get("treeEntries") or []:
            item_path = str(item.get("relativePath") or "")
            if prefix and not item_path.startswith(prefix.lstrip("/")):
                continue
            entries.append(
                TreeEntry.model_validate(
                    {
                        "id": item.get("objectId"),
                        "name": item_path.rsplit("/", 1)[-1],
                        "type": item.get("gitObjectType") or "blob",
                        "path": item_path,
                        "mode": None,
                    }
                )
            )
        return entries

    async def get_issue(self, project_id: int, issue_iid: int) -> Issue:
        """The work item *issue_iid*, mapped onto the neutral Issue DTO.

        AzDO identities are GUIDs — the neutral DTO's int ``id`` is set to 0
        and the stable e-mail-like ``uniqueName`` is carried in
        ``username``/``email`` (research §2.8: uniqueName is THE identity to
        normalize).
        """
        data = await self._client.get_work_item(self._project, issue_iid)
        fields = data.get("fields") or {}
        web_url = None
        links = data.get("_links")
        if isinstance(links, dict):
            html = links.get("html")
            if isinstance(html, dict) and isinstance(html.get("href"), str):
                web_url = html["href"]
        created_by = fields.get("System.CreatedBy")
        if isinstance(created_by, dict):
            author = _map_identity(created_by)
        elif isinstance(created_by, str) and created_by:
            # Field form may be a bare display-name string (webhook shape).
            author = _map_identity({"displayName": created_by})
        else:
            author = None
        return Issue.model_validate(
            {
                "id": issue_iid,
                "iid": issue_iid,
                "title": fields.get("System.Title") or "",
                "description": fields.get("System.Description"),
                "state": fields.get("System.State") or "",
                "labels": [],
                "web_url": web_url,
                "author": author,
            }
        )

    async def _resolve_tree_id(self, ref: str) -> str:
        """Resolve a ref to a tree/commit id for the trees endpoint."""
        if _looks_like_sha(ref):
            return ref
        branch = ref
        if ref == "HEAD":
            repository = await self._client.get_repository(self._project, self._repo)
            default_branch = str(repository.get("defaultBranch") or "")
            if not default_branch.startswith("refs/heads/"):
                raise AzureDevOpsError(
                    200, f"repository {self._repo!r} has no usable defaultBranch"
                )
            branch = default_branch.removeprefix("refs/heads/")
        refs = await self._client.get_refs(self._project, self._repo, filter=f"heads/{branch}")
        for entry in refs:
            object_id = entry.get("objectId")
            if object_id:
                return str(object_id)
        raise AzureDevOpsNotFoundError(404, f"branch head not found for {branch!r}")


def _map_identity(identity: dict[str, Any]) -> UserInfo:
    """Map an IdentityRef onto the neutral UserInfo (int-id compromise)."""
    unique_name = str(identity.get("uniqueName") or identity.get("displayName") or "")
    return UserInfo.model_validate(
        {
            # AzDO identity ids are GUIDs; the neutral DTO wants an int — 0
            # and the uniqueName carry the identity instead (research §2.8).
            "id": 0,
            "name": str(identity.get("displayName") or unique_name),
            "username": unique_name,
            "email": unique_name or None,
        }
    )
