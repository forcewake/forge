"""R36-11 — the live discovery AUTHORITY surface (read-many/write-one).

The system-context profile (NEXT-21,
:mod:`forge.adaptive.system_context`) says WHICH repositories a run may
read and which ONE it may write. This module is the LIVE half of that
bargain — the qualification harness that proves the boundary at the real
discovery seams instead of only on the declarative model:

- **Connection-identity reader resolution** (:func:`resolve_readers`) —
  neighbor readers are constructed from CONNECTION identities
  (provider family + base URL + numeric connection id + numeric
  repository id), never from filenames or agent-supplied URLs. Two
  same-named repositories on different hosts/connections stay DISTINCT
  bindings (the review's decoy arm: a colliding display name cannot be
  substituted by an ID collision), and a REQUIRED neighbor nothing can
  resolve becomes a typed refusal rendered as a BLOCKING QUESTION in
  the planning input — never a confidently complete plan over an
  unreadable source.
- **Authorized-set enforcement at the READ boundary**
  (:class:`AuthorizedReadSurface`) — a repository outside the approved
  set contributes NO source content even when a model or tool requests
  it: the guarded reader surface raises the typed
  :class:`DiscoveryAuthorizationRefusal` BEFORE any underlying read
  runs, and the authorized set is recorded under
  ``discovery.authorized_repo_set_digest``.
- **Neighbor-write refusal** (:func:`review_write_proposals`) — a
  proposal that writes to a neighbor is refused by the profile's
  ``authorize_write`` verdict and SURFACED as a material
  ``write_scope.expansion_request``; the publication boundary keeps
  naming ONLY the approved target (a refusal is never a silent widening).
- **Truncation visibility** (:class:`BoundedNeighborReader`,
  :func:`truncation_document`) — per-repository read limits produce an
  explicit ``discovery.truncation`` marker in the discovery evidence and
  a bounded planning-input section naming WHICH repos/windows were cut,
  reusing the existing budget machinery rather than rebuilding it.
- **Snapshot invalidation** (:func:`evaluate_snapshot_invalidation`) —
  changing a referenced neighbor snapshot (a different OID) invalidates
  the relevant discovery/plan evidence through an EXPLICIT typed
  decision over the neighbor set digest, in the
  ``identity_changed`` style of :mod:`forge.adaptive.workpackage`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from forge.adaptive.system_context import (
    NEIGHBOR_PROVIDER_VALUES,
    OWN_REPO_KEY,
    SystemContextProfile,
    WriteDecision,
)

__all__ = [
    "AUTHORIZED_SET_SCHEMA",
    "AUTHORITY_BEGIN",
    "AUTHORITY_END",
    "AuthorityQuestion",
    "AuthorizedReadSurface",
    "AuthorizedRepoSet",
    "BoundedNeighborReader",
    "CatalogRepository",
    "ConnectionIdentity",
    "DEFAULT_MAX_BYTES_PER_READ",
    "DiscoveryAuthorizationRefusal",
    "EXPANSION_SCHEMA",
    "ReaderBinding",
    "ReaderRefusal",
    "ReaderResolution",
    "READER_AMBIGUOUS",
    "READER_UNAVAILABLE",
    "READERS_SCHEMA",
    "SnapshotInvalidationDecision",
    "TRUNCATION_SCHEMA",
    "TRUNCATION_SECTION_MAX_CHARS",
    "TruncationEvent",
    "WriteScopeExpansionRequest",
    "WriteScopeReview",
    "attach_authority_section",
    "attach_truncation_section",
    "authorized_repo_set_digest",
    "evaluate_snapshot_invalidation",
    "neighbor_entries_of_profile",
    "neighbor_entries_of_record",
    "neighbor_identity_changed",
    "neighbor_set_digest",
    "render_truncation_section",
    "resolve_readers",
    "review_write_proposals",
    "truncation_document",
]


#: The schema stamp of the recorded authorized repo set (the identity a
#: completed run's ``discovery.authorized_repo_set_digest`` binds to).
AUTHORIZED_SET_SCHEMA = "forge.discovery.authorized_set/1"

#: The schema stamp of a reader-resolution blocking-question section.
READERS_SCHEMA = "forge.discovery.readers/1"

#: The schema stamp of the recorded truncation markers.
TRUNCATION_SCHEMA = "forge.discovery.truncation/1"

#: The schema stamp of a write-scope review document.
EXPANSION_SCHEMA = "forge.write_scope.expansion/1"

#: Delimiters around the authority section — the same survival property
#: as the evidence/answers/research sections: a delimited block passes
#: prompt assembly unchanged and can be inspected without parsing the
#: whole prompt.
AUTHORITY_BEGIN = "<<<FORGE_DISCOVERY_AUTHORITY"
AUTHORITY_END = "FORGE_DISCOVERY_AUTHORITY>>>"

#: The refusal codes a reader resolution may carry.
READER_UNAVAILABLE = "unavailable"
READER_AMBIGUOUS = "ambiguous"

#: Default per-read byte budget of a :class:`BoundedNeighborReader` (the
#: same order as the discovery snapshot's per-file expectations — small
#: enough that a runaway blob is cut, loud enough that the cut is marked).
DEFAULT_MAX_BYTES_PER_READ = 64 * 1024

#: Bound of the rendered truncation section (the planning-input section
#: names truncated repos/windows; it never replaces the evidence itself).
TRUNCATION_SECTION_MAX_CHARS = 2000


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Connection identity — what a reader is ACTUALLY built from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionIdentity:
    """One forge connection: provider family + base URL + numeric id.

    This is the ONLY currency reader construction accepts. A repository
    is addressed by which CONNECTION exposes it and its numeric id on
    that connection — never by a display name, a filename or an
    agent-supplied URL, all of which collide across hosts.
    """

    provider: str
    base_url: str
    connection_id: int

    def __post_init__(self) -> None:
        if self.provider not in NEIGHBOR_PROVIDER_VALUES:
            raise ValueError(
                f"connection provider {self.provider!r} is outside the vocabulary"
                f" {NEIGHBOR_PROVIDER_VALUES}"
            )
        url = str(self.base_url or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"connection base_url {self.base_url!r} must be an http(s) URL")
        object.__setattr__(self, "base_url", url)
        if int(self.connection_id) < 1:
            raise ValueError("connection_id must be a positive numeric identity")

    @property
    def key(self) -> str:
        """The unambiguous connection identity ``provider:base_url#id``."""
        return f"{self.provider}:{self.base_url}#{self.connection_id}"


