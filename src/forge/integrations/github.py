"""GitHub integration: App credentials, REST/GraphQL client, repository reader.

First GitHub slice (ADR-0019, review finding F32). Ground truth for every
endpoint, field and error shape is ``docs/research/github-api.md`` (tagged
[documented] / [observed] / [inference] there); anything not covered by that
document is called out in the docstring of the method that relies on it.

Error taxonomy (mirrors :mod:`forge.gitlab.client`):

- :class:`GitHubAPIError` — any non-2xx REST failure or a failed GraphQL
  response. 422 validation failures keep GitHub's ``errors`` list on
  ``.errors`` (research §9.2).
- :class:`GitHubRateLimited` — primary/secondary rate limit (403/429 carrying
  ``Retry-After`` or ``x-ratelimit-remaining: 0``, research §8.4). Carries
  ``retry_after`` seconds and is raised, never internally retried — the
  caller owns the wait, so a saturated budget cannot turn into a hot loop.
- :class:`GitHubStaleBranchError` — GraphQL ``STALE_DATA``: the branch head
  moved against ``expectedHeadOid``. The CAS mismatch is surfaced as a
  BranchDrift-like outcome (ADR-0016 §3: concurrent writers are detected),
  never silently retried.

Retries: 500/502/503/504 (and 429 without a rate-limit body) are retried with
exponential backoff for idempotent calls only — non-idempotent writes
(issue comments, PR creation, commit mutations) pass ``retry=False`` so a
single HTTP failure cannot duplicate a side effect, exactly like the GitLab
transport. A 401 triggers exactly one token re-mint and one retry: the
request was never processed (it was never authenticated), so resending it
cannot duplicate a remote effect.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import jwt

from forge.gitlab.schemas import Issue, RepositoryFile, TreeEntry

logger = logging.getLogger(__name__)

#: Pinned calendar API version (research §1.2: send an explicit value).
API_VERSION = "2022-11-28"

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1  # seconds
_MAX_PAGES = 50
_PER_PAGE = 100

#: Installation tokens live one hour; treat them as stale this many seconds
#: before ``expires_at`` so a call never dies mid-flight (research §8.1).
_TOKEN_EXPIRY_MARGIN_SECONDS = 300


class GitHubAPIError(Exception):
    """Raised when a GitHub API request (or GraphQL operation) fails."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response: httpx.Response | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.response = response
        #: Parsed ``errors`` list from 422 validation failures / GraphQL
        #: error entries (research §3.2, §9.2).
        self.errors: list[dict[str, Any]] = list(errors or [])
        super().__init__(f"GitHub API error {status_code}: {message}")


