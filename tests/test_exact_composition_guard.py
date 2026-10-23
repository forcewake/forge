"""Promotion verifies the staged release bytes instead of a qualification grade."""

import copy
import hashlib

import pytest

from tools.release.artifacts import prepare, verify
from tests.test_release_flow import candidate as candidate


def test_the_exact_candidate_passes(candidate):
    directory, manifest = candidate
    verify(manifest, directory)
    assert (
        manifest["wheel_sha256"]
        == hashlib.sha256((directory / "forge-1.0.0-py3-none-any.whl").read_bytes()).hexdigest()
    )


def test_a_changed_candidate_is_refused(candidate):
    directory, manifest = candidate
    (directory / "forge-1.0.0-py3-none-any.whl").write_bytes(b"rebuilt bytes")
    with pytest.raises(ValueError, match="artifact changed"):
        verify(manifest, directory)


@pytest.mark.parametrize(
    "name",
    [
        "forge_protocol-0.1.0-py3-none-any.whl",
        "forge_lane-0.1.0-py3-none-any.whl",
        "forge-1.0.0.tar.gz",
        "lane-requirements.txt",
    ],
)
def test_other_distribution_drift_is_also_refused(candidate, name):
    directory, manifest = candidate
    (directory / name).write_bytes(b"another artifact")
    with pytest.raises(ValueError, match="artifact changed"):
        verify(manifest, directory)


def test_a_missing_artifact_never_qualifies(candidate):
    directory, manifest = candidate
    (directory / "forge-1.0.0-py3-none-any.whl").unlink()
    with pytest.raises(FileNotFoundError):
        verify(manifest, directory)


@pytest.mark.parametrize("name,sha", [("../outside.whl", "a" * 64), ("wheel.whl", "not-a-digest")])
def test_unsafe_or_unbound_artifact_names_are_refused(candidate, name, sha):
    directory, manifest = candidate
    modified = copy.deepcopy(manifest)
    modified["artifacts"] = {name: sha}
    with pytest.raises(ValueError, match="artifact identity"):
        verify(modified, directory)


@pytest.mark.parametrize(
    "field,value",
    [("tag", "v1.0.0-rc.1"), ("commit", "main"), ("image", "ghcr.io/acme/forge:latest")],
)
def test_unpinned_release_identity_is_refused(candidate, field, value):
    directory, manifest = candidate
    args = dict(
        dist=directory,
        repo="acme/forge",
        tag="v1.0.0",
        rc=manifest["rc_tag"],
        commit=manifest["commit"],
        image=manifest["image"],
    )
    args[field] = value
    with pytest.raises(ValueError):
        prepare(**args)
