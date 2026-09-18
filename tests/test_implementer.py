"""LLMImplementer authoritative-evidence policy (R14).

The authoritative base contents behind materialization are typed reads
(:meth:`RepositoryReader.read_blob`): ONLY a provider-confirmed ``not_found``
may treat a path as absent. A 403, a timeout or an undecodable payload must
BLOCK the proposal with ``authoritative_read_failed`` evidence — never flip
an update into a create. Evidence reads (tree, prompt files) stay lenient:
incomplete evidence degrades the prompt, never the existence facts.
"""

import json

import httpx
import pytest

from forge.durable import FlowRun
from forge.factory.implementer import AuthoritativeReadError, LLMImplementer
from forge.gitlab.blob_reads import BlobReadResult
from forge.gitlab.client import GitLabAPIError
from forge.repository.changeset import MaterializationError
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_llm import FakeLLM

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Tweak the widget"
BASE_SHA = "base-sha-1"


def make_run(**overrides) -> FlowRun:
    values = dict(id="runabc123", project_id=PROJECT_ID, issue_iid=ISSUE_IID, base_sha=BASE_SHA)
    values.update(overrides)
    return FlowRun(**values)


def update_draft(path: str, old_text: str, new_text: str) -> str:
    return json.dumps(
        {
            "branch": "model/chose/this",
            "commit_message": "model's own message",
            "changes": [
                {"path": path, "operation": "update", "old_text": old_text, "new_text": new_text}
            ],
        }
    )


def create_draft(path: str, content: str = "# new\n") -> str:
    return json.dumps(
        {
            "branch": "model/chose/this",
            "commit_message": "model's own message",
            "changes": [{"path": path, "operation": "create", "content": content}],
        }
    )


class _ForbiddenReader(FakeGitLab):
    """A repo where EVERY authenticated read answers 403 (token scoped out)."""

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD"):
        raise GitLabAPIError(403, "authentication/authorization failed")

    async def get_tree(self, project_id: int, path: str = "", ref: str = "HEAD", recursive=False):
        raise GitLabAPIError(403, "authentication/authorization failed")


class _TimeoutReader(FakeGitLab):
    """A repo where blob reads time out while the file EXISTS."""

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD"):
        raise httpx.ConnectTimeout("timed out")


@pytest.fixture()
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_commit("main", BASE_SHA, "initial")
    return fake


@pytest.fixture()
def implementer_factory():
    def build(reader, script: list[str]) -> tuple[FakeLLM, LLMImplementer]:
        llm = FakeLLM(script=script)
        return llm, LLMImplementer(llm, gitlab=reader)

    return build