class GitHubRateLimited(GitHubAPIError):
    """A primary or secondary rate limit was hit (research §8.4).

    ``retry_after`` is the number of seconds the caller should wait before
    the next attempt (from ``Retry-After``, else derived from
    ``x-ratelimit-reset``). The client never retries rate limits internally.
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


class GitHubStaleBranchError(GitHubAPIError):
    """GraphQL ``STALE_DATA``: the branch head moved against ``expectedHeadOid``.

    The BranchDrift analog for the GitHub write path: the caller must
    re-read the head and reconcile — never blind-retry the mutation
    (research §3.2, ADR-0016 §3).
    """

    def __init__(self, branch: str, expected_head_oid: str, message: str = "") -> None:
        self.branch = branch
        self.expected_head_oid = expected_head_oid
        super().__init__(200, message or f"branch {branch!r} moved against expected head")


def _rate_limit_retry_after(response: httpx.Response) -> int | None:
    """Extract a rate-limit wait from GitHub's headers (research §8.4, §9.2).

    ``Retry-After`` wins; ``x-ratelimit-remaining: 0`` marks a primary limit
    whose budget resets at the ``x-ratelimit-reset`` epoch. Returns None when
    the response carries no rate-limit signal (a plain 403 forbidden, say).
    """
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(1, int(retry_after))
        except ValueError:
            pass
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                return max(1, int(float(reset) - time.time()))
            except ValueError:
                return 60
        return 60
    if response.status_code == 429:
        # Secondary limit without a specific wait: best practice says wait at
        # least a minute (research §8.4) — surfaced, not slept here.
        return 60
    return None


class TokenProvider(Protocol):
    """Supplies short-lived credentials to :class:`GitHubClient`."""

    async def token(self) -> str: ...

    async def invalidate(self) -> None: ...


@dataclass(frozen=True)
class GitHubStaticCredentials:
    """PAT/static-token provider (lab + PAT mode): returns one fixed token."""

    token: str

    async def token(self) -> str:
        return self.token

    async def invalidate(self) -> None:
        pass  # a static token cannot be re-minted


@dataclass(frozen=True)
class InstallationToken:
    """One minted installation access token (research §1.2)."""

    token: str
    expires_at: datetime


class GitHubAppCredentials:
    """GitHub App → installation-token credential broker (research §1).

    Mints RS256 JWTs from the App private key and exchanges them for
    installation access tokens. Tokens are cached until five minutes before
    expiry and re-minted on demand after :meth:`invalidate` (the client calls
    this on a 401 — regenerate rather than retry with the same credential,
    research §1.6/§8.2). The private key lives ONLY here; nothing downstream
    ever sees it.
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._app_id = str(app_id)
        self._private_key = private_key
        self._installation_id = str(installation_id)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )
        self._cached: InstallationToken | None = None
        self._lock = asyncio.Lock()

    # -- JWT (research §1.1) -------------------------------------------------

    def app_jwt(self, now: datetime | None = None) -> str:
        """RS256 JWT for App-level endpoints.

        Claims: ``iss`` = App ID, ``iat`` 60 s in the past (clock-skew
        absorption), ``exp`` 9 minutes out (limit: 10 minutes).
        """
        now = now or datetime.now(timezone.utc)
        stamp = int(now.timestamp())
        claims = {"iss": self._app_id, "iat": stamp - 60, "exp": stamp + 540}
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    # -- Installation token (research §1.2) -----------------------------------

    async def installation_token(self) -> InstallationToken:
        """The cached installation token, re-minted when stale."""
        async with self._lock:
            if self._cached is not None and self._still_valid(self._cached):
                return self._cached
            self._cached = await self._mint()
            return self._cached

    @staticmethod
    def _still_valid(token: InstallationToken, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now < token.expires_at - timedelta(seconds=_TOKEN_EXPIRY_MARGIN_SECONDS)

    async def _mint(self) -> InstallationToken:
        response = await self._client.post(
            f"/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if response.status_code >= 400:
            raise GitHubAPIError(response.status_code, response.text, response=response)
        data = response.json()
        return InstallationToken(
            token=str(data["token"]),
            expires_at=_parse_expires_at(str(data["expires_at"])),
        )

    # -- TokenProvider protocol ------------------------------------------------

    async def token(self) -> str:
        return (await self.installation_token()).token

    async def invalidate(self) -> None:
        """Drop the cached token: the next :meth:`token` re-mints."""
        self._cached = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubAppCredentials:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _parse_expires_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_maybe_datetime(value: Any) -> datetime | None:
    """Parse GitHub's ISO-8601 timestamps defensively (None when absent/garbage)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return _parse_expires_at(value)
    except ValueError:
        return None


class GitHubClient:
    """Async client for the GitHub REST + GraphQL APIs (ADR-0019 slice)."""

    def __init__(
        self,
        base_url: str = "https://api.github.com",
        *,
        token_provider: TokenProvider,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )
        self._tokens = token_provider

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
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
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """Request with GitLab-transport retry semantics plus 401 re-auth.

        - 401: invalidate the cached token and retry ONCE (safe for writes:
          the request was never authenticated, hence never processed).
        - Rate limits: raised as :class:`GitHubRateLimited`, never retried
          here (the caller owns the wait — no hot loop).
        - Other retryable statuses: exponential backoff, idempotent calls
          only; ``retry=False`` marks non-idempotent writes.
        """
        max_attempts = _MAX_RETRIES if retry else 1
        attempt = 0
        reauth_used = False
        while True:
            token = await self._tokens.token()
            request_headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {token}",
                **(headers or {}),
            }
            response = await self._client.request(
                method,
                path,
                json=json,
                params=params,
                headers=request_headers,
                follow_redirects=follow_redirects,
            )
            if response.status_code == 401 and not reauth_used:
                logger.info("401 from %s %s — re-minting installation token", method, path)
                await self._tokens.invalidate()
                reauth_used = True
                continue
            if response.status_code < 400:
                return response
            wait = _rate_limit_retry_after(response)
            if wait is not None:
                raise GitHubRateLimited(
                    response.status_code, response.text, response=response, retry_after=wait
                )
            if response.status_code in _RETRYABLE_STATUSES and attempt < max_attempts - 1:
                delay = _BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "%s %s returned %d (attempt %d) — retrying in %ds",
                    method,
                    path,
                    response.status_code,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            raise GitHubAPIError(
                response.status_code,
                response.text,
                response=response,
                errors=_validation_errors(response),
            )

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, *, retry: bool = True, **kwargs: Any) -> httpx.Response:
        """POST with explicit retry semantics (non-idempotent writes pass False)."""
        return await self._request("POST", path, retry=retry, **kwargs)

    async def _paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        envelope: str | None = None,
    ) -> list[Any]:
        """Follow GitHub's Link-header pagination (research §9.1).

        Iterate until no ``rel="next"`` — never compute totals from item
        counts, ``rel="last"`` may be absent. *envelope* names the array
        inside an object response (the Actions list endpoints wrap their
        items, e.g. ``workflow_runs``); a bare list response ignores it.
        """
        params = dict(params or {})
        params.setdefault("per_page", _PER_PAGE)
        results: list[Any] = []
        for _ in range(_MAX_PAGES):
            response = await self._get(path, params=params)
            data = response.json()
            if envelope is not None and isinstance(data, dict):
                data = data.get(envelope) or []
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
            if 'rel="next"' not in (response.headers.get("link") or ""):
                break
            params["page"] = int(params.get("page", 1)) + 1
        return results

    # -- REST: repositories / issues ---------------------------------------------

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        response = await self._get(f"/repos/{owner}/{repo}")
        return dict(response.json())

    async def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        response = await self._get(f"/repos/{owner}/{repo}/issues/{number}")
        data = response.json()
        return _map_issue(data)

    async def get_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        raw = await self._paginated(f"/repos/{owner}/{repo}/issues/{number}/comments")
        return [dict(item) for item in raw]

    async def create_issue_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        # Non-idempotent: a lost response must be reconciled, not retried.
        response = await self._post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            retry=False,
            json={"body": body},
        )
        return dict(response.json())

    # -- REST: git refs -----------------------------------------------------------

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        """The current head commit SHA of *branch*.

        Primary: ``GET /repos/{o}/{r}/git/refs/heads/{branch}`` →
        ``object.sha``. Fallback for exotic ref shapes: the commits endpoint
        ``GET /repos/{o}/{r}/commits/{ref}`` → ``sha``.
        """
        encoded = quote(branch, safe="/")
        try:
            response = await self._get(f"/repos/{owner}/{repo}/git/refs/heads/{encoded}")
            data = response.json()
            obj = data.get("object") or {}
            sha = obj.get("sha")
            if sha:
                return str(sha)
            raise GitHubAPIError(200, f"refs response missing object.sha for {branch!r}")
        except GitHubAPIError as exc:
            if exc.status_code != 404:
                raise
        response = await self._get(f"/repos/{owner}/{repo}/commits/{encoded}")
        data = response.json()
        if isinstance(data, list):  # ref matched several commits — take the tip
            data = data[0] if data else {}
        sha = data.get("sha")
        if not sha:
            raise GitHubAPIError(404, f"branch head not found for {branch!r}")
        return str(sha)

    async def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]:
        """Create ``refs/heads/{branch}`` at *sha* (REST Git Database API).

        Needed because ``createCommitOnBranch`` requires the ref to already
        exist (research §3.1). A 422 "already exists" propagates as
        :class:`GitHubAPIError` — callers treat it as idempotent re-entry.
        """
        # Non-idempotent: creation is reconciled by the 422-on-conflict shape.
        response = await self._post(
            f"/repos/{owner}/{repo}/git/refs",
            retry=False,
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        return dict(response.json())

    # -- GraphQL: the publish write path ------------------------------------------

    async def create_commit_on_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        *,
        headline: str,
        body: str | None = None,
        additions: list[tuple[str, str]] | None = None,
        deletions: list[str] | None = None,
        expected_head_oid: str,
        client_mutation_id: str = "",
    ) -> dict[str, Any]:
        """Append a commit via ``createCommitOnBranch`` (research §3).

        *additions* are (path, text-content) pairs — contents are base64
        encoded here; *deletions* are bare paths. ``expectedHeadOid`` is the
        CAS token: a moved head fails with :class:`GitHubStaleBranchError`
        (``errors[].type == "STALE_DATA"``, matched by type, never by
        message). ``client_mutation_id`` is pure correlation — GitHub does
        NOT dedupe on it (research §3.4); the CAS is the exactly-once guard.
        """
        file_changes: dict[str, Any] = {}
        if additions:
            file_changes["additions"] = [
                {"path": path, "contents": _b64_encode(content)} for path, content in additions
            ]
        if deletions:
            file_changes["deletions"] = [{"path": path} for path in deletions]
        message: dict[str, str] = {"headline": headline}
        if body is not None:
            message["body"] = body
        input_data: dict[str, Any] = {
            "branch": {
                "repositoryNameWithOwner": f"{owner}/{repo}",
                "branchName": branch,
            },
            "message": message,
            "expectedHeadOid": expected_head_oid,
        }
        if file_changes:
            input_data["fileChanges"] = file_changes
        if client_mutation_id:
            input_data["clientMutationId"] = client_mutation_id
        payload = {
            "query": (
                "mutation($input: CreateCommitOnBranchInput!) {\n"
                "  createCommitOnBranch(input: $input) {\n"
                "    clientMutationId\n"
                "    commit { oid url }\n"
                "  }\n"
                "}"
            ),
            "variables": {"input": input_data},
        }
        # Mutations are never retried at the transport level: a lost response
        # is reconciled by re-reading the branch head — the CAS turns a
        # would-be duplicate into STALE_DATA (research §3.4).
        response = await self._post("/graphql", retry=False, json=payload)
        if response.status_code >= 400:
            raise GitHubAPIError(
                response.status_code,
                response.text,
                response=response,
            )
        data = response.json()
        errors = data.get("errors") or []
        stale = next((e for e in errors if e.get("type") == "STALE_DATA"), None)
        if stale is not None:
            raise GitHubStaleBranchError(
                branch, expected_head_oid, str(stale.get("message") or "STALE_DATA")
            )
        if errors:
            raise GitHubAPIError(
                200,
                "; ".join(str(e.get("message") or "") for e in errors),
                response=response,
                errors=list(errors),
            )
        result = (data.get("data") or {}).get("createCommitOnBranch")
        if not result or not (result.get("commit") or {}).get("oid"):
            raise GitHubAPIError(200, "createCommitOnBranch returned no commit", response=response)
        return {
            "oid": str(result["commit"]["oid"]),
            "url": result["commit"].get("url"),
            "client_mutation_id": result.get("clientMutationId"),
        }

    # -- REST: pull requests -------------------------------------------------------

    async def create_draft_pr(
        self,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Open a Draft PR (REST, research §4.1) — same-repository head branch."""
        # Non-idempotent: callers find-by-head first and adopt, never replay.
        response = await self._post(
            f"/repos/{owner}/{repo}/pulls",
            retry=False,
            json={"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        return dict(response.json())

    async def get_pr_by_head(
        self, owner: str, repo: str, head_branch: str, base: str | None = None
    ) -> dict[str, Any] | None:
        """The open PR for *head_branch* (``head=owner:branch``), or None."""
        params: dict[str, Any] = {"state": "open", "head": f"{owner}:{head_branch}"}
        if base is not None:
            params["base"] = base
        response = await self._get(f"/repos/{owner}/{repo}/pulls", params=params)
        pull_requests = response.json()
        if isinstance(pull_requests, list) and pull_requests:
            return dict(pull_requests[0])
        return None

    async def get_pr_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """The changed-file entries of PR *number* (REST, paginated).

        Each entry carries ``filename``, ``status`` and, for text changes,
        the unified ``patch`` — the readonly reviewer's diff surface (E3a).
        """
        return [
            dict(entry)
            for entry in await self._paginated(f"/repos/{owner}/{repo}/pulls/{number}/files")
        ]

    # -- REST: verification reads ---------------------------------------------------

    async def list_check_runs_for_sha(
        self, owner: str, repo: str, sha: str
    ) -> list[dict[str, Any]]:
        """Check runs reported for *sha* (``head_sha`` each run carries)."""
        response = await self._get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs")
        data = response.json()
        return [dict(run) for run in (data.get("check_runs") or [])]

    async def list_workflow_runs_for_sha(
        self,
        owner: str,
        repo: str,
        sha: str,
        workflow_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Actions runs with ``head_sha == sha``, optionally by workflow name.

        The ``head_sha`` filter is native (research §5.4); workflow-name
        narrowing is client-side on the run's ``name`` field.
        """
        response = await self._get(
            f"/repos/{owner}/{repo}/actions/runs",
            params={"head_sha": sha, "per_page": _PER_PAGE},
        )
        data = response.json()
        runs = [dict(run) for run in (data.get("workflow_runs") or [])]
        if workflow_name is None:
            return runs
        return [run for run in runs if run.get("name") == workflow_name]

    # -- REST: Actions (the E3b execution adapter, ADR-0020) ----------------------
    #
    # Ground truth: docs/research/github-actions-executor.md (tagged there).

    async def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow_filename: str,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Trigger ``workflow_dispatch`` on *workflow_filename* at *ref*.

        The workflow file must declare ``on: workflow_dispatch`` **on that
        ref** (research §1); ``actions:write`` is required. Non-idempotent —
        a lost response must be reconciled by discovery, never replayed (a
        replay would start a second harness run).

        Since 2026-02-19 the response carries the run id
        (research §1, github.blog changelog) — parsed here as ``run_id``
        (falling back to ``id``) when the body provides one. Legacy/GHES
        answers with an EMPTY body: the caller discovers the run via
        :meth:`list_workflow_dispatch_runs`. Returns ``{}`` in that case.
        """
        encoded = quote(workflow_filename, safe="")
        response = await self._post(
            f"/repos/{owner}/{repo}/actions/workflows/{encoded}/dispatches",
            retry=False,
            json={"ref": ref, "inputs": dict(inputs or {})},
        )
        try:
            data = response.json()
        except Exception:
            return {}  # legacy empty 202 — discovery is the caller's job
        if not isinstance(data, dict):
            return {}
        run_id = data.get("run_id", data.get("id"))
        if run_id is None:
            return {}
        return {"run_id": int(run_id)}

    async def list_workflow_dispatch_runs(
        self,
        owner: str,
        repo: str,
        workflow_filename: str,
        *,
        head_branch: str | None = None,
        head_sha: str | None = None,
        created_after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """The workflow's ``workflow_dispatch`` runs, newest first.

        The legacy-dispatch correlation primitive (research §1): filter by
        ``event=workflow_dispatch`` server-side, narrow by ``head_sha`` /
        ``head_branch`` natively, and enforce the created window client-side
        (``created_after`` — ordering alone is never trusted, ADR-0020 §1).
        The runs list endpoint returns newest first by default.
        """
        encoded = quote(workflow_filename, safe="")
        params: dict[str, Any] = {"event": "workflow_dispatch", "per_page": _PER_PAGE}
        if head_sha is not None:
            params["head_sha"] = head_sha
        if head_branch is not None:
            params["head_branch"] = head_branch
        runs = [
            dict(run)
            for run in await self._paginated(
                f"/repos/{owner}/{repo}/actions/workflows/{encoded}/runs",
                params,
                envelope="workflow_runs",
            )
        ]
        if created_after is None:
            return runs
        window = []
        for run in runs:
            created = _parse_maybe_datetime(run.get("created_at"))
            if created is None or created >= created_after:
                window.append(run)
        return window

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        """One Actions workflow run (status / conclusion / head_sha)."""
        response = await self._get(f"/repos/{owner}/{repo}/actions/runs/{run_id}")
        return dict(response.json())

    async def get_workflow_run_jobs(
        self, owner: str, repo: str, run_id: int
    ) -> list[dict[str, Any]]:
        """The jobs of run *run_id* (ids + statuses + conclusions, paginated)."""
        return [
            dict(job)
            for job in await self._paginated(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", envelope="jobs"
            )
        ]

    async def list_workflow_run_artifacts(
        self, owner: str, repo: str, run_id: int
    ) -> list[dict[str, Any]]:
        """Artifacts uploaded by run *run_id* (artifacts v4, research §2)."""
        return [
            dict(artifact)
            for artifact in await self._paginated(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts", envelope="artifacts"
            )
        ]

    async def download_artifact_zip(self, owner: str, repo: str, artifact_id: int) -> bytes:
        """Download one artifact archive (raw bytes, research §2).

        The endpoint answers ``302`` to a short-lived signed URL on a
        separate host — followed here explicitly (per-request, so the rest
        of the client keeps its strict no-redirect posture). Any token with
        ``actions:read`` works, including cross-run downloads (v4 removed
        the same-run limit of v3).
        """
        response = await self._get(
            f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip",
            follow_redirects=True,
        )
        return response.content

    async def get_job_log(self, owner: str, repo: str, job_id: int) -> str:
        """The raw log text of job *job_id* (``302`` → plain text, followed)."""
        response = await self._get(
            f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            follow_redirects=True,
        )
        return response.text

    async def cancel_workflow_run(self, owner: str, repo: str, run_id: int) -> None:
        """Request cancellation of run *run_id* (research §4).

        Non-idempotent: fired once. A 409 (already finished) / 404 (never
        started) propagates as :class:`GitHubAPIError` — the caller decides
        whether that matters (a finished run has nothing left to cancel).
        """
        await self._post(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel",
            retry=False,
        )

    # -- REST: raw content primitives (the reader builds on these) ------------------

    async def get_contents_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ref is not None and ref != "HEAD":
            params["ref"] = ref
        encoded = quote(path, safe="/")
        response = await self._get(f"/repos/{owner}/{repo}/contents/{encoded}", params=params)
        return dict(response.json())

    async def get_git_blob(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        response = await self._get(f"/repos/{owner}/{repo}/git/blobs/{sha}")
        return dict(response.json())

    async def get_git_tree(
        self, owner: str, repo: str, ref: str, recursive: bool = False
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if recursive:
            params["recursive"] = "1"
        encoded = quote(ref, safe="/")
        response = await self._get(f"/repos/{owner}/{repo}/git/trees/{encoded}", params=params)
        return dict(response.json())


def _validation_errors(response: httpx.Response) -> list[dict[str, Any]]:
    """Parse GitHub's 422 validation ``errors`` list (research §9.2)."""
    try:
        data = response.json()
    except Exception:
        return []
    errors = data.get("errors") if isinstance(data, dict) else None
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _b64_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _b64_decode(raw: str) -> str:
    """Decode base64 content, tolerating the blob API's embedded newlines."""
    return base64.b64decode(raw.replace("\n", "").strip()).decode("utf-8", errors="replace")


def _map_issue(data: dict[str, Any]) -> Issue:
    """Map a GitHub issue/PR JSON object onto the provider-neutral Issue DTO."""
    user = data.get("user") or {}
    return Issue.model_validate(
        {
            "id": data.get("id") or data.get("number") or 0,
            "iid": data.get("number") or 0,
            "title": data.get("title") or "",
            "description": data.get("body"),
            "state": data.get("state") or "open",
            "labels": [
                lb.get("name", "") if isinstance(lb, dict) else str(lb)
                for lb in (data.get("labels") or [])
            ],
            "web_url": data.get("html_url"),
            "author": {
                "id": user.get("id") or 0,
                "name": user.get("name") or user.get("login") or "",
                "username": user.get("login") or "",
            }
            if user
            else None,
        }
    )


class GitHubRepositoryReader:
    """Authoritative repository reads over the GitHub contents API.

    Bound to one repository; duck-types the GitLabClient read surface the
    builtin implementer uses (``get_file`` / ``get_tree`` / ``get_issue``),
    so :class:`~forge.factory.implementer.LLMImplementer` runs against GitHub
    unchanged. AuthoritativeReader semantics (ADR-0016 §2, ADR-0019 §1):

    - full blobs, no truncation: a contents-API response without inline
      content (files over 1 MB arrive empty) is resolved through the git
      blobs API;
    - symlinks and submodules are rejected explicitly, never lossily
      converted (research §3.3: ``createCommitOnBranch`` only handles
      regular files anyway);
    - a truncated tree listing (``truncated: true``) raises instead of
      silently returning a partial snapshot.
    """

    def __init__(self, client: GitHubClient, owner: str, repo: str) -> None:
        self._client = client
        self._owner = owner
        self._repo = repo

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD") -> RepositoryFile:
        """Complete file content at *ref*, base64-encoded like GitLab's schema.

        ``project_id`` is accepted for signature compatibility with the
        GitLabClient surface and ignored — the reader is repo-bound.
        """
        data = await self._client.get_contents_file(self._owner, self._repo, file_path, ref)
        if data.get("type") not in (None, "file"):
            raise GitHubAPIError(422, f"{file_path!r} is a {data.get('type')}, not a regular file")
        content = data.get("content") or ""
        if not content and (data.get("size") or 0) > 0 and data.get("sha"):
            blob = await self._client.get_git_blob(self._owner, self._repo, str(data["sha"]))
            content = str(blob.get("content") or "")
        return RepositoryFile.model_validate(
            {
                "file_name": file_path.rsplit("/", 1)[-1],
                "file_path": file_path,
                "size": data.get("size"),
                "encoding": "base64",
                "content": content,
                "ref": ref,
            }
        )

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        """Decoded full text of *file_path* at *ref*."""
        repo_file = await self.get_file(0, file_path, ref)
        try:
            return _b64_decode(repo_file.content)
        except (binascii.Error, ValueError) as exc:
            raise GitHubAPIError(200, f"{file_path!r}: undecodable base64 content") from exc

    async def get_tree(
        self,
        project_id: int,
        path: str = "",
        ref: str = "HEAD",
        recursive: bool = False,
    ) -> list[TreeEntry]:
        """Repository tree at *ref* (Git trees API), never a truncated view."""
        data = await self._client.get_git_tree(self._owner, self._repo, ref, recursive)
        if data.get("truncated"):
            raise GitHubAPIError(
                200,
                f"tree listing for {ref!r} is truncated by GitHub — refusing a partial snapshot",
            )
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries: list[TreeEntry] = []
        for item in data.get("tree") or []:
            item_path = str(item.get("path") or "")
            if prefix and not item_path.startswith(prefix):
                continue
            entries.append(
                TreeEntry.model_validate(
                    {
                        "id": item.get("sha"),
                        "name": item_path.rsplit("/", 1)[-1],
                        "type": item.get("type") or "blob",
                        "path": item_path,
                        "mode": item.get("mode"),
                    }
                )
            )
        return entries

    async def get_issue(self, project_id: int, issue_iid: int) -> Issue:
        response = await self._client.get_issue(self._owner, self._repo, issue_iid)
        return response
