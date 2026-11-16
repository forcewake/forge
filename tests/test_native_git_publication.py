"""Real Git ref transactions preserve validated modes and cannot overwrite a human tip."""

import asyncio
import hashlib
import os
import subprocess

import pytest

from forge.domain.candidate import (
    BlobReplacement,
    CandidateBundle,
    CreateFile,
    FullReplacement,
    validate_candidate_bundle,
)
from forge.domain.blob import BlobContent
from forge.providers.git_commit import GitTarget, publish
from forge.providers.git_process import GitFailure, GitProcess

URL = "https://fixture.test/owner/repository.git"
BRANCH = "forge/proof"
STAMP = 1792750000


def git(cwd, *args, data=None, raw=False):
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Human",
        "GIT_COMMITTER_NAME": "Human",
        "GIT_AUTHOR_EMAIL": "human@example.test",
        "GIT_COMMITTER_EMAIL": "human@example.test",
    }
    result = subprocess.check_output(["git", *args], cwd=cwd, env=environment, input=data)
    return result if raw else result.strip()


@pytest.fixture
def native(tmp_path, monkeypatch):
    server = tmp_path / "native.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(tmp_path, "init", "--bare", "--quiet", str(server))
    git(seed, "init", "--quiet", "--template=")
    (seed / "src").mkdir()
    (seed / "src/file.txt").write_text("old\n")
    (seed / "keep.dat").write_bytes(b"\x00untouched\xff\n")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "Initial")
    base = git(seed, "rev-parse", "HEAD").decode()
    git(seed, "push", "--quiet", str(server), "HEAD:refs/heads/main", "HEAD:refs/heads/" + BRANCH)
    original = GitProcess.run
    calls = []
    receiver = tmp_path / "receive-pack"
    receiver.write_text(
        '#!/bin/sh\nunset GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS\nexec git receive-pack "$@"\n'
    )
    receiver.chmod(0o700)

    async def local_transport(self, *args, **kwargs):
        assert all("synthetic-credential" not in arg for arg in args)
        assert "GIT_TRACE" not in self.env
        assert self.env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        pairs = {
            self.env[f"GIT_CONFIG_KEY_{n}"]: self.env[f"GIT_CONFIG_VALUE_{n}"]
            for n in range(int(self.env["GIT_CONFIG_COUNT"]))
        }
        assert pairs["protocol.file.allow"] == "never"
        assert pairs["core.hooksPath"] == "/dev/null"
        assert pairs["http.followRedirects"] == "false"
        calls.append(args)
        replacement = tuple(str(server) if arg == URL else arg for arg in args)
        if args[0] == "push":
            # A real hosted receiver has its own environment; the local file
            # transport otherwise inherits the client's disabled hook setting.
            replacement = (replacement[0], "--receive-pack=" + str(receiver), *replacement[1:])
        saved = self.env
        # Only this fixture changes the URL to its owned bare repo. All production
        # Git construction, object writes, index changes and ref transactions run.
        self.env = dict(saved)
        n = next(
            n
            for n in range(int(saved["GIT_CONFIG_COUNT"]))
            if saved[f"GIT_CONFIG_KEY_{n}"] == "protocol.file.allow"
        )
        self.env[f"GIT_CONFIG_VALUE_{n}"] = "always"
        try:
            return await original(self, *replacement, **kwargs)
        finally:
            self.env = saved

    monkeypatch.setattr(GitProcess, "run", local_transport)
    return server, seed, base, calls


def candidate(base):
    return validate_candidate_bundle(
        CandidateBundle(
            attempt_base_oid=base,
            driver_exit="completed",
            entries=(
                FullReplacement("src/file.txt", "approved\n"),
                CreateFile("src/tool.sh", "#!/bin/sh\nexit 0\n", mode="100755"),
                CreateFile("src/link", "file.txt", mode="120000"),
            ),
        ),
        base_contents={"src/file.txt": "old\n"},
        branch=BRANCH,
        commit_message="Approved change",
    )


