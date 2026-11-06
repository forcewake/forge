"""Read-only release lane, oracle and manifest identity checks."""

import ast
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from tools.release import live_oracle

ORACLE_PATH = "tests/test_forge_release_smoke.py"
IMPLEMENTATION = "forge_release_smoke.py"
ORACLE = (
    b"from forge_release_smoke import answer\n\ndef test_answer():\n    assert answer() == 42\n"
)
GL_INCLUDE = "forge-lane.gitlab-include.yml"


def artifact(manifest: dict, field: str, directory: Path) -> bytes:
    url = urlsplit(manifest[field + "_url"])
    filename = Path(url.path).name
    expected = manifest[field + "_sha256"]
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not re.fullmatch(r"[0-9a-f]{64}", expected)
    ):
        raise ValueError("release artifact identity is invalid")
    if manifest["artifacts"].get(filename) != expected:
        raise ValueError("release asset is outside the immutable manifest")
    body = (directory / filename).read_bytes()
    if len(body) > 2 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != expected:
        raise ValueError("release asset bytes changed")
    return body


def literal_return(raw: bytes, expected: int) -> None:
    tree = ast.parse(raw.decode("utf-8"))
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "answer"
    ]
    if len(functions) != 1:
        raise ValueError("smoke implementation must have one answer function")
    body = [
        node
        for node in functions[0].body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    if (
        len(body) != 1
        or not isinstance(body[0], ast.Return)
        or not isinstance(body[0].value, ast.Constant)
        or type(body[0].value.value) is not int
        or body[0].value.value != expected
    ):
        raise ValueError("smoke did not produce the explicitly requested literal behavior")


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("ambiguous smoke CI configuration")
        result[key] = value
    return result


async def baseline(runtime, connection, repository, manifest: dict, artifacts: Path) -> str:
    if (
        runtime.config.lane_requirements_url != manifest["lane_requirements_url"]
        or runtime.config.lane_requirements_sha256 != manifest["lane_requirements_sha256"]
    ):
        raise ValueError("server is not dispatching the exact lane-only RC requirements")
    if (
        manifest["workflow_commit"] != manifest["commit"]
        or re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]) is None
    ):
        raise ValueError("lane workflow requires the complete release source commit")
    native = runtime.connections.registry.named(repository.key)
    sha = await native.repository.head(native.ref, repository.policy.target_branch)
    if sha is None or await native.repository.read_blob(native.ref, sha, ORACLE_PATH) != ORACLE:
        raise ValueError("sandbox independent oracle differs from the approved fixed behavior")
    if await native.repository.read_blob(native.ref, sha, IMPLEMENTATION) is not None:
        raise ValueError("release sandbox base must have no smoke implementation")
    if (
        repository.policy.write_policy.allowed_globs != (IMPLEMENTATION,)
        or "release-smoke-oracle" not in repository.policy.verification.required_checks
        or "release-smoke-oracle" not in repository.verification_failure_jobs
    ):
        raise ValueError("sandbox must restrict writes and require native oracle failure evidence")
    if connection.provider == "github":
        artifact(manifest, "verification_reporter", artifacts)
        if repository.verification_workflows != (live_oracle.GITHUB_PATH,) or (
            await native.repository.read_blob(native.ref, sha, live_oracle.GITHUB_PATH)
            != live_oracle.github(manifest)
        ):
            raise ValueError("native independent oracle workflow or reporter pin changed")
        expected = artifact(manifest, "github_wrapper", artifacts)
        pinned = (
            manifest["workflow_repository"] + "/.github/workflows/lane.yml@" + manifest["commit"]
        )
        if manifest["github_workflow_ref"] != pinned or pinned.encode() not in expected:
            raise ValueError("release reusable workflow is not source-commit pinned")
        if await native.repository.read_blob(native.ref, sha, repository.lane_workflow) != expected:
            raise ValueError("native target wrapper is not the immutable release wrapper")
    else:
        expected = artifact(manifest, "gitlab_include", artifacts)
        template_url = f"https://raw.githubusercontent.com/{manifest['workflow_repository']}/{manifest['commit']}/ci/lane.gitlab-ci.yml"
        if (
            manifest["gitlab_template_url"] != template_url
            or expected != ("include:\n  - remote: " + json.dumps(template_url) + "\n").encode()
        ):
            raise ValueError("GitLab lane source is not the immutable release commit")
        if await native.repository.read_blob(native.ref, sha, GL_INCLUDE) != expected:
            raise ValueError("native GitLab include differs from the immutable release include")
        raw = await native.repository.read_blob(native.ref, sha, repository.ci_configuration)
        # The dedicated release fixture uses JSON, a strict YAML subset, so no server YAML dependency or heuristic parser is needed.
        document = json.loads(raw, object_pairs_hook=unique_object) if raw is not None else {}
        if document != live_oracle.gitlab():
            raise ValueError("sandbox CI must include only the checked lane include and its oracle")
    return sha