@dataclass(frozen=True)
class CatalogRepository:
    """One repository a connection exposes, as the catalog records it.

    ``display_name`` is a HUMAN label — deliberately decoy-prone and
    deliberately NOT identity. The identity is
    :attr:`ConnectionIdentity.key` plus the repository's ``numeric_id``
    ON that connection: two same-named repositories on different
    connections (or equal numeric ids on different connections) are two
    different repositories, and neither can be substituted for the other.
    """

    connection: ConnectionIdentity
    numeric_id: int
    display_name: str
    default_ref: str = "HEAD"

    def __post_init__(self) -> None:
        if int(self.numeric_id) < 1:
            raise ValueError("catalog repository numeric_id must be positive")
        if not str(self.display_name or "").strip():
            raise ValueError("catalog repository display_name must be non-empty")

    @property
    def provider(self) -> str:
        return self.connection.provider

    @property
    def identity(self) -> str:
        """The full composite identity: ``connection!numeric_id``."""
        return f"{self.connection.key}!{self.numeric_id}"


# ---------------------------------------------------------------------------
# Reader resolution — connection identities, typed refusals, blocking questions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReaderRefusal:
    """Why one REQUIRED authorized neighbor got no reader.

    A typed answer, never an exception and never a silent narrowing:
    ``code`` is ``unavailable`` (no connection of the declared provider
    family exposes the repository) or ``ambiguous`` (several do — the
    colliding-display-name decoy). Either way the run cannot honestly
    claim it read that repository.
    """

    neighbor_key: str
    code: str
    detail: str

    def as_blocking_question(self) -> str:
        """The operator-facing question the refusal becomes.

        R36-11: a required reader that cannot be resolved is a BLOCKING
        QUESTION carried into the planning input — the plan may not
        present itself as complete over a source nothing read.
        """
        return (
            f"[{self.neighbor_key}] {self.code}: {self.detail} — the system"
            " context authorizes this neighbor but no reader can be built"
            " for it; resolve the connection (or remove the neighbor from"
            " forge.yml) before planning claims it was read"
        )


