"""R36-11 — the live discovery AUTHORITY surface, checked.

- CONNECTION-IDENTITY reader resolution: two same-named repositories on
  different hosts/connections resolve to DISTINCT bindings (no ID
  collision), a name matching several connections refuses as AMBIGUOUS
  (the decoy arm), and an unavailable required neighbor becomes a typed
  refusal carried into the planning input as a BLOCKING QUESTION —
  never a confidently complete plan;
- the READ BOUNDARY: a repository outside the approved set contributes
  NO content — the guarded read surface raises the typed
  ``DiscoveryAuthorizationRefusal`` before any underlying read runs,
  and the authorized set is recorded under
  ``discovery.authorized_repo_set_digest``;
- the WRITE BOUNDARY: a proposal that writes to a neighbor is refused
  and SURFACED as a ``write_scope.expansion_request`` while the
  publication boundary keeps naming ONLY the approved target;
- TRUNCATION VISIBILITY: per-repository read limits leave an explicit
  ``discovery.truncation`` marker and a planning-input section naming
  the cut repos/windows — including through a REAL discovery stage run;
- SNAPSHOT INVALIDATION: a referenced neighbor snapshot moving to a
  different OID invalidates the recorded evidence through an EXPLICIT
  typed decision over the neighbor set digest;
- the CAPTURED RECORDING: a real ToolObservation-backed offline capture
  over SnapshotToolbox fixtures (the decisive dependency lives ONLY in
  the neighbor, at a non-initial window) round-trips through the cohort
  loader and re-captures byte-identically.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.adaptive.discovery_authority import (
    AUTHORITY_BEGIN,
    AUTHORITY_END,
    AuthorizedReadSurface,
    AuthorizedRepoSet,
    BoundedNeighborReader,
    CatalogRepository,
    ConnectionIdentity,
    DiscoveryAuthorizationRefusal,
    TruncationEvent,
    attach_authority_section,
    attach_truncation_section,
    authorized_repo_set_digest,
    evaluate_snapshot_invalidation,
    neighbor_entries_of_profile,
    neighbor_entries_of_record,
    neighbor_identity_changed,
    render_truncation_section,
    resolve_readers,
    review_write_proposals,
    truncation_document,
)
from forge.adaptive.discovery_stage import run_discovery_stage
from forge.adaptive.discovery_tools import SnapshotToolbox
from forge.adaptive.research_cohort import (
    DEFAULT_RUBRICS,
    MODE_RESEARCH,
    RECORD_SCHEMA,
    CohortTask,
    RecordedRun,
    Snapshot,
    SurfaceRef,
    grade_run,
)
from forge.adaptive.research_planner import (
    ResearchHarness,
    ResearchRepo,
    ToolObservation,
    _execute_call,
    run_research_pass,
)
from forge.adaptive.system_context import (
    NeighborRepository,
    SystemContextProfile,
    WritableTarget,
)
from forge.durable import FlowRun
from forge.models.base import Base

OWN = WritableTarget(provider="github", repository_id="example/repo", ref="f" * 40)

GITLAB_CONN = ConnectionIdentity(
    provider="gitlab", base_url="https://git.acme.io/", connection_id=7
)
GITHUB_CONN = ConnectionIdentity(
    provider="github", base_url="https://github.example.com", connection_id=3
)
GITLAB_MIRROR = ConnectionIdentity(
    provider="gitlab", base_url="https://git.mirror.example.net", connection_id=11
)

COHORT_DIR = Path(__file__).resolve().parents[1] / "evaluation" / "research_cohort"
SNAPSHOTS_DIR = COHORT_DIR / "snapshots"
RECORDED_DIR = COHORT_DIR / "recorded"

CAPTURE_TASK_ID = "RC-08-offline-neighbor-deadline-policy"
CAPTURE_SNAPSHOT_PATH = SNAPSHOTS_DIR / "snap-offline-neighbor.json"
CAPTURE_RECORD_PATH = RECORDED_DIR / CAPTURE_TASK_ID / "research.json"

CAPTURE_PLANNER_INPUT = (
    "Orders' dispatch must enforce the courier deadline window Billing owns. "
    "The deadline policy is defined in the neighbor repository only."
)


class CountingReader:
    """The reader duck-type that COUNTS every call — the "zero content"
    assertion for refused reads needs to see whether the underlying
    surface was ever touched."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.calls: list[tuple[str, str]] = []

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> list:
        self.calls.append(("get_tree", path))
        return [SimpleNamespace(path=p, type="blob") for p in sorted(self._files)]

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        self.calls.append(("read_text", file_path))
        return self._files[file_path]


def _two_neighbor_profile() -> SystemContextProfile:
    """Two neighbors with DELIBERATELY colliding display names on different
    provider families — the review's decoy arm."""
    return SystemContextProfile(
        writable=OWN,
        neighbors=(
            NeighborRepository(provider="gitlab", repository_id="acme/api", ref="a" * 40),
            NeighborRepository(provider="github", repository_id="acme/api", ref="b" * 40),
        ),
    )


