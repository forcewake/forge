"""Q35-08 — clean lane installation is reproducible from the promoted release.

The shipped GitHub lane template (and its dogfood mirror) may no longer pair
a current controller with a historical lane: the lane version is the pin
GENERATED from the latest archived promotion record, an unset pin refuses
with an upgrade instruction, source installs ride only the explicitly
less-qualified dev override, and the installed identity is verified BEFORE
any model call.

These are TEMPLATE-LEVEL contract tests plus real-bash route executions of
the SHIPPED install fragment (a stubbed pip — the network routes and the
real-pip wheel install are exercised in tests/test_github_actions_template.py,
R32-08). A true EMPTY-RUNNER cold install (no pip state, network restricted
to the intended sources) is #246's production-entry suite — noted here, not
claimed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "ci" / "templates" / "forge-harness.github.yml"
MIRROR = ROOT / ".github" / "workflows" / "forge-harness.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

#: A parameter expansion whose default is a version LITERAL — the exact
#: shape of the retired `:-v0.27.0` fallback. A ref default must now come
#: from the GENERATED pin variable, never from a hardcoded version.
_VERSION_LITERAL_DEFAULT = re.compile(r"\$\{[A-Z_]*REF:-v[0-9]+\.")


def _load_pins_module():
    spec = importlib.util.spec_from_file_location(
        "generate_template_pins", ROOT / "scripts" / "generate_template_pins.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_template_pins"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The rendered contract: no historical fallback, refuse-unset, labeled dev
# override, verified identity, hash-checked wheel
# ---------------------------------------------------------------------------


class TestNoHardcodedFallback:
    def test_no_version_literal_default_survives_in_either_workflow(self):
        for path in (TEMPLATE, MIRROR):
            assert _VERSION_LITERAL_DEFAULT.search(path.read_text()) is None, (
                f"{path.name}: a git-ref default may only come from the generated "
                "FORGE_LANE_PROMOTED_VERSION pin — a hardcoded version literal is "
                "the retired v0.27.0 fallback shape"
            )

    def test_the_stale_v0270_fallback_is_gone(self):
        for path in (TEMPLATE, MIRROR):
            assert "v0.27.0" not in path.read_text()

    def test_the_generated_lane_pin_is_present_and_fence_bounded(self):
        pins = _load_pins_module()
        for path in (TEMPLATE, MIRROR):
            text = path.read_text()
            assert pins.LANE_PIN_BEGIN in text and pins.LANE_PIN_END in text
            assert 'FORGE_LANE_PROMOTED_VERSION="' in text
            # The rendered default IS the promoted version — and the
            # committed template must be exactly what the archive renders.
            assert pins.main(["--root", str(ROOT), "--check"]) == 0

    def test_the_committed_pin_matches_the_latest_archived_record(self):
        pins = _load_pins_module()
        record, _ = pins.latest_record(ROOT)
        for path in (TEMPLATE, MIRROR):
            text = path.read_text()
            assert f'FORGE_LANE_PROMOTED_VERSION="v{record.version}"' in text
            if record.wheel_sha256:
                assert f'FORGE_LANE_PROMOTED_WHEEL_SHA256="{record.wheel_sha256}"' in text
            else:
                # image-only record: the pin says so honestly, never a
                # fabricated wheel pin.
                assert 'FORGE_LANE_PROMOTED_WHEEL_SHA256=""' in text


class TestRefuseUnset:
    def test_an_unset_lane_version_refuses_with_the_upgrade_instruction(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert "no lane version pinned" in run
            # The instruction names the REPAIR, not just the failure.
            assert "generate_template_pins.py" in run
            assert "FORGE_LANE_REF" in run
            assert 'echo "failed" > .forge/bootstrap' in run

    def test_the_refusal_precedes_the_driver_step(self):
        import yaml

        for path in (TEMPLATE, MIRROR):
            doc = yaml.safe_load(path.read_text())
            names = [step.get("name") for step in doc["jobs"]["harness"]["steps"]]
            assert names.index("Render the implementation brief") < names.index(
                "Run harness driver"
            )


class TestDevOverrideIsExplicitlyLessQualified:
    def test_the_override_variable_reaches_the_install_step(self):
        for path in (TEMPLATE, MIRROR):
            text = path.read_text()
            assert "FORGE_LANE_DEV_SOURCE_INSTALL" in text

    def test_the_dev_route_records_qualified_false(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert '"pin":"git-ref-dev"' in run
            assert '"qualified":false' in run

    def test_the_qualified_routes_record_qualified_true(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert '"pin":"git-ref","ref":"%s","qualified":true' in run
            assert (
                '"pin":"wheel","expected_sha256":"%s","actual_sha256":"%s","qualified":true' in run
            )

    def test_the_dev_route_demands_an_explicit_ref(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert "FORGE_LANE_DEV_SOURCE_INSTALL=true requires FORGE_LANE_REF" in run

    def test_a_non_tag_ref_without_the_override_is_refused(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert "is not a released version tag" in run
            assert "less-qualified development route" in run

    def test_the_mirror_defaults_to_the_dev_route_the_template_does_not(self):
        template = _brief_env(TEMPLATE)
        mirror = _brief_env(MIRROR)
        # The dogfood lane tracks main (this repo IS the product) — the dev
        # route with a main default; the SHIPPED template runs the qualified
        # route unless an operator opts into the override.
        assert (
            mirror["FORGE_LANE_DEV_SOURCE_INSTALL"]
            == "${{ vars.FORGE_LANE_DEV_SOURCE_INSTALL || 'true' }}"
        )
        assert mirror["FORGE_LANE_REF"] == "${{ vars.FORGE_LANE_REF || 'main' }}"
        assert (
            template["FORGE_LANE_DEV_SOURCE_INSTALL"] == "${{ vars.FORGE_LANE_DEV_SOURCE_INSTALL }}"
        )
        assert template["FORGE_LANE_REF"] == "${{ vars.FORGE_LANE_REF }}"


class TestInstallIdentityVerification:
    def test_the_installed_identity_is_captured_before_the_driver_runs(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert "import forge; print(forge.__version__)" in run
            assert ".forge/install-identity.json" in run
            # The version gate: expected vs installed, BEFORE model calls.
            assert "FORGE_LANE_INSTALL_IDENTITY_MISMATCH" in run

    def test_the_mismatch_marker_classifies_infra_never_code_repair(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert "never code repair" in run
            assert 'echo "failed" > .forge/bootstrap' in run


class TestWheelSha256Verification:
    def test_the_download_is_hashed_and_refused_before_install(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert 'pip download --no-deps -d .forge/wheel "$FORGE_WHEEL_URL"' in run
            assert "FORGE_LANE_WHEEL_SHA256 must be a 64-hex sha256" in run
            assert "forge wheel hash mismatch" in run
            # The hash check precedes the install of the verified path.
            assert run.index("forge wheel hash mismatch") < run.index('pip install "$WHEEL_FILE"')

    def test_the_promoted_wheel_pin_is_the_default_route(self):
        for path in (TEMPLATE, MIRROR):
            run = _brief_run(path)
            assert 'FORGE_WHEEL_URL="${FORGE_LANE_WHEEL:-$FORGE_LANE_PROMOTED_WHEEL_URL}"' in run
            assert (
                'FORGE_WHEEL_SHA="${FORGE_LANE_WHEEL_SHA256:-$FORGE_LANE_PROMOTED_WHEEL_SHA256}"'
                in run
            )


class TestReleaseWorkflowBuildsTheLaneWheelSet:
    """The authoritative lane distribution is published and recorded."""

    def test_the_workflow_builds_sdist_and_wheel_with_recorded_digests(self):
        text = RELEASE_WORKFLOW.read_text()
        assert "uv build --sdist --wheel --out-dir dist" in text
        assert "wheel_sha256" in text
        assert "sdist_sha256" in text

    def test_the_gate_records_the_lane_artifact_identity(self):
        text = RELEASE_WORKFLOW.read_text()
        assert "--wheel-sha256" in text
        assert "--wheel-url" in text
        assert "--sdist-sha256" in text
        assert "--sdist-url" in text
        # The recorded URL is the release-asset download location.
        assert "releases/download/${{ github.ref_name }}" in text

    def test_the_wheel_set_is_published_as_release_assets_after_qualification(self):
        text = RELEASE_WORKFLOW.read_text()
        assert "lane-dist-v${{ github.ref_name }}" in text
        assert "gh release upload" in text
        # The attach job verifies the published bytes against the digests
        # the promotion record carries — no unverified asset ships.
        assert "Verify the wheel set against the recorded digests" in text


# ---------------------------------------------------------------------------
# The generator: template defaults round-trip from the archived record
# ---------------------------------------------------------------------------


def _fake_root(tmp_path: Path, record: dict, *, template_with_fence: bool = True) -> Path:
    root = tmp_path / "repo"
    version = record["version"]
    evidence = root / "docs" / "releases" / "evidence" / f"v{version}"
    evidence.mkdir(parents=True)
    (evidence / "promotion.json").write_text(json.dumps(record), encoding="utf-8")
    for relpath in (
        "ci/templates/forge-harness.github.yml",
        ".github/workflows/forge-harness.yml",
    ):
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if template_with_fence:
            target.write_text((ROOT / relpath).read_text(), encoding="utf-8")
        else:
            target.write_text(_LEGACY_TEMPLATE, encoding="utf-8")
    return root


def _record_document(
    version: str = "0.35.0",
    *,
    wheel_name: str | None = "forge-0.35.0-py3-none-any.whl",
    wheel_sha: str | None = "e" * 64,
    wheel_url: str | None = None,
) -> dict:
    document: dict = {
        "version": version,
        "image_digest": "sha256:" + "a" * 64,
        "ci_run_id": "77",
        "decision": {"verdict": "promote", "reasons": [], "checks": []},
        "canary": [{"stage": "fresh", "capability": "c", "outcome": "pass", "detail": ""}],
    }
    if wheel_name is not None:
        document["wheel"] = {
            "sdist": None,
            "wheel": {"name": wheel_name, "sha256": wheel_sha},
            "note": "",
        }
        if wheel_sha is not None:
            document["wheel_sha256"] = wheel_sha
        if wheel_url is not None:
            document["wheel_url"] = wheel_url
    else:
        document["wheel"] = {
            "sdist": None,
            "wheel": None,
            "note": "no wheel/sdist built by the release pipeline (image-only release)",
        }
    return document


#: The pre-Q35-08 install preamble (the retired shape): a wheel route with
#: NO generated pin and a version-literal git fallback — what the
#: generator's first-takeover anchor must handle.
_LEGACY_TEMPLATE = """\
name: forge-harness
jobs:
  harness:
    steps:
      - name: Render the implementation brief
        run: |
          mkdir -p .forge
          FORGE_WHEEL_URL="${FORGE_LANE_WHEEL:-}"
          FORGE_WHEEL_SHA="${FORGE_LANE_WHEEL_SHA256:-}"
          if [ -n "$FORGE_WHEEL_URL" ]; then
            pip install "$WHEEL_FILE"
          else
            pip install "forge @ git+https://github.com/forcewake/forge@${FORGE_LANE_REF:-v0.27.0}"
          fi
