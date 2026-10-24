"""Publish the exact validated manifest with a native expected-old-ref transaction."""

from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import Sha
from forge.providers.git_process import GitFailure, GitProcess


@dataclass(frozen=True)
class GitTarget:
    url: str
    authorization: str = field(repr=False)
    author: str = "forge"
    email: str = "forge@localhost"

    def __post_init__(self) -> None:
        url = urlsplit(self.url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "\\" in self.url
            or any(ord(c) < 33 or ord(c) == 127 for c in self.url)
            or any(part in (".", "..") for part in url.path.split("/"))
            or not url.path.endswith(".git")
        ):
            raise ValueError("Git publication requires an explicit trusted HTTPS repository")
        if any(ord(c) < 32 or ord(c) == 127 for c in self.author + self.email + self.authorization):
            raise ValueError("Git identity and authorization must be single-line values")
        if not self.authorization or not self.author or "@" not in self.email:
            raise ValueError("Git publication requires an explicit credential and bot identity")


def factory_branch(branch: str) -> str:
    if (
        not branch.startswith("forge/")
        or len(branch) > 300
        or any(ord(c) < 33 or ord(c) == 127 or c in "~^:?*[\\" for c in branch)
        or ".." in branch
        or "@{" in branch
        or branch.endswith(("/", ".", ".lock"))
        or any(
            not part or part.startswith(".") or part.endswith(".lock") for part in branch.split("/")
        )
    ):
        raise ValueError("only an explicit valid factory branch may be published")
    return branch


async def _checked(git: GitProcess, *args: str, data: bytes | None = None) -> bytes:
    result = await git.run(*args, data=data)
    if result.code:
        stale = b"[rejected]" in result.stdout and any(
            reason in result.stdout
            for reason in (b"stale info", b"fetch first", b"non-fast-forward")
        )
        raise GitFailure("git_ref_conflict" if stale else "git_operation_unavailable", stale=stale)
    return result.stdout.strip()


async def publish(
    target: GitTarget, candidate: ValidatedCandidate, expected_head: Sha, marker: str, *, stamp: int
) -> Sha:
    if not isinstance(candidate, ValidatedCandidate):
        raise TypeError("publication requires a ValidatedCandidate")
    branch = factory_branch(candidate.changeset.branch)
    if (
        not re.fullmatch(r"[0-9a-f]{40}", expected_head)
        or candidate.bundle.attempt_base_oid != expected_head
        or not marker
        or len(marker) > 1024
        or marker.strip() != marker
        or "\n" in marker
        or "\r" in marker
        or len(candidate.manifest) > 4096
    ):
        raise ValueError("publication requires an exact base and bounded immutable manifest")
    with tempfile.TemporaryDirectory(prefix="forge-publish-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        git = GitProcess(root, target.authorization, target.author, target.email, stamp)
        await _checked(git, "init", "--quiet", "--template=", "--object-format=sha1")
        await _checked(
            git,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            "--filter=blob:none",
            target.url,
            expected_head,
        )
        fetched = await _checked(git, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if fetched.decode() != expected_head:
            raise GitFailure("fetched_commit_identity_mismatch")
        await _checked(git, "read-tree", expected_head)
        await _materialize(git, candidate)
        tree = await _checked(git, "write-tree")
        message = candidate.changeset.commit_message.rstrip() + "\n\n" + marker + "\n"
        sha = (
            await _checked(
                git, "commit-tree", tree.decode(), "-p", expected_head, data=message.encode()
            )
        ).decode()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitFailure("created_commit_identity_invalid")
        # The Git receiver compares its old ref with this exact value under the
        # native ref lock. The new commit has that value as its only parent.
        await _checked(
            git,
            "push",
            "--porcelain",
            "--no-verify",
            "--no-follow-tags",
            f"--force-with-lease=refs/heads/{branch}:{expected_head}",
            target.url,
            f"{sha}:refs/heads/{branch}",
        )
        return sha


async def _materialize(git: GitProcess, candidate: ValidatedCandidate) -> None:
    blobs = git.root / "blobs"
    blobs.mkdir(mode=0o700)
    paths: list[str] = []
    total = 0
    additions = [entry for entry in candidate.manifest if entry.operation != "delete"]
    for number, entry in enumerate(additions):
        if entry.content_bytes is None or entry.mode not in ("100644", "100755", "120000"):
            raise ValueError("validated manifest must preserve a supported mode and exact bytes")
        body = entry.content_bytes
        total += len(body)
        if total > 16 * 1024 * 1024:
            raise ValueError("publication content bound exceeded")
        path = blobs / str(number)
        path.write_bytes(body)
        path.chmod(0o600)
        paths.append("blobs/" + str(number))
    oids = (
        (
            await _checked(
                git,
                "hash-object",
                "-w",
                "--no-filters",
                "--stdin-paths",
                data=("\n".join(paths) + "\n").encode(),
            )
        )
        .decode()
        .splitlines()
        if paths
        else []
    )
    if len(oids) != len(additions) or any(not re.fullmatch(r"[0-9a-f]{40}", oid) for oid in oids):
        raise GitFailure("created_blob_identity_invalid")
    instructions = bytearray()
    for entry in candidate.manifest:
        instructions.extend(f"0 {'0' * 40}\t{entry.path}\0".encode())
    for entry, oid in zip(additions, oids, strict=True):
        instructions.extend(f"{entry.mode} {oid}\t{entry.path}\0".encode())
    await _checked(git, "update-index", "-z", "--index-info", data=bytes(instructions))