# ---------------------------------------------------------------------------
# Connection-identity reader resolution
# ---------------------------------------------------------------------------


class TestResolveReaders:
    def test_colliding_display_names_across_connections_stay_distinct(self):
        """Both connections expose a repository NAMED 'acme/api' with the
        SAME numeric id — the bindings stay distinct because identity is
        the CONNECTION (provider + base URL + connection id) plus the
        numeric id, never the display name alone."""
        catalog = [
            CatalogRepository(connection=GITLAB_CONN, numeric_id=101, display_name="acme/api"),
            CatalogRepository(connection=GITHUB_CONN, numeric_id=101, display_name="acme/api"),
        ]
        built: list[CatalogRepository] = []

        def factory(repository: CatalogRepository) -> object:
            built.append(repository)
            return object()

        resolution = resolve_readers(
            _two_neighbor_profile(), repositories=catalog, reader_factory=factory
        )

        assert resolution.ok
        assert sorted(resolution.readers) == ["github:acme/api", "gitlab:acme/api"]
        identities = {binding.identity for binding in resolution.bindings}
        assert identities == {
            "gitlab:https://git.acme.io#7!101",
            "github:https://github.example.com#3!101",
        }
        # the reader factory saw CONNECTION identities, never a URL string
        # or a filename: each argument is the catalog record itself.
        assert built == catalog

    def test_equal_numeric_ids_on_different_connections_never_collide(self):
        left = CatalogRepository(connection=GITLAB_CONN, numeric_id=101, display_name="acme/api")
        right = CatalogRepository(connection=GITLAB_MIRROR, numeric_id=101, display_name="acme/api")
        assert left.identity != right.identity
        assert left.connection.key != right.connection.key

    def test_a_name_matching_several_connections_refuses_as_ambiguous(self):
        """The decoy arm: a mirrored host holds a repository with the SAME
        name (and numeric id). Forge refuses to pick — a same-named
        repository on another connection must not be substituted by an ID
        collision."""
        profile = SystemContextProfile(
            writable=OWN,
            neighbors=(NeighborRepository(provider="gitlab", repository_id="acme/api"),),
        )
        catalog = [
            CatalogRepository(connection=GITLAB_CONN, numeric_id=101, display_name="acme/api"),
            CatalogRepository(connection=GITLAB_MIRROR, numeric_id=101, display_name="acme/api"),
        ]
        built: list[CatalogRepository] = []
        resolution = resolve_readers(
            profile, repositories=catalog, reader_factory=lambda repo: built.append(repo)
        )

        assert resolution.ok is False
        assert resolution.readers == {}
        assert built == []  # nothing was constructed from the decoy
        (refusal,) = resolution.refusals
        assert refusal.code == "ambiguous"
        assert refusal.neighbor_key == "gitlab:acme/api"
        assert GITLAB_CONN.key in refusal.detail and GITLAB_MIRROR.key in refusal.detail

    def test_an_unavailable_required_neighbor_is_a_blocking_question(self):
        """No connection of the declared family exposes the repository: the
        refusal is typed, rendered into the planning input as a blocking
        question, and the resolution honestly says incomplete."""
        profile = SystemContextProfile(
            writable=OWN,
            neighbors=(NeighborRepository(provider="gitlab", repository_id="acme/api"),),
        )
        resolution = resolve_readers(
            profile,
            repositories=[
                CatalogRepository(connection=GITHUB_CONN, numeric_id=1, display_name="acme/api")
            ],
            reader_factory=lambda repo: object(),
        )

        assert resolution.ok is False
        assert resolution.readers == {}
        (refusal,) = resolution.refusals
        assert refusal.code == "unavailable"

        document = resolution.as_document()
        assert document["complete"] is False
        (question,) = document["plan.blocking_questions"]
        assert "gitlab:acme/api" in question["text"]
        assert question["source"] == "reader_resolution:gitlab:acme/api"

        section = resolution.planning_input_section()
        assert section.startswith(AUTHORITY_BEGIN) and section.rstrip().endswith(AUTHORITY_END)
        assert "plan.blocking_questions" in section
        planning_input = attach_authority_section("Fix the expiry event payload.", section)
        assert "Fix the expiry event payload." in planning_input
        assert "gitlab:acme/api" in planning_input

    def test_an_incomplete_resolution_never_builds_a_discovery_context(self):
        """The refused neighbor has no reader, so the composition with the
        profile's context builder refuses loudly — no half-built context,
        no confident complete plan over an unreadable source."""
        profile = SystemContextProfile(
            writable=OWN,
            neighbors=(
                NeighborRepository(provider="gitlab", repository_id="acme/api"),
                NeighborRepository(provider="github", repository_id="other/repo"),
            ),
        )
        resolution = resolve_readers(
            profile,
            repositories=[
                # github neighbor resolvable; gitlab neighbor not in the catalog
                CatalogRepository(connection=GITHUB_CONN, numeric_id=5, display_name="other/repo")
            ],
            reader_factory=lambda repo: object(),
        )
        assert resolution.ok is False
        with pytest.raises(ValueError, match="no reader for authorized neighbor"):
            profile.build_discovery_context(
                run_id="run-1",
                project_id=1,
                session_factory=object(),
                neighbor_readers=resolution.readers,
                own_reader=object(),
            )

    def test_the_single_repo_profile_resolves_with_no_bindings(self):
        resolution = resolve_readers(
            SystemContextProfile(writable=OWN),
            repositories=[],
            reader_factory=lambda repo: object(),
        )
        assert resolution.ok and resolution.bindings == () and resolution.refusals == ()
        assert resolution.planning_input_section() == ""

    def test_reader_resolution_documents_the_connection_identities(self):
        catalog = [
            CatalogRepository(connection=GITLAB_CONN, numeric_id=101, display_name="acme/api"),
            CatalogRepository(connection=GITHUB_CONN, numeric_id=101, display_name="acme/api"),
        ]
        document = resolve_readers(
            _two_neighbor_profile(), repositories=catalog, reader_factory=lambda repo: object()
        ).as_document()
        assert document["complete"] is True
        assert document["plan.blocking_questions"] == []
        assert {entry["connection"] for entry in document["resolved"]} == {
            GITLAB_CONN.key,
            GITHUB_CONN.key,
        }


