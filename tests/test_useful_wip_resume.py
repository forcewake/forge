"""R38-05 (#306) — the useful-WIP cross-runner resume machinery (offline).

``scripts/run_useful_wip_resume.py`` carries the drill's OFFLINE-testable
machinery; these pins hold it without a lab, a runner or a paid model call:

- the TASK fixture: exactly THREE file shapes (new/modified/deleted), the
  shipped-template generation (verbatim + ``--require-generation`` guard),
  and the independent oracle committed before any run;
- the OBSERVED-WORK waiter: the checkpoint ``files>0`` predicate, the
  adaptive probe-rung ladder (empty sample advances, useful stops, no
  checkpoint refuses), the GitLab trace anchor parse, and the works-index
  / manifest reads with the content-address verification;
- the EVIDENCE-RECORD schema: every identity the issue demands, the Draft
  -never-merged invariants, the spend cap, and the honest failure outcomes;
- the SPEND accounting: SDK cost preferred, token estimates labeled.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from scripts.run_useful_wip_resume import (
    FALLBACK_PRICE_PER_MTOK,
    PROBE_RUNGS_S,
    RECORD_SCHEMA,
    SUPERSEDED_EMPTY_WIP,
    _seed_app_py,
    _seed_legacy_py,
    build_record,
    ci_yaml,
    driver_phase_started_at,
    evaluate_probe,
    latest_checkpoint_entry,
    manifest_evidence,
    manifest_path,
    new_record,
    probe_rungs,
    read_manifest,
    read_works_index,
    seed_files,
    smoke_oracle_script,
    spend_from_receipts,
    task_shapes,
    trace_anchor_epoch,
    useful_wip,
    validate_record,
    write_record,
)


# ---------------------------------------------------------------------------
# the task fixture — three shapes, oracle committed before any run
# ---------------------------------------------------------------------------


def test_task_shapes_are_exactly_the_three_kinds() -> None:
    shapes = dict(task_shapes())
    assert shapes == {
        "src/utils/text.py": "new",
        "src/app.py": "modified",
        "src/utils/legacy.py": "deleted",
    }


def test_seed_tree_carries_the_deprecated_baseline_the_shapes_transform() -> None:
    files = seed_files("forge-wip-test")
    # the DELETION target exists and the MODIFICATION target still uses it
    assert "from utils.legacy import shout" in files["src/app.py"]
    assert "def shout(" in files["src/utils/legacy.py"]
    # the NEW file does not exist yet
    assert "src/utils/text.py" not in files
    # the oracle and the template ride the seed commit
    assert ".gitlab-ci.yml" in files and "tests/test_text_utils.py" in files


def test_ci_yaml_inlines_the_shipped_template_plus_the_oracle() -> None:
    yaml = ci_yaml()
    assert "forge-agent-claude-sdk:" in yaml
    assert "--require-generation" in yaml  # the #302 finalization guard
    assert "smoke:" in yaml
    assert "slugify" in yaml
    assert "'$FORGE_RUN_ID'" in yaml  # the oracle skips dispatch pipelines
    # the template body rides VERBATIM — the staged phases survive generation
    assert "FORGE_LANE_OUTCOME:" in yaml
    assert "collect-candidate" in yaml


def test_ci_yaml_refuses_a_template_without_the_302_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_useful_wip_resume as driver

    stale = tmp_path / "claude-sdk-lane.gitlab-ci.yml"
    stale.write_text("forge-agent-claude-sdk:\n  script: ['true']\n", encoding="utf-8")
    monkeypatch.setattr(driver, "TEMPLATE_SOURCE", stale)
    with pytest.raises(Exception, match="require-generation"):
        ci_yaml()


def test_the_oracle_asserts_all_three_shapes() -> None:
    script = smoke_oracle_script()
    assert "from utils.text import slugify" in script
    assert "'legacy' not in app" in script  # app.py rewired
    assert "legacy.py').exists()" in script  # the deletion
    assert "slugify" in script


def test_the_six_frozen_cases_pass_a_reference_slugify() -> None:
    """The frozen cases are self-consistent against the contract's
    reference implementation (the oracle stays the judge; this pins the
    fixture, not the lane)."""
    import re

    from scripts.run_useful_wip_resume import SLUGIFY_CASES

    def slugify(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

    for text, expected in SLUGIFY_CASES:
        assert slugify(text) == expected, (text, slugify(text), expected)


def test_the_oracle_script_fails_on_the_seeded_baseline(tmp_path: Path) -> None:
    """The oracle is a REAL negative control: red before implementation."""
    for rel, content in seed_files("forge-wip-test").items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-c", smoke_oracle_script().split("<<'PY'\n")[1].rsplit("\nPY", 1)[0]],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert completed.returncode != 0, "the oracle must be RED on the seeded baseline"


# ---------------------------------------------------------------------------
# the observed-work waiter
# ---------------------------------------------------------------------------


def test_useful_wip_is_files_above_zero() -> None:
    assert useful_wip({"files": 0}) is False
    assert useful_wip({"files": 1}) is True
    assert useful_wip({"files": "3"}) is True
    assert useful_wip({}) is False
    assert useful_wip({"files": "garbage"}) is False
    assert useful_wip({"files": None}) is False


def test_probe_rungs_are_ordered_and_advancing_needs_a_next_rung() -> None:
    rungs = probe_rungs()
    assert [r.offset_s for r in rungs] == list(PROBE_RUNGS_S)
    assert rungs[0].label == "rung-1@45s"
    # empty sample on a NON-LAST rung advances; on the LAST rung it exhausts
    assert evaluate_probe(rungs[0], {"files": 0})["advance"] is True
    assert evaluate_probe(rungs[-1], {"files": 0})["advance"] is False
    with pytest.raises(ValueError):
        probe_rungs(())


def test_evaluate_probe_verdicts() -> None:
    rung = probe_rungs()[0]
    assert evaluate_probe(rung, None) == {
        "rung": rung.label,
        "verdict": "no-checkpoint",
        "advance": False,
    }
    empty = evaluate_probe(rung, {"files": 0, "checkpoint_id": "a" * 64})
    assert empty["verdict"] == "empty" and empty["advance"] is True
    useful = evaluate_probe(rung, {"files": 2, "checkpoint_id": "b" * 64})
    assert useful["verdict"] == "useful"
    assert useful["checkpoint"]["files"] == 2


def test_the_empty_ladder_advances_until_useful() -> None:
    """The R37-08 failure mode (empty sample) is measured, not guessed: the
    ladder re-enters on the NEXT rung and stops at the first useful one."""
    rungs = probe_rungs()
    samples = [{"files": 0}, {"files": 0}, {"files": 3}]
    verdicts = []
    for rung, sample in zip(rungs, samples, strict=True):
        evaluation = evaluate_probe(rung, sample)
        verdicts.append(evaluation["verdict"])
        if evaluation["verdict"] == "useful":
            break
    assert verdicts == ["empty", "empty", "useful"]


def test_driver_phase_started_at_parses_both_template_spellings() -> None:
    # the SHIPPED (#302) template's driver block begins with mkdir
    new_trace = (
        "2026-09-24T12:27:57.799043Z 01O $ uv pip install --quiet …\n"
        "2026-09-24T12:28:02.515131Z 01O $ mkdir -p .forge # collapsed multi-line command\n"
    )
    assert driver_phase_started_at(new_trace) == "2026-09-24T12:28:02.515131Z"
    # the pre-#302 template's block began with FORGE_DRIVER_EXIT=
    old_trace = (
        '2026-09-24T12:28:02.515132Z 01O $ FORGE_DRIVER_EXIT="completed"'
        " # collapsed multi-line command\n"
    )
    assert driver_phase_started_at(old_trace) == "2026-09-24T12:28:02.515132Z"


def test_driver_phase_started_at_is_silent_before_the_turn() -> None:
    before_script_only = (
        "2026-09-24T12:27:49.171605Z 01O 2.1.273 (Claude Code)\n"
        "2026-09-24T12:27:57.799043Z 01O $ uv pip install --quiet --python …\n"
    )
    assert driver_phase_started_at(before_script_only) is None
    assert driver_phase_started_at("") is None
    # a collapsed block that is NOT the driver phase never anchors
    other = "2026-09-24T12:20:00.000000Z 01O $ echo hi # collapsed multi-line command\n"
    assert driver_phase_started_at(other) is None


def test_trace_anchor_epoch_parses_utc_z_stamps() -> None:
    epoch = trace_anchor_epoch("2026-09-24T12:28:02.515131Z")
    assert epoch == 1790252882.515131


def test_latest_checkpoint_entry_takes_the_highest_sequence() -> None:
    index = {
        "work_id": "w",
        "checkpoints": [
            {"checkpoint_id": "a" * 64, "files": 3, "sequence": 0},
            {"checkpoint_id": "b" * 64, "files": 5, "sequence": 1},
        ],
    }
    entry = latest_checkpoint_entry(index)
    assert entry is not None and entry["checkpoint_id"] == "b" * 64
    assert latest_checkpoint_entry({"work_id": "w", "checkpoints": []}) is None
    assert latest_checkpoint_entry({}) is None


# ---------------------------------------------------------------------------
# the checkpoint store reads — works index + verified manifest
# ---------------------------------------------------------------------------


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_checkpoint_store(tmp_path: Path) -> tuple[str, dict[str, Any]]:
    manifest = {
        "schema": "forge.wip.manifest/2",
        "work_id": "work",
        "sequence": 1,
        "source_oids": {"attempt_base": "0" * 40},
        "files": {
            "src/utils/text.py": {"digest": "c" * 64, "mode": 0o100644, "role": "new"},
            "src/app.py": {"digest": "d" * 64, "mode": 0o100644, "role": "modified"},
        },
        "deletions": ["src/utils/legacy.py"],
    }
    raw = _manifest_bytes(manifest)
    checkpoint_id = hashlib.sha256(raw).hexdigest()
    blob = tmp_path / checkpoint_id[:2]
    blob.mkdir(parents=True, exist_ok=True)
    (blob / checkpoint_id).write_bytes(raw)
    return checkpoint_id, manifest


def test_read_works_index_and_manifest_roundtrip(tmp_path: Path) -> None:
    checkpoint_id, manifest = _write_checkpoint_store(tmp_path)
    works = tmp_path / "works"
    works.mkdir()
    (works / "work.json").write_text(
        json.dumps(
            {
                "work_id": "work",
                "checkpoints": [{"checkpoint_id": checkpoint_id, "files": 2, "sequence": 1}],
            }
        ),
        encoding="utf-8",
    )
    index = read_works_index(tmp_path, "work")
    assert index is not None and index["checkpoints"][0]["checkpoint_id"] == checkpoint_id
    assert read_works_index(tmp_path, "other") is None
    assert read_manifest(tmp_path, checkpoint_id) == manifest


def test_read_manifest_refuses_a_tampered_or_missing_checkpoint(tmp_path: Path) -> None:
    checkpoint_id, _ = _write_checkpoint_store(tmp_path)
    # tamper: same document, one byte of content changed → address mismatch
    path = manifest_path(tmp_path, checkpoint_id)
    path.write_bytes(path.read_bytes().replace(b'"sequence":1', b'"sequence":2'))
    with pytest.raises(Exception, match="does not hash to its own address"):
        read_manifest(tmp_path, checkpoint_id)
    with pytest.raises(Exception, match="absent from the store"):
        read_manifest(tmp_path, "e" * 64)
    with pytest.raises(Exception, match="not a checkpoint content address"):
        manifest_path(tmp_path, "../escape")
    # wrong schema refused
    bad = {"schema": "forge.wip.manifest/1", "work_id": "work", "files": {}, "deletions": []}
    raw = _manifest_bytes(bad)
    bad_id = hashlib.sha256(raw).hexdigest()
    (tmp_path / bad_id[:2]).mkdir(parents=True, exist_ok=True)
    (tmp_path / bad_id[:2] / bad_id).write_bytes(raw)
    with pytest.raises(Exception, match="schema"):
        read_manifest(tmp_path, bad_id)


def test_manifest_evidence_covers_the_three_shapes() -> None:
    checkpoint_id, manifest = _write_checkpoint_store(
        Path(tempfile.mkdtemp())
    )  # manifest dict only
    assert checkpoint_id
    evidence = manifest_evidence(manifest)
    assert evidence["file_count"] == 2
    assert evidence["deletions"] == ["src/utils/legacy.py"]
    assert evidence["digests_hex64"] is True
    assert evidence["shape_coverage"] == {
        "src/utils/text.py": "present",
        "src/app.py": "present",
        "src/utils/legacy.py": "deleted",
    }
    assert evidence["all_shapes_present"] is True


def test_manifest_evidence_names_missing_and_wrong_roles() -> None:
    evidence = manifest_evidence(
        {
            "files": {
                "src/utils/text.py": {"digest": "c" * 64, "role": "modified"},
                "src/app.py": {"digest": "d" * 64, "role": "new"},
            },
            "deletions": [],
        }
    )
    assert evidence["shape_coverage"]["src/utils/legacy.py"] == "missing"
    assert evidence["shape_coverage"]["src/utils/text.py"] == "wrong-role:modified"
    assert evidence["all_shapes_present"] is False


# ---------------------------------------------------------------------------
# the evidence record — schema, invariants, honest failures
# ---------------------------------------------------------------------------


def _green_record() -> dict[str, Any]:
    record = new_record("forcewake/forge-wip-test", "2026-09-25T00:00:00+00:00")
    record.update(
        {
            "useful_checkpoint": {
                "id": "a" * 64,
                "digest": "a" * 64,
                "shape_coverage": {"src/utils/text.py": "present"},
            },
            "resume": {
                "decision_id": "dec",
                "envelope_digest": "env",
                "pipeline_id": 10,
                "lane_job_id": 20,
            },
            "candidate": {
                "diff_digest": "diff",
                "shapes_present": {
                    "src/utils/text.py": "new",
                    "src/app.py": "modified",
                    "src/utils/legacy.py": "deleted",
                },
            },
            "oracle": {"pipeline_id": 30, "status": "success", "candidate_sha": "sha"},
            "mr": {"url": "https://gitlab/mr/1", "draft": True, "merged": False},
            "spend": {"cap_usd": 2.0, "total_usd": 0.4},
            "failures": [],
            "outcome": "useful-wip-continued",
        }
    )
    return record


def test_a_complete_honest_record_validates_clean(tmp_path: Path) -> None:
    record = _green_record()
    assert validate_record(record) == []
    write_record(record, tmp_path / "record.json")
    written = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert written["schema"] == RECORD_SCHEMA
    assert written["validation_findings"] == []


def test_the_record_references_the_superseded_empty_wip_trace() -> None:
    record = new_record("p", "t")
    superseded = record["superseded_negative_evidence"]
    assert superseded["checkpoint_id"] == SUPERSEDED_EMPTY_WIP["checkpoint_id"]
    assert superseded["files"] == 0
    assert "negative" in superseded["note"]


def test_validate_record_demands_every_identity() -> None:
    record = _green_record()
    del record["resume"]["decision_id"]
    findings = validate_record(record)
    assert any("resume.decision_id" in f for f in findings)


def test_validate_record_holds_the_merge_and_draft_invariants() -> None:
    merged = _green_record()
    merged["mr"]["merged"] = True
    assert any("MERGED" in f for f in validate_record(merged))
    non_draft = _green_record()
    non_draft["mr"]["draft"] = False
    assert any("not a Draft" in f for f in validate_record(non_draft))


def test_validate_record_holds_the_spend_cap() -> None:
    over = _green_record()
    over["spend"]["total_usd"] = 2.5
    assert any("cap" in f for f in validate_record(over))


def test_validate_record_demands_the_candidate_shapes() -> None:
    wrong = _green_record()
    wrong["candidate"]["shapes_present"]["src/utils/legacy.py"] = "missing"
    assert any("src/utils/legacy.py" in f for f in validate_record(wrong))


def test_validate_record_demands_failures_behind_failing_outcomes() -> None:
    for outcome in ("empty-wip-exhausted", "restore-failed", "model-no-op", "resumed-turn-failed"):
        record = new_record("p", "t")
        record.update({"outcome": outcome, "failures": [{"reason": "diagnosed"}]})
        assert validate_record(record) == [], outcome
        record["failures"] = []
        assert validate_record(record), outcome


def test_validate_record_refuses_unknown_and_incomplete_outcomes() -> None:
    record = new_record("p", "t")
    assert any("incomplete" in f for f in validate_record(record))
    record["outcome"] = "mystery"
    assert any("unknown outcome" in f for f in validate_record(record))


def test_build_record_folds_the_bundle_honestly(tmp_path: Path) -> None:
    bundle = {
        "created_at": "2026-09-25T00:00:00+00:00",
        "phases": {
            "setup": {
                "project": {"id": 5, "path": "forcewake/forge-wip-test"},
                "seed_commit_sha": "seed",
                "template_sha256": "t" * 64,
            },
            "collect": {
                "worker_envelope_lines": [
                    {
                        "run": "11111111",
                        "attempt": "0",
                        "kind": "fresh",
                        "checkpoint": "none",
                        "decision": "none",
                        "envelope": "c27b34b729ac",
                    },
                    {
                        "run": "11111111",
                        "attempt": "2",
                        "kind": "required",
                        "checkpoint": "a" * 12,
                        "decision": "dec",
                        "envelope": "09f9eacbce97",
                    },
                ]
            },
            "uninterrupted-attempt-1": {
                "dispatches": [{"lane_job_id": 9, "usage_receipt": {"total_cost_usd": 0.1}}]
            },
            "interrupt": {
                "result": "green",
                "finished_at": "2026-09-25T01:00:00+00:00",
                "plan": {"run_id": "1" * 32},
                "probe_ladder": [{"rung": "rung-1@45s", "verdict": "useful"}],
                "useful_checkpoint": {
                    "id": "a" * 64,
                    "digest": "a" * 64,
                    "files": 3,
                    "shape_coverage": {"src/utils/text.py": "present"},
                },
                "blocked_classification": {"status": "blocked", "status_reason": "lane canceled"},
                "resume": {
                    "decision_id": "dec",
                    "envelope_digest": None,  # the live shape: filled by the journal
                    "pipeline_id": 10,
                    "lane_job_id": 20,
                },
                "mr": {
                    "mr_url": "https://gitlab/mr/1",
                    "draft": True,
                    "merged": False,
                    "diff_digest": "diff",
                    "shapes_present": {
                        "src/utils/text.py": "new",
                        "src/app.py": "modified",
                        "src/utils/legacy.py": "deleted",
                    },
                    "changed_paths": ["src/app.py", "src/utils/text.py"],
                    "deleted_paths": ["src/utils/legacy.py"],
                    "verification_pipeline_id": 30,
                    "candidate_sha": "sha",
                },
                "dispatches": [
                    {"lane_job_id": 11, "usage_receipt": {"total_cost_usd": 0.2}},
                    {"lane_job_id": 20, "usage_receipt": {"total_cost_usd": 0.15}},
                ],
            },
        },
        "failures": [],
    }
    record = build_record(bundle)
    assert record["outcome"] == "useful-wip-continued"
    # cross-phase spend: the interrupted arm's legs + the honest attempt-1 leg
    assert record["spend"]["total_usd"] == 0.45
    assert record["spend"]["jobs"].keys() == {"9", "11", "20"}
    # the LAST required-resume envelope names the final leg's identities
    assert record["resume"]["envelope_digest"] == "09f9eacbce97"
    assert record["resume"]["dispatched_checkpoint"] == "a" * 12
    assert record["mr"]["url"] == "https://gitlab/mr/1"
    assert validate_record(record) == []


def test_build_record_maps_every_honest_failure_outcome() -> None:
    base = {
        "created_at": "t",
        "phases": {"setup": {"project": {}}, "interrupt": {}},
        "failures": [],
    }
    for result, outcome in (
        ("empty-wip-exhausted", "empty-wip-exhausted"),
        ("restore-failed", "restore-failed"),
        ("model-no-op", "model-no-op"),
        ("resumed-turn-failed", "resumed-turn-failed"),
        ("spend-guard", "refused"),
        ("no-anchor", "refused"),
    ):
        base["phases"]["interrupt"] = {"result": result}
        base["failures"] = [{"reason": "diagnosed root cause"}]
        assert build_record(base)["outcome"] == outcome


# ---------------------------------------------------------------------------
# the spend accounting
# ---------------------------------------------------------------------------


def test_spend_prefers_the_sdk_cost_field() -> None:
    spend = spend_from_receipts(
        [{"total_cost_usd": 0.2180, "input_tokens": 36125}, {"total_cost_usd": 0.2005}]
    )
    assert spend["total_usd"] == 0.4185
    assert spend["cost_basis"] == "sdk-total_cost_usd"


def test_spend_labels_token_estimates_when_the_sdk_cost_is_absent() -> None:
    spend = spend_from_receipts(
        [{"input_tokens": 1_000_000, "cached_input_tokens": 2_000_000, "output_tokens": 500_000}]
    )
    expected = (
        1_000_000 * FALLBACK_PRICE_PER_MTOK["input"]
        + 2_000_000 * FALLBACK_PRICE_PER_MTOK["cached_input"]
        + 500_000 * FALLBACK_PRICE_PER_MTOK["output"]
    ) / 1_000_000
    assert spend["total_usd"] == round(expected, 4)
    assert spend["cost_basis"] == "sdk-total_cost_usd+token-estimate"


def test_worker_envelope_regex_matches_the_escaped_journal_dashes() -> None:
    """LIVE-found: the worker journal escapes the em dash as \\u2014 inside
    its JSON message — the regex must match BOTH the escaped and the raw
    spelling or the record loses the envelope digest."""
    from scripts.run_useful_wip_resume import _ENVELOPE_LINE_RE

    escaped = (
        '{"timestamp": "2026-09-24T23:04:55.912973+00:00", "level": "INFO", '
        '"logger": "forge.runs.service", "message": "gitlab.dispatch_envelope_digest: '
        "run 04bca389 attempt 2 dispatched a required resume (checkpoint df813ea7496f, "
        "decision 6251bbd5672a) " + "\\u2014" + ' envelope 09f9eacbce97"}'
    )
    raw = (
        "gitlab.dispatch_envelope_digest: run 04bca389 attempt 0 dispatched a fresh "
        "resume (checkpoint none, decision none) \u2014 envelope c27b34b729ac"
    )
    for line in (escaped, raw):
        match = _ENVELOPE_LINE_RE.search(line)
        assert match is not None, line
        assert match.group("envelope") in ("09f9eacbce97", "c27b34b729ac")


def test_spend_on_no_receipts_is_zero() -> None:
    spend = spend_from_receipts([])
    assert spend["total_usd"] == 0.0
    assert spend["receipt_count"] == 0


def test_seeded_app_and_legacy_stay_importable_together() -> None:
    """The seeded baseline is a coherent python tree (the modification
    target compiles against the deletion target until the task rewires)."""
    app = _seed_app_py()
    legacy = _seed_legacy_py()
    assert 'shout(f"hello {name}")' in app
    assert "text.upper() + '!'" in legacy
