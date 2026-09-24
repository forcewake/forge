"""R36-10 (#269) — the hash-locked lane closure: build, verify, enforce.

The wheel was hash-pinned; the ENVIRONMENT was not — pip still resolved
the runtime dependencies at install time. These tests pin the closure
contract end to end:

- the manifest (:class:`LaneClosureManifest`) is the closure identity:
  every artifact name + sha256, the forge wheel identity, the canonical
  resolution command — and its ``closure_digest`` is deterministic over
  identical closures, different on ANY change (a pinned digest and the
  closure it names cannot drift silently apart);
- a REAL build (the local ``uv build`` wheel + a tiny hash-pinned dep
  set fetched with ``--require-hashes``) produces a wheelhouse whose
  manifest verifies green, and two clean builds produce the SAME digest;
- verify mode refuses every corruption BEFORE execution: a missing
  artifact, a tampered one, an UNDECLARED file (the poisoned-cache
  fixture), a manifest that cannot reproduce its own digest, no
  manifest at all;
- the optional ``closure-wheel`` install route resolves from the lane
  env (additive to the R36-07 ladder — conflicts with every other
  explicit source refuse, naming both variables);
- the identity gate binds the INSTALLED set to the pinned closure:
  the wrong closure refuses, and the exact-set fingerprint semantics
  ride ``verify_installed_fingerprints`` (a target-repo lockfile cannot
  replace the collector runtime under ``--no-index``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from forge.adaptive.qualification import (
    CLOSURE_INSTALL_ROUTE,
    CLOSURE_MANIFEST_FILENAME,
    CLOSURE_MANIFEST_SCHEMA,
    FORGE_LANE_CLOSURE_DIR_ENV,
    FORGE_LANE_CLOSURE_SHA256_ENV,
    ClosureArtifact,
    ClosureInstallRoute,
    ClosureVerificationError,
    FingerprintMismatch,
    LaneClosureManifest,
    LaneInstallRouteConflict,
    closure_digest_of_document,
    closure_pin_of_wheel_name,
    enforce_closure_install,
    resolve_closure_install_route,
    verify_closure_dir,
    write_closure_manifest_file,
)

#: The expected forge wheel/version is DERIVED from the tree (a literal
#: broke on every version bump — the v0.36.0 CI bite).
from forge import __version__ as _TREE_FORGE_VERSION

ROOT = Path(__file__).resolve().parents[1]

#: A tiny, real, hash-pinned dependency set (six 1.17.0 — the same
#: disposable sentinel the pip-download probe used; never a forge
#: dependency of consequence, exactly what a TEST closure wants).
_TINY_REQUIREMENTS = (
    "six==1.17.0 \\\n"
    "    --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274\n"
)

_FORGE_WHEEL = f"forge-{_TREE_FORGE_VERSION}-py3-none-any.whl"
#: A DIFFERENT version for mutation tests (never the tree version — the
#: v0.36.0 bump made the old 0.36.0 "future" a no-op).
_BUMPED_FORGE_VERSION = "999.0.0"
_SIX_WHEEL = "six-1.17.0-py2.py3-none-any.whl"


def _load_closure_script():
    spec = importlib.util.spec_from_file_location(
        "build_lane_closure", ROOT / "scripts" / "build_lane_closure.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_lane_closure"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _artifact(name: str, payload: bytes = b"wheel-bytes") -> ClosureArtifact:
    return ClosureArtifact(name=name, sha256=hashlib.sha256(payload).hexdigest())


def _manifest(**overrides: object) -> LaneClosureManifest:
    fields: dict[str, object] = {
        "forge_wheel": _artifact(_FORGE_WHEEL, b"forge-bytes"),
        "forge_version": _TREE_FORGE_VERSION,
        "forge_source": "promotion-record",
        "resolution_command": (
            "uv export --frozen --no-dev --no-emit-project --format requirements-txt "
            "-o lane-requirements.txt && python -m pip download --require-hashes "
            "-r lane-requirements.txt"
        ),
        "artifacts": (
            _artifact(_FORGE_WHEEL, b"forge-bytes"),
            _artifact(_SIX_WHEEL, b"six-bytes"),
        ),
    }
    fields.update(overrides)
    return LaneClosureManifest(**fields)  # type: ignore[arg-type]


def _closure_dir(tmp_path: Path, payloads: dict[str, bytes] | None = None) -> Path:
    """A materialized wheelhouse: each artifact written with EXACTLY the
    bytes its sha256 pin was computed over, plus the manifest file —
    the green fixture every refusal arm starts from."""
    payload_map = (
        payloads
        if payloads is not None
        else {
            _FORGE_WHEEL: b"forge-bytes",
            _SIX_WHEEL: b"six-bytes",
        }
    )
    artifacts = tuple(
        ClosureArtifact(name=name, sha256=hashlib.sha256(payload).hexdigest())
        for name, payload in sorted(payload_map.items())
    )
    forge = next(a for a in artifacts if a.name == _FORGE_WHEEL)
    manifest = LaneClosureManifest(
        forge_wheel=forge,
        forge_version=_TREE_FORGE_VERSION,
        forge_source="promotion-record",
        resolution_command=_manifest().resolution_command,
        artifacts=artifacts,
    )
    closure = tmp_path / "closure"
    closure.mkdir()
    for name, payload in payload_map.items():
        (closure / name).write_bytes(payload)
    write_closure_manifest_file(closure / CLOSURE_MANIFEST_FILENAME, manifest)
    return closure


# ---------------------------------------------------------------------------
# The manifest contract
# ---------------------------------------------------------------------------


class TestLaneClosureManifest:
    def test_the_manifest_is_the_closure_identity(self):
        manifest = _manifest()
        document = manifest.to_document()
        assert document["schema"] == CLOSURE_MANIFEST_SCHEMA == "forge.lane.closure/1"
        assert document["forge"] == {
            "wheel": {"name": _FORGE_WHEEL, "sha256": manifest.forge_wheel.sha256},
            "version": _TREE_FORGE_VERSION,
            "source": "promotion-record",
        }
        assert [a["name"] for a in document["artifacts"]] == [_FORGE_WHEEL, _SIX_WHEEL]
        assert manifest.pins == (("forge", _TREE_FORGE_VERSION), ("six", "1.17.0"))

    def test_the_digest_is_deterministic_and_change_sensitive(self):
        baseline = _manifest()
        assert baseline.closure_digest == _manifest().closure_digest
        # ANY change is a different closure identity — a different byte
        # in one artifact, a different pin, a different resolution.
        tampered_artifact = replace(
            baseline,
            artifacts=(
                baseline.forge_wheel,
                ClosureArtifact(name=_SIX_WHEEL, sha256="c" * 64),
            ),
        )
        assert tampered_artifact.closure_digest != baseline.closure_digest
        bumped_wheel = _artifact(f"forge-{_BUMPED_FORGE_VERSION}-py3-none-any.whl", b"forge-bytes")
        bumped = _manifest(
            forge_wheel=bumped_wheel,
            forge_version=_BUMPED_FORGE_VERSION,
            artifacts=(bumped_wheel, _artifact(_SIX_WHEEL, b"six-bytes")),
        )
        assert bumped.closure_digest != baseline.closure_digest
        assert (
            replace(baseline, resolution_command="curl ...").closure_digest
            != baseline.closure_digest
        )

    def test_the_freeze_round_trip_reproduces_the_manifest_and_digest(self):
        manifest = _manifest()
        frozen = json.loads(json.dumps(manifest.to_document()))
        thawed = LaneClosureManifest.from_document(frozen)
        assert thawed == manifest
        assert thawed.closure_digest == manifest.closure_digest

    def test_a_document_with_the_wrong_schema_is_refused(self):
        document = _manifest().to_document()
        document["schema"] = "forge.lane.closure/0"
        with pytest.raises(ValueError, match="schema"):
            LaneClosureManifest.from_document(document)

    def test_the_digest_never_covers_itself(self):
        document = _manifest().to_document()
        self_declared = dict(document, closure_digest="f" * 64)
        assert closure_digest_of_document(self_declared) == closure_digest_of_document(document)

    def test_artifacts_must_be_wheels_with_real_digests(self):
        with pytest.raises(ValueError, match="not a wheel"):
            _artifact("forge-0.35.0.tar.gz")
        with pytest.raises(ValueError, match="non-sha256"):
            ClosureArtifact(name=_SIX_WHEEL, sha256="deadbeef")

    def test_artifacts_must_arrive_sorted_by_name(self):
        with pytest.raises(ValueError, match="sorted"):
            _manifest(
                artifacts=(
                    _artifact(_SIX_WHEEL, b"six-bytes"),
                    _artifact(_FORGE_WHEEL, b"forge-bytes"),
                )
            )

    def test_the_forge_wheel_must_be_a_member_of_its_own_closure(self):
        with pytest.raises(ValueError, match="own closure"):
            _manifest(artifacts=(_artifact(_SIX_WHEEL, b"six-bytes"),))

    def test_the_declared_version_agrees_with_the_wheel_name(self):
        with pytest.raises(ValueError, match="disagrees"):
            _manifest(forge_version="0.34.0")

    def test_an_unknown_source_is_a_modelling_error(self):
        with pytest.raises(ValueError, match="vocabulary"):
            _manifest(forge_source="somebody-else")

    def test_wheel_name_pins_parse(self):
        assert closure_pin_of_wheel_name(_SIX_WHEEL) == ("six", "1.17.0")
        assert closure_pin_of_wheel_name("pyjwt-2.10.1-py3-none-any.whl") == ("pyjwt", "2.10.1")
        with pytest.raises(ValueError, match="pin"):
            closure_pin_of_wheel_name("nonsense.whl")


# ---------------------------------------------------------------------------
# Verify mode: refusals before execution
# ---------------------------------------------------------------------------


class TestVerifyClosureDir:
    def test_a_clean_wheelhouse_verifies_green(self, tmp_path):
        closure = _closure_dir(tmp_path)
        manifest = verify_closure_dir(closure)
        assert manifest.pins == (("forge", _TREE_FORGE_VERSION), ("six", "1.17.0"))

    def test_a_missing_artifact_refuses(self, tmp_path):
        closure = _closure_dir(tmp_path)
        (closure / _SIX_WHEEL).unlink()
        with pytest.raises(ClosureVerificationError) as excinfo:
            verify_closure_dir(closure)
        assert any("missing artifact" in p and _SIX_WHEEL in p for p in excinfo.value.problems)

    def test_a_tampered_artifact_refuses_with_both_digests_named(self, tmp_path):
        closure = _closure_dir(tmp_path)
        (closure / _SIX_WHEEL).write_bytes(b"six-bytes-tampered")
        with pytest.raises(ClosureVerificationError) as excinfo:
            verify_closure_dir(closure)
        problem = excinfo.value.problems[0]
        assert "tampered artifact" in problem and _SIX_WHEEL in problem

    def test_a_poisoned_cache_undeclared_file_refuses(self, tmp_path):
        closure = _closure_dir(tmp_path)
        (closure / "evil-666-py3-none-any.whl").write_bytes(b"planted")
        with pytest.raises(ClosureVerificationError) as excinfo:
            verify_closure_dir(closure)
        assert any("undeclared file" in p and "evil-666" in p for p in excinfo.value.problems)

    def test_a_poisoned_cache_undeclared_directory_refuses(self, tmp_path):
        closure = _closure_dir(tmp_path)
        (closure / "cache").mkdir()
        with pytest.raises(ClosureVerificationError) as excinfo:
            verify_closure_dir(closure)
        assert any("undeclared file" in p and "cache" in p for p in excinfo.value.problems)

    def test_every_problem_surfaces_no_partial_pass(self, tmp_path):
        closure = _closure_dir(tmp_path)
        (closure / _SIX_WHEEL).unlink()  # missing
        (closure / _FORGE_WHEEL).write_bytes(b"forge-bytes-tampered")  # tampered
        (closure / "planted-1.0-py3-none-any.whl").write_bytes(b"x")  # undeclared
        with pytest.raises(ClosureVerificationError) as excinfo:
            verify_closure_dir(closure)
        kinds = ("missing artifact", "tampered artifact", "undeclared file")
        for kind in kinds:
            assert any(kind in problem for problem in excinfo.value.problems), kind

    def test_a_manifest_that_cannot_reproduce_its_own_digest_refuses(self, tmp_path):
        closure = _closure_dir(tmp_path)
        manifest_path = closure / CLOSURE_MANIFEST_FILENAME
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["closure_digest"] = "e" * 64  # the recorded identity, forged
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ClosureVerificationError) as excinfo:
            verify_closure_dir(closure)
        assert any("does not reproduce" in p for p in excinfo.value.problems)

    def test_a_tampered_manifest_body_is_detectable(self, tmp_path):
        """A row edited WITHOUT touching the stored digest: the body no
        longer hashes to the identity recorded beside it."""
        closure = _closure_dir(tmp_path)
        manifest_path = closure / CLOSURE_MANIFEST_FILENAME
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["artifacts"][1]["sha256"] = "d" * 64
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ClosureVerificationError) as excinfo:
            verify_closure_dir(closure)
        assert any("does not reproduce" in p for p in excinfo.value.problems)

    def test_no_manifest_at_all_refuses(self, tmp_path):
        closure = _closure_dir(tmp_path)
        (closure / CLOSURE_MANIFEST_FILENAME).unlink()
        with pytest.raises(ClosureVerificationError, match=CLOSURE_MANIFEST_FILENAME):
            verify_closure_dir(closure)

    def test_an_unparseable_manifest_refuses(self, tmp_path):
        closure = _closure_dir(tmp_path)
        (closure / CLOSURE_MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(ClosureVerificationError, match="unreadable"):
            verify_closure_dir(closure)


# ---------------------------------------------------------------------------
# The real build: local uv build wheel + a tiny hash-pinned dep set
# ---------------------------------------------------------------------------


class TestBuildLaneClosureScript:
    """A REAL end-to-end build against the shipped script: ``uv build``
    produces the forge wheel (deterministically — two builds hash the
    same), a seeded builder pip downloads the tiny pinned set with
    ``--require-hashes``, and the manifest verifies green. Network is
    used for the one sentinel dependency, exactly like the suite's
    existing ``uv venv --seed`` lane-runner fixture."""

    def _build(self, script, tmp_path: Path, name: str):
        requirements = tmp_path / f"{name}-requirements.txt"
        requirements.write_text(_TINY_REQUIREMENTS, encoding="utf-8")
        closure = tmp_path / f"{name}-closure"
        return closure, script.build_closure(ROOT, closure, local=True, requirements=requirements)

    def test_a_real_small_closure_builds_and_verifies(self, tmp_path):
        script = _load_closure_script()
        closure, manifest = self._build(script, tmp_path, "main")
        assert (closure / CLOSURE_MANIFEST_FILENAME).is_file()
        assert [a.name for a in manifest.artifacts] == [_FORGE_WHEEL, _SIX_WHEEL]
        assert manifest.forge_source == "local-uv-build"
        assert manifest.forge_version == _TREE_FORGE_VERSION
        # the canonical, path-free resolution command — reproducible
        assert manifest.resolution_command == script.RESOLUTION_COMMAND
        assert "require-hashes" in manifest.resolution_command
        # every artifact is hash-pinned with its REAL bytes
        for artifact in manifest.artifacts:
            path = closure / artifact.name
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256
        # and the wheelhouse verifies green through the library
        assert verify_closure_dir(closure) == manifest

    def test_two_clean_builds_produce_the_same_closure_digest(self, tmp_path, monkeypatch):
        """The acceptance core: two clean installs of the same inputs
        produce the same declared identity. The second build reuses the
        EXACT wheel bytes the first produced (the working tree is live —
        a sibling edit between two ``uv build`` calls is a different
        SOURCE, and a different closure for different bytes is correct
        behavior, not an unstable digest); the wheel's own byte-level
        build determinism is uv/hatchling's property, verified out of
        band."""
        script = _load_closure_script()
        closure, first = self._build(script, tmp_path, "first")
        frozen = tmp_path / "frozen-wheel"
        frozen.mkdir()
        shutil.copy2(closure / first.forge_wheel.name, frozen / first.forge_wheel.name)
        monkeypatch.setattr(
            script,
            "_forge_wheel_local",
            lambda root, staging: (
                shutil.copy2(frozen / first.forge_wheel.name, staging / first.forge_wheel.name),
                "local-uv-build",
            ),
        )
        _, second = self._build(script, tmp_path, "second")
        assert first.closure_digest == second.closure_digest
        assert first == second

    def test_a_build_refuses_a_populated_directory(self, tmp_path):
        script = _load_closure_script()
        closure = tmp_path / "dirty"
        closure.mkdir()
        (closure / "leftover.whl").write_bytes(b"old")
        with pytest.raises(script.ClosureBuildError, match="not empty"):
            script.build_closure(ROOT, closure, local=True, requirements=None)

    def test_the_cli_verifies_green_and_refuses_tampering(self, tmp_path, capsys):
        script = _load_closure_script()
        closure, _manifest = self._build(script, tmp_path, "cli")
        assert script.main(["--verify", str(closure)]) == 0
        assert "closure verified" in capsys.readouterr().out
        # the tamper arm: one flipped byte → typed refusal, exit 1
        wheel = closure / _SIX_WHEEL
        wheel.write_bytes(wheel.read_bytes() + b"poison")
        assert script.main(["--verify", str(closure)]) == 1
        captured = capsys.readouterr()
        assert "CLOSURE VERIFICATION REFUSED" in captured.err
        assert "tampered artifact" in captured.err and _SIX_WHEEL in captured.err

    def test_the_cli_refuses_a_missing_directory(self, tmp_path, capsys):
        script = _load_closure_script()
        assert script.main(["--verify", str(tmp_path / "ghost")]) == 1
        assert CLOSURE_MANIFEST_FILENAME in capsys.readouterr().err

    def test_an_unpinned_explicit_wheel_url_refuses(self, tmp_path):
        script = _load_closure_script()
        with pytest.raises(script.ClosureBuildError, match="requires --wheel-sha256"):
            script.build_closure(ROOT, tmp_path / "c", wheel_url="https://example.invalid/x.whl")


# ---------------------------------------------------------------------------
# The closure-wheel install route (additive to the R36-07 ladder)
# ---------------------------------------------------------------------------


class TestClosureInstallRoute:
    _ENV = {FORGE_LANE_CLOSURE_SHA256_ENV: "c" * 64, FORGE_LANE_CLOSURE_DIR_ENV: "/opt/closure"}

    def test_unselected_when_the_pin_is_absent_or_empty(self):
        for env in ({}, {FORGE_LANE_CLOSURE_SHA256_ENV: ""}):
            route = resolve_closure_install_route(env, forge_version="0.35.0")
            assert route == ClosureInstallRoute(
                selected=False, mode="", closure_dir="", closure_digest="", install_argv=()
            )
            assert route.receipt_route == ""

    def test_selected_routes_install_offline_from_the_wheelhouse(self):
        route = resolve_closure_install_route(self._ENV, forge_version="0.35.0")
        assert route.selected and route.mode == CLOSURE_INSTALL_ROUTE == "closure-wheel"
        assert route.install_argv == (
            "pip",
            "install",
            "--no-index",  # pip can never consult a registry
            "--find-links",
            "/opt/closure",
            "forge==0.35.0",
        )
        assert route.receipt_route == "closure-wheel"

    def test_a_malformed_digest_refuses(self):
        with pytest.raises(ValueError, match="64-hex sha256"):
            resolve_closure_install_route(
                {FORGE_LANE_CLOSURE_SHA256_ENV: "deadbeef", FORGE_LANE_CLOSURE_DIR_ENV: "/x"},
                forge_version="0.35.0",
            )

    def test_a_digest_without_a_wheelhouse_refuses(self):
        with pytest.raises(ValueError, match="does not name the wheelhouse"):
            resolve_closure_install_route(
                {FORGE_LANE_CLOSURE_SHA256_ENV: "c" * 64}, forge_version="0.35.0"
            )

    @pytest.mark.parametrize(
        "conflict",
        [
            {
                FORGE_LANE_CLOSURE_SHA256_ENV: "c" * 64,
                FORGE_LANE_CLOSURE_DIR_ENV: "/x",
                "FORGE_LANE_DEV_SOURCE_INSTALL": "true",
                "FORGE_LANE_REF": "feature-x",
            },
            {
                FORGE_LANE_CLOSURE_SHA256_ENV: "c" * 64,
                FORGE_LANE_CLOSURE_DIR_ENV: "/x",
                "FORGE_LANE_WHEEL": "https://example.invalid/forge-8.8.8.whl",
            },
            {
                FORGE_LANE_CLOSURE_SHA256_ENV: "c" * 64,
                FORGE_LANE_CLOSURE_DIR_ENV: "/x",
                "FORGE_LANE_REF": "v9.9.9",
            },
        ],
    )
    def test_every_other_explicit_source_conflicts(self, conflict):
        with pytest.raises(LaneInstallRouteConflict) as excinfo:
            resolve_closure_install_route(conflict, forge_version="0.35.0")
        message = str(excinfo.value)
        assert "contradictory lane install routes" in message
        assert "never silently ignore" in message

    def test_the_off_spellings_of_the_dev_flag_do_not_conflict(self):
        for off in ("false", "0"):
            route = resolve_closure_install_route(
                dict(self._ENV, FORGE_LANE_DEV_SOURCE_INSTALL=off), forge_version="0.35.0"
            )
            assert route.selected

    def test_a_versionless_install_refuses(self):
        with pytest.raises(ValueError, match="forge version"):
            resolve_closure_install_route(self._ENV, forge_version="")


# ---------------------------------------------------------------------------
# The identity gate: the installed set vs the pinned closure
# ---------------------------------------------------------------------------


class TestEnforceClosureInstall:
    def test_the_pinned_closure_with_the_exact_installation_passes(self):
        manifest = _manifest()
        installed = {"forge": _TREE_FORGE_VERSION, "six": "1.17.0"}
        match = enforce_closure_install(manifest.closure_digest, manifest, installed)
        assert match.fingerprints == manifest.pins

    def test_a_verified_wheelhouse_that_is_not_the_pinned_closure_refuses(self):
        manifest = _manifest()
        with pytest.raises(ClosureVerificationError, match="not the pinned closure"):
            enforce_closure_install("d" * 64, manifest, {"forge": "0.35.0", "six": "1.17.0"})

    def test_an_unpinnable_expected_digest_refuses(self):
        with pytest.raises(ValueError, match="sha256"):
            enforce_closure_install("not-a-digest", _manifest(), {})

    def test_an_installed_divergence_rides_the_fingerprint_refusal(self):
        """The collector runtime cannot be silently replaced: under
        --no-index pip never sees an index, and any drift that STILL
        lands is refused here — the target repository's own lockfile
        changed forge out from under the lane."""
        manifest = _manifest()
        replaced = {"forge": _TREE_FORGE_VERSION, "six": "1.18.0", "planted": "1.0"}
        with pytest.raises(FingerprintMismatch) as excinfo:
            enforce_closure_install(manifest.closure_digest, manifest, replaced)
        assert {(d.kind, d.name) for d in excinfo.value.divergences} == {
            ("version_mismatch", "six"),
            ("undeclared_installed", "planted"),
        }