# ---------------------------------------------------------------------------
# The authorized set + the read boundary
# ---------------------------------------------------------------------------


class TestAuthorizedReadBoundary:
    def _surface(self) -> tuple[AuthorizedReadSurface, CountingReader, CountingReader]:
        own = CountingReader({"src/app.py": "class LLMPlanner:\n    pass\n"})
        neighbor = CountingReader({"src/events.py": "ORDER_EXPIRED = 'order.expired.v2'\n"})
        rogue = CountingReader({"SECRET.md": "not authorized\n"})
        surface = AuthorizedReadSurface(
            _two_neighbor_profile(),
            {"own": own, "gitlab:acme/api": neighbor, "github:acme/api": rogue},
        )
        return surface, own, neighbor

    async def test_an_unauthorized_read_is_a_typed_refusal_with_zero_content(self):
        surface, own, neighbor = self._surface()
        before = (list(own.calls), list(neighbor.calls))

        with pytest.raises(DiscoveryAuthorizationRefusal) as excinfo:
            await surface.read_text("github:rogue/repo", "SECRET.md")

        refusal = excinfo.value
        assert refusal.code == "outside_authorized_set"
        assert refusal.requested == "github:rogue/repo"
        assert "github:rogue/repo" in str(refusal) and "authorized" in str(refusal)
        # ZERO content: no underlying reader was touched by the refused ask
        assert (own.calls, neighbor.calls) == before
        assert surface.requests == []
        assert surface.refusals == [refusal]

    async def test_an_unauthorized_tree_listing_is_refused_before_the_reader_runs(self):
        surface, _own, _neighbor = self._surface()
        with pytest.raises(DiscoveryAuthorizationRefusal):
            await surface.get_tree("gitlab:rogue/repo", project_id=1)
        assert surface.refusals[0].requested == "gitlab:rogue/repo"

    async def test_authorized_reads_delegate_and_are_audited(self):
        surface, _own, _neighbor = self._surface()
        text = await surface.read_text("gitlab:acme/api", "src/events.py")
        assert text.startswith("ORDER_EXPIRED")
        tree = await surface.get_tree("own", project_id=1)
        assert [entry.path for entry in tree] == ["src/app.py"]
        assert surface.requests == ["gitlab:acme/api", "own"]
        assert surface.refusals == []

    def test_the_surface_covers_exactly_the_authorized_set(self):
        profile = _two_neighbor_profile()
        rogue = CountingReader({"SECRET.md": "x"})
        with pytest.raises(ValueError, match="UNAUTHORIZED"):
            AuthorizedReadSurface(
                profile,
                {
                    "own": object(),
                    "gitlab:acme/api": object(),
                    "github:acme/api": object(),
                    "gitlab:rogue/repo": rogue,
                },
            )
        with pytest.raises(ValueError, match="no reader for authorized"):
            AuthorizedReadSurface(profile, {"own": object()})

    def test_the_authorized_set_digest_is_recorded_and_identity_bound(self):
        profile = _two_neighbor_profile()
        document = AuthorizedRepoSet(profile).as_document()

        assert document["discovery.authorized_repo_set_digest"] == authorized_repo_set_digest(
            profile
        )
        assert document["read_keys"] == ["own", "gitlab:acme/api", "github:acme/api"]
        assert document["write_target"] == "github:example/repo"

        # stable for the same authorization...
        assert authorized_repo_set_digest(profile) == authorized_repo_set_digest(
            _two_neighbor_profile()
        )
        # ...and it MOVES with the authorization, not the reads
        moved_ref = SystemContextProfile(
            writable=OWN,
            neighbors=(
                NeighborRepository(provider="gitlab", repository_id="acme/api", ref="c" * 40),
                NeighborRepository(provider="github", repository_id="acme/api", ref="b" * 40),
            ),
        )
        assert authorized_repo_set_digest(moved_ref) != authorized_repo_set_digest(profile)
        renamed = SystemContextProfile(
            writable=OWN,
            neighbors=(
                NeighborRepository(provider="gitlab", repository_id="acme/api-v2", ref="a" * 40),
                NeighborRepository(provider="github", repository_id="acme/api", ref="b" * 40),
            ),
        )
        assert authorized_repo_set_digest(renamed) != authorized_repo_set_digest(profile)
        narrowed_scope = SystemContextProfile(
            writable=WritableTarget(
                provider="github",
                repository_id="example/repo",
                ref="f" * 40,
                allowed_globs=("src/**",),
            ),
            neighbors=_two_neighbor_profile().neighbors,
        )
        assert authorized_repo_set_digest(narrowed_scope) != authorized_repo_set_digest(profile)

    def test_the_read_gate_type_carries_the_authorized_vocabulary(self):
        authorized = AuthorizedRepoSet(_two_neighbor_profile())
        with pytest.raises(DiscoveryAuthorizationRefusal) as excinfo:
            authorized.authorize_read("gitlab:decoy/api")
        assert list(excinfo.value.authorized) == ["own", "gitlab:acme/api", "github:acme/api"]