"""


class TestPinsGeneratorRendersTemplateDefaults:
    def test_the_template_defaults_round_trip_from_an_archived_record(self, tmp_path):
        pins = _load_pins_module()
        root = _fake_root(
            tmp_path,
            _record_document(
                wheel_url="https://github.com/forcewake/forge/releases/download/"
                "v0.35.0/forge-0.35.0-py3-none-any.whl"
            ),
        )
        assert pins.main(["--root", str(root), "--templates"]) == 0
        text = (root / "ci/templates/forge-harness.github.yml").read_text()
        assert 'FORGE_LANE_PROMOTED_VERSION="v0.35.0"' in text
        assert 'FORGE_LANE_PROMOTED_WHEEL_SHA256="' + "e" * 64 + '"' in text
        assert "releases/download/v0.35.0/forge-0.35.0-py3-none-any.whl" in text
        # Idempotent + drift-detected (the CI guard).
        assert pins.main(["--root", str(root), "--templates"]) == 0
        assert pins.main(["--root", str(root), "--templates", "--check"]) == 0
        drifted = text.replace("v0.35.0", "v0.34.0")
        (root / "ci/templates/forge-harness.github.yml").write_text(drifted, encoding="utf-8")
        assert pins.main(["--root", str(root), "--templates", "--check"]) == 1

    def test_the_url_is_derived_when_the_record_carries_only_the_file_identity(self, tmp_path):
        pins = _load_pins_module()
        root = _fake_root(tmp_path, _record_document(wheel_url=None))
        assert pins.main(["--root", str(root), "--templates"]) == 0
        text = (root / "ci/templates/forge-harness.github.yml").read_text()
        assert (
            'FORGE_LANE_PROMOTED_WHEEL_URL="https://github.com/forcewake/forge/'
            'releases/download/v0.35.0/forge-0.35.0-py3-none-any.whl"' in text
        )

    def test_an_image_only_record_pins_the_promoted_tag_and_no_wheel(self, tmp_path):
        pins = _load_pins_module()
        root = _fake_root(tmp_path, _record_document(wheel_name=None))
        assert pins.main(["--root", str(root), "--templates"]) == 0
        text = (root / "ci/templates/forge-harness.github.yml").read_text()
        assert 'FORGE_LANE_PROMOTED_VERSION="v0.35.0"' in text
        assert 'FORGE_LANE_PROMOTED_WHEEL_URL=""' in text
        assert 'FORGE_LANE_PROMOTED_WHEEL_SHA256=""' in text
        assert "image-only" in text  # the honest not-built note, on the record

    def test_the_pre_release_window_keeps_the_last_promoted_version(self, tmp_path):
        pins = _load_pins_module()
        root = _fake_root(
            tmp_path,
            _record_document(
                version="0.35.0",
                wheel_url="https://github.com/forcewake/forge/releases/download/"
                "v0.35.0/forge-0.35.0-py3-none-any.whl",
            ),
        )
        # The tree is being released as 0.36.0 — NEWER than the archive.
        init = root / "src" / "forge"
        init.mkdir(parents=True)
        (init / "__init__.py").write_text('__version__ = "0.36.0"\n', encoding="utf-8")
        assert pins.main(["--root", str(root), "--templates"]) == 0
        text = (root / "ci/templates/forge-harness.github.yml").read_text()
        assert 'FORGE_LANE_PROMOTED_VERSION="v0.35.0"' in text
        assert 'FORGE_LANE_PROMOTED_VERSION="v0.36.0"' not in text

    def test_the_first_takeover_retires_a_version_literal_fallback(self, tmp_path):
        pins = _load_pins_module()
        root = _fake_root(tmp_path, _record_document(wheel_name=None), template_with_fence=False)
        assert pins.main(["--root", str(root), "--templates"]) == 0
        text = (root / "ci/templates/forge-harness.github.yml").read_text()
        assert pins.LANE_PIN_BEGIN in text
        assert 'FORGE_LANE_PROMOTED_VERSION="v0.35.0"' in text
        # The pin is inserted BEFORE the wheel-route preamble the install
        # reads it from.
        assert text.index("FORGE_LANE_PROMOTED_VERSION") < text.index("FORGE_WHEEL_URL=")

    def test_the_default_run_covers_readme_and_templates_together(self, tmp_path):
        pins = _load_pins_module()
        root = _fake_root(tmp_path, _record_document(wheel_name=None))
        (root / "README.md").write_text(_LEGACY_README, encoding="utf-8")
        assert pins.main(["--root", str(root)]) == 0
        assert pins.BEGIN in (root / "README.md").read_text()
        assert (
            'FORGE_LANE_PROMOTED_VERSION="v0.35.0"'
            in (root / "ci/templates/forge-harness.github.yml").read_text()
        )


_LEGACY_README = """\
# forge

