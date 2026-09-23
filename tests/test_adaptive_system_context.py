"""NEXT-21 — the read-many/write-one system context profile, checked.

- the PROFILE: 2 declared neighbors + 1 writable target, derived from
  the project's ``forge.yml`` ``neighbors:`` section with explicit
  authorization — unknown fields, unknown providers and half-declared
  entries refuse, never silently narrow or broaden;
- the WRITE BOUNDARY: the writable target is the only allowed write; a
  write-targeting tool call naming a NEIGHBOR is refused
  (``read_only_neighbor``), anything outside the context as
  ``outside_context``;
- the DISCOVERY CONNECTION: the profile feeds
  ``DiscoveryRunContext.from_readers`` with the own repo first and every
  neighbor under its own namespace — a 2-neighbor profile discovers
  across all 3 repositories, and a reader for an UNAUTHORIZED repository
  refuses (an unauthorized neighbor is absent from the context by
  construction, never merely unused).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.adaptive.system_context import (
    NeighborRepository,
    SystemContextProfile,
    WritableTarget,
    from_project_config,
)
from forge.config import ForgeConfig
from forge.durable import FlowRun
from forge.models.base import Base

OWN = WritableTarget(provider="github", repository_id="example/repo", ref="f" * 40)

NEIGHBOR_FILES = {
    "partner": {
        "src/app/planner.py": (
            "# the neighbor's fork\n# longer,\n# so line bounds disagree.\n"
            "class LLMPlanner:\n    def plan(self, issue, ctx):\n        return ctx\n"
        ),
    },
    "second": {"README.md": "A second authorized neighbor.\n"},
}


def _config(tmp_path: Path, neighbors_yaml: str) -> ForgeConfig:
    path = tmp_path / "forge.yml"
    path.write_text(f"forge:\n{neighbors_yaml}")
    return ForgeConfig(path)


class FakeReader:
    """The duck-typed read surface discovery needs (get_tree / read_text)."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.text_reads: list[str] = []

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> list:
        return [SimpleNamespace(path=p, type="blob") for p in sorted(self._files)]

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        self.text_reads.append(file_path)
        return self._files[file_path]


# ---------------------------------------------------------------------------
# from_project_config — explicit authorization, loud refusals
# ---------------------------------------------------------------------------