class TestForbiddenNeverProvesAbsence:
    """The R14 headline: a 403 on an EXISTING file must not flip UPDATE→CREATE."""

    async def test_403_on_existing_file_raises_instead_of_creating(
        self, implementer_factory
    ):
        repo = _ForbiddenReader()
        repo.seed_commit("main", BASE_SHA, "initial")
        repo.seed_file("src/app.py", "x = 1\n")
        llm, implementer = implementer_factory(
            repo, [update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        with pytest.raises(AuthoritativeReadError) as exc:
            await implementer.propose(make_run(), ISSUE_TITLE)

        # Blocked with the R14 marker — not silently reclassified as absence.
        assert "authoritative_read_failed" in str(exc.value)
        assert "forbidden" in str(exc.value)
        assert "src/app.py" in str(exc.value)
        # The model was asked exactly once; no proposal survived the failure.
        assert len(llm.calls_for("implementer")) == 1

    async def test_timeout_on_existing_file_raises_instead_of_creating(
        self, implementer_factory
    ):
        repo = _TimeoutReader()
        repo.seed_commit("main", BASE_SHA, "initial")
        repo.seed_file("src/app.py", "x = 1\n")
        _, implementer = implementer_factory(
            repo, [update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        with pytest.raises(AuthoritativeReadError) as exc:
            await implementer.propose(make_run(), ISSUE_TITLE)

        assert "authoritative_read_failed" in str(exc.value)
        assert "unavailable" in str(exc.value)

    async def test_create_over_existing_is_rejected_when_the_file_is_readable(
        self, fake_gitlab, implementer_factory
    ):
        # The other half of the flip: a model proposing CREATE for a file the
        # authoritative read CAN see is rejected — create-over-existing is
        # impossible in either direction.
        fake_gitlab.seed_file("src/app.py", "x = 1\n")
        llm, implementer = implementer_factory(fake_gitlab, [create_draft("src/app.py")])

        with pytest.raises(MaterializationError, match="create but file already exists"):
            await implementer.propose(make_run(), ISSUE_TITLE)
        assert len(llm.calls_for("implementer")) == 1


class TestStrictDecodeAtAuthority:
    async def test_invalid_utf8_base_is_explicitly_rejected(
        self, fake_gitlab, implementer_factory
    ):
        fake_gitlab.seed_bytes_file("assets/logo.bin", b"\xff\xfe\x00\x80 binary")
        _, implementer = implementer_factory(
            fake_gitlab, [update_draft("assets/logo.bin", "x", "y")]
        )

        with pytest.raises(AuthoritativeReadError) as exc:
            await implementer.propose(make_run(), ISSUE_TITLE)

        assert "incomplete" in str(exc.value)
        assert "not valid UTF-8" in str(exc.value)


class TestConfirmedAbsence:
    async def test_not_found_lets_a_create_materialize(self, fake_gitlab, implementer_factory):
        llm, implementer = implementer_factory(fake_gitlab, [create_draft("forge-demo/x.md")])

        cs = await implementer.propose(make_run(), ISSUE_TITLE)

        assert cs.changes[0].operation.value == "create"
        assert cs.changes[0].content == "# new\n"
        # The absence was provider-confirmed (a 404 at the pinned base), not
        # synthesized from a failure.
        assert ("get_file", (PROJECT_ID, "forge-demo/x.md", BASE_SHA)) in fake_gitlab.calls_of(
            "get_file"
        )
        assert len(llm.calls_for("implementer")) == 1

    async def test_not_found_reports_missing_for_an_update(self, fake_gitlab, implementer_factory):
        # Absence is honest in the other direction too: an update against a
        # confirmed-missing file fails materialization — not a create flip,
        # not a read failure.
        _, implementer = implementer_factory(fake_gitlab, [update_draft("gone.py", "x", "y")])

        with pytest.raises(MaterializationError, match="not in base snapshot"):
            await implementer.propose(make_run(), ISSUE_TITLE)

    async def test_confirmed_absence_is_not_a_failure_marker(self):
        assert BlobReadResult.not_found("404").confirmed_absent
        assert not BlobReadResult.forbidden("403").confirmed_absent


class TestEvidenceStaysLenient:
    async def test_incomplete_tree_listing_is_incomplete_evidence_not_a_create_flip(
        self, fake_gitlab, implementer_factory
    ):
        # The tree read fails (incomplete LISTING evidence) but the file
        # exists: the authoritative read still proves existence, so the
        # update materializes against the FULL content — never flipped.
        async def dead_tree(project_id, path="", ref="HEAD", recursive=False):
            raise GitLabAPIError(500, "tree listing exploded")

        fake_gitlab.get_tree = dead_tree  # type: ignore[method-assign]
        tail = "".join(f"line {i:04d}\n" for i in range(50))
        fake_gitlab.seed_file("src/app.py", "x = 1\n" + tail)
        _, implementer = implementer_factory(
            fake_gitlab, [update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        cs = await implementer.propose(make_run(), ISSUE_TITLE)

        assert cs.changes[0].content == "x = 42\n" + tail

    async def test_evidence_read_failure_does_not_block_when_model_uses_no_files(
        self, fake_gitlab, implementer_factory
    ):
        # A lenient evidence read failure (the hinted file 403s) only
        # shrinks the prompt; existence facts still come from the strict
        # reads (the created path is provider-confirmed absent).
        async def partial_read(project_id, file_path, ref="HEAD"):
            if file_path == "docs/hint.md":
                raise GitLabAPIError(403, "denied")
            raise GitLabAPIError(404, f"file {file_path} not found")

        fake_gitlab.get_file = partial_read  # type: ignore[method-assign]
        fake_gitlab.seed_file("docs/hint.md", "hint text\n")
        _, implementer = implementer_factory(fake_gitlab, [create_draft("forge-demo/x.md")])

        cs = await implementer.propose(make_run(), ISSUE_TITLE, files_hint=["docs/hint.md"])

        assert cs.changes[0].path == "forge-demo/x.md"


class TestAuthoritativeReadErrorType:
    def test_is_a_materialization_error_so_existing_blocks_apply(self):
        from forge.repository.changeset import MaterializationError as ME

        err = AuthoritativeReadError("src/app.py at base1234 read forbidden: 403")
        assert isinstance(err, ME)
        assert err.detail == "src/app.py at base1234 read forbidden: 403"
        assert str(err).startswith("authoritative_read_failed:")