@dataclass(frozen=True)
class ReaderBinding:
    """One resolved neighbor: its catalog identity plus its reader."""

    neighbor_key: str
    repository: CatalogRepository
    reader: Any

    @property
    def identity(self) -> str:
        """The composite connection identity the reader was built from."""
        return self.repository.identity

    @property
    def connection_key(self) -> str:
        return self.repository.connection.key


@dataclass(frozen=True)
class AuthorityQuestion:
    """A blocking question rendered into the planning input."""

    text: str
    source: str

    def as_document(self) -> dict[str, str]:
        return {"text": self.text, "source": self.source}


@dataclass(frozen=True)
class ReaderResolution:
    """The verdict of one :func:`resolve_readers` call.

    ``ok`` (no refusals) means every authorized neighbor has a reader
    built from a connection identity, and :attr:`readers` is the map
    :meth:`SystemContextProfile.build_discovery_context` expects. Any
    refusal means the resolution is INCOMPLETE: the refused neighbors
    have no readers, the planning input must carry the blocking
    questions, and nothing downstream may treat the authorized set as
    read.
    """

    profile: SystemContextProfile
    bindings: tuple[ReaderBinding, ...] = ()
    refusals: tuple[ReaderRefusal, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def readers(self) -> dict[str, Any]:
        """``neighbor key -> reader`` — exactly the resolved neighbors."""
        return {binding.neighbor_key: binding.reader for binding in self.bindings}

    def blocking_questions(self) -> tuple[AuthorityQuestion, ...]:
        return tuple(
            AuthorityQuestion(
                text=refusal.as_blocking_question(),
                source=f"reader_resolution:{refusal.neighbor_key}",
            )
            for refusal in self.refusals
        )

    def as_document(self) -> dict[str, Any]:
        """The honest resolution record — ``complete`` is False while any
        authorized neighbor stays unread, and the blocking questions ride
        beside it (the ``plan.blocking_questions`` observability key)."""
        questions = self.blocking_questions()
        return {
            "schema": READERS_SCHEMA,
            "complete": not questions,
            "plan.blocking_questions": [question.as_document() for question in questions],
            "resolved": [
                {
                    "neighbor_key": binding.neighbor_key,
                    "identity": binding.identity,
                    "connection": binding.connection_key,
                }
                for binding in self.bindings
            ],
        }

    def planning_input_section(self) -> str:
        """The delimited planning-input section ("" when ok).

        Carries the blocking questions so the planner (and the operator
        reviewing the input) sees that a required source was NOT read —
        the section survives prompt assembly like every other delimited
        forge section.
        """
        questions = self.blocking_questions()
        if not questions:
            return ""
        doc = self.as_document()
        body = _canonical(
            {
                "schema": doc["schema"],
                "complete": doc["complete"],
                "plan.blocking_questions": doc["plan.blocking_questions"],
            }
        )
        return f"{AUTHORITY_BEGIN}\n{body}\n{AUTHORITY_END}"


def resolve_readers(
    profile: SystemContextProfile,
    *,
    repositories: Iterable[CatalogRepository],
    reader_factory: Callable[[CatalogRepository], Any],
) -> ReaderResolution:
    """Resolve every authorized neighbor's reader from CONNECTION identities.

    Matching is by provider family and the neighbor's declared
    ``repository_id`` against the catalog's display names — but the
    READER is built from what matched: the catalog repository's
    connection identity (provider + base URL + numeric connection id +
    numeric repository id), handed to *reader_factory* verbatim. The
    factory never sees a filename or an agent-supplied URL.

    Refusal rules (both typed, both blocking):

    - **unavailable** — no connection of the neighbor's provider family
      exposes the repository: an authorized neighbor nothing can read.
    - **ambiguous** — SEVERAL distinct catalog identities match the name
      (the colliding-display-name decoy): forge refuses to pick one,
      because a same-named repository on another host/connection must
      not be substituted by an ID collision.
    """
    catalog = tuple(repositories)
    bindings: list[ReaderBinding] = []
    refusals: list[ReaderRefusal] = []
    for neighbor in profile.neighbors:
        matches = [
            repo
            for repo in catalog
            if repo.provider == neighbor.provider and repo.display_name == neighbor.repository_id
        ]
        identities = {repo.identity for repo in matches}
        if not matches:
            refusals.append(
                ReaderRefusal(
                    neighbor_key=neighbor.key,
                    code=READER_UNAVAILABLE,
                    detail=(
                        f"no {neighbor.provider} connection exposes a repository"
                        f" named {neighbor.repository_id!r}"
                    ),
                )
            )
            continue
        if len(identities) > 1:
            connections = ", ".join(sorted({repo.connection.key for repo in matches}))
            refusals.append(
                ReaderRefusal(
                    neighbor_key=neighbor.key,
                    code=READER_AMBIGUOUS,
                    detail=(
                        f"{len(identities)} same-named repositories across"
                        f" connections [{connections}] — pin one connection"
                        " in the catalog or disambiguate the neighbor"
                    ),
                )
            )
            continue
        repo = matches[0]
        bindings.append(
            ReaderBinding(
                neighbor_key=neighbor.key,
                repository=repo,
                reader=reader_factory(repo),
            )
        )
    return ReaderResolution(
        profile=profile,
        bindings=tuple(bindings),
        refusals=tuple(refusals),
    )


def _attach_section(planner_input: str, section: str, *, label: str, cap: int) -> str:
    """Return the input with *section* appended, within *cap*.

    The same bargain as the digest/research attach: the combined prompt
    stays within the planner's input cap by cutting the input's HEAD —
    the delimited sections at the end survive — never the section
    itself.
    """
    if not section:
        return planner_input
    if len(planner_input) + 2 + len(section) <= cap:
        return f"{planner_input}\n\n{section}"
    kept = planner_input[: max(0, cap - len(section) - 2 - 96)]
    while kept and len(kept) + 2 + len(section) + 96 > cap:
        kept = kept[:-1]
    marker = (
        f"(input truncated to {len(kept)} chars to fit the {label}"
        f" within the planner input cap)\n\n"
    )
    return f"{kept}\n\n{marker}{section}"


def attach_authority_section(planner_input: str, section: str, *, cap: int = 12000) -> str:
    """Attach the authority (blocking-question) section to the input."""
    return _attach_section(planner_input, section, label="discovery authority section", cap=cap)


def attach_truncation_section(planner_input: str, section: str, *, cap: int = 12000) -> str:
    """Attach the truncation-visibility section to the input."""
    return _attach_section(planner_input, section, label="discovery truncation section", cap=cap)


# ---------------------------------------------------------------------------
# The authorized set — the read boundary's frozen vocabulary
# ---------------------------------------------------------------------------


class DiscoveryAuthorizationRefusal(Exception):
    """The TYPED read refusal: a repository outside the approved set.

    Raised BEFORE any underlying read runs, so an unauthorized repository
    contributes no content — not its tree, not a file, not a byte — even
    when a model or tool requests it. Carries the requested key and the
    authorized set so a tool boundary can render it without parsing the
    message.
    """

    code = "outside_authorized_set"

    def __init__(self, requested: str, authorized: Sequence[str]) -> None:
        self.requested = requested
        self.authorized = tuple(authorized)
        super().__init__(
            f"read of {requested!r} refused — {self.code}: the authorized"
            f" discovery set is {list(self.authorized)}; the repository"
            " contributes no content"
        )


def authorized_repo_set_digest(profile: SystemContextProfile) -> str:
    """The authorization digest over the FROZEN repo identities + OIDs.

    Distinct from the discovery stage's content digests (which hash
    bytes): this one freezes WHAT WAS AUTHORIZED before anything was
    read — the own repository's identity, ref and path scope plus every
    neighbor's identity, pinned ref and globs. A neighbor re-pinned to a
    different OID, a renamed repository or a changed path scope moves it;
    identical authorizations hash equal regardless of read order.
    """
    payload = {
        "schema": AUTHORIZED_SET_SCHEMA,
        "repos": {
            OWN_REPO_KEY: {
                "repository_id": profile.writable.repository_id,
                "source_oid": profile.writable.ref,
                "allowed_globs": list(profile.writable.allowed_globs),
                "writable": True,
            },
            **{
                neighbor.key: {
                    "repository_id": neighbor.repository_id,
                    "source_oid": neighbor.ref,
                    "allowed_globs": list(neighbor.allowed_globs),
                    "writable": False,
                }
                for neighbor in profile.neighbors
            },
        },
    }
    return _sha256(_canonical(payload))


@dataclass(frozen=True)
class AuthorizedRepoSet:
    """The frozen approved read set, with its digest and read gate.

    ``read_keys`` is the ENTIRE vocabulary a read may name: the own
    repository (``own``) plus every neighbor under its composite
    ``provider:repository_id`` key. :meth:`authorize_read` is the gate
    every read passes through — anything else raises the typed
    :class:`DiscoveryAuthorizationRefusal`.
    """

    profile: SystemContextProfile

    @property
    def digest(self) -> str:
        return authorized_repo_set_digest(self.profile)

    @property
    def read_keys(self) -> tuple[str, ...]:
        return (OWN_REPO_KEY,) + tuple(neighbor.key for neighbor in self.profile.neighbors)

    def authorize_read(self, repo_key: str) -> str:
        """The gate: returns *repo_key* when authorized, raises otherwise."""
        if repo_key not in self.read_keys:
            raise DiscoveryAuthorizationRefusal(repo_key, self.read_keys)
        return repo_key

    def as_document(self) -> dict[str, Any]:
        """The recorded authorization — the observability surface's anchor.

        Carries ``discovery.authorized_repo_set_digest`` (R36-11) beside
        the per-repo identities it was computed over, so a consumer can
        re-derive the digest from the document and refuse a mismatched
        one.
        """
        writable = self.profile.writable
        repos = [
            {
                "key": OWN_REPO_KEY,
                "provider": writable.provider,
                "repository_id": writable.repository_id,
                "source_oid": writable.ref,
                "allowed_globs": list(writable.allowed_globs),
                "writable": True,
            }
        ]
        repos.extend(
            {
                "key": neighbor.key,
                "provider": neighbor.provider,
                "repository_id": neighbor.repository_id,
                "source_oid": neighbor.ref,
                "allowed_globs": list(neighbor.allowed_globs),
                "writable": False,
            }
            for neighbor in self.profile.neighbors
        )
        return {
            "schema": AUTHORIZED_SET_SCHEMA,
            "discovery.authorized_repo_set_digest": self.digest,
            "write_target": writable.key,
            "read_keys": list(self.read_keys),
            "repos": repos,
        }


def neighbor_of(profile: SystemContextProfile, key: str) -> Any:
    """The neighbor declared under *key* (None when there is none)."""
    return next((neighbor for neighbor in profile.neighbors if neighbor.key == key), None)


class AuthorizedReadSurface:
    """The guarded multi-repo reader surface tools and models read through.

    Wraps one reader per authorized key (the own repository plus every
    neighbor) and authorizes EVERY read before it runs: a request for a
    repository outside the approved set raises the typed
    :class:`DiscoveryAuthorizationRefusal` and the underlying reader is
    never touched — zero tree listings, zero file bytes. The surface
    keeps an audit of authorized requests and typed refusals so a
    qualification run can prove the negative ("the unauthorized repo
    received no requests").

    Construction mirrors :meth:`SystemContextProfile.build_discovery_context`:
    the reader map must cover EXACTLY the authorized keys — a missing
    reader refuses (a half-built surface), and an EXTRA reader refuses
    (an unauthorized repository is absent by construction, never merely
    unused).
    """

    def __init__(self, profile: SystemContextProfile, readers: Mapping[str, Any]) -> None:
        authorized = AuthorizedRepoSet(profile)
        expected = set(authorized.read_keys)
        supplied = set(readers)
        missing = sorted(expected - supplied)
        if missing:
            raise ValueError(
                f"no reader for authorized repository key(s) {missing} — the"
                " read surface covers exactly the authorized set"
            )
        extra = sorted(supplied - expected)
        if extra:
            raise ValueError(
                f"reader(s) for UNAUTHORIZED repository key(s) {extra} — an"
                " unauthorized repository is absent from the read surface by"
                " construction"
            )
        self.profile = profile
        self.authorized = authorized
        self._readers = dict(readers)
        self.requests: list[str] = []
        self.refusals: list[DiscoveryAuthorizationRefusal] = []

    def authorize_read(self, repo_key: str) -> str:
        """The read gate (records the refusal; raises the typed answer)."""
        try:
            return self.authorized.authorize_read(repo_key)
        except DiscoveryAuthorizationRefusal as refusal:
            self.refusals.append(refusal)
            raise
        finally:
            if repo_key in self.authorized.read_keys:
                self.requests.append(repo_key)

    async def get_tree(
        self,
        repo_key: str,
        project_id: int,
        path: str = "",
        ref: str = "HEAD",
        recursive: bool = False,
    ) -> Any:
        """The guarded tree listing (the reader duck-type, keyed by repo)."""
        self.authorize_read(repo_key)
        return await self._readers[repo_key].get_tree(project_id, path, ref, recursive)

    async def read_text(self, repo_key: str, file_path: str, ref: str = "HEAD") -> str:
        """The guarded file read — zero content for an unauthorized repo."""
        self.authorize_read(repo_key)
        return await self._readers[repo_key].read_text(file_path, ref)


# ---------------------------------------------------------------------------
# Neighbor-write refusal — surfaced expansion requests, never silent widening
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteScopeExpansionRequest:
    """One material scope-change proposal, surfaced — not applied.

    The proposal wanted to write to a READ-ONLY neighbor; the boundary
    refused it (:meth:`SystemContextProfile.authorize_write`) and
    records the refusal HERE, as an explicit expansion request naming
    the requested target and the single approved one. The publication
    boundary is never widened by the request's existence.
    """

    requested: str
    code: str
    reason: str
    approved_target: str

    def as_document(self) -> dict[str, str]:
        return {
            "requested": self.requested,
            "code": self.code,
            "reason": self.reason,
            "approved_target": self.approved_target,
            "decision": "refused_pending_explicit_authorization",
        }


@dataclass(frozen=True)
class WriteScopeReview:
    """The verdict of one batch of write proposals against the profile.

    ``publication_targets`` is ALWAYS exactly the writable target — a
    refused neighbor write is surfaced as an expansion request, never
    absorbed as a second writer.
    """

    profile: SystemContextProfile
    proposals: tuple[tuple[str, str], ...] = ()
    verdicts: tuple[WriteDecision, ...] = ()
    expansion_requests: tuple[WriteScopeExpansionRequest, ...] = ()

    @property
    def publication_targets(self) -> tuple[str, ...]:
        """The ONLY repository names publication may target."""
        return (self.profile.writable.key,)

    @property
    def widened(self) -> bool:
        return False

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": EXPANSION_SCHEMA,
            "write_scope.target": self.profile.writable.key,
            "publication_targets": list(self.publication_targets),
            "widened": self.widened,
            "write_scope.expansion_requests": [
                request.as_document() for request in self.expansion_requests
            ],
            "verdicts": [
                {
                    "requested": f"{provider}:{repository_id}",
                    "allowed": verdict.allowed,
                    "code": verdict.code,
                }
                for (provider, repository_id), verdict in zip(
                    self.proposals, self.verdicts, strict=True
                )
            ],
        }