# ---------------------------------------------------------------------------
# Neighbor-write refusal — surfaced expansion requests
# ---------------------------------------------------------------------------


class TestNeighborWriteRefusal:
    def test_a_neighbor_write_is_refused_and_surfaced_as_an_expansion_request(self):
        profile = _two_neighbor_profile()
        review = review_write_proposals(profile, [("gitlab", "acme/api")])

        (verdict,) = review.verdicts
        assert verdict.allowed is False
        assert verdict.code == "read_only_neighbor"

        (request,) = review.expansion_requests
        assert request.requested == "gitlab:acme/api"
        assert request.approved_target == "github:example/repo"
        assert request.code == "read_only_neighbor"

        document = review.as_document()
        assert document["write_scope.target"] == "github:example/repo"
        (recorded,) = document["write_scope.expansion_requests"]
        assert recorded["requested"] == "gitlab:acme/api"
        assert recorded["decision"] == "refused_pending_explicit_authorization"

    def test_the_publication_boundary_names_only_the_approved_target(self):
        review = review_write_proposals(
            _two_neighbor_profile(),
            [("gitlab", "acme/api"), ("github", "acme/api"), ("github", "example/repo")],
        )
        assert review.publication_targets == ("github:example/repo",)
        assert review.widened is False
        document = review.as_document()
        assert document["publication_targets"] == ["github:example/repo"]
        assert document["widened"] is False
        # BOTH neighbor proposals are surfaced — none silently absorbed
        assert [request.requested for request in review.expansion_requests] == [
            "gitlab:acme/api",
            "github:acme/api",
        ]
        assert [verdict.code for verdict in review.verdicts] == [
            "read_only_neighbor",
            "read_only_neighbor",
            "writable_target",
        ]

    def test_an_outside_context_write_is_refused_but_not_a_scope_expansion(self):
        review = review_write_proposals(_two_neighbor_profile(), [("gitlab", "nobody/repo")])
        (verdict,) = review.verdicts
        assert verdict.code == "outside_context"
        assert review.expansion_requests == ()
        assert review.publication_targets == ("github:example/repo",)

    def test_a_write_to_the_own_repository_needs_no_expansion(self):
        review = review_write_proposals(_two_neighbor_profile(), [("github", "example/repo")])
        assert review.verdicts[0].allowed is True
        assert review.expansion_requests == ()
        assert review.as_document()["write_scope.expansion_requests"] == []


# ---------------------------------------------------------------------------
# Truncation visibility
# ---------------------------------------------------------------------------


