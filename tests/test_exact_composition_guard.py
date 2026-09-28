"""R42-04 (#377) — the build-once guard: promote THOSE exact bytes.

``scripts/generate_template_pins.py`` is a CI runner, not a package: these
tests load it the same way ``tests/test_pg_gate.py`` loads the pg gate.

The guard's one rule, two windows (AC-8 — a changed candidate FAILS the
exact-composition check until re-qualified):

- release time (``--expect-version``/``--expect-wheel-sha256``): the
  just-built wheel must EQUAL the newest strict qualification record's
  wheel for that version;
- tree time: the pending version's candidate must be BOUND and every
  artifact naming it (dist/ when present, else the committed wheel
  receipt; the frozen supported-profile manifest) must BE it;
  post-release, strict records for the promoted version must pin exactly
  the promoted wheel.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_template_pins.py"

spec = importlib.util.spec_from_file_location("template_pins_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
pins = importlib.util.module_from_spec(spec)
sys.modules.setdefault("template_pins_under_test", pins)
spec.loader.exec_module(pins)

#: The qualified candidate's REAL identity: the plain sha256 of its bytes.
CANDIDATE_BYTES = b"the qualified candidate bytes"
A_SHA = hashlib.sha256(CANDIDATE_BYTES).hexdigest()
#: Any other digest (a rebuild, another version) — never the candidate.
B_SHA = "b" * 64


def _promotion(root: Path, version: str, wheel_sha: str) -> None:
    path = root / "docs" / "releases" / "evidence" / f"v{version}" / "promotion.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": version,
                "image_digest": "sha256:" + "0" * 64,
                "decision": {"verdict": "promote", "checks": []},
                "wheel_sha256": wheel_sha,
                "wheel_url": f"https://example.invalid/forge-{version}.whl",
            }
        ),
        encoding="utf-8",
    )


def _record(
    root: Path,
    name: str,
    *,
    version: str,
    wheel_sha: str,
    executed_at: str = "2026-09-28T00:00:00+00:00",
    legacy: bool = False,
) -> None:
    path = root / "qualification" / "records" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "stamp": "forge.profile.qualification/1",
                "release_version": version,
                "wheel_sha256": wheel_sha,
                "executed_at": executed_at,
                "legacy": legacy,
            }
        ),
        encoding="utf-8",
    )


def _tree(root: Path, version: str) -> None:
    init = root / "src" / "forge" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def _wheel(root: Path, version: str, content: bytes) -> None:
    path = root / "dist" / f"forge-{version}-py3-none-any.whl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _receipt(root: Path, version: str, wheel_sha: str) -> None:
    path = root / pins.WORKING_TREE_WHEEL_RECEIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": f"forge-{version}-py3-none-any.whl",
                "path": f"dist/forge-{version}-py3-none-any.whl",
                "sha256": wheel_sha,
                "source": "local-uv-build",
            }
        ),
        encoding="utf-8",
    )


def _freeze(root: Path, version: str, wheel_sha: str | None) -> None:
    path = root / pins.SUPPORTED_PROFILE_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    composition: dict[str, object] = {}
    if wheel_sha is not None:
        composition = {
            "qualification_composition": {
                "wheel": {
                    "name": f"forge-{version}-py3-none-any.whl",
                    "sha256": wheel_sha,
                }
            }
        }
    path.write_text(
        json.dumps(
            {"control_plane": {"executed_lab": dict(composition)}},
        ),
        encoding="utf-8",
    )


class TestReleaseTime:
    def test_a_changed_candidate_is_refused_with_both_digests(self, tmp_path: Path):
        _promotion(tmp_path, "0.42.0", A_SHA)
        _record(tmp_path, "review-loop.json", version="0.42.0", wheel_sha=A_SHA)
        findings = pins.exact_composition_findings(
            tmp_path, expect_version="0.42.0", expect_wheel_sha256=B_SHA
        )
        assert len(findings) == 1
        assert "CHANGED candidate" in findings[0]
        assert A_SHA[:16] in findings[0] and B_SHA[:16] in findings[0]

    def test_the_exact_candidate_passes(self, tmp_path: Path):
        _promotion(tmp_path, "0.42.0", A_SHA)
        _record(tmp_path, "review-loop.json", version="0.42.0", wheel_sha=A_SHA)
        assert (
            pins.exact_composition_findings(
                tmp_path, expect_version="0.42.0", expect_wheel_sha256=A_SHA
            )
            == []
        )

    def test_an_unqualified_version_is_refused(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        findings = pins.exact_composition_findings(
            tmp_path, expect_version="0.42.0", expect_wheel_sha256=A_SHA
        )
        assert len(findings) == 1
        assert "no strict qualification record" in findings[0]

    def test_legacy_records_do_not_qualify_a_candidate(self, tmp_path: Path):
        _promotion(tmp_path, "0.42.0", A_SHA)
        _record(tmp_path, "old.json", version="0.42.0", wheel_sha=A_SHA, legacy=True)
        findings = pins.exact_composition_findings(
            tmp_path, expect_version="0.42.0", expect_wheel_sha256=A_SHA
        )
        assert len(findings) == 1 and "no strict qualification record" in findings[0]

    def test_the_newest_record_for_the_version_wins(self, tmp_path: Path):
        _promotion(tmp_path, "0.42.0", B_SHA)
        _record(
            tmp_path,
            "older.json",
            version="0.42.0",
            wheel_sha=A_SHA,
            executed_at="2026-09-27T00:00:00+00:00",
        )
        _record(
            tmp_path,
            "newer.json",
            version="0.42.0",
            wheel_sha=B_SHA,
            executed_at="2026-09-28T00:00:00+00:00",
        )
        assert (
            pins.exact_composition_findings(
                tmp_path, expect_version="0.42.0", expect_wheel_sha256=B_SHA
            )
            == []
        )


class TestPendingWindow:
    def test_a_pending_version_without_a_record_is_refused(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.42.0")
        findings = pins.exact_composition_findings(tmp_path)
        assert len(findings) == 1
        assert "pending v0.42.0" in findings[0]

    def test_local_dist_bytes_must_be_the_candidate(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.42.0")
        _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=A_SHA)
        _wheel(tmp_path, "0.42.0", CANDIDATE_BYTES)
        assert pins.exact_composition_findings(tmp_path) == []

    def test_drifted_local_dist_bytes_are_refused(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.42.0")
        _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=A_SHA)
        _wheel(tmp_path, "0.42.0", b"a rebuilt candidate - different bytes")
        findings = pins.exact_composition_findings(tmp_path)
        assert len(findings) == 1
        assert "dist/forge-0.42.0" in findings[0]

    def test_the_committed_receipt_covers_a_dist_less_checkout(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.42.0")
        _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=A_SHA)
        _receipt(tmp_path, "0.42.0", A_SHA)
        assert pins.exact_composition_findings(tmp_path) == []

    def test_a_receipt_for_another_version_is_refused(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.42.0")
        _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=A_SHA)
        _receipt(tmp_path, "0.41.0", B_SHA)
        findings = pins.exact_composition_findings(tmp_path)
        assert any("not the pending" in finding for finding in findings)

    def test_a_freeze_binding_a_different_wheel_is_refused(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.42.0")
        _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=A_SHA)
        _wheel(tmp_path, "0.42.0", CANDIDATE_BYTES)
        _freeze(tmp_path, "0.42.0", B_SHA)
        findings = pins.exact_composition_findings(tmp_path)
        assert len(findings) == 1
        assert "frozen supported-profile manifest" in findings[0]

    def test_a_freeze_binding_the_same_wheel_passes(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.42.0")
        _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=A_SHA)
        _wheel(tmp_path, "0.42.0", CANDIDATE_BYTES)
        _freeze(tmp_path, "0.42.0", A_SHA)
        assert pins.exact_composition_findings(tmp_path) == []


class TestPostRelease:
    def test_a_record_pinning_other_bytes_fails_the_promotion(self, tmp_path: Path):
        """The v0.41.0 shape, made mechanical: the promotion shipped one
        wheel while the qualification record pinned another — the guard
        refuses that version until a record binds the promoted bytes."""
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.41.0")
        _record(tmp_path, "loop.json", version="0.41.0", wheel_sha=A_SHA)
        findings = pins.exact_composition_findings(tmp_path)
        assert len(findings) == 1
        assert "loop.json" in findings[0] and B_SHA[:16] in findings[0]

    def test_records_for_other_versions_do_not_trip_it(self, tmp_path: Path):
        _promotion(tmp_path, "0.41.0", B_SHA)
        _tree(tmp_path, "0.41.0")
        _record(tmp_path, "older-loop.json", version="0.40.0", wheel_sha=A_SHA)
        assert pins.exact_composition_findings(tmp_path) == []


def test_the_guard_uses_real_sha256_over_real_bytes(tmp_path: Path):
    """Sanity on the digest axis itself: the pinned value is the plain
    sha256 of the wheel bytes (no prefix, no wrapping)."""
    _promotion(tmp_path, "0.41.0", B_SHA)
    _tree(tmp_path, "0.42.0")
    content = b"real wheel bytes"
    sha = hashlib.sha256(content).hexdigest()
    _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=sha)
    _wheel(tmp_path, "0.42.0", content)
    assert pins.exact_composition_findings(tmp_path) == []


def test_cli_exit_codes(tmp_path: Path, capsys, monkeypatch):
    """The CI step shape: exit 1 on a refusal (stderr names the rule),
    exit 0 on a green composition."""
    _promotion(tmp_path, "0.41.0", B_SHA)
    _tree(tmp_path, "0.42.0")
    monkeypatch.chdir(tmp_path)
    code = pins.main(["--root", str(tmp_path), "--exact-composition"])
    assert code == 1
    assert "R42-04" in capsys.readouterr().err
    _record(tmp_path, "loop.json", version="0.42.0", wheel_sha=A_SHA)
    _wheel(tmp_path, "0.42.0", CANDIDATE_BYTES)
    _freeze(tmp_path, "0.42.0", A_SHA)
    assert pins.main(["--root", str(tmp_path), "--exact-composition"]) == 0


def test_a_missing_store_is_empty_not_fatal(tmp_path: Path):
    assert pins.qualification_wheel_records(tmp_path) == []
    assert pins.newest_qualification_record(tmp_path, "0.42.0") is None