def review_write_proposals(
    profile: SystemContextProfile, proposals: Iterable[tuple[str, str]]
) -> WriteScopeReview:
    """Review ``(provider, repository_id)`` write proposals against the profile.

    Each proposal gets the profile's ``authorize_write`` verdict. A
    proposal targeting a NEIGHBOR is additionally recorded as a material
    :class:`WriteScopeExpansionRequest` (``write_scope.expansion_requests``)
    — a scope change an operator must approve explicitly, never an
    implicit second writer. Proposals outside the context are refused
    plainly (they are not expansions of THIS context's scope).
    """
    reviewed = tuple(proposals)
    verdicts: list[WriteDecision] = []
    expansions: list[WriteScopeExpansionRequest] = []
    for provider, repository_id in reviewed:
        decision = profile.authorize_write(provider, repository_id)
        verdicts.append(decision)
        if decision.code == "read_only_neighbor":
            expansions.append(
                WriteScopeExpansionRequest(
                    requested=f"{provider}:{repository_id}",
                    code=decision.code,
                    reason=decision.reason,
                    approved_target=profile.writable.key,
                )
            )
    return WriteScopeReview(
        profile=profile,
        proposals=reviewed,
        verdicts=tuple(verdicts),
        expansion_requests=tuple(expansions),
    )


