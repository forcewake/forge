"""Native lane onboarding stays bound to verified bytes and a frozen checkout."""

from pathlib import Path
import json
import os
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("relative", [".github/workflows/lane.yml", "ci/lane.gitlab-ci.yml"])
def test_bootstrap_requires_hashes_and_an_isolated_lane(relative):
    text = (ROOT / relative).read_text()
    assert "sha256sum --check --status" in text
    assert "--require-hashes --only-binary=:all:" in text
    assert "--proto-redir '=https'" in text
    assert 'find_spec("forge") is None' in text
    assert "@github/copilot-linux-" in text and "@1.0.86" in text
    assert "--ignore-scripts" in text


def test_dispatch_is_data_and_the_checkout_uses_the_frozen_base():
    workflow = yaml.load((ROOT / ".github/workflows/lane.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["lane"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == (
        "${{ fromJSON(inputs.dispatch).repair.candidate_sha || fromJSON(inputs.dispatch).base_sha }}"
    )
    assert workflow["jobs"]["lane"]["env"]["DISPATCH_JSON"] == "${{ inputs.dispatch }}"
    assert all("${{ inputs.dispatch }}" not in step.get("run", "") for step in steps)
    gitlab = (ROOT / "ci/lane.gitlab-ci.yml").read_text()
    assert 'git checkout --detach "$base_sha"' in gitlab
    assert "printf '%s'" in gitlab and '"$FORGE_LANE_DISPATCH"' in gitlab


@pytest.mark.parametrize("repair", (False, True))
def test_gitlab_checkout_executes_on_the_exact_authorized_source(tmp_path, repair):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Owned lane fixture")
    git("config", "user.email", "lane@example.invalid")
    file = tmp_path / "source.txt"
    file.write_text("approved base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    file.write_text("published candidate\n")
    git("commit", "-qam", "candidate")
    candidate = git("rev-parse", "HEAD")
    git("checkout", "--detach", base)
    dispatch = {
        "provider": "gitlab",
        "driver": "claude_code",
        "base_sha": base,
        "repair": {"candidate_sha": candidate} if repair else None,
    }
    template = (ROOT / "ci/lane.gitlab-ci.yml").read_text()
    checkout = template[
        template.index('      base_sha="') : template.index("      git remote set-url")
    ]
    subprocess.run(
        ["bash", "-eu", "-c", checkout],
        cwd=tmp_path,
        env={**os.environ, "FORGE_LANE_DISPATCH": json.dumps(dispatch)},
        check=True,
        capture_output=True,
    )
    assert git("rev-parse", "HEAD") == (candidate if repair else base)
    assert file.read_text() == ("published candidate\n" if repair else "approved base\n")