class TestFromProjectConfig:
    def test_two_neighbors_and_one_writable_come_from_the_config(self, tmp_path):
        config = _config(
            tmp_path,
            """
  neighbors:
    - provider: gitlab
      repository_id: partner/org/neighbor
      ref: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      allowed_globs: ["src/**"]
    - provider: github
      repository_id: other/team/repo
      ref: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
""",
        )

        profile = from_project_config(config, OWN)

        assert profile.writable == OWN  # the run's own repository, verbatim
        assert profile.neighbors == (
            NeighborRepository(
                provider="gitlab",
                repository_id="partner/org/neighbor",
                ref="a" * 40,
                allowed_globs=("src/**",),
            ),
            NeighborRepository(
                provider="github",
                repository_id="other/team/repo",
                ref="b" * 40,
            ),
        )

    def test_a_missing_section_is_the_single_repo_context(self, tmp_path):
        profile = from_project_config(_config(tmp_path, "  port: 8420\n"), OWN)

        assert profile.neighbors == ()
        assert profile.writable == OWN

    def test_unknown_fields_refuse_not_ignore(self, tmp_path):
        config = _config(
            tmp_path,
            """
  neighbors:
    - provider: gitlab
      repository_id: partner/org/neighbor
      write: true
""",
        )
        with pytest.raises(ValueError, match="unknown field"):
            from_project_config(config, OWN)

    @pytest.mark.parametrize(
        "entry_yaml, complaint",
        [
            ("    - provider: gitea\n      repository_id: x\n", "vocabulary"),
            ("    - provider: gitlab\n", "repository_id"),
            ("    - repository_id: x\n", "vocabulary"),
            (
                "    - provider: gitlab\n      repository_id: x\n      allowed_globs: src/**\n",
                "list",
            ),
        ],
    )
    def test_half_declared_entries_refuse(self, tmp_path, entry_yaml, complaint):
        config = _config(tmp_path, f"  neighbors:\n{entry_yaml}")
        with pytest.raises(ValueError, match=complaint):
            from_project_config(config, OWN)

    def test_a_non_list_section_refuses(self, tmp_path):
        config = _config(tmp_path, "  neighbors: partner/org/neighbor\n")
        with pytest.raises(ValueError, match="list"):
            from_project_config(config, OWN)

    def test_the_own_repo_may_not_also_be_a_neighbor(self, tmp_path):
        config = _config(
            tmp_path,
            """
  neighbors:
    - provider: github
      repository_id: example/repo
""",
        )
        with pytest.raises(ValueError, match="writable target"):
            from_project_config(config, OWN)

    def test_duplicate_neighbors_refuse(self, tmp_path):
        config = _config(
            tmp_path,
            """
  neighbors:
    - provider: gitlab
      repository_id: partner/org/neighbor
    - provider: gitlab
      repository_id: partner/org/neighbor
""",
        )
        with pytest.raises(ValueError, match="twice"):
            from_project_config(config, OWN)

    def test_equal_ids_across_providers_stay_distinct(self, tmp_path):
        config = _config(
            tmp_path,
            """
  neighbors:
    - provider: gitlab
      repository_id: core/api
    - provider: github
      repository_id: core/api
""",
        )
        profile = from_project_config(config, OWN)

        assert [n.key for n in profile.neighbors] == ["gitlab:core/api", "github:core/api"]


# ---------------------------------------------------------------------------
# The write boundary
# ---------------------------------------------------------------------------


class TestAuthorizeWrite:
    def _profile(self) -> SystemContextProfile:
        return SystemContextProfile(
            writable=OWN,
            neighbors=(
                NeighborRepository(provider="gitlab", repository_id="partner/org/neighbor"),
                NeighborRepository(provider="github", repository_id="other/team/repo"),
            ),
        )

    def test_the_own_repository_is_the_single_writable_target(self):
        decision = self._profile().authorize_write("github", "example/repo")

        assert decision.allowed is True
        assert decision.code == "writable_target"

    def test_a_write_targeting_tool_call_to_a_neighbor_is_refused(self):
        decision = self._profile().authorize_write("gitlab", "partner/org/neighbor")

        assert decision.allowed is False
        assert decision.code == "read_only_neighbor"
        assert "read-only neighbor" in decision.reason

    def test_the_same_id_on_another_provider_is_not_the_writable_target(self):
        decision = self._profile().authorize_write("gitlab", "example/repo")

        assert decision.allowed is False
        assert decision.code == "outside_context"

    def test_an_unknown_repository_is_outside_the_context(self):
        decision = self._profile().authorize_write("github", "nobody/repo")

        assert decision.allowed is False
        assert decision.code == "outside_context"
        assert "not part of the authorized system context" in decision.reason


# ---------------------------------------------------------------------------
# The discovery connection
# ---------------------------------------------------------------------------