## Status

**v0.35.0** — the thing. (`ghcr.io/forcewake/forge:0.35.0`).

## Quick start

```bash
docker run -d --name forge -p 8420:8420 \\
  --env-file .env ghcr.io/forcewake/forge:0.35.0
```
"""


# ---------------------------------------------------------------------------
# Real-bash route executions of the SHIPPED install fragment (stubbed pip)
# ---------------------------------------------------------------------------


def _brief_run(path: Path) -> str:
    import yaml

    doc = yaml.safe_load(path.read_text())
    step = next(
        s
        for s in doc["jobs"]["harness"]["steps"]
        if s.get("name") == "Render the implementation brief"
    )
    return step["run"]


def _brief_env(path: Path) -> dict:
    import yaml

    doc = yaml.safe_load(path.read_text())
    step = next(
        s
        for s in doc["jobs"]["harness"]["steps"]
        if s.get("name") == "Render the implementation brief"
    )
    return step["env"]


def _install_fragment(path: Path) -> str:
    """The install section verbatim: from ``mkdir -p .forge`` through the
    identity gate (everything before the toolchain provisioning)."""
    lines = path.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "mkdir -p .forge")
    end = next(i for i, line in enumerate(lines) if "RUFF_VER=$(" in line)
    block = lines[start:end]
    indent = len(block[0]) - len(block[0].lstrip())
    return "\n".join(line[indent:] if line.strip() else line for line in block) + "\n"


def _rendered_fragment(tmp_path: Path, subdir: str, **record_kwargs) -> str:
    """The install fragment rendered against a CONTROLLED archive.

    The route tests must not depend on which record the live archive
    happens to carry (image-only renders the tag route; a wheel record
    renders the wheel route) — each test pins the route it exercises.
    """
    pins = _load_pins_module()
    base = tmp_path / subdir / "base"
    base.mkdir(parents=True)
    record = _record_document(**record_kwargs)
    # _fake_root builds under <root>/repo; give it its own tmp area
    archive_root = _fake_root(base, record)
    assert pins.main(["--root", str(archive_root), "--templates"]) == 0
    return _install_fragment(archive_root / "ci/templates/forge-harness.github.yml")


@pytest.fixture(scope="session")
def lane_runner(tmp_path_factory):
    """An isolated interpreter with a STUB pip first on PATH: the route
    logic under test never touches the network, while ``python`` stays real
    (the identity capture genuinely imports — or fails to import — forge).
    Built once per session via ``uv venv --seed`` (the repo's toolchain)."""
    import shutil

    root = tmp_path_factory.mktemp("forge-lane-runner")
    uv = shutil.which("uv")
    assert uv, "uv is required to seed the lane runner interpreter"
    seeded = subprocess.run(
        [uv, "venv", "--seed", str(root / "venv")],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert seeded.returncode == 0, seeded.stderr
    stub = root / "bin"
    stub.mkdir()
    (stub / "pip").write_text("#!/bin/sh\nexit 0\n")
    (stub / "pip").chmod((stub / "pip").stat().st_mode | stat.S_IEXEC)
    python = root / "venv" / "bin" / "python"
    return {"STUB": str(stub), "PYTHON": str(python)}


def _run_fragment(
    lane_runner, workdir: Path, script: str, env_extra: dict
) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("FORGE_") and k not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    env.update(env_extra)
    env["PATH"] = f"{lane_runner['STUB']}:{env['PATH']}"
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestInstallFragmentRoutesOnRealBash:
    def test_a_stripped_pin_with_no_variable_refuses_before_any_install(
        self, tmp_path, lane_runner
    ):
        fragment = _rendered_fragment(tmp_path, "stripped-root", wheel_name=None)
        promoted = re.search(r'FORGE_LANE_PROMOTED_VERSION="v([0-9.]+)"', fragment)
        assert promoted is not None
        stripped = fragment.replace(
            f'FORGE_LANE_PROMOTED_VERSION="v{promoted.group(1)}"',
            'FORGE_LANE_PROMOTED_VERSION=""',
        )
        workdir = tmp_path / "stripped"
        workdir.mkdir()
        result = _run_fragment(lane_runner, workdir, stripped, {})
        assert result.returncode != 0
        out = result.stdout + result.stderr
        assert "FORGE_BOOTSTRAP_FAILED" in out
        assert "no lane version pinned" in out
        assert "generate_template_pins.py" in out
        assert (workdir / ".forge" / "bootstrap").read_text() == "failed\n"

    def test_the_qualified_default_installs_the_promoted_tag(self, tmp_path, lane_runner):
        """No overrides at all: the install takes the GENERATED pin's tag,
        records the qualified git-ref route, and — when the importable forge
        reports exactly the pinned version — the identity gate stays open
        (a PYTHONPATH shim stands in for the installed package so the
        comparison is deterministic in any environment)."""
        fragment = _rendered_fragment(tmp_path, "default-root", wheel_name=None)
        pin = re.search(r'FORGE_LANE_PROMOTED_VERSION="v([0-9.]+)"', fragment)
        assert pin is not None
        shim = tmp_path / "shim-default"
        (shim / "forge").mkdir(parents=True)
        (shim / "forge" / "__init__.py").write_text(
            f'__version__ = "{pin.group(1)}"\n', encoding="utf-8"
        )
        workdir = tmp_path / "default"
        workdir.mkdir()
        result = _run_fragment(lane_runner, workdir, fragment, {"PYTHONPATH": str(shim)})
        assert result.returncode == 0, result.stdout + result.stderr
        record = json.loads((workdir / ".forge" / "lane_install.json").read_text())
        assert record["pin"] == "git-ref"
        assert record["qualified"] is True
        assert record["ref"] == f"v{pin.group(1)}"
        identity = json.loads((workdir / ".forge" / "install-identity.json").read_text())
        assert identity["route"] == "git-ref"
        assert identity["expected_version"] == identity["installed_version"] == pin.group(1)

    def test_the_dev_override_installs_source_and_skips_the_version_gate(
        self, tmp_path, lane_runner
    ):
        workdir = tmp_path / "dev"
        workdir.mkdir()
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "dev-root", wheel_name=None),
            {"FORGE_LANE_DEV_SOURCE_INSTALL": "true", "FORGE_LANE_REF": "feature-x"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        record = json.loads((workdir / ".forge" / "lane_install.json").read_text())
        assert record == {"pin": "git-ref-dev", "ref": "feature-x", "qualified": False}
        identity = json.loads((workdir / ".forge" / "install-identity.json").read_text())
        assert identity["route"] == "git-ref-dev"
        assert identity["expected_version"] == ""  # a moving ref is never a pinned identity

    def test_the_dev_override_without_a_ref_refuses(self, tmp_path, lane_runner):
        workdir = tmp_path / "dev-noref"
        workdir.mkdir()
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "dev-noref-root", wheel_name=None),
            {"FORGE_LANE_DEV_SOURCE_INSTALL": "true"},
        )
        assert result.returncode != 0
        assert "requires FORGE_LANE_REF" in result.stdout + result.stderr

    def test_a_non_tag_ref_without_the_override_is_refused(self, tmp_path, lane_runner):
        workdir = tmp_path / "branch"
        workdir.mkdir()
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "branch-root", wheel_name=None),
            {"FORGE_LANE_REF": "main"},
        )
        assert result.returncode != 0
        out = result.stdout + result.stderr
        assert "is not a released version tag" in out
        assert "FORGE_LANE_DEV_SOURCE_INSTALL=true" in out  # the escape hatch is named

    def test_an_installed_version_mismatch_fails_before_model_calls(self, tmp_path, lane_runner):
        """The acceptance negative: the pin names v9.9.9 but the importable
        lane reports something else — the job fails HERE, in the brief step,
        never in the driver step's model calls."""
        workdir = tmp_path / "mismatch"
        workdir.mkdir()
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "mismatch-root", wheel_name=None),
            {"FORGE_LANE_REF": "v9.9.9"},
        )
        assert result.returncode != 0
        out = result.stdout + result.stderr
        assert "FORGE_LANE_INSTALL_IDENTITY_MISMATCH" in out
        assert "never code repair" in out
        identity = json.loads((workdir / ".forge" / "install-identity.json").read_text())
        assert identity["expected_version"] == "9.9.9"

    def test_a_matching_identity_passes_the_gate(self, tmp_path, lane_runner):
        """Positive control: when the importable forge reports the pinned
        version, the gate stays open. A real forge wheel is not needed — a
        PYTHONPATH shim providing ``forge.__version__`` proves the
        comparison honors the INSTALLED bytes, not the pin alone."""
        shim = tmp_path / "shim"
        (shim / "forge").mkdir(parents=True)
        (shim / "forge" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
        workdir = tmp_path / "match"
        workdir.mkdir()
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "match-root", wheel_name=None),
            {"FORGE_LANE_REF": "v9.9.9", "PYTHONPATH": str(shim)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        identity = json.loads((workdir / ".forge" / "install-identity.json").read_text())
        assert identity == {
            "route": "git-ref",
            "expected_version": "9.9.9",
            "installed_version": "9.9.9",
        }

    def test_the_mirror_fragment_carries_the_same_route_logic(self, tmp_path, lane_runner):
        for fragment_owner in (TEMPLATE, MIRROR):
            workdir = tmp_path / f"mirror-{fragment_owner.name}"
            workdir.mkdir()
            live = _install_fragment(fragment_owner)
            promoted = re.search(r'FORGE_LANE_PROMOTED_VERSION="v([0-9.]+)"', live)
            assert promoted is not None, "the live pin must always name a version"
            # Strip the WHOLE generated pin (version + wheel identity): the
            # route refusal differs (tag route vs wheel route), the
            # no-pin refusal must fire either way.
            stripped = live.replace(
                f'FORGE_LANE_PROMOTED_VERSION="v{promoted.group(1)}"',
                'FORGE_LANE_PROMOTED_VERSION=""',
            ).replace(
                f'FORGE_LANE_PROMOTED_WHEEL_URL="https://github.com/forcewake/forge/'
                f'releases/download/v{promoted.group(1)}/forge-{promoted.group(1)}-py3-none-any.whl"',
                'FORGE_LANE_PROMOTED_WHEEL_URL=""',
            )
            stripped = re.sub(
                r'FORGE_LANE_PROMOTED_WHEEL_SHA256="[0-9a-f]+"',
                'FORGE_LANE_PROMOTED_WHEEL_SHA256=""',
                stripped,
            )
            result = _run_fragment(lane_runner, workdir, stripped, {})
            assert result.returncode != 0
            assert "no lane version pinned" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The promotion record: additive lane-artifact identity fields
# ---------------------------------------------------------------------------


class TestPromotionRecordLaneArtifactFields:
    def test_the_archived_image_only_records_parse_with_the_fields_absent(self):
        from forge.release_promotion import load_promotion_records

        records = {r.version: r for r in load_promotion_records(ROOT)}
        # v0.33.0 and v0.34.0 archived BEFORE the wheel set existed — the
        # additive fields parse to None (honestly not built), exactly like
        # the image-only note, and the record still loads.
        for version in ("0.33.0", "0.34.0"):
            assert version in records, f"archived v{version} record missing"
            record = records[version]
            assert record.wheel_sha256 is None
            assert record.sdist_sha256 is None
            assert record.wheel_url is None
            assert record.sdist_url is None
            assert record.wheel.wheel is None  # the legacy shape agrees

    def test_a_record_with_the_fields_round_trips(self, tmp_path):
        from forge.release_promotion import (
            FileIdentity,
            PromotionDecision,
            PromotionRecord,
            WheelIdentity,
            archive_release_evidence,
            load_promotion_records,
        )

        record = PromotionRecord(
            version="0.35.0",
            image_ref="ghcr.io/forcewake/forge",
            image_digest="sha256:" + "a" * 64,
            wheel=WheelIdentity(
                sdist=FileIdentity("forge-0.35.0.tar.gz", "f" * 64),
                wheel=FileIdentity("forge-0.35.0-py3-none-any.whl", "e" * 64),
            ),
            wheel_sha256="e" * 64,
            sdist_sha256="f" * 64,
            wheel_url="https://github.com/forcewake/forge/releases/download/v0.35.0/forge-0.35.0-py3-none-any.whl",
            sdist_url="https://github.com/forcewake/forge/releases/download/v0.35.0/forge-0.35.0.tar.gz",
            decision=PromotionDecision(verdict="promote"),
        )
        document = record.to_json()
        assert document["wheel_sha256"] == "e" * 64
        assert document["sdist_sha256"] == "f" * 64
        archive_release_evidence("0.35.0", tmp_path, record=record)
        loaded = load_promotion_records(tmp_path)[0]
        assert loaded.wheel_sha256 == "e" * 64
        assert loaded.sdist_sha256 == "f" * 64
        assert loaded.wheel_url and loaded.wheel_url.endswith("forge-0.35.0-py3-none-any.whl")

    def test_a_record_without_the_fields_serializes_them_as_none(self):
        from forge.release_promotion import PromotionDecision, PromotionRecord

        document = PromotionRecord(
            version="0.34.0",
            image_ref="r",
            image_digest="d",
            decision=PromotionDecision(verdict="promote"),
        ).to_json()
        assert document["wheel_sha256"] is None
        assert document["sdist_sha256"] is None
        assert document["wheel_url"] is None
        assert document["sdist_url"] is None


class TestInstallFragmentWheelRouteOnRealBash:
    """The wheel route — exercised the moment the live archive carries a
    wheel-bearing record (as v0.35.0 does): exactly-one-wheel, sha256
    verification, the sdist refusal, and the identity gate."""

    def test_a_matching_wheel_sha_installs_qualified(self, tmp_path, lane_runner):
        import hashlib

        wheel_bytes = b"PEFkeitenwheel-bytes"
        sha = hashlib.sha256(wheel_bytes).hexdigest()
        workdir = tmp_path / "wheel-ok"
        workdir.mkdir()
        (workdir / ".forge" / "wheel").mkdir(parents=True)
        (workdir / ".forge" / "wheel" / "forge-9.8.7-py3-none-any.whl").write_bytes(wheel_bytes)
        shim = tmp_path / "shim-wheel"
        (shim / "forge").mkdir(parents=True)
        (shim / "forge" / "__init__.py").write_text('__version__ = "9.8.7"\n', encoding="utf-8")
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(
                tmp_path,
                "wheel-ok-root",
                version="9.8.7",
                wheel_name="forge-9.8.7-py3-none-any.whl",
                wheel_sha=sha,
            ),
            {"PYTHONPATH": str(shim)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        record = json.loads((workdir / ".forge" / "lane_install.json").read_text())
        assert record["pin"] == "wheel"
        assert record["qualified"] is True

    def test_zero_wheels_refuses(self, tmp_path, lane_runner):
        workdir = tmp_path / "wheel-none"
        workdir.mkdir()
        (workdir / ".forge" / "wheel").mkdir(parents=True)
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "wheel-none-root"),
            {},
        )
        assert result.returncode != 0
        assert "exactly one wheel" in result.stdout + result.stderr

    def test_two_wheels_refuse(self, tmp_path, lane_runner):
        workdir = tmp_path / "wheel-two"
        workdir.mkdir()
        (workdir / ".forge" / "wheel").mkdir(parents=True)
        (workdir / ".forge" / "wheel" / "a-py3-none-any.whl").write_bytes(b"a")
        (workdir / ".forge" / "wheel" / "b-py3-none-any.whl").write_bytes(b"b")
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "wheel-two-root"),
            {},
        )
        assert result.returncode != 0
        assert "exactly one wheel" in result.stdout + result.stderr

    def test_a_sha256_mismatch_refuses_before_pip(self, tmp_path, lane_runner):
        workdir = tmp_path / "wheel-badsha"
        workdir.mkdir()
        (workdir / ".forge" / "wheel").mkdir(parents=True)
        (workdir / ".forge" / "wheel" / "forge-9.8.7-py3-none-any.whl").write_bytes(b"tampered")
        result = _run_fragment(
            lane_runner,
            workdir,
            _rendered_fragment(tmp_path, "wheel-badsha-root", version="9.8.7"),
            {},
        )
        assert result.returncode != 0
        out = result.stdout + result.stderr
        assert "sha256" in out and "FORGE_BOOTSTRAP_FAILED" in out