class TestTruncationVisibility:
    def test_a_cut_read_leaves_an_explicit_marker_and_a_planning_section(self):
        big = "x" * 500
        reader = CountingReader({"src/big.py": big, "README.md": "small\n"})
        bounded = BoundedNeighborReader(reader, repo_key="gitlab:acme/api", max_bytes_per_read=128)

        text = asyncio.run(bounded.read_text("src/big.py"))
        assert text == big[:128]
        readme = asyncio.run(bounded.read_text("README.md"))
        assert readme == "small\n"

        document = truncation_document([bounded])
        entry = document["discovery.truncation"]
        assert entry["any"] is True
        assert entry["repos"] == ["gitlab:acme/api"]
        (event,) = entry["events"]
        assert event == {
            "repo_key": "gitlab:acme/api",
            "path": "src/big.py",
            "window": "bytes 0..128 of 500",
            "limit": 128,
            "reason": "max_bytes_per_read",
        }

        section = render_truncation_section(document)
        assert "NEIGHBOR READS TRUNCATED" in section
        assert "gitlab:acme/api:src/big.py" in section
        assert "bytes 0..128 of 500" in section
        planning_input = attach_truncation_section("Refactor the API.", section)
        assert planning_input.startswith("Refactor the API.")
        assert "gitlab:acme/api:src/big.py" in planning_input

    def test_an_untruncated_run_is_explicitly_not_truncated(self):
        bounded = BoundedNeighborReader(
            CountingReader({"README.md": "small\n"}), repo_key="gitlab:acme/api"
        )
        document = truncation_document([bounded])
        assert document["discovery.truncation"] == {
            "any": False,
            "repos": [],
            "events": [],
        }
        assert render_truncation_section(document) == ""

    def test_the_marker_names_which_window_of_which_repo_was_partial(self):
        event = TruncationEvent(
            repo_key="github:acme/api",
            path="src/generated.go",
            window="bytes 0..64 of 4096",
            limit=64,
            reason="max_bytes_per_read",
        )
        section = render_truncation_section(
            {
                "discovery.truncation": {
                    "any": True,
                    "repos": ["github:acme/api"],
                    "events": [event.as_document()],
                }
            }
        )
        assert "github:acme/api:src/generated.go — bytes 0..64 of 4096" in section

    async def test_truncation_surfaces_through_a_real_discovery_run(self, tmp_path):
        """The journey: a neighbor's oversized file is cut by the bounded
        wrapper while the REAL discovery stage reads through it — the
        marker names the repo/window, and the stage's frozen evidence
        never claims bytes beyond the cut."""
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )
        async with factory() as session:
            session.add(FlowRun(id="run-auth-trunc", project_id=1, status="planning"))
            await session.commit()

        profile = SystemContextProfile(
            writable=OWN,
            neighbors=(NeighborRepository(provider="gitlab", repository_id="acme/api"),),
        )
        own = CountingReader({"src/app.py": "class LLMPlanner:\n    pass\n"})
        neighbor_files = {
            "README.md": "# partner\n",
            "src/payload.py": ("PAYLOAD = 'x'\n" * 40) + "EXPIRY_EVENT = 'order.expired.v2'\n",
        }
        bounded = BoundedNeighborReader(
            CountingReader(neighbor_files), repo_key="gitlab:acme/api", max_bytes_per_read=64
        )
        ctx = profile.build_discovery_context(
            run_id="run-auth-trunc",
            project_id=1,
            session_factory=factory,
            neighbor_readers={"gitlab:acme/api": bounded},
            own_reader=own,
        )
        outcome = await run_discovery_stage(ctx, "Refactor the expiry event payload.")

        document = truncation_document([bounded])
        assert document["discovery.truncation"]["any"] is True
        assert document["discovery.truncation"]["repos"] == ["gitlab:acme/api"]
        assert all(
            event["path"] == "src/payload.py"
            for event in document["discovery.truncation"]["events"]
        )
        # the frozen snapshot the stage probed is the CUT one — the recorded
        # tree vouches for fewer lines than the neighbor actually holds
        recorded = outcome.record["snapshot_tree"]["repos"]["gitlab:acme/api"]["files"]
        assert recorded["src/payload.py"]["lines"] < len(
            neighbor_files["src/payload.py"].splitlines()
        )
        assert outcome.record["status"] == "complete"
        await engine.dispose()


# ---------------------------------------------------------------------------
# Snapshot invalidation — explicit decisions over the neighbor set digest
# ---------------------------------------------------------------------------