class TestDiscoveryConnection:
    def _profile(self) -> SystemContextProfile:
        return SystemContextProfile(
            writable=WritableTarget(provider="github", repository_id="example/repo", ref="f" * 40),
            neighbors=(
                NeighborRepository(
                    provider="gitlab",
                    repository_id="partner/org/neighbor",
                    ref="a" * 40,
                    allowed_globs=("src/**", "README.md"),
                ),
                NeighborRepository(
                    provider="github",
                    repository_id="other/team/repo",
                    ref="b" * 40,
                ),
            ),
        )

    def _readers(self):
        own = FakeReader({"src/app/planner.py": "class LLMPlanner:\n    pass\n"})
        partner = FakeReader(NEIGHBOR_FILES["partner"])
        second = FakeReader(NEIGHBOR_FILES["second"])
        return own, partner, second

    def test_the_profile_builds_a_context_over_exactly_own_plus_neighbors(self):
        own, partner, second = self._readers()
        ctx = self._profile().build_discovery_context(
            run_id="run-1",
            project_id=1,
            session_factory=object(),
            neighbor_readers={
                "gitlab:partner/org/neighbor": partner,
                "github:other/team/repo": second,
            },
            own_reader=own,
        )

        assert ctx.repository_id == "example/repo"  # the own repo stays primary
        assert ctx.source_oid == "f" * 40
        assert [(spec.repo_key, spec.repository_id, spec.ref) for spec in ctx.repo_specs] == [
            ("own", "example/repo", "f" * 40),
            ("gitlab:partner/org/neighbor", "partner/org/neighbor", "a" * 40),
            ("github:other/team/repo", "other/team/repo", "b" * 40),
        ]
        # each neighbor's allowed_globs ride its own spec
        by_key = {spec.repo_key: spec for spec in ctx.repo_specs}
        assert by_key["gitlab:partner/org/neighbor"].allowed_globs == ["src/**", "README.md"]
        assert by_key["github:other/team/repo"].allowed_globs is None

    def test_a_missing_neighbor_reader_refuses(self):
        own, partner, _second = self._readers()
        with pytest.raises(ValueError, match="no reader for authorized neighbor"):
            self._profile().build_discovery_context(
                run_id="run-1",
                project_id=1,
                session_factory=object(),
                neighbor_readers={"gitlab:partner/org/neighbor": partner},
                own_reader=own,
            )

    def test_an_unauthorized_neighbor_is_absent_from_the_context(self):
        """No implicit discovery: a reader for a repository the profile
        never authorized refuses — it can never smuggle a fourth repo in."""
        own, partner, second = self._readers()
        rogue = FakeReader({"SECRET.md": "not authorized\n"})
        with pytest.raises(ValueError, match="UNAUTHORIZED"):
            self._profile().build_discovery_context(
                run_id="run-1",
                project_id=1,
                session_factory=object(),
                neighbor_readers={
                    "gitlab:partner/org/neighbor": partner,
                    "github:other/team/repo": second,
                    "github:rogue/repo": rogue,
                },
                own_reader=own,
            )

    async def test_discovery_reads_all_three_repositories(self, tmp_path):
        """The journey test: 2 neighbors + 1 writable → ONE discovery
        record whose evidence spans all THREE repositories, each entry
        bound to the repo it was found in — and every reader was used."""
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )
        async with factory() as session:
            session.add(FlowRun(id="run-ctx-1", project_id=1, status="planning"))
            await session.commit()

        from forge.adaptive.discovery_stage import run_discovery_stage

        own, partner, second = self._readers()
        ctx = self._profile().build_discovery_context(
            run_id="run-ctx-1",
            project_id=1,
            session_factory=factory,
            neighbor_readers={
                "gitlab:partner/org/neighbor": partner,
                "github:other/team/repo": second,
            },
            own_reader=own,
        )
        outcome = await run_discovery_stage(ctx, "Refactor the LLMPlanner with neighbor context.")

        record = outcome.record
        assert record["status"] == "complete"
        assert {entry["repository_id"] for entry in record["evidence"]} == {
            "example/repo",
            "partner/org/neighbor",
            "other/team/repo",
        }
        assert set(record["dispatch"]["repositories"]) == {
            "own",
            "gitlab:partner/org/neighbor",
            "github:other/team/repo",
        }
        # every authorized reader actually read
        assert own.text_reads, "the own repo was read"
        assert partner.text_reads, "neighbor 1 was read"
        assert second.text_reads, "neighbor 2 was read"
        # the write boundary holds while discovery ran: the neighbors are
        # readable, not writable
        profile = self._profile()
        assert profile.authorize_write("gitlab", "partner/org/neighbor").allowed is False
        await engine.dispose()
