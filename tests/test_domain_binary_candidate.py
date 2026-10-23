"""Actual Git object oracles for native candidate bytes, modes and journal identity."""

import json
import subprocess

from pydantic import TypeAdapter
import pytest

from forge.domain.blob import BlobContent
from forge.domain.candidate import ValidatedCandidate, validate_candidate_bundle
from forge.domain.git_candidate import candidate_paths, parse_git_candidate
from forge.domain.patch import CandidateError
from forge.domain.paths import resolve_write_policy


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "--quiet", "--template=")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Synthetic fixture")
    return tmp_path


def commit_base(root, path, old, old_mode):
    target = root / path
    if old is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if old_mode == "120000":
            target.symlink_to(old.decode())
        else:
            target.write_bytes(old)
            target.chmod(0o755 if old_mode == "100755" else 0o644)
        git(root, "add", "--", path)
    git(root, "commit", "--allow-empty", "--quiet", "-m", "synthetic base")
    return git(root, "rev-parse", "HEAD").decode().strip()


def write_new(root, path, new, mode):
    target = root / path
    if target.exists() or target.is_symlink():
        target.unlink()
    if new is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            target.symlink_to(new.decode())
        else:
            target.write_bytes(new)
            target.chmod(0o755 if mode == "100755" else 0o644)
    git(root, "add", "-A")


BINARY = bytes(range(256)) * 80


@pytest.mark.parametrize(
    "path,old,new,old_mode,mode",
    [
        ("space name ☃.dat", None, BINARY, "100644", "100644"),
        ("binary.dat", BINARY, BINARY[:100] + b"\x00new\xff" + BINARY[105:], "100644", "100644"),
        ("binary.dat", BINARY, None, "100644", "100644"),
        ("text.dat", b"old\xff\r\n", b"new\xfe\r\n", "100644", "100644"),
        ("script.py", b"old\n", b"new\n", "100755", "100755"),
        ("script.py", b"unchanged\n", b"unchanged\n", "100644", "100755"),
        ("link", None, b"target.py", "100644", "120000"),
        ("link", b"old-target", b"new-target", "120000", "120000"),
        ("link", b"was a regular file", b"target.py", "100644", "120000"),
        ("link", b"old-target", b"new regular file", "120000", "100644"),
    ],
)
def test_git_binary_mode_and_symlink_oracle(repository, path, old, new, old_mode, mode):
    root = repository
    sha = commit_base(root, path, old, old_mode)
    write_new(root, path, new, mode)
    diff = git(root, "diff", "--cached", "--binary", "--full-index", sha)
    assert candidate_paths(diff, base_sha=sha) == (path,)
    base = {path: old} if old is not None else {}
    bundle = parse_git_candidate(diff, base_sha=sha, base_contents=base)
    validated = validate_candidate_bundle(
        bundle,
        base_contents=base,
        base_modes={path: old_mode},
        branch="forge/test",
        commit_message="Change",
        policy=resolve_write_policy(),
    )
    (entry,) = validated.manifest
    assert entry.path == path and entry.content_bytes == new
    assert entry.operation == ("delete" if new is None else "create" if old is None else "modify")
    assert entry.mode == (old_mode if new is None else mode)
    if new is not None:
        oid = git(root, "rev-parse", ":" + path).decode().strip()
        actual = git(root, "cat-file", "blob", oid)
        assert entry.content_bytes == actual
    encoded = TypeAdapter(ValidatedCandidate).dump_json(validated)
    decoded = TypeAdapter(ValidatedCandidate).validate_json(encoded)
    assert decoded.manifest[0].content_bytes == new
    assert decoded == validated


def test_binary_corruption_stale_base_and_journal_roundtrip(repository):
    root = repository
    old, new = BINARY, BINARY[:100] + b"\x00correction\xff" + BINARY[112:]
    sha = commit_base(root, "binary.dat", old, "100644")
    write_new(root, "binary.dat", new, "100644")
    diff = git(root, "diff", "--cached", "--binary", "--full-index", sha)
    assert b"GIT binary patch" in diff
    bundle = parse_git_candidate(diff, base_sha=sha, base_contents={"binary.dat": old})
    with pytest.raises(CandidateError, match="base"):
        validate_candidate_bundle(
            bundle,
            base_contents={"binary.dat": b"wrong"},
            branch="forge/test",
            commit_message="Change",
        )
    with pytest.raises(CandidateError):
        parse_git_candidate(
            diff.replace(b"GIT binary patch", b"GIT binary patch\nliteral 99999999999"),
            base_sha=sha,
            base_contents={"binary.dat": old},
        )
    snapshot = BlobContent(raw=b"\x00\xffexact")
    assert BlobContent.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert json.loads(snapshot.model_dump_json())["raw"] != "\x00\xffexact"