class TestSnapshotInvalidation:
    def _entries(self, gitlab_oid: str = "a" * 40, github_oid: str = "b" * 40):
        return {
            "gitlab:acme/api": {"repository_id": "acme/api", "source_oid": gitlab_oid},
            "github:acme/api": {"repository_id": "acme/api", "source_oid": github_oid},
        }

    def test_unchanged_neighbor_snapshots_keep_the_evidence_applicable(self):
        entries = self._entries()
        decision = evaluate_snapshot_invalidation(entries, self._entries())
        assert decision.code == "current"
        assert decision.invalidated is False
        assert decision.changed == ()
        assert decision.recorded_digest == decision.current_digest
        assert neighbor_identity_changed(entries, self._entries()) is False

    def test_a_moved_neighbor_oid_invalidates_through_an_explicit_decision(self):
        recorded = self._entries()
        current = self._entries(gitlab_oid="c" * 40)
        decision = evaluate_snapshot_invalidation(recorded, current)
        assert decision.code == "identity_changed"
        assert decision.invalidated is True
        assert decision.changed == ("gitlab:acme/api",)
        assert "gitlab:acme/api" in decision.reason
        assert decision.recorded_digest != decision.current_digest
        assert neighbor_identity_changed(recorded, current) is True

    def test_a_repointed_repository_is_an_identity_change_even_at_the_same_oid(self):
        recorded = self._entries()
        current = {
            "gitlab:acme/api": {"repository_id": "acme/api-v2", "source_oid": "a" * 40},
            "github:acme/api": {"repository_id": "acme/api", "source_oid": "b" * 40},
        }
        decision = evaluate_snapshot_invalidation(recorded, current)
        assert decision.invalidated is True
        assert decision.changed == ("gitlab:acme/api",)

    def test_an_added_or_removed_neighbor_invalidates(self):
        recorded = self._entries()
        grown = {
            **self._entries(),
            "gitlab:billing/core": {"repository_id": "billing/core", "source_oid": "d" * 40},
        }
        decision = evaluate_snapshot_invalidation(recorded, grown)
        assert decision.invalidated is True
        assert decision.changed == ("gitlab:billing/core",)
        shrunk = evaluate_snapshot_invalidation(grown, recorded)
        assert shrunk.invalidated is True and shrunk.changed == ("gitlab:billing/core",)

    def test_the_profile_entries_feed_the_decision(self):
        profile = _two_neighbor_profile()
        entries = neighbor_entries_of_profile(profile)
        assert entries == self._entries()
        assert (
            evaluate_snapshot_invalidation(entries, neighbor_entries_of_profile(profile)).code
            == "current"
        )

    def test_a_real_discovery_records_neighbor_oids_the_decision_reads(self):
        record = {
            "dispatch": {
                "repositories": {
                    "own": {"repository_id": "example/repo", "source_oid": "f" * 40},
                    "gitlab:acme/api": {"repository_id": "acme/api", "source_oid": "a" * 40},
                    "github:acme/api": {"repository_id": "acme/api", "source_oid": "b" * 40},
                }
            }
        }
        recorded = neighbor_entries_of_record(record)
        assert recorded == self._entries()  # the own repo never rides the neighbor set
        assert evaluate_snapshot_invalidation(recorded, self._entries()).code == "current"
        assert evaluate_snapshot_invalidation(
            recorded, self._entries(github_oid="e" * 40)
        ).changed == ("github:acme/api",)


# ---------------------------------------------------------------------------
# The captured offline recording — a real ToolObservation-backed discovery
# ---------------------------------------------------------------------------


class _FakeClock:
    """A deterministic clock: the capture must replay byte-identically."""

    def __init__(self, start: float = 1000.0, step: float = 0.25) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> float:
        current = self._now
        self._now += self._step
        return current


class _ScriptedModel:
    """The offline 'model': a fixed, REACTIVE investigation script.

    Iteration 1 greps the NEIGHBOR for the policy name and reads the own
    repo's dispatcher. Iteration 2 reacts to the ACTUAL grep observation
    the harness fed back (``src/courier.py:<line>: COURIER_...``) by
    reading the NON-INITIAL char window around that line. Iteration 3
    declares done. Every call goes through the REAL tool execution
    machinery — the observations in the capture are what the tools
    actually returned, never authored prose.
    """

    def __init__(self, snap: Snapshot) -> None:
        self._snap = snap
        self._stage = 0
        self.proposed: list[dict] = []
        self._input_tokens = iter([640, 830, 410])
        self._output_tokens = iter([96, 44, 130])

    async def __call__(self, system_prompt: str, user_prompt: str) -> SimpleNamespace:
        self._stage += 1
        if self._stage == 1:
            turn = {
                "calls": [
                    {"tool": "grep", "repo": "billing", "args": {"pattern": "DEADLINE_POLICY"}},
                    {
                        "tool": "read_file",
                        "repo": "orders",
                        "args": {"path": "src/dispatch.py", "offset": 0, "length": 600},
                    },
                ]
            }
        elif self._stage == 2:
            match = re.search(r"src/courier\.py:(\d+): COURIER_DEADLINE_POLICY", user_prompt)
            assert match is not None, "the grep observation did not reach the model"
            offset, length = self._window_around("billing", "src/courier.py", int(match.group(1)))
            turn = {
                "calls": [
                    {
                        "tool": "read_file",
                        "repo": "billing",
                        "args": {"path": "src/courier.py", "offset": offset, "length": length},
                    }
                ]
            }
        else:
            turn = {
                "done": True,
                "summary": (
                    "Orders' dispatch imports COURIER_DEADLINE_POLICY from Billing"
                    " (src/dispatch.py); Billing defines it in src/courier.py at a"
                    " non-initial window — window_seconds 900, grace_seconds 60,"
                    " version v3. The decisive contract lives only in the neighbor."
                ),
                "assumptions": [],
                "contradictions": [],
            }
        for call in turn.get("calls") or []:
            self.proposed.append(dict(call))
        return SimpleNamespace(
            text=json.dumps(turn),
            input_tokens=next(self._input_tokens),
            output_tokens=next(self._output_tokens),
        )

    def _window_around(self, repo_key: str, path: str, line_no: int) -> tuple[int, int]:
        """The char ``[offset, length)`` covering lines ``line_no-2..line_no+2``."""
        content = self._snap.files_of(repo_key)[path]
        lines = content.splitlines(keepends=True)
        start = sum(len(text) for text in lines[: max(0, line_no - 3)])
        stop = sum(len(text) for text in lines[: min(len(lines), line_no + 2)])
        return start, stop - start


