"""Native Git uses the same explicit trusted CA as provider HTTPS adapters."""

from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import subprocess
from threading import Thread

import pytest

from forge.providers.git_commit import GitTarget
from forge.providers.git_process import GitProcess
from tests.runtime_fixture import certificate


def git(root, *arguments):
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=synthetic",
            "-c",
            "user.email=synthetic@invalid",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ],
        check=True,
        capture_output=True,
    ).stdout.strip()


@contextmanager
def repository(root):
    source = root / "source"
    source.mkdir()
    git(source, "init", "--quiet", "--template=", "--initial-branch=main")
    (source / "a.txt").write_text("synthetic\n")
    git(source, "add", "a.txt")
    git(source, "commit", "--quiet", "-m", "synthetic")
    commit = git(source, "rev-parse", "HEAD").decode()
    bare = root / "repository.git"
    git(root, "clone", "--quiet", "--bare", str(source), str(bare))
    git(bare, "update-server-info")
    private, public = certificate(root)
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers.get("Authorization") == "Bearer owned-synthetic"
            requests.append(self.path)
            return super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(public, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            "https://127.0.0.1:" + str(server.server_port) + "/repository.git",
            public,
            commit,
            requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.slow
async def test_native_git_verifies_explicit_ca_without_ambient_tls_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    monkeypatch.setenv("GIT_SSL_CAINFO", "/ambient/untrusted/ca.pem")
    with repository(tmp_path) as (url, ca, commit, requests):
        isolated = tmp_path / "git-work"
        isolated.mkdir()
        unknown = GitProcess(
            isolated, "Bearer owned-synthetic", "synthetic", "synthetic@invalid", 1
        )
        assert "GIT_SSL_NO_VERIFY" not in unknown.env and "GIT_SSL_CAINFO" not in unknown.env
        assert (await unknown.run("ls-remote", url, "refs/heads/main")).code != 0
        assert requests == []
        explicit = GitProcess(
            isolated, "Bearer owned-synthetic", "synthetic", "synthetic@invalid", 1, ca_path=ca
        )
        config = {
            explicit.env[f"GIT_CONFIG_KEY_{i}"]: explicit.env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(explicit.env["GIT_CONFIG_COUNT"]))
        }
        assert config["http.sslVerify"] == "true" and config["http.sslCAInfo"] == str(ca)
        result = await explicit.run("ls-remote", url, "refs/heads/main")
        assert result.code == 0 and commit.encode() in result.stdout and requests


def test_native_git_target_refuses_nonabsolute_or_missing_trusted_ca(tmp_path):
    for path in (Path("relative.pem"), tmp_path / "missing.pem"):
        with pytest.raises(ValueError, match="CA"):
            GitTarget("https://native.invalid/repo.git", "Bearer synthetic", ca_path=path)
