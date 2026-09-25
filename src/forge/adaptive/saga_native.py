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

R38-13 (#314) qualifies the same surface WITHOUT overstating what each
provider guarantees. Three extensions, all on the existing seam:

- **payload preconditions + recheck** (:class:`GitLabNativeEffects`)
  — beside the pinned expected head, the commit records the PAYLOAD
  BASE (the content digest of the payload blob the change was computed
  against) and the AFFECTED-FILE VERSIONS (path → blob digest at
  preflight). The window between the preflight read and the server
  apply is RECHECKED against those preconditions twice: once BEFORE the
  apply (a drifted payload file refuses with a typed
  :class:`ContentConflictError` while the concurrent content is still
  the branch head — nothing overwritten), and once AFTER it (a commit
  that landed on a base other than the preflight head gets its payload
  file read back AT THE PARENT revision; a drift there is the
  full-file-replacement hazard the review named — preserving a human
  COMMIT in history is not preserving its CONTENT — and surfaces as the
  same typed conflict, never a silent overwrite).
- **single-writer exclusivity** — where the provider offers no atomic
  branch-wide precondition (GitLab), the effect surface enforces ONE
  in-flight writer per branch: a second writer entering while the first
  holds the preflight→apply→recheck window is refused with a typed
  :class:`WriterExclusivityError`, never interleaved. GitHub does not
  need the policy — its CAS is native (see
  :data:`PROVIDER_ATOMICITY`, the matrix the profile statement renders).
- **content-level adoption verification** — correlation does not stop
  at the message: a commit counts as carrying the saga's marker for
  ADOPTION only when the payload file's CONTENT at that commit is the
  intended payload (blob-level read, :meth:`GitLabNativeEffects.
  commits_carrying`). A forged commit with the right message and the
  wrong content is not adopted. GitHub's client exposes no repository
  blob read, so its correlation stays marker+parent — the matrix says
  so instead of pretending a verification that transport cannot make.

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

import hashlib
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
    "CONTENT_CONFLICT_TAG",
    "DEFAULT_PAYLOAD_PATH",
    "PROVIDER_ATOMICITY",
    "PUBLICATION_PAYLOAD_SCHEMA",
    "WRITER_EXCLUSIVITY_TAG",
    "SagaEffectSurface",
    "GitLabNativeEffects",
    "GitHubNativeEffects",
    "NativeEffectsBase",
    "ContentConflictError",
    "WriterExclusivityError",
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


#: The digest spelling of a payload blob — the "blob sha" the
#: preconditions record (providers disagree on blob-id shapes; the plain
#: sha256 of the decoded utf-8 bytes is the provider-neutral identity,
#: the same digest :class:`BlobReadResult` carries).
def payload_digest(content: str | bytes) -> str:
    """The content digest a precondition or an adoption check compares."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


#: The note tag a :class:`ContentConflictError` books under (the durable
#: outbox keys its ``saga.content_conflict`` event on this marker).
CONTENT_CONFLICT_TAG = "content conflict"

#: The note tag a :class:`WriterExclusivityError` books under.
WRITER_EXCLUSIVITY_TAG = "writer exclusivity"


class ContentConflictError(ProviderRejectedError):
    """A concurrent edit touched the payload file inside the publication's
    read/apply window — the change was computed against a DIFFERENT
    content than the branch now carries (R38-13's named hazard:
    preserving a human COMMIT in history is not preserving its CONTENT
    after a full-file replacement).

    Typed so the saga's booking and the outbox can name the conflict
    explicitly instead of folding it into a generic refusal: the
    repository is left exactly as the concurrent writer left it (or, on
    the post-apply shape, the landed commit is surfaced with the parent
    revision where the concurrent content is recoverable) — never
    silently overwritten, never claimed published.
    """


class WriterExclusivityError(ProviderRejectedError):
    """A second writer entered while another writer held the same branch's
    preflight→apply→recheck window — the single-writer policy for
    providers with no atomic branch-wide precondition (GitLab).

    Refused, never queued-behind-silently and never interleaved: the
    policy is what stands in for the CAS the provider does not offer
    (see :data:`PROVIDER_ATOMICITY`)."""


#: The two-writer capability declaration's CAS/exclusivity matrix —
#: EXACTLY which provider has native atomicity and which relies on the
#: stricter writer policy (R38-13's profile statement; the evaluation
#: README renders it verbatim).
PROVIDER_ATOMICITY: Mapping[str, Mapping[str, Any]] = {
    "gitlab": {
        "native_branch_cas": False,
        "same_file_window": "client-side preconditions + pre/post-apply recheck"
        " (typed content conflict, never a silent overwrite)",
        "concurrent_writers": "single-writer exclusivity policy on the effect"
        " surface (one in-flight writer per branch; the second parks typed)",
        "adoption_verification": "blob-level: the payload file's content at the"
        " adopted commit must equal the intended payload",
    },
    "github": {
        "native_branch_cas": True,
        "same_file_window": "native: createCommitOnBranch expectedHeadOid — the"
        " server refuses the commit outright (STALE_DATA)",
        "concurrent_writers": "native CAS (the exclusivity policy is not the guarantee of record)",
        "adoption_verification": "marker + parent over the listed history — the"
        " GitHub client exposes no repository blob read, so no content check is"
        " claimed",
    },
}


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
      first (``create_commit`` with sha/parent/message — and, since
      R38-13, the PAYLOAD PRECONDITIONS beside the expected head:
      ``payload_base`` and ``file_versions`` — plus
      ``create_merge_request``/``adopt_merge_request`` with iid/url) —
      plus the read ops correlation performed;
    - :attr:`writer_exclusivity_enforced` — whether THIS provider's
      guarantee of record is the single-writer policy (GitLab: no native
      CAS) or a native CAS that makes the policy redundant (GitHub);
    - :attr:`_in_flight` — the branches whose preflight→apply→recheck
      window a writer currently HOLDS: the exclusivity set the second
      writer is refused against, never interleaved;
    - :attr:`_unresolved` — mutations whose provider outcome was never
      established (a lost response): a retried ``commit`` for the SAME
      ``(repository, branch, marker)`` is refused until a
      content-verified probe resolves it — ADR-0005's "reconcile before
      any retry" made structural, because a negative probe does not
      prove a delayed apply absent;
    - :meth:`destructive_operations` — ALWAYS empty: the adapter surface
      has no merge/force/delete call to make (structural, not
      conventional).
    """

    #: The provider this adapter talks to (journal + report tagging).
    provider: str = "abstract"

    #: Whether the single-writer exclusivity policy is this provider's
    #: guarantee of record (True on GitLab; GitHub's native CAS makes it
    #: redundant — see :data:`PROVIDER_ATOMICITY`).
    writer_exclusivity_enforced: bool = False

    def __init__(self) -> None:
        self._pins: dict[tuple[str, str], str] = {}
        #: Every native effect with its identity, oldest first.
        self.journal: list[dict[str, Any]] = []
        self.commit_calls: dict[str, int] = {}
        self.review_calls: dict[str, int] = {}
        #: The injected lost-response window (test knob): repositories
        #: whose commit RESPONSE dies after the effect landed.
        self.lose_commit_response: set[str] = set()
        #: Branches whose commit window a writer currently holds
        #: (R38-13's exclusivity set — see the class docstring).
        self._in_flight: set[tuple[str, str]] = set()
        #: Mutations attempted whose provider outcome was never
        #: established: ``(repository, branch, marker) -> reason``. A
        #: retried commit for the same mutation is refused until a
        #: content-verified correlation read resolves it.
        self._unresolved: dict[tuple[str, str, str], str] = {}

    # -- the expected-head pin ------------------------------------------------

    def pin_expected_head(self, repository_id: str, branch: str, expected_head: str) -> None:
        """The publication's CAS pin (the writer's ``expected_head`` drift guard)."""
        self._pins[(repository_id, branch)] = expected_head

    def _pinned_head(self, repository_id: str, branch: str) -> str | None:
        return self._pins.get((repository_id, branch))

    # -- the single-writer exclusivity window (R38-13) --------------------------

    def _begin_writer_window(self, repository_id: str, branch: str) -> None:
        """Enter the branch's exclusive preflight→apply→recheck window.

        The policy provider's stand-in for a branch-wide CAS: while one
        writer holds the window, a second writer targeting the same
        branch is REFUSED (:class:`WriterExclusivityError`) — parked
        typed, never interleaved. On the CAS provider the policy is not
        enforced: concurrent writers are arbitrated by the SERVER's
        ``expectedHeadOid`` refusal, which is the guarantee of record
        (:data:`PROVIDER_ATOMICITY`).
        """
        if not self.writer_exclusivity_enforced:
            return
        key = (repository_id, branch)
        if key in self._in_flight:
            raise WriterExclusivityError(
                f"{WRITER_EXCLUSIVITY_TAG}: another writer holds the publication window of"
                f" {repository_id}/{branch} — the single-writer policy for providers"
                " without a native branch-wide CAS refuses the second writer"
                " instead of interleaving it"
            )
        self._in_flight.add(key)

    def _end_writer_window(self, repository_id: str, branch: str) -> None:
        self._in_flight.discard((repository_id, branch))

    # -- the unresolved-mutation ledger (ADR-0005: reconcile before any retry) ----

    def _refuse_unresolved_redispatch(self, repository_id: str, branch: str, marker: str) -> None:
        """Refuse to re-send a mutation whose earlier outcome was never
        established — a negative probe does not prove a delayed apply
        absent, so the same mutation is never blindly redispatched.
        Resolution is by evidence only: a content-verified correlation
        read (or an explicit new attempt under a NEW marker)."""
        reason = self._unresolved.get((repository_id, branch, marker))
        if reason is None:
            return
        raise ProviderUnavailableError(
            f"{repository_id}/{branch}: an earlier mutation for this marker never"
            f" resolved ({reason}) — a negative probe does not prove it absent;"
            " refusing to blindly redispatch the same mutation (ADR-0005)."
            " Resolve by correlation or an explicit new attempt"
        )

    def _mark_mutation_unresolved(
        self, repository_id: str, branch: str, marker: str, reason: str
    ) -> None:
        self._unresolved[(repository_id, branch, marker)] = reason

    def _resolve_mutation(self, repository_id: str, branch: str, marker: str) -> None:
        """The surface ESTABLISHED the mutation (a content-verified commit
        carrying the marker) — the window is closed by evidence."""
        self._unresolved.pop((repository_id, branch, marker), None)

    # -- the native reads every adapter provides -------------------------------

    async def list_commits(self, repository_id: str, branch: str) -> tuple[NativeCommit, ...]:
        """The branch's commits, OLDEST first — the listing correlation reads."""
        raise NotImplementedError  # the adapter's provider transport

    async def commits_carrying(
        self, repository_id: str, branch: str, marker: str
    ) -> tuple[NativeCommit, ...]:
        """Every DISTINCT commit whose message carries the marker — the
        duplicate-effect view read straight off the provider's listing
        (two entries = a duplicated logical effect).

        The base spelling is MESSAGE-ONLY; :class:`GitLabNativeEffects`
        overrides it with the R38-13 content-verified correlation (the
        payload blob at each marker commit must equal the intended
        payload — a matching message alone proves nothing). GitHub keeps
        the message-only spelling: its client exposes no repository blob
        read, and :data:`PROVIDER_ATOMICITY` says so rather than
        pretending a verification that transport cannot make."""
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

    def _journal_commit(
        self,
        repository_id: str,
        branch: str,
        commit: Mapping[str, Any],
        *,
        preconditions: Mapping[str, Any] | None = None,
        conflicted: bool = False,
    ) -> None:
        """Journal one create-commit with its native identity AND — the
        R38-13 addition — the payload preconditions the effect was
        checked against, recorded BESIDE the expected head (the
        ``parent``): ``payload_base`` (the digest of the payload blob the
        change was computed against; ``""`` when the preflight read
        proved absence) and ``file_versions`` (path → blob digest at
        preflight). The durable layer harvests these into the run's
        evidence so a recovery or an audit can recheck the landed effect
        against exactly what was preflighted. ``conflicted`` marks an
        effect that LANDED but whose post-apply recheck surfaced a
        content conflict — journaled as what it is, never as clean."""
        entry: dict[str, Any] = {
            "op": "create_commit",
            "provider": self.provider,
            "repository_id": repository_id,
            "branch": branch,
            "sha": str(commit.get("sha") or ""),
            "parent": str(commit.get("parent") or ""),
            "message": str(commit.get("message") or ""),
            "author": str(commit.get("author") or ""),
        }
        if preconditions:
            extra: dict[str, Any] = {
                "payload_base": str(preconditions.get("payload_base") or ""),
                "file_versions": dict(preconditions.get("file_versions") or {}),
            }
            if preconditions.get("expected_head"):
                # the head the change was PREFLIGHTED against — carried
                # explicitly because the applied parent can differ from it
                # (a concurrent commit landing inside the window)
                extra["expected_head"] = str(preconditions["expected_head"])
            entry.update(extra)
        if conflicted:
            entry["content_conflict"] = True
        self.journal.append(entry)

    def _count(self, calls: dict[str, int], repository_id: str) -> None:
        calls[repository_id] = calls.get(repository_id, 0) + 1

    def _maybe_lose_commit_response(
        self, repository_id: str, branch: str = "", marker: str = ""
    ) -> None:
        """The injected lost-response window: the RESPONSE dies after the
        effect was sent — from the caller's chair the outcome is unknown,
        so the mutation is MARKED unresolved exactly like a provider
        timeout (resolution only by a content-verified probe)."""
        if repository_id in self.lose_commit_response:
            if branch and marker:
                self._mark_mutation_unresolved(
                    repository_id, branch, marker, "response lost after the effect was sent"
                )
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
    2. RACE NOTE (R38-13's qualification): the check narrows, but cannot
       close, the window — a push landing between the read and the
       server's apply ends up UNDERNEATH our commit. R37-14 leaned on
       history preservation there; R38-13 closes the CONTENT half: the
       payload base and affected-file versions are recorded at preflight
       and the branch is RECHECKED against them both BEFORE the apply
       (a drifted payload file refuses with a typed
       :class:`ContentConflictError` while the concurrent content is
       still the head — nothing overwritten) and AFTER it (a commit
       that landed on a different base gets its payload file read back
       AT THE PARENT revision; a drift there — the full-file-replacement
       hazard — surfaces as the same typed conflict, never a silent
       overwrite);
    3. decides the payload action from an AUTHORITATIVE ``read_blob``
       (``not_found`` → create, ``found`` → update, anything else fails
       closed — R14);
    4. maps ``CommitOutcomeUnknown`` (the client's own never-retried
       timeout) to ``TimeoutError`` — the lost-response window — and
       MARKS the mutation unresolved: the same mutation is never
       blindly redispatched on a negative probe (ADR-0005).

    Single-writer policy: this provider's guarantee of record — the
    adapter enforces ONE in-flight writer per branch
    (:attr:`NativeEffectsBase.writer_exclusivity_enforced`); a second
    writer entering the window is refused typed
    (:class:`WriterExclusivityError`), never interleaved.

    Adoption is content-verified (:meth:`commits_carrying`): a commit
    counts as carrying the marker only when the payload file's CONTENT
    at that commit equals the intended payload — a matching commit
    message alone proves nothing (R38-13).

    Merge requests: GitLab refuses a second OPEN MR for the same source
    branch (a real 400) — the adapter probes the opened MRs by
    ``(project, source, target)`` FIRST and adopts, never replays.
    """

    provider = "gitlab"
    writer_exclusivity_enforced = True

    def __init__(
        self,
        client: GitLabClient,
        projects: Mapping[str, int],
        *,
        target_branch: str = "main",
        targets: Mapping[str, str] | None = None,
        payload_path: str = DEFAULT_PAYLOAD_PATH,
        scoped_clients: Mapping[str, GitLabClient] | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._projects = dict(projects)
        self._target_branch = target_branch
        #: Optional per-repository review targets (default: *target_branch*).
        self._targets = dict(targets or {})
        self._payload_path = payload_path
        #: Optional per-repository CLIENTS — the one-writer-credential-
        #: per-repository doctrine (``credential_staging``): when a
        #: repository has its own scoped client, every call the surface
        #: makes for it goes through THAT client and no other — there is
        #: no fallback path to the shared client when the scoped
        #: credential is refused (R38-13's revoked-access discipline).
        self._scoped_clients = dict(scoped_clients or {})
        #: Test knob (the delayed-apply window): repositories whose
        #: CORRELATION reads still see the pre-apply state for a bounded
        #: number of probes — the provider window in which an accepted
        #: mutation is not yet visible (the issue's "initially negative
        #: probe" injection; the commits themselves are real).
        self.delayed_visibility: dict[str, int] = {}
        #: Test knob (the read/apply window): human edits applied through
        #: the REAL client INSIDE the publication's preflight→apply
        #: window — ``(file_path, content)`` pairs per repository (a
        #: ``None`` content spells a DELETE — the third drift shape). The
        #: live failpoint matrix's window schedules are injected here;
        #: the commits they create are real provider commits.
        self.in_window_human_edits: dict[str, tuple[tuple[str, str | None], ...]] = {}

    # -- the per-repository transport (no credential fallback, ever) ---------

    def _client_for(self, repository_id: str) -> GitLabClient:
        scoped = self._scoped_clients.get(repository_id)
        return scoped if scoped is not None else self._client

    # -- the native reads ---------------------------------------------------

    def _blind_with_delayed_visibility(self, repository_id: str) -> bool:
        """Consume one unit of the injected delayed-apply window, if any."""
        remaining = self.delayed_visibility.get(repository_id, 0)
        if remaining <= 0:
            return False
        self.delayed_visibility[repository_id] = remaining - 1
        return True

    async def list_commits(self, repository_id: str, branch: str) -> tuple[NativeCommit, ...]:
        """The branch's commits OLDEST first, from the client's normalized
        listing (``sha``/``message``/``parent_ids``; the client shape
        carries no author — correlation rides sha/parent/message)."""
        try:
            listed = await self._client_for(repository_id).list_commits(
                self._project(repository_id), branch
            )
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
            listed = await self._client_for(repository_id).list_merge_requests(
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

    # -- content-level correlation (R38-13) --------------------------------

    async def _payload_digest_at(self, repository_id: str, ref: str) -> str | None:
        """The payload blob's content digest at *ref* — ``None`` when the
        provider PROVES the file absent there; unreadable surfaces fail
        CLOSED (an outage proves nothing, and adoption must not claim on
        a surface that cannot be read)."""
        try:
            blob = await self._client_for(repository_id).read_blob(
                self._project(repository_id), self._payload_path, ref=ref
            )
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        if blob.status == "found":
            return blob.content_sha256 or payload_digest(blob.content or "")
        if blob.status == "not_found":
            return None
        raise ProviderUnavailableError(
            f"{repository_id}: payload read at ref {ref[:12]}… came back {blob.status}"
            f" ({blob.detail[:120]}) — the content check cannot be made, so the"
            " surface proves nothing"
        )

    async def commits_carrying(
        self, repository_id: str, branch: str, marker: str
    ) -> tuple[NativeCommit, ...]:
        """Every DISTINCT commit whose message carries the marker AND whose
        payload-file CONTENT at that commit is the intended payload — the
        R38-13 content half of the correlation. A matching commit message
        proves nothing: a forged commit with the marker in its message and
        absent/wrong payload content does NOT carry the effect, so recovery
        cannot adopt it (the repo parks for a human instead). A verified
        hit also RESOLVES the marker's unresolved-mutation mark — the
        window closed by evidence, not by a redispatch."""
        expected = payload_digest(payload_document(marker))
        carrying: list[NativeCommit] = []
        for commit in await self.list_commits(repository_id, branch):
            if marker not in commit.message:
                continue
            digest = await self._payload_digest_at(repository_id, commit.sha)
            if digest == expected:
                carrying.append(commit)
                self._resolve_mutation(repository_id, branch, marker)
        return tuple(carrying)

    # -- the SagaEffectSurface ------------------------------------------------

    async def remote_head(self, repository_id: str, branch: str) -> str:
        """The branch head in ONE GET (the F28 discipline — never the
        paginated history). A missing branch proves nothing: fail closed.
        Under the injected delayed-apply window the read still sees the
        PINNED head (the pre-apply state the issue's negative probe
        returns) — the knob's honest spelling of "accepted but not yet
        visible"."""
        if self._blind_with_delayed_visibility(repository_id):
            pinned = self._pinned_head(repository_id, branch)
            if pinned is None:
                raise ProviderUnavailableError(
                    f"{repository_id}/{branch}: the delayed-visibility window has no"
                    " pinned head to return — the surface proves nothing"
                )
            return pinned
        try:
            return await self._client_for(repository_id).get_branch_head(
                self._project(repository_id), branch
            )
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
        """NATIVE correlation: list the branch's commits, scan the messages —
        AND verify the payload CONTENT at each marker-carrying commit
        (:meth:`commits_carrying`): a matching message alone does not
        carry the effect."""
        return bool(await self.commits_carrying(repository_id, branch, marker))

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        self._count(self.commit_calls, repository_id)
        project = self._project(repository_id)
        client = self._client_for(repository_id)
        # (0) the single-writer window + the unresolved-mutation guard: this
        # provider's policy stands in for the branch-wide CAS it lacks.
        self._begin_writer_window(repository_id, branch)
        try:
            return await self._commit_under_window(client, project, repository_id, branch, marker)
        finally:
            self._end_writer_window(repository_id, branch)

    async def _commit_under_window(
        self,
        client: GitLabClient,
        project: int,
        repository_id: str,
        branch: str,
        marker: str,
    ) -> str:
        # Reconcile BEFORE any retry (ADR-0005): a mutation whose outcome
        # was never established is never blindly redispatched.
        self._refuse_unresolved_redispatch(repository_id, branch, marker)
        # (1) the CLIENT-SIDE expected-head check (GitLab has no native one).
        try:
            live_head = await client.get_branch_head(project, branch)
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
        # read must never forge "file does not exist". The same read RECORDS
        # the R38-13 preconditions: the payload base (the blob digest the
        # change is computed against) and the affected-file versions.
        try:
            blob = await client.read_blob(project, self._payload_path, ref=branch)
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        if blob.status == "found":
            action = "update"
            payload_base = blob.content_sha256 or payload_digest(blob.content or "")
        elif blob.status == "not_found":
            action = "create"
            payload_base = ""  # proved absent — the create's base identity
        else:
            raise ProviderUnavailableError(
                f"{repository_id}: payload read at {self._payload_path!r} came back"
                f" {blob.status} — the create/update decision cannot be made"
            )
        file_versions = {self._payload_path: payload_base}
        # (2b) the read/apply WINDOW: the failpoint matrix's human-edit
        # schedules land here — real commits pushed through the real client
        # between the preflight read and the apply, exactly the race the
        # client-side head check cannot close.
        await self._apply_in_window_edits(client, project, repository_id, branch)
        # (2c) the PRE-APPLY recheck: the payload file must still carry the
        # preflighted content. Any drift shape — changed content, a file
        # created underneath a create's proved absence, a file deleted
        # underneath an update — refuses with the TYPED conflict BEFORE any
        # effect: the concurrent content stays the branch head, nothing is
        # overwritten.
        drift = await self._payload_drift_against(
            client, project, repository_id, branch, payload_base
        )
        if drift is not None:
            raise ContentConflictError(
                f"{CONTENT_CONFLICT_TAG} (422) commit {repository_id}/{branch}: the payload"
                f" file {self._payload_path!r} changed inside the publication's"
                f" read/apply window ({drift}) — the change was computed against"
                f" payload base {payload_base or 'absence'}; refusing before any"
                " effect so the concurrent content stays the branch head"
            )
        message = _message_of(marker)
        # (3) the never-retried create (ADR-0005): a lost response is
        # reconciled by correlation, never replayed — and the mutation is
        # MARKED unresolved until a content-verified probe resolves it.
        try:
            created = await client.create_commit(
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
            self._mark_mutation_unresolved(
                repository_id, branch, marker, "create_commit outcome unknown"
            )
            raise TimeoutError(
                f"{repository_id}: create_commit outcome unknown — reconcile by correlation"
            ) from exc
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.TimeoutException as exc:
            self._mark_mutation_unresolved(repository_id, branch, marker, "response lost")
            raise TimeoutError(f"{repository_id}: create_commit response lost") from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=True) from exc
        sha = str(created.get("id") or "")
        if not sha:
            self._mark_mutation_unresolved(
                repository_id, branch, marker, "no usable sha in the response"
            )
            raise ProviderUnavailableError(
                f"{repository_id}: create_commit returned no usable sha — the surface"
                " proves nothing about the effect"
            )
        # (4) the POST-APPLY recheck: what base did the server actually
        # apply on? A parent other than the preflight head means a
        # concurrent commit landed INSIDE the window — our full-file
        # payload replacement may have buried its content. Read the payload
        # file back AT THE PARENT revision: still the preflighted content
        # → the concurrent change did not touch our file (it proceeds,
        # preserved underneath); drifted → the typed conflict, surfaced on
        # the landed effect — never a silent overwrite. The parent sha in
        # the message is where the concurrent content is recoverable.
        applied_parent = await self._applied_parent(repository_id, branch, sha, created)
        if applied_parent != live_head:
            drift = await self._payload_drift_against(
                client, project, repository_id, applied_parent, payload_base
            )
            if drift is not None:
                # The effect LANDED on a base we never preflighted: journal
                # it as what it is (a conflicted, unblessed effect — never
                # a silently clean one), then surface the typed conflict.
                self._journal_commit(
                    repository_id,
                    branch,
                    {
                        "sha": sha,
                        "parent": str(applied_parent),
                        "message": message,
                        "author": str(created.get("author_name") or ""),
                    },
                    preconditions={
                        "payload_base": payload_base,
                        "file_versions": file_versions,
                        "expected_head": live_head,
                    },
                    conflicted=True,
                )
                raise ContentConflictError(
                    f"{CONTENT_CONFLICT_TAG} (landed) commit {repository_id}/{branch}: the"
                    " server applied the publication on base"
                    f" {applied_parent} — not the preflighted head {live_head} —"
                    f" and the payload file at that base {drift}. The concurrent"
                    " content is preserved at the parent revision"
                    f" {applied_parent}; the effect stands conflicted for a human"
                    " decision — never a silent overwrite, never claimed published"
                )
        parent = str(applied_parent or live_head)
        self._journal_commit(
            repository_id,
            branch,
            {
                "sha": sha,
                "parent": parent,
                "message": message,
                "author": str(created.get("author_name") or ""),
            },
            preconditions={
                "payload_base": payload_base,
                "file_versions": file_versions,
                "expected_head": live_head,
            },
        )
        self._resolve_mutation(repository_id, branch, marker)
        self._maybe_lose_commit_response(repository_id, branch, marker)
        return sha

    async def _apply_in_window_edits(
        self, client: GitLabClient, project: int, repository_id: str, branch: str
    ) -> None:
        """The injected read/apply-window schedule (a TEST knob): perform
        the configured human edits as REAL commits through the real
        client, inside the publication window. Empty by default — the
        knob exists so the live failpoint matrix can exercise the exact
        race between the preflight read and the server apply."""
        edits = self.in_window_human_edits.get(repository_id)
        if not edits:
            return
        try:
            await client.create_commit(
                project,
                branch,
                [
                    (
                        {"action": "delete", "file_path": file_path}
                        if content is None
                        else {"action": "create", "file_path": file_path, "content": content}
                    )
                    for file_path, content in edits
                ],
                "human edit inside the publication window (injected schedule)",
            )
        except GitLabAPIError as exc:
            raise _gitlab_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=True) from exc

    async def _payload_drift_against(
        self,
        client: GitLabClient,
        project: int,
        repository_id: str,
        ref: str,
        payload_base: str,
    ) -> str | None:
        """Compare the payload file's content at *ref* against the
        preflighted *payload_base*; a human-readable drift description, or
        ``None`` when the content still matches. An unreadable surface
        fails CLOSED (the recheck cannot be made → nothing proceeds)."""
        try:
            blob = await client.read_blob(project, self._payload_path, ref=ref)
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=False) from exc
        if blob.status == "found":
            digest = blob.content_sha256 or payload_digest(blob.content or "")
            if digest == payload_base:
                return None
            return (
                f"carries digest {digest[:12]}… "
                f"({'was absent at preflight' if not payload_base else f'expected {payload_base[:12]}…'})"
            )
        if blob.status == "not_found":
            if not payload_base:
                return None  # still proved absent — the create's base holds
            return "no longer exists (deleted inside the window)"
        raise ProviderUnavailableError(
            f"{repository_id}: payload recheck at {self._payload_path!r} came back"
            f" {blob.status} — the content precondition cannot be verified, so the"
            " effect cannot proceed"
        )

    async def _applied_parent(
        self, repository_id: str, branch: str, sha: str, created: Mapping[str, Any]
    ) -> str:
        """The parent of the just-applied commit — the base the server
        ACTUALLY applied on. Prefers the create response's own
        ``parent_ids`` (the real service returns them); falls back to the
        authoritative listing."""
        parents = list(created.get("parent_ids") or [])
        if parents:
            return str(parents[0])
        listed = await self.list_commits(repository_id, branch)
        for commit in listed:
            if commit.sha == sha:
                return commit.parent
        raise ProviderUnavailableError(
            f"{repository_id}: the applied commit {sha[:12]}… is not in the listed"
            " history — the surface proves nothing about the effect"
        )

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        self._count(self.review_calls, repository_id)
        project = self._project(repository_id)
        target = self._targets.get(repository_id, self._target_branch)
        # PROBE FIRST: GitLab natively refuses a second open MR for the same
        # source branch — adoption by the native key, never a replayed create.
        existing = await self._opened_review(repository_id, project, branch, target)
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
            created = await self._client_for(repository_id).create_merge_request(
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
        self, repository_id: str, project: int, source_branch: str, target_branch: str
    ) -> dict[str, Any] | None:
        """The already-open MR for the native key ``(project, source, target)``,
        read through the repository's OWN client (a scoped credential is
        never swapped for a wider one to find a review)."""
        try:
            listed = await self._client_for(repository_id).list_merge_requests(
                project, state="opened"
            )
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

    R38-13 qualification, stated honestly for THIS provider: the
    same-file read/apply window is closed NATIVELY (the server refuses
    the commit when the head moved — no client-side precondition dance
    is needed and none is performed), concurrent writers are arbitrated
    by that same CAS (the single-writer exclusivity policy is NOT
    GitHub's guarantee of record), and adoption correlation stays
    MARKER + PARENT over the listed history because the GitHub client
    exposes no repository blob read — no content-level adoption
    verification is claimed here (:data:`PROVIDER_ATOMICITY` says the
    same, and the journal still records the INTENDED payload digest
    beside the CAS token so an audit knows exactly what the commit was
    supposed to carry).

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
        # Reconcile BEFORE any retry (ADR-0005) — same discipline as GitLab:
        # an unresolved mutation for this marker is never blindly resent.
        self._refuse_unresolved_redispatch(repository_id, branch, marker)
        expected = self._pinned_head(repository_id, branch)
        if expected is None:
            # No pin: CAS on the live head at call time — the mutation is
            # still single-apply (the server refuses a moved head).
            expected = await self.remote_head(repository_id, branch)
        headline = _headline_of(marker)
        intended = payload_document(marker)
        try:
            created = await self._client.create_commit_on_branch(
                owner,
                name,
                branch,
                headline=headline,
                additions=[(self._payload_path, intended)],
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
            self._mark_mutation_unresolved(repository_id, branch, marker, "response lost")
            raise TimeoutError(f"{repository_id}: createCommitOnBranch response lost") from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc, effect=True) from exc
        sha = str(created.get("oid") or "")
        if not sha:
            self._mark_mutation_unresolved(
                repository_id, branch, marker, "no usable oid in the response"
            )
            raise ProviderUnavailableError(
                f"{repository_id}: createCommitOnBranch returned no oid — the surface"
                " proves nothing about the effect"
            )
        # GitHub's native CAS is the postcondition of record: the server
        # guarantees the commit's parent IS the CAS token (a moved head was
        # refused outright), so the GitLab-side post-apply recheck has no
        # equivalent to perform here. What IS recorded, beside the token:
        # the intended payload digest (the content identity the commit was
        # to carry — the audit's half of the content story; the transport
        # offers no blob read to verify it against after the fact).
        self._journal_commit(
            repository_id,
            branch,
            {"sha": sha, "parent": expected, "message": headline, "author": ""},
            preconditions={
                "payload_base": payload_digest(intended),
                "file_versions": {self._payload_path: payload_digest(intended)},
            },
        )
        self._resolve_mutation(repository_id, branch, marker)
        self._maybe_lose_commit_response(repository_id, branch, marker)
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