async def capture_offline_recording() -> dict:
    """Reproduce the checked-in offline capture, deterministically.

    A REAL ``run_research_pass`` over ``SnapshotToolbox`` repos built from
    the fixture snapshot (the same construction ``_run_research_leg``
    uses in the discovery stage), driven by a scripted offline model —
    no live provider, no network. The decisive dependency
    (COURIER_DEADLINE_POLICY) lives ONLY in the neighbor repository, at
    a non-initial file window the model reaches through the actual grep
    and read_file observations.
    """
    snap = Snapshot.load(CAPTURE_SNAPSHOT_PATH)
    repos = {
        key: ResearchRepo(
            repo_key=key,
            repository_id=str(entry.get("repository_id") or ""),
            source_oid=str(entry.get("source_oid") or ""),
            toolbox=SnapshotToolbox(dict(snap.files_of(key))),
        )
        for key, entry in snap.repos.items()
    }
    model = _ScriptedModel(snap)
    harness = ResearchHarness(complete=model, max_calls=10, wall_seconds=90.0)
    outcome = await run_research_pass(
        harness,
        planner_input=CAPTURE_PLANNER_INPUT,
        lexical=[],
        repos=repos,
        now=_FakeClock(),
    )
    # Re-execute the scripted proposals through the same executor the loop
    # used, minting the REAL ToolObservation records for the recording.
    observations = [_execute_call(call, repos)[1] for call in model.proposed]

    # the plan: authored from what the run ACTUALLY established — the
    # claim lines/bytes come from the snapshot the tools read.
    policy_line, policy_text = _line_of(
        snap, "billing", "src/courier.py", "COURIER_DEADLINE_POLICY = {"
    )
    import_line, import_text = _line_of(
        snap, "orders", "src/dispatch.py", "from billing.courier import"
    )

    document = dict(outcome.document)
    document["findings"] = [
        {
            "evidence_id": f"ev-{index}",
            "repo_key": finding.repo_key,
            "repository_id": finding.repository_id,
            "path": finding.path,
            "line": finding.line,
            "kind": finding.kind,
            "detail": finding.detail,
        }
        for index, finding in enumerate(outcome.findings, start=1)
    ]
    return {
        "schema": RECORD_SCHEMA,
        "task_id": CAPTURE_TASK_ID,
        "mode": MODE_RESEARCH,
        "snapshot_digest": snap.digest,
        "budget": {"max_calls": 10, "wall_seconds": 90.0},
        "attempts": [
            {
                "attempt": 1,
                "stopped_reason": "",
                "cost": {
                    "calls_proposed": document["calls_proposed"],
                    "calls_executed": document["calls_executed"],
                    "wall_seconds_used": document["wall_seconds_used"],
                    "tokens": document["tokens"],
                },
            }
        ],
        "research_document": document,
        "observations": [
            {
                "tool": observation.tool,
                "repo_key": observation.repo_key,
                "call": observation.call,
                "content": observation.content,
                "error": observation.error,
                "truncated": observation.truncated,
            }
            for observation in observations
        ],
        "plan": {
            "steps": [
                {
                    "step_id": "s1",
                    "objective": (
                        "enforce Billing's courier deadline window (900s window, 60s"
                        " grace, v3) in Orders' dispatch"
                    ),
                    "evidence_refs": ["ev-3"],
                },
                {
                    "step_id": "s2",
                    "objective": "keep importing the policy from billing.courier — never redefine it in Orders",
                    "evidence_refs": ["ev-2"],
                },
            ],
            "surface": [
                {"repo": "billing", "path": "src/courier.py", "why": "owns the deadline policy"},
                {"repo": "orders", "path": "src/dispatch.py", "why": "imports and enforces it"},
            ],
            "claims": [
                {
                    "claim_id": "c1",
                    "text": policy_text,
                    "repo": "billing",
                    "path": "src/courier.py",
                    "line": policy_line,
                    "asserted_content": policy_text,
                    "importance": "important",
                    "evidence": "evidence:ev-3",
                },
                {
                    "claim_id": "c2",
                    "text": import_text,
                    "repo": "orders",
                    "path": "src/dispatch.py",
                    "line": import_line,
                    "asserted_content": import_text,
                    "importance": "important",
                    "evidence": "evidence:ev-2",
                },
            ],
            "questions": [],
            "assumptions": [],
        },
        "reviewer": {
            "correction_severity": "unrecorded",
            "notes": (
                "captured offline: scripted model over SnapshotToolbox through the"
                " real run_research_pass/_execute_call machinery — live-cohort seed"
                " for #271; human review pending (no correction estimate recorded)"
            ),
        },
        "capture": {
            "provenance": "offline-scripted-model",
            "recorded_at": "2026-09-23",
            "harness": "forge.adaptive.research_planner.run_research_pass",
            "live_provider": False,
            "issue": "R36-11 / #270",
            "decisive_dependency": "billing/src/courier.py (neighbor only, non-initial window)",
        },
    }