# ---------------------------------------------------------------------------
# Truncation visibility — explicit markers for bounded neighbor reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TruncationEvent:
    """One explicit cut a per-repository read limit made."""

    repo_key: str
    path: str
    window: str
    limit: int
    reason: str

    def as_document(self) -> dict[str, Any]:
        return {
            "repo_key": self.repo_key,
            "path": self.path,
            "window": self.window,
            "limit": self.limit,
            "reason": self.reason,
        }


class BoundedNeighborReader:
    """One neighbor's reader behind an explicit per-read byte limit.

    Wraps the same duck-typed surface discovery reads through
    (``get_tree`` + ``read_text``), so it composes with
    :meth:`SystemContextProfile.build_discovery_context` unchanged: the
    stage reads through the wrapper, and a read that exceeds the limit
    comes back cut WITH a recorded :class:`TruncationEvent` — the
    existing budget machinery does the cutting implicitly, this surface
    makes it VISIBLE (``discovery.truncation``).
    """

    def __init__(
        self,
        reader: Any,
        *,
        repo_key: str,
        max_bytes_per_read: int = DEFAULT_MAX_BYTES_PER_READ,
    ) -> None:
        if not hasattr(reader, "read_text"):
            raise ValueError(
                "a bounded neighbor reader wraps a reader with read_text"
                " (the discovery snapshot surface)"
            )
        if int(max_bytes_per_read) < 1:
            raise ValueError("max_bytes_per_read must be >= 1")
        self._reader = reader
        self._repo_key = repo_key
        self._max_bytes_per_read = int(max_bytes_per_read)
        self.truncation_events: list[TruncationEvent] = []

    @property
    def repo_key(self) -> str:
        return self._repo_key

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> Any:
        return await self._reader.get_tree(project_id, path, ref, recursive)

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        text = await self._reader.read_text(file_path, ref)
        if len(text) <= self._max_bytes_per_read:
            return text
        limit = self._max_bytes_per_read
        self.truncation_events.append(
            TruncationEvent(
                repo_key=self._repo_key,
                path=file_path,
                window=f"bytes 0..{limit} of {len(text)}",
                limit=limit,
                reason="max_bytes_per_read",
            )
        )
        return text[:limit]


