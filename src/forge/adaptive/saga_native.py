"""The durable two-writer saga over the REAL native provider clients (#295, R37-14).

R36-18 (#277) proved the durable two-writer saga against
:class:`~forge.adaptive.saga_durable.NativeShapedRemote` — an in-process
reference object with CHOSEN duplicate/CAS semantics. This module is the
step issue #295 asks for: the SAME saga driven through the EXISTING
native provider clients, with each provider's ACTUAL preconditions kept
provider-specific (no assumed generic branch-wide CAS):

- **:class:`SagaEffectSurface`** — the effect interface
  :class:`~forge.adaptive.saga_durable.DurablePublicationEntry` consumes,
  formalized as a Protocol. The reference ``NativeShapedRemote`` already
  satisfies it (verified by the adapter-compat tests); the adapters here
  satisfy it over REAL HTTP. Each method maps to one native-effect
  concept: :meth:`~forge.adaptive.saga_durable.NativeShapedRemote.commit`
  is create-commit under an expected-head precondition
  (:meth:`pin_expected_head` is the publication's drift guard),
  ``open_review`` is create-merge-request idempotent by the
  provider-native ``(repository, source branch)`` key, and
  ``remote_head``/``head_carries_marker`` are the read-correlation
  surface (branch head + the LISTED commit history).
- **:class:`GitLabNativeEffects`** — the adapter over the REAL
  :class:`forge.gitlab.client.GitLabClient`. GitLab's Commits API
  (``POST /repository/commits``) has NO branch-wide CAS and NO
  idempotency — a repeated POST produces a SECOND distinct commit. The
  adapter therefore implements the expected-head check CLIENT-SIDE (a
  pre-write ``get_branch_head`` compared against the pin, refused as a
  422-shaped :class:`ProviderRejectedError` on a moved head) plus the
  RACE NOTE: between that read and the server applying the commit a
  concurrent push can still land underneath — the window is NARROWED,
  not closed. What closes it honestly is the saga's own verify/park
  logic: a head the saga cannot account for is a HUMAN edit (parked,
  preserved, never force-overwritten), so the race can never produce a
  lost human commit or a silent duplicate — at worst a commit that
  appends on top of a concurrent edit, which the listed history shows.
  The commit payload lands through the writer's own create-vs-update
  discipline (an AUTHORITATIVE ``read_blob`` decides the action; a
  failed read must never forge "file does not exist" — R14), and the
  merge request rides GitLab's native duplicate refusal: the adapter
  PROBES the opened MRs by ``(project, source branch, target)`` FIRST
  and adopts, because GitLab refuses a second open MR for the same
  source (a real 400) — probe-first, never replay.
- **:class:`GitHubNativeEffects`** — the same seam over the REAL
  :class:`forge.integrations.github.GitHubClient`, where the CAS is
  NATIVE: ``createCommitOnBranch`` carries ``expectedHeadOid`` and a
  moved head fails with ``GitHubStaleBranchError`` (``STALE_DATA``) —
  the exactly-once guard the research established. PRs are found by the
  native ``(head, base)`` key and opened as DRAFTS.

Correlation is by NATIVE IDENTITY everywhere: recovery lists the
branch's commits and matches the marker IN THE MESSAGE plus the parent
oid — never a marker-keyed provider dedup (none exists; the marker is a
commit-message trailer, exactly how forge's real writer correlates).
Every effect lands in the adapter's :attr:`journal <NativeEffectsBase.journal>`
with its native identity (commit sha + parent, MR iid + url), and the
adapter surface structurally CANNOT merge, force-push or delete: no
such client call exists on it, so ``destructive_operations`` is empty
by construction, not convention. The lost-response window
(:attr:`NativeEffectsBase.lose_commit_response`) drops the RESPONSE
after the provider accepted the effect — the exact crash window
``outcome_unknown`` exists for.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import httpx

from forge.adaptive.publication_saga import (
    ProviderRejectedError,
    ProviderUnavailableError,
)
from forge.adaptive.saga_durable import NativeCommit
from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError, GitLabClient
from forge.integrations.github import (
    GitHubAPIError,
    GitHubClient,
    GitHubRateLimited,
    GitHubStaleBranchError,
)

__all__ = [
    "DEFAULT_PAYLOAD_PATH",
    "PUBLICATION_PAYLOAD_SCHEMA",
    "SagaEffectSurface",
    "GitLabNativeEffects",
    "GitHubNativeEffects",
    "NativeEffectsBase",
    "payload_document",
]

#: The schema tag of the deterministic payload document each publication
#: commit carries (the content half of the effect's native identity).
PUBLICATION_PAYLOAD_SCHEMA = "forge.saga.native.payload/1"

#: Where the publication payload lands on the branch (create-vs-update is
#: decided by an authoritative read, exactly like the writer's own file
#: actions).
DEFAULT_PAYLOAD_PATH = "forge/publication.json"


def payload_document(marker: str) -> str:
    """The deterministic payload for *marker* — same marker, same bytes.

    No timestamps: a repeated logical effect produces byte-identical
    content, so the commit history's CONTENT is correlatable alongside
    the message marker and the parent oid.
    """
    return (
        json.dumps(
            {"schema": PUBLICATION_PAYLOAD_SCHEMA, "marker": marker}, sort_keys=True, indent=2
        )
        + "\n"
    )


def _message_of(marker: str) -> str:
    """The commit message — the reference remote's exact shape.

    The marker rides the message BODY (the ``forge-saga:<id>:<digest>``
    trailer shape forge's real writer uses); providers offer no
    marker-keyed dedup and this module never pretends one exists.
    """
    return f"forge: publish candidate\n\n({marker})"


def _headline_of(marker: str) -> str:
    """GitHub's message spelling — the marker in the HEADLINE, because
    GitHub's REST commit listing is what correlation scans and the
    headline is the one line every listing shape carries verbatim."""
    return f"forge: publish candidate ({marker})"


# ---------------------------------------------------------------------------
# The effect interface — what DurablePublicationEntry consumes.
# ---------------------------------------------------------------------------


@runtime_checkable
class SagaEffectSurface(Protocol):
    """The native-effect seam the durable two-writer entry drives.

    Deliberately the SAME shape :class:`NativeShapedRemote` already
    exposes (the in-process reference satisfies this Protocol; the
    adapters here satisfy it over REAL provider HTTP):

    - :meth:`commit` — create-commit under the expected-head
      precondition pinned by :meth:`pin_expected_head` (a moved head is
      a 4xx-shaped :class:`ProviderRejectedError` BEFORE any effect;
      on GitLab the check is client-side — see the module's race note);
    - :meth:`open_review` — create-merge-request, idempotent ONLY by
      the provider-native ``(repository, source branch)`` key (a repeat
      returns the SAME review, never a second one);
    - :meth:`remote_head` / :meth:`head_carries_marker` — the read
      correlation: the branch head plus the LISTED commit history
      scanned for the saga's marker (fail with
      :class:`ProviderUnavailableError` when the surface proves
      nothing — fail closed).

    There is NO merge, NO force-push and NO branch-delete anywhere on
    the seam — the Protocol cannot express the effects the publication
    doctrine forbids.
    """

    async def remote_head(self, repository_id: str, branch: str) -> str:
        """The branch's current head oid (unreadable surface fails closed)."""
        ...

    async def head_carries_marker(self, repository_id: str, branch: str, marker: str) -> bool:
        """Whether the branch's LISTED history contains a commit carrying ``marker``."""
        ...

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        """Create the publication commit under the pinned expected head;
        return the new commit's native sha. A lost response raises
        ``TimeoutError`` AFTER the effect may have landed."""
        ...

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        """Open (or adopt the already-open) review by the native
        ``(repository, source branch)`` key; return its URL."""
        ...

    def pin_expected_head(self, repository_id: str, branch: str, expected_head: str) -> None:
        """Pin the branch head the next commit must build on (the drift guard)."""
        ...


# ---------------------------------------------------------------------------
# Shared native-effect bookkeeping (pins, counters, journal, correlation).
# ---------------------------------------------------------------------------


class NativeEffectsBase:
    """The bookkeeping every native adapter shares.

    - :attr:`commit_calls`/:attr:`review_calls` — the no-duplicate
      proofs read (ONE provider commit per logical intent, even across
      recovery processes);
    - :attr:`journal` — every native effect with its identity, oldest
      first (``create_commit`` with sha/parent/message,
      ``create_merge_request``/``adopt_merge_request`` with iid/url) —
      plus the read ops correlation performed;
    - :meth:`destructive_operations` — ALWAYS empty: the adapter surface
      has no merge/force/delete call to make (structural, not
      conventional).
    """

    #: The provider this adapter talks to (journal + report tagging).
    provider: str = "abstract"

    def __init__(self) -> None:
        self._pins: dict[tuple[str, str], str] = {}
        #: Every native effect with its identity, oldest first.
        self.journal: list[dict[str, Any]] = []
        self.commit_calls: dict[str, int] = {}
        self.review_calls: dict[str, int] = {}
        #: The injected lost-response window (test knob): repositories
        #: whose commit RESPONSE dies after the effect landed.
        self.lose_commit_response: set[str] = set()

    # -- the expected-head pin ------------------------------------------------

    def pin_expected_head(self, repository_id: str, branch: str, expected_head: str) -> None:
        """The publication's CAS pin (the writer's ``expected_head`` drift guard)."""
        self._pins[(repository_id, branch)] = expected_head

    def _pinned_head(self, repository_id: str, branch: str) -> str | None:
        return self._pins.get((repository_id, branch))

    # -- the native reads every adapter provides -------------------------------

    async def list_commits(self, repository_id: str, branch: str) -> tuple[NativeCommit, ...]:
        """The branch's commits, OLDEST first — the listing correlation reads."""
        raise NotImplementedError  # the adapter's provider transport

    async def commits_carrying(
        self, repository_id: str, branch: str, marker: str
    ) -> tuple[NativeCommit, ...]:
        """Every DISTINCT commit whose message carries the marker — the
        duplicate-effect view read straight off the provider's listing
        (two entries = a duplicated logical effect)."""
        listed = await self.list_commits(repository_id, branch)
        return tuple(commit for commit in listed if marker in commit.message)

    async def branch_history(self, repository_id: str, branch: str) -> tuple[str, ...]:
        """The branch's commit shas, oldest first (the prefix-check view)."""
        return tuple(commit.sha for commit in await self.list_commits(repository_id, branch))

    # -- the inspection surface the failpoint matrix asserts through -------------

    def merge_request_creates(self, repository_id: str) -> int:
        """How many DISTINCT reviews the provider CREATED (adoptions of an
        already-open review are not creates — native idempotence)."""
        return sum(
            1
            for entry in self.journal
            if entry.get("op") == "create_merge_request"
            and entry.get("repository_id") == repository_id
        )

    def effects_for(self, repository_id: str) -> list[dict[str, Any]]:
        """Every journaled effect that names this repository."""
        return [
            dict(entry) for entry in self.journal if entry.get("repository_id") == repository_id
        ]

    def destructive_operations(self) -> list[str]:
        """Always empty — the adapter surface cannot express a merge,
        force-push or branch deletion."""
        return []

    def _journal_commit(self, repository_id: str, branch: str, commit: Mapping[str, Any]) -> None:
        self.journal.append(
            {
                "op": "create_commit",
                "provider": self.provider,
                "repository_id": repository_id,
                "branch": branch,
                "sha": str(commit.get("sha") or ""),
                "parent": str(commit.get("parent") or ""),
                "message": str(commit.get("message") or ""),
                "author": str(commit.get("author") or ""),
            }
        )

    def _count(self, calls: dict[str, int], repository_id: str) -> None:
        calls[repository_id] = calls.get(repository_id, 0) + 1

    def _maybe_lose_commit_response(self, repository_id: str) -> None:
        if repository_id in self.lose_commit_response:
            raise TimeoutError(f"{repository_id}: response lost after the effect landed")


# ---------------------------------------------------------------------------
# Error mapping — provider exceptions to the saga's three-way contract
# (ProviderRejectedError = definitive 4xx · TimeoutError = lost response ·
# ProviderUnavailableError = the surface proves nothing, fail closed).
# ---------------------------------------------------------------------------


def _gitlab_api_error(exc: GitLabAPIError) -> Exception:
    if exc.status_code >= 500:
        return ProviderUnavailableError(f"gitlab api {exc.status_code}: {exc.message[:200]}")
    return ProviderRejectedError(f"gitlab refused ({exc.status_code}): {exc.message[:200]}")


def _github_api_error(exc: GitHubAPIError) -> Exception:
    if exc.status_code >= 500:
        return ProviderUnavailableError(f"github api {exc.status_code}: {exc.message[:200]}")
    return ProviderRejectedError(f"github refused ({exc.status_code}): {exc.message[:200]}")


def _transport_error(exc: httpx.HTTPError, *, effect: bool) -> Exception:
    if effect and isinstance(exc, httpx.TimeoutException):
        return TimeoutError(f"response lost: {exc}")
    return ProviderUnavailableError(f"transport unavailable: {exc}")


# ---------------------------------------------------------------------------
# GitLabNativeEffects — the REAL GitLabClient, provider-realistic preconditions.
# ---------------------------------------------------------------------------


class GitLabNativeEffects(NativeEffectsBase):
    """The saga's effect surface over the REAL GitLab REST v4 client.

    Precondition honesty (the R37-14 basis): GitLab's Commits API has
    NO branch-wide CAS — ``POST /repository/commits`` appends on the
    current head, and a repeated POST is a SECOND commit. The adapter
    therefore:

    1. checks the pinned expected head CLIENT-SIDE (one
       ``get_branch_head``, refused 422-shaped on a moved head — the
       writer's own ``BranchDriftError`` discipline);
    2. RACE NOTE: the check narrows, but cannot close, the window — a
       push landing between the read and the server's apply ends up
       UNDERNEATH our commit (nothing is overwritten; the saga's
       verify/park logic surfaces it from the listed history);
    3. decides the payload action from an AUTHORITATIVE ``read_blob``
       (``not_found`` → create, ``found`` → update, anything else fails
       closed — R14);
    4. maps ``CommitOutcomeUnknown`` (the client's own never-retried
       timeout) to ``TimeoutError`` — the lost-response window.

    Merge requests: GitLab refuses a second OPEN MR for the same source
    branch (a real 400) — the adapter probes the opened MRs by
    ``(project, source, target)`` FIRST and adopts, never replays.
    """

    provider = "gitlab"

    def __init__(
        self,
        client: GitLabClient,
        projects: Mapping[str, int],
        *,
        target_branch: str = "main",
        targets: Mapping[str, str] | None = None,
        payload_path: str = DEFAULT_PAYLOAD_PATH,
    ) -> None:
        super().__init__()
        self._client = client
        self._projects = dict(projects)
        self._target_branch = target_branch
        #: Optional per-repository review targets (default: *target_branch*).
        self._targets = dict(targets or {})
        self._payload_path = payload_path

    # -- the native reads ---------------------------------------------------

    async def list_commits(self, repository_id: str, branch: str) -> tuple[NativeCommit, ...]:
        """The branch's commits OLDEST first, from the client's normalized
        listing (``sha``/``message``/``parent_ids``; the client shape
        carries no author — correlation rides sha/parent/message)."""
        try:
            listed = await self._client.list_commits(self._project(repository_id), branch)
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        commits: list[NativeCommit] = []
        for entry in reversed(listed):  # newest-first listing -> oldest first
            parents = list(entry.get("parent_ids") or [])
            commits.append(
                NativeCommit(
                    sha=str(entry.get("sha") or ""),
                    parent=str(parents[0]) if parents else "",
                    message=str(entry.get("message") or ""),
                    author="",
                )
            )
        return tuple(commits)

    async def merge_requests(self, repository_id: str) -> list[dict[str, Any]]:
        """The provider-listed OPENED merge requests (the never-merged view
        the matrix asserts on: state/draft/merged_at straight from GitLab)."""
        try:
            listed = await self._client.list_merge_requests(
                self._project(repository_id), state="opened"
            )
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        return [
            {
                "iid": mr.iid,
                "title": mr.title,
                "state": mr.state,
                "source_branch": mr.source_branch,
                "target_branch": mr.target_branch,
                "web_url": mr.web_url or "",
                "draft": mr.draft,
                "merged_at": mr.merged_at or "",
            }
            for mr in listed
        ]

    # -- the SagaEffectSurface ------------------------------------------------

    async def remote_head(self, repository_id: str, branch: str) -> str:
        """The branch head in ONE GET (the F28 discipline — never the
        paginated history). A missing branch proves nothing: fail closed."""
        try:
            return await self._client.get_branch_head(self._project(repository_id), branch)
        except GitLabAPIError as exc:
            if exc.status_code >= 500:
                raise _gitlab_api_error(exc) from exc
            # A 4xx on the head read (a missing branch/ref) still proves
            # NOTHING about the effect window — fail closed, never guess.
            raise ProviderUnavailableError(
                f"gitlab surface unreadable ({exc.status_code}): {exc.message[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc

    async def head_carries_marker(self, repository_id: str, branch: str, marker: str) -> bool:
        """NATIVE correlation: list the branch's commits, scan the messages."""
        return bool(await self.commits_carrying(repository_id, branch, marker))

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        self._count(self.commit_calls, repository_id)
        project = self._project(repository_id)
        # (1) the CLIENT-SIDE expected-head check (GitLab has no native one).
        try:
            live_head = await self._client.get_branch_head(project, branch)
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        pin = self._pinned_head(repository_id, branch)
        if pin is not None and live_head != pin:
            raise ProviderRejectedError(
                f"422 commit {repository_id}/{branch}: expected head {pin}, live head"
                f" {live_head} — the branch moved under the publication"
                " (client-side CAS; GitLab's Commits API has none)"
            )
        # (2) the payload action from an AUTHORITATIVE read (R14): a failed
        # read must never forge "file does not exist".
        try:
            blob = await self._client.read_blob(project, self._payload_path, ref=branch)
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        if blob.status == "found":
            action = "update"
        elif blob.status == "not_found":
            action = "create"
        else:
            raise ProviderUnavailableError(
                f"{repository_id}: payload read at {self._payload_path!r} came back"
                f" {blob.status} — the create/update decision cannot be made"
            )
        message = _message_of(marker)
        # (3) the never-retried create (ADR-0005): a lost response is
        # reconciled by correlation, never replayed.
        try:
            created = await self._client.create_commit(
                project,
                branch,
                [
                    {
                        "action": action,
                        "file_path": self._payload_path,
                        "content": payload_document(marker),
                    }
                ],
                message,
            )
        except CommitOutcomeUnknown as exc:
            raise TimeoutError(
                f"{repository_id}: create_commit outcome unknown — reconcile by correlation"
            ) from exc
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"{repository_id}: create_commit response lost") from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=True) from exc
        sha = str(created.get("id") or "")
        if not sha:
            raise ProviderUnavailableError(
                f"{repository_id}: create_commit returned no usable sha — the surface"
                " proves nothing about the effect"
            )
        parent = str(live_head)
        self._journal_commit(
            repository_id,
            branch,
            {
                "sha": sha,
                "parent": parent,
                "message": message,
                "author": str(created.get("author_name") or ""),
            },
        )
        self._maybe_lose_commit_response(repository_id)
        return sha

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        self._count(self.review_calls, repository_id)
        project = self._project(repository_id)
        target = self._targets.get(repository_id, self._target_branch)
        # PROBE FIRST: GitLab natively refuses a second open MR for the same
        # source branch — adoption by the native key, never a replayed create.
        existing = await self._opened_review(project, branch, target)
        if existing is not None:
            self.journal.append(
                {
                    "op": "adopt_merge_request",
                    "provider": self.provider,
                    "repository_id": repository_id,
                    "source_branch": branch,
                    "target_branch": target,
                    "iid": existing["iid"],
                    "url": existing["web_url"],
                }
            )
            return str(existing["web_url"])
        title = f"Draft: forge publication ({marker})"
        try:
            created = await self._client.create_merge_request(
                project,
                branch,
                target,
                title,
                description=f"forge two-writer publication; correlation marker {marker}",
            )
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=True) from exc
        url = str(created.get("web_url") or "")
        self.journal.append(
            {
                "op": "create_merge_request",
                "provider": self.provider,
                "repository_id": repository_id,
                "source_branch": branch,
                "target_branch": target,
                "iid": int(created.get("iid") or 0),
                "url": url,
                "title": title,
                "draft": True,
            }
        )
        return url

    # -- internals ---------------------------------------------------------------

    def _project(self, repository_id: str) -> int:
        try:
            return self._projects[repository_id]
        except KeyError:
            raise KeyError(
                f"no GitLab project mapping for repository {repository_id!r} — the"
                " adapter refuses to guess where a repository lives"
            ) from None

    async def _opened_review(
        self, project: int, source_branch: str, target_branch: str
    ) -> dict[str, Any] | None:
        """The already-open MR for the native key ``(project, source, target)``."""
        try:
            listed = await self._client.list_merge_requests(project, state="opened")
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        for mr in listed:
            if mr.source_branch == source_branch and mr.target_branch == target_branch:
                return {
                    "iid": mr.iid,
                    "web_url": mr.web_url or "",
                    "title": mr.title,
                    "state": mr.state,
                }
        return None