async def test_publication_keeps_untouched_blobs_without_a_promisor_filter(tmp_path, monkeypatch):
    server = tmp_path / "partial.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(tmp_path, "init", "--bare", "--quiet", str(server))
    git(server, "config", "uploadpack.allowFilter", "true")
    git(server, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(seed, "init", "--quiet", "--template=")
    (seed / "src").mkdir()
    (seed / "src/file.txt").write_text("old\n")
    (seed / "keep.dat").write_bytes(b"\x00untouched\xff\n")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "Initial")
    base = git(seed, "rev-parse", "HEAD").decode()
    git(seed, "push", "--quiet", str(server), "HEAD:refs/heads/" + BRANCH)
    original = GitProcess.run
    fetches = []

    async def file_transport(self, *args, **kwargs):
        fetches.append(args)
        replacement = tuple(("file://" + str(server)) if arg == URL else arg for arg in args)
        saved = self.env
        self.env = dict(saved)
        slot = next(
            n
            for n in range(int(saved["GIT_CONFIG_COUNT"]))
            if saved[f"GIT_CONFIG_KEY_{n}"] == "protocol.file.allow"
        )
        self.env[f"GIT_CONFIG_VALUE_{slot}"] = "always"
        try:
            return await original(self, *replacement, **kwargs)
        finally:
            self.env = saved

    monkeypatch.setattr(GitProcess, "run", file_transport)
    sha = await publish(
        GitTarget(URL, "Basic synthetic-credential"),
        candidate(base),
        base,
        "<!-- partial-clone -->",
        stamp=STAMP,
    )
    assert not any("--filter=blob:none" in arg for args in fetches for arg in args)
    assert git(server, "show", sha + ":keep.dat", raw=True) == b"\x00untouched\xff\n"
    assert git(server, "show", sha + ":src/file.txt", raw=True) == b"approved\n"


async def test_native_publication_preserves_manifest_modes_bytes_and_parent(native):
    server, _, base, calls = native
    sha = await publish(
        GitTarget(URL, "Basic synthetic-credential"),
        candidate(base),
        base,
        "<!-- exact-effect -->",
        stamp=STAMP,
    )
    assert git(server, "rev-parse", "refs/heads/" + BRANCH).decode() == sha
    assert git(server, "rev-parse", sha + "^").decode() == base
    assert git(server, "show", sha + ":src/file.txt", raw=True) == b"approved\n"
    assert git(server, "show", sha + ":keep.dat", raw=True) == b"\x00untouched\xff\n"
    modes = git(server, "ls-tree", "-r", sha).decode()
    assert "100755 blob" in next(
        line for line in modes.splitlines() if line.endswith("src/tool.sh")
    )
    assert "120000 blob" in next(line for line in modes.splitlines() if line.endswith("src/link"))
    assert git(server, "show", sha + ":src/link") == b"file.txt"
    assert git(server, "log", "-1", "--format=%B", sha).decode().endswith("<!-- exact-effect -->")
    pushes = [args for args in calls if args[0] == "push"]
    assert len(pushes) == 1
    assert f"--force-with-lease=refs/heads/{BRANCH}:{base}" in pushes[0]
    assert "--force" not in pushes[0]
    assert not any("checkout" in args or "merge" in args for args in calls)


async def test_native_publication_updates_binary_bytes_without_text_conversion(native):
    server, _, base, _ = native
    before, after = b"\x00untouched\xff\n", bytes(range(256)) * 20
    proposed = validate_candidate_bundle(
        CandidateBundle(
            attempt_base_oid=base,
            driver_exit="completed",
            entries=(
                BlobReplacement(
                    "keep.dat",
                    BlobContent(raw=after),
                    "modify",
                    base_blob_digest="sha256:" + hashlib.sha256(before).hexdigest(),
                    intended_digest="sha256:" + hashlib.sha256(after).hexdigest(),
                ),
            ),
        ),
        base_contents={"keep.dat": before},
        branch=BRANCH,
        commit_message="Binary update",
    )
    sha = await publish(
        GitTarget(URL, "Basic synthetic-credential"), proposed, base, "binary", stamp=STAMP
    )
    assert git(server, "show", sha + ":keep.dat", raw=True) == after
    assert git(server, "show", base + ":keep.dat", raw=True) == before


