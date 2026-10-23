"""Retained authority-read lessons at the lane candidate publication boundary."""

import pytest

from forge.gitlab.blob_reads import BlobReadResult
from forge.gitlab.client import GitLabAPIError
from forge.repository import Change, ChangeSet, Operation
from forge.runs.candidate import CandidateError, bundle_from_changeset
from forge.runs.publisher import _fetch_base_contents, validate_candidate_bundle
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
BASE_SHA = "b" * 40


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_failed_authoritative_read_never_means_absence(status):
    remote = FakeGitLab()

    async def refused(*args, **kwargs):
        raise GitLabAPIError(status, "cannot read pinned base")

    remote.get_file = refused
    with pytest.raises(CandidateError) as raised:
        await _fetch_base_contents(remote, PROJECT_ID, BASE_SHA, ["src/app.py"])
    assert raised.value.reason == "authoritative_read_failed"
    assert remote.calls_of("create_commit") == []


async def test_invalid_utf8_base_refuses_instead_of_replacement_decoding():
    remote = FakeGitLab()
    remote.seed_bytes_file("assets/binary", b"\xff\xfe\x00")
    with pytest.raises(CandidateError, match="incomplete") as raised:
        await _fetch_base_contents(remote, PROJECT_ID, BASE_SHA, ["assets/binary"])
    assert raised.value.reason == "authoritative_read_failed"


async def test_complete_pinned_blob_keeps_the_tail_and_deduplicates_reads():
    remote = FakeGitLab()
    content = "first\n" + "tail\n" * 3000
    remote.seed_file("src/app.py", content)
    actual = await _fetch_base_contents(remote, PROJECT_ID, BASE_SHA, ["src/app.py"] * 2)
    assert actual == {"src/app.py": content}
    assert remote.calls_of("get_file") == [("get_file", (PROJECT_ID, "src/app.py", BASE_SHA))]


async def test_authoritative_size_cap_refuses_without_truncating(monkeypatch):
    from forge.runs import publisher

    monkeypatch.setattr(publisher, "FORGE_MATERIALIZE_MAX_FILE_CHARS", 8)
    remote = FakeGitLab()
    remote.seed_file("src/app.py", "123456789")
    with pytest.raises(CandidateError) as raised:
        await _fetch_base_contents(remote, PROJECT_ID, BASE_SHA, ["src/app.py"])
    assert raised.value.reason == "file_too_large"


async def test_confirmed_absence_permits_create_at_the_publisher():
    remote = FakeGitLab()
    base = await _fetch_base_contents(remote, PROJECT_ID, BASE_SHA, ["forge-demo/new.md"])
    bundle = bundle_from_changeset(
        ChangeSet(
            "untrusted/model-branch",
            "model message",
            [
                Change("forge-demo/new.md", Operation.CREATE, "new\n"),
            ],
        ),
        attempt_base_oid=BASE_SHA,
    )
    validated = validate_candidate_bundle(
        bundle,
        base_contents=base,
        branch="factory/trusted",
        commit_message="trusted message",
    )
    assert validated.changeset.branch == "factory/trusted"
    assert validated.changeset.commit_message == "trusted message"
    assert validated.changeset.changes[0].content == "new\n"


async def test_create_over_an_existing_file_is_refused():
    remote = FakeGitLab()
    remote.seed_file("src/app.py", "keep\n")
    base = await _fetch_base_contents(remote, PROJECT_ID, BASE_SHA, ["src/app.py"])
    bundle = bundle_from_changeset(
        ChangeSet("factory/trusted", "message", [Change("src/app.py", Operation.CREATE, "new\n")]),
        attempt_base_oid=BASE_SHA,
    )
    from forge.runs.publisher import PolicyViolation

    with pytest.raises(PolicyViolation, match="already exists"):
        validate_candidate_bundle(
            bundle, base_contents=base, branch="factory/trusted", commit_message="trusted"
        )


def test_only_provider_confirmed_absence_is_absence():
    assert BlobReadResult.not_found("404").confirmed_absent
    for result in (
        BlobReadResult.forbidden("403"),
        BlobReadResult.unavailable("timeout"),
        BlobReadResult.incomplete("bad bytes"),
    ):
        assert not result.confirmed_absent