# ---------------------------------------------------------------------------
# GitHubNativeEffects — the REAL GitHubClient, NATIVE branch-wide CAS.
# ---------------------------------------------------------------------------


class GitHubNativeEffects(NativeEffectsBase):
    """The saga's effect surface over the REAL GitHub client.

    GitHub is the CAS-protected lane (``CAS_PROTECTED_PROVIDERS``): the
    commit rides ``createCommitOnBranch`` with ``expectedHeadOid`` — a
    moved head fails with :class:`GitHubStaleBranchError` server-side
    (``STALE_DATA``), which is the exactly-once guard the A12 research
    established. The pin (when the publication set one) is the CAS
    token; without one the live head at call time is (the mutation
    still cannot double-apply — the CAS refuses it). The marker rides
    the commit HEADLINE (the one line every listing shape carries).

    Pull requests are found by the NATIVE ``(head, base)`` key and
    opened as DRAFTS — the bot never merges.
    """

    provider = "github"

    def __init__(
        self,
        client: GitHubClient,
        repositories: Mapping[str, tuple[str, str]],
        *,
        base_branch: str = "main",
        payload_path: str = DEFAULT_PAYLOAD_PATH,
    ) -> None:
        super().__init__()
        self._client = client
        self._repositories = {
            repository_id: (owner, name) for repository_id, (owner, name) in repositories.items()
        }
        self._base_branch = base_branch
        self._payload_path = payload_path

    # -- the native reads ---------------------------------------------------

    async def list_commits(self, repository_id: str, branch: str) -> tuple[NativeCommit, ...]:
        """The branch's commits OLDEST first, from the client's normalized
        ``{sha, message, parent_ids}`` listing."""
        owner, name = self._repo(repository_id)
        try:
            listed = await self._client.list_commits(owner, name, branch)
        except GitHubAPIError as exc:
            raise _github_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        commits: list[NativeCommit] = []
        for entry in reversed(listed):
            parents = list(entry.get("parent_ids") or [])
            commits.append(
                NativeCommit(
                    sha=str(entry.get("sha") or ""),
                    parent=str(parents[0]) if parents else "",
                    message=str(entry.get("message") or ""),
                    author="",
                )
            )
        return tuple(commits)

    # -- the SagaEffectSurface ------------------------------------------------

    async def remote_head(self, repository_id: str, branch: str) -> str:
        try:
            owner, name = self._repo(repository_id)
            return await self._client.get_branch_head(owner, name, branch)
        except GitHubAPIError as exc:
            raise _github_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc

    async def head_carries_marker(self, repository_id: str, branch: str, marker: str) -> bool:
        return bool(await self.commits_carrying(repository_id, branch, marker))

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        self._count(self.commit_calls, repository_id)
        owner, name = self._repo(repository_id)
        expected = self._pinned_head(repository_id, branch)
        if expected is None:
            # No pin: CAS on the live head at call time — the mutation is
            # still single-apply (the server refuses a moved head).
            expected = await self.remote_head(repository_id, branch)
        headline = _headline_of(marker)
        try:
            created = await self._client.create_commit_on_branch(
                owner,
                name,
                branch,
                headline=headline,
                additions=[(self._payload_path, payload_document(marker))],
                expected_head_oid=expected,
                client_mutation_id=marker,  # pure correlation — GitHub does NOT dedupe on it
            )
        except GitHubStaleBranchError as exc:
            raise ProviderRejectedError(
                f"422 commit {repository_id}/{branch}: {exc} — the branch moved under"
                " the publication (native createCommitOnBranch CAS)"
            ) from exc
        except GitHubRateLimited as exc:
            raise ProviderUnavailableError(f"github rate limited: {exc}") from exc
        except GitHubAPIError as exc:
            raise _github_api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"{repository_id}: createCommitOnBranch response lost") from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=True) from exc
        sha = str(created.get("oid") or "")
        if not sha:
            raise ProviderUnavailableError(
                f"{repository_id}: createCommitOnBranch returned no oid — the surface"
                " proves nothing about the effect"
            )
        self._journal_commit(
            repository_id,
            branch,
            {"sha": sha, "parent": expected, "message": headline, "author": ""},
        )
        self._maybe_lose_commit_response(repository_id)
        return sha

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        self._count(self.review_calls, repository_id)
        owner, name = self._repo(repository_id)
        # PROBE FIRST: the native (head, base) key — a repeat returns the
        # SAME PR, never a second review.
        try:
            existing = await self._client.get_pr_by_head(
                owner, name, branch, base=self._base_branch
            )
        except GitHubAPIError as exc:
            raise _github_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        if existing is not None:
            url = str(existing.get("html_url") or "")
            self.journal.append(
                {
                    "op": "adopt_merge_request",
                    "provider": self.provider,
                    "repository_id": repository_id,
                    "source_branch": branch,
                    "target_branch": self._base_branch,
                    "iid": int(existing.get("number") or 0),
                    "url": url,
                }
            )
            return url
        title = f"Draft: forge publication ({marker})"
        try:
            created = await self._client.create_draft_pr(
                owner,
                name,
                head=branch,
                base=self._base_branch,
                title=title,
                body=f"forge two-writer publication; correlation marker {marker}",
            )
        except GitHubAPIError as exc:
            raise _github_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=True) from exc
        url = str(created.get("html_url") or "")
        self.journal.append(
            {
                "op": "create_merge_request",
                "provider": self.provider,
                "repository_id": repository_id,
                "source_branch": branch,
                "target_branch": self._base_branch,
                "iid": int(created.get("number") or 0),
                "url": url,
                "title": title,
                "draft": bool(created.get("draft")),
            }
        )
        return url

    # -- internals ---------------------------------------------------------------

    def _repo(self, repository_id: str) -> tuple[str, str]:
        try:
            return self._repositories[repository_id]
        except KeyError:
            raise KeyError(
                f"no GitHub repository mapping for {repository_id!r} — the adapter"
                " refuses to guess where a repository lives"
            ) from None