def human_commit(seed, server, base):
    (seed / "src/file.txt").write_text("Human edit\n")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "Human edit")
    sha = git(seed, "rev-parse", "HEAD").decode()
    git(seed, "push", "--quiet", str(server), "HEAD:refs/heads/staged-human")
    assert git(seed, "rev-parse", sha + "^").decode() == base
    return sha


async def test_exact_lease_refuses_a_human_tip_even_when_old_base_is_still_available(native):
    server, seed, base, _ = native
    human = human_commit(seed, server, base)
    git(server, "update-ref", "refs/heads/" + BRANCH, human, base)
    with pytest.raises(GitFailure) as error:
        await publish(
            GitTarget(URL, "Basic synthetic-credential"),
            candidate(base),
            base,
            "<!-- stale -->",
            stamp=STAMP,
        )
    assert error.value.stale
    assert git(server, "rev-parse", "refs/heads/" + BRANCH).decode() == human


async def test_human_edit_inside_the_native_ref_write_window_survives(native):
    server, seed, base, _ = native
    human = human_commit(seed, server, base)
    hook = server / "hooks/pre-receive"
    hook.write_text(
        f"#!/bin/sh\nunset GIT_QUARANTINE_PATH\ngit update-ref refs/heads/{BRANCH} {human} {base}\n"
    )
    hook.chmod(0o700)
    with pytest.raises(GitFailure):
        await publish(
            GitTarget(URL, "Basic synthetic-credential"),
            candidate(base),
            base,
            "<!-- window -->",
            stamp=STAMP,
        )
    assert git(server, "rev-parse", "refs/heads/" + BRANCH).decode() == human
    assert git(server, "show", human + ":src/file.txt", raw=True) == b"Human edit\n"


async def test_publication_rejects_default_namespace_before_git(native):
    _, _, base, calls = native
    from dataclasses import replace

    value = candidate(base)
    bad = replace(value, changeset=replace(value.changeset, branch="main"))
    with pytest.raises(ValueError, match="factory branch"):
        await publish(
            GitTarget(URL, "Basic synthetic-credential"), bad, base, "marker", stamp=STAMP
        )
    assert not calls


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/repo.git",
        "https://user:pass@fixture.test/repo.git",
        "https://fixture.test/repo.git?key=secret",
        "https://fixture.test/../repo.git",
    ],
)
def test_native_git_target_has_no_ambient_or_url_credential_fallback(url):
    with pytest.raises(ValueError):
        GitTarget(url, "Basic synthetic-credential")


async def test_git_output_limit_stops_a_running_process(tmp_path):
    import sys

    process = GitProcess(tmp_path, "Basic synthetic", "forge", "forge@example.test", STAMP)
    process.executable = sys.executable
    with pytest.raises(GitFailure, match="git_output_too_large"):
        await process.run(
            "-c",
            "import sys,time;sys.stdout.write('x'*1100000);sys.stdout.flush();time.sleep(30)",
            timeout=2,
        )


async def test_git_timeout_kills_its_owned_child_process_group(tmp_path):
    import sys

    process = GitProcess(tmp_path, "Basic synthetic", "forge", "forge@example.test", STAMP)
    process.executable = sys.executable
    code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',\"import time,pathlib;time.sleep(.4);pathlib.Path('escaped').touch()\"]);time.sleep(30)"
    with pytest.raises(GitFailure, match="unavailable"):
        await process.run("-c", code, timeout=0.15)
    await asyncio.sleep(0.5)
    assert not (tmp_path / "escaped").exists()


async def test_git_cleanup_stops_children_after_the_group_leader_exits(tmp_path):
    import sys

    process = GitProcess(tmp_path, "Basic synthetic", "forge", "forge@example.test", STAMP)
    process.executable = sys.executable
    code = "import subprocess,sys;subprocess.Popen([sys.executable,'-c',\"import time,pathlib;time.sleep(.4);pathlib.Path('escaped').touch()\"])"
    with pytest.raises(GitFailure, match="unavailable"):
        await process.run("-c", code, timeout=0.15)
    await asyncio.sleep(0.5)
    assert not (tmp_path / "escaped").exists()