@pytest.mark.asyncio
async def test_effect_journal_preserves_binary_manifest_and_lost_commit(repository, tmp_path):
    from datetime import datetime, timezone
    from uuid import uuid4
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from forge.domain.states import Handler
    from forge.domain.verdicts import Committed, Unknown
    from forge.engine.effects import Effects
    from forge.models.base import Base
    from forge.providers.fake import FakeProvider, Fault
    from forge.store.effects import EffectStore
    from forge.store.records import RunRequest, StepRequest
    from forge.store.runs import RunStore
    from forge.store.steps import StepStore
    from tests.fakes.ports import FixedClock
    from tests.test_ports import REPO

    commit_base(repository, "new.bin", None, "100644")
    write_new(repository, "new.bin", BINARY, "100755")
    diff = git(repository, "diff", "--cached", "--binary", "--full-index", "HEAD")
    fake = FakeProvider(connection_id=REPO.identity.connection_id)
    base = fake.seed_repository(REPO, {})
    bundle = parse_git_candidate(diff, base_sha=base, base_contents={})
    candidate = validate_candidate_bundle(
        bundle, base_contents={}, branch="forge/binary", commit_message="Binary"
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'effects.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        run = await RunStore(factory).admit(
            RunRequest(uuid4().hex, "github", REPO.identity.connection_id, "42", "issue", "1")
        )
        clock = FixedClock(datetime.now(timezone.utc))
        steps = StepStore(factory)
        await steps.enqueue(StepRequest(run.id, Handler.PUBLISH, "publish", "{}", clock.now()))
        (lease,) = await steps.claim_due("worker", clock.now(), 60)
        effects = Effects(EffectStore(factory), fake, fake, fake, fake, fake, fake, clock)
        await effects.ensure_branch(lease, "branch", REPO, "forge/binary", base)
        fake.fail_next("commit", Fault(unknown_after_success=True))
        assert isinstance(
            await effects.commit(lease, "commit", REPO, candidate, base, "marker"), Unknown
        )
        row = await effects.store.get("commit")
        roundtrip = TypeAdapter(ValidatedCandidate).validate_json(
            json.dumps(json.loads(row.payload_json)["candidate"])
        )
        assert (
            roundtrip.manifest[0].content_bytes == BINARY and roundtrip.manifest[0].mode == "100755"
        )
        assert (await effects.recover(row)).state == "adopted"
        adopted = await effects.commit(lease, "commit", REPO, candidate, base, "marker")
        assert isinstance(adopted, Committed)
        assert await fake.read_blob(REPO, adopted.sha, "new.bin") == BINARY
        assert (await fake.read_tree(REPO, adopted.sha)).entries[0].mode == "100755"
        assert sum(name == "commit" for name, _ in fake.calls) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "target",
    [
        b"/etc/passwd",
        b"../../outside",
        b"../.git/config",
        b"../.forge/key",
        b"safe/../../.ssh/id_rsa",
        b"a\x00b",
        b"a\nb",
        b"C:\\secret",
        b"\xff",
    ],
)
def test_symlink_target_is_confined_and_cannot_redirect_to_authority(repository, target):
    root = repository
    sha = commit_base(root, "src/link", None, "100644")
    # Build the exact Git blob directly so even malformed filesystem targets reach
    # the same pure publication boundary without relying on OS symlink checks.
    from forge.domain.candidate import BlobReplacement, CandidateBundle
    import hashlib

    oid = hashlib.sha1(b"blob " + str(len(target)).encode() + b"\x00" + target).hexdigest()
    bundle = CandidateBundle(
        sha,
        "completed",
        (
            BlobReplacement(
                "src/link",
                BlobContent(raw=target),
                "create",
                "120000",
                intended_digest="blob:" + oid,
            ),
        ),
    )
    with pytest.raises(CandidateError, match="symlink"):
        validate_candidate_bundle(
            bundle, base_contents={}, branch="forge/test", commit_message="Link"
        )


def test_symlink_target_may_use_a_confined_relative_parent(repository):
    sha = commit_base(repository, "src/link", None, "100644")
    write_new(repository, "src/link", b"../target.py", "120000")
    diff = git(repository, "diff", "--cached", "--binary", "--full-index", sha)
    bundle = parse_git_candidate(diff, base_sha=sha, base_contents={})
    result = validate_candidate_bundle(
        bundle, base_contents={}, branch="forge/test", commit_message="Link"
    )
    assert result.manifest[0].content_bytes == b"../target.py"