def truncation_document(readers: Iterable[BoundedNeighborReader]) -> dict[str, Any]:
    """The ``discovery.truncation`` evidence over a run's bounded readers.

    Empty (``any: false``) when nothing was cut — the marker is explicit
    in BOTH directions, so a consumer can distinguish "fully read" from
    "cut but unmarked".
    """
    events = [event for reader in readers for event in reader.truncation_events]
    return {
        "schema": TRUNCATION_SCHEMA,
        "discovery.truncation": {
            "any": bool(events),
            "repos": sorted({event.repo_key for event in events}),
            "events": [event.as_document() for event in events],
        },
    }


def render_truncation_section(
    document: Mapping[str, Any], *, max_chars: int = TRUNCATION_SECTION_MAX_CHARS
) -> str:
    """The bounded planning-input section naming truncated repos/windows.

    "" when nothing was truncated; otherwise one line per event naming
    the repository, path, cut window and reason, bounded to *max_chars*
    with an explicit "and N more" tail — the planning input SHOWS which
    repos/windows were partial, it never re-broadcasts their content.
    """
    entry = document.get("discovery.truncation")
    if not isinstance(entry, Mapping) or not bool(entry.get("any")):
        return ""
    lines = [
        f"{event.get('repo_key')}:{event.get('path')} — {event.get('window')}"
        f" (limit {event.get('limit')}, {event.get('reason')})"
        for event in entry.get("events") or []
        if isinstance(event, Mapping)
    ]
    if not lines:
        return ""
    header = "NEIGHBOR READS TRUNCATED (the listed windows are PARTIAL):"
    section = header
    kept = 0
    for line in lines:
        candidate = f"{section}\n{line}"
        if len(candidate) > max_chars:
            break
        section = candidate
        kept += 1
    if kept < len(lines):
        section += f"\n…and {len(lines) - kept} more truncated window(s)"
    return section


