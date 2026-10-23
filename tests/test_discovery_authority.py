"""Repository discovery cannot manufacture read or write authorization."""

import pytest

from forge.adaptive.discovery_tools import SnapshotToolbox
from forge.adaptive.system_context import WritableTarget, from_project_config
from forge.project_config import ProjectConfig

OWN = WritableTarget(provider="github", repository_id="example/app", ref="a" * 40)


def test_only_explicit_project_neighbors_authorize_reads_and_never_writes():
    profile = from_project_config(
        ProjectConfig(
            neighbors=[
                {
                    "provider": "gitlab",
                    "repository_id": "partner/library",
                    "ref": "b" * 40,
                    "allowed_globs": ["src/**"],
                }
            ]
        ),
        OWN,
    )
    assert len(profile.neighbors) == 1
    assert profile.neighbors[0].ref == "b" * 40
    assert profile.authorize_write("gitlab", "partner/library").code == "read_only_neighbor"
    assert profile.authorize_write("github", "unknown/repo").code == "outside_context"
    assert profile.authorize_write("github", "example/app").allowed


@pytest.mark.parametrize(
    "entry",
    [
        {"provider": "github", "repository_id": "partner/lib", "write": True},
        {"provider": "invented", "repository_id": "partner/lib"},
        {"provider": "github", "repository_id": ""},
    ],
)
def test_ambiguous_authorization_refuses_loudly(entry):
    with pytest.raises(ValueError):
        from_project_config(ProjectConfig(neighbors=[entry]), OWN)


def test_unauthorized_files_are_filtered_before_search_not_only_rendering():
    toolbox = SnapshotToolbox(
        {"src/api.py": "TOKEN public", ".secrets/key": "TOKEN secret"}, allowed_globs=["src/**"]
    )
    result = toolbox.grep("TOKEN")
    assert result["complete"]
    assert [hit["path"] for hit in result["matches"]] == ["src/api.py"]
    with pytest.raises(KeyError):
        toolbox.read_file(".secrets/key")
    assert toolbox.list_paths()["paths"] == ["src/api.py"]