def _line_of(snap: Snapshot, repo_key: str, path: str, needle: str) -> tuple[int, str]:
    for number, text in enumerate(snap.files_of(repo_key)[path].splitlines(), start=1):
        if needle in text:
            return number, text
    raise AssertionError(f"{repo_key}:{path} does not carry {needle!r}")


class TestOfflineCapturedRecording:
    async def test_the_capture_reproduces_the_checked_in_recording(self):
        """The recording is REAL: re-running the deterministic capture
        yields the checked-in document byte-for-byte (canonical JSON)."""
        captured = await capture_offline_recording()
        checked_in = json.loads(CAPTURE_RECORD_PATH.read_text(encoding="utf-8"))
        assert json.dumps(captured, sort_keys=True) == json.dumps(checked_in, sort_keys=True)

    def test_the_recording_round_trips_through_the_cohort_loader(self):
        run = RecordedRun.load(CAPTURE_RECORD_PATH)

        assert run.task_id == CAPTURE_TASK_ID
        assert run.mode == MODE_RESEARCH
        assert run.outcome == "graded"
        assert run.snapshot_digest == Snapshot.load(CAPTURE_SNAPSHOT_PATH).digest
        assert run.research_document is not None
        assert run.research_document["complete"] is True
        assert run.research_document["stopped_reason"] == ""
        assert run.research_document["repos_consulted"] == ["billing", "orders"]

        # REAL ToolObservation records, not prose: the neighbor read is the
        # deep window, and the grep observation carries the actual matches.
        assert len(run.observations) == 3
        assert all(isinstance(observation, ToolObservation) for observation in run.observations)
        assert all(observation.error == "" for observation in run.observations)
        grep, own_read, deep_read = run.observations
        assert grep.tool == "grep" and grep.repo_key == "billing"
        assert "src/courier.py:" in grep.content
        assert own_read.repo_key == "orders" and "COURIER_DEADLINE_POLICY" in own_read.content
        assert deep_read.tool == "read_file" and deep_read.repo_key == "billing"
        assert "window_seconds" in deep_read.content and deep_read.truncated is False
        # the window is NON-INITIAL: the read paged into the file, it did
        # not start at byte 0
        offset = int(deep_read.call.split("offset ")[1].split()[0])
        assert offset > 0
        courier = Snapshot.load(CAPTURE_SNAPSHOT_PATH).files_of("billing")["src/courier.py"]
        assert courier[offset : offset + len(deep_read.content)] == deep_read.content

        # labeled captured-offline, never live-provider
        capture = json.loads(CAPTURE_RECORD_PATH.read_text(encoding="utf-8"))["capture"]
        assert capture["provenance"] == "offline-scripted-model"
        assert capture["live_provider"] is False

    def test_the_recording_grades_against_the_frozen_snapshot(self):
        snap = Snapshot.load(CAPTURE_SNAPSHOT_PATH)
        task = CohortTask(
            task_id=CAPTURE_TASK_ID,
            archetype="neighbor_dependency",
            statement=CAPTURE_PLANNER_INPUT,
            snapshot_digest=snap.digest,
            snapshot_ref=CAPTURE_SNAPSHOT_PATH.name,
            rubric=DEFAULT_RUBRICS["neighbor_dependency"],
            expected_surface=(
                SurfaceRef(repo_key="billing", path="src/courier.py"),
                SurfaceRef(repo_key="orders", path="src/dispatch.py"),
            ),
        )
        run = RecordedRun.load(CAPTURE_RECORD_PATH)
        grade = grade_run(task, run, snap)

        assert grade["outcome"] == "graded"
        assert grade["evidence_accuracy"]["score"] == 1.0
        assert grade["impacted_surface_recall"]["score"] == 1.0
        assert grade["impacted_surface_recall"]["missed"] == []
        claims = {entry["claim_id"]: entry for entry in grade["evidence_accuracy"]["claims"]}
        assert claims["c1"]["verdict"] == "semantic_match"
        # the decisive claim cites the NON-INITIAL window in the NEIGHBOR
        plan = run.plan or {}
        (policy_claim,) = [c for c in plan["claims"] if c["claim_id"] == "c1"]
        assert policy_claim["repo"] == "billing"
        assert policy_claim["line"] > 20  # deep, not the file head