# ---------------------------------------------------------------------------
# Snapshot invalidation — explicit decisions over the neighbor set digest
# ---------------------------------------------------------------------------


def neighbor_entries_of_profile(
    profile: SystemContextProfile,
) -> dict[str, dict[str, str]]:
    """``neighbor key -> {repository_id, source_oid}`` off the profile."""
    return {
        neighbor.key: {"repository_id": neighbor.repository_id, "source_oid": neighbor.ref}
        for neighbor in profile.neighbors
    }


def neighbor_entries_of_record(record: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """The neighbor OIDs a REAL discovery record bound to.

    Reads the multi-repo dispatch's per-repository identities (the
    ``snapshot_tree``/``dispatch.repositories`` shape
    :func:`~forge.adaptive.discovery_stage.run_discovery_stage` records),
    excluding the run's own repository — the neighbor set is what
    invalidation is about.
    """
    dispatch = record.get("dispatch") if isinstance(record, Mapping) else None
    repos = dispatch.get("repositories") if isinstance(dispatch, Mapping) else None
    if not isinstance(repos, Mapping):
        return {}
    return {
        str(key): {
            "repository_id": str((entry or {}).get("repository_id") or ""),
            "source_oid": str((entry or {}).get("source_oid") or ""),
        }
        for key, entry in repos.items()
        if str(key) != OWN_REPO_KEY and isinstance(entry, Mapping)
    }


def neighbor_set_digest(entries: Mapping[str, Mapping[str, str]]) -> str:
    """sha256 over the canonical neighbor identity set.

    Each entry carries ``repository_id`` and ``source_oid`` (the pinned
    immutable OID), so the digest moves when a referenced neighbor
    snapshot changes to a different OID — and when the set itself
    grows or shrinks. Equal sets hash equal regardless of order.
    """
    return _sha256(
        _canonical(
            {
                str(key): {
                    "repository_id": str((entry or {}).get("repository_id") or ""),
                    "source_oid": str((entry or {}).get("source_oid") or ""),
                }
                for key, entry in entries.items()
            }
        )
    )


@dataclass(frozen=True)
class SnapshotInvalidationDecision:
    """The EXPLICIT decision a moved neighbor snapshot forces (R36-11).

    ``code`` is ``current`` (the referenced neighbor OIDs are unchanged;
    the recorded discovery/plan evidence stays applicable) or
    ``identity_changed`` (at least one neighbor moved — the evidence
    bound to the recorded digest is invalidated and must be re-derived
    through a NEW explicit run, not silently reused). ``changed`` names
    the neighbor keys that moved, were added or were removed.
    """

    code: str
    recorded_digest: str
    current_digest: str
    changed: tuple[str, ...] = ()
    reason: str = ""

    @property
    def invalidated(self) -> bool:
        return self.code == "identity_changed"

    def as_document(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "invalidated": self.invalidated,
            "recorded_digest": self.recorded_digest,
            "current_digest": self.current_digest,
            "changed": list(self.changed),
            "reason": self.reason,
        }


def evaluate_snapshot_invalidation(
    recorded: Mapping[str, Mapping[str, str]],
    current: Mapping[str, Mapping[str, str]],
) -> SnapshotInvalidationDecision:
    """Compare the recorded neighbor set against the current one.

    The ``identity_changed`` style of
    :func:`forge.adaptive.workpackage.identity_changed`, applied to the
    neighbor snapshot set: same function of the digests, plus the
    per-neighbor attribution an operator needs to act on.
    """
    recorded_digest = neighbor_set_digest(recorded)
    current_digest = neighbor_set_digest(current)
    if recorded_digest == current_digest:
        return SnapshotInvalidationDecision(
            code="current",
            recorded_digest=recorded_digest,
            current_digest=current_digest,
            changed=(),
            reason="the referenced neighbor snapshots are unchanged — recorded evidence stays applicable",
        )
    changed: set[str] = set()
    for key in set(recorded) | set(current):
        was = dict(recorded.get(key) or {})
        now = dict(current.get(key) or {})
        if was != now:
            changed.add(str(key))
    listed = ", ".join(sorted(changed)) or "(set reshaped)"
    return SnapshotInvalidationDecision(
        code="identity_changed",
        recorded_digest=recorded_digest,
        current_digest=current_digest,
        changed=tuple(sorted(changed)),
        reason=(
            f"referenced neighbor snapshot(s) moved: {listed} — evidence bound"
            f" to {recorded_digest[:12]}… is invalidated; re-derive it through"
            " an explicit new discovery run"
        ),
    )


def neighbor_identity_changed(
    recorded: Mapping[str, Mapping[str, str]],
    current: Mapping[str, Mapping[str, str]],
) -> bool:
    """The thin boolean over :func:`evaluate_snapshot_invalidation`."""
    return evaluate_snapshot_invalidation(recorded, current).invalidated
