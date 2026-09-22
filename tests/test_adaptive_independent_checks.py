"""NXT-26 — independent contract and DB/broker checks: the checker checked.

The suite under test never trusts the lane's own claims; these tests
never trust the suite's. Two groups:

- PURE contract checks against the PUBLISHED ``.forge/candidate.meta.json``
  + ``.forge/candidate.diff`` contract, with the negative cases the
  review demands: a tampered meta key claiming authority, a path-escaping
  diff (``..`` traversal, the reserved ``.forge/`` namespace, absolute and
  windows-absolute paths), the usage zero-lie (a zeroed receipt claiming
  completeness; claimed completeness with no counters), exit/reason
  incoherence and attempt-base mismatches against the pinned base.
- DB integration checks against real sessions — SQLite always, real
  Postgres too when ``FORGE_PG_TEST_URL`` is set (the FI convention: the
  schema comes from the REAL migration chain via ``tests.fi_os.lab``,
  never a second schema factory): the frozen attempt base recomputed from
  durable state, the publication intent's commit against the recorded
  candidate head, branch exclusivity across runs, control-command claims
  their own row contradicts, and the skip-clean path when the table does
  not exist.

The report-shape tests pin the honesty rule: the report is EVIDENCE —
per-check verdicts with one evidence line each, recorded skips — and
never a substitute verdict.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.independent_checks import (
    DB_CHECK_NAMES,
    MEMBER_COMMIT_CHECK,
    SET_CHECK_NAMES,
    ContractCheckSuite,
    DbIntegrationChecks,
    IndependentCheckReport,
    run_independent_checks,
    verify_candidate_set,
)
from forge.adaptive.mailbox_db import ControlCommandRow
from forge.adaptive.models import CandidateSet
from forge.adaptive.workpackage import freeze_candidate_set
from forge.durable.models import FlowRun, MRReservation, PublicationIntent
from forge.models.base import Base

SQLITE_URL = "sqlite+aiosqlite:///:memory:"

BASE_OID = "a" * 40
CANDIDATE_OID = "b" * 40
OTHER_OID = "c" * 40
PINNED_BASE = BASE_OID


# ----------------------------------------------------------------------
# Fixtures and builders
# ----------------------------------------------------------------------


def _meta(**overrides) -> dict:
    """An honest meta: exactly the keys the lane's write_artifacts writes."""
    base = {
        "attempt_base": BASE_OID,
        "driver": "claude-code",
        "model": "claude-sonnet-4-5",
        "exit": "completed",
        "terminal_reason": "completed",
        "usage": None,
    }
    base.update(overrides)
    return base


def _new_file_diff(path: str, content: str = "VALUE = 1\n") -> str:
    lines = "\n".join(f"+{line}" for line in content.splitlines())
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(content.splitlines())} @@\n"
        f"{lines}\n"
    )


def _modify_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 2222222..3333333\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )


def _write_diff(tmp_path: Path, text: str) -> Path:
    diff_path = tmp_path / "candidate.diff"
    diff_path.write_text(text, encoding="utf-8")
    return diff_path


def _database_urls() -> list[str]:
    """SQLite always; real Postgres too when the FI lab URL is set."""
    urls = [SQLITE_URL]
    if os.environ.get("FORGE_PG_TEST_URL"):
        urls.append(os.environ["FORGE_PG_TEST_URL"])
    return urls


def _url_id(url: str) -> str:
    return "postgres" if url.startswith("postgres") else "sqlite"


@pytest.fixture(params=_database_urls(), ids=_url_id)
async def session_factory(request):
    url = request.param
    if url.startswith("postgres"):
        from tests.fi_os.lab import run_migrations

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            present = await conn.run_sync(lambda sync: inspect(sync).has_table("flow_runs"))
        await engine.dispose()
        if not present:
            run_migrations(url)  # the REAL chain — never a second schema factory
            engine = create_async_engine(url)
        # Fresh rows for the tables this suite touches; everything else
        # is left alone (this suite coexists with the FI lab's own runs).
        async with engine.begin() as conn:
            for table in (
                "control_command_deliveries",
                "control_commands",
                "publication_intents",
                "mr_reservations",
                "flow_runs",
            ):
                await conn.execute(text(f"DELETE FROM {table}"))
    else:
        engine = create_async_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        async with engine.begin() as conn:
            # Importing the module under test registered every table below
            # (control_commands included) in the shared Base.metadata.
            await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_run(
    factory,
    *,
    run_id: str = "run-1",
    base_sha: str | None = BASE_OID,
    candidate_shas: list[str] | None = None,
    commit_cycle: int = 1,
) -> None:
    async with factory() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=1,
                status="reviewing",
                base_sha=base_sha,
                candidate_shas=list(candidate_shas or []),
                commit_cycle=commit_cycle,
            )
        )
        await session.commit()


async def _seed_intent(
    factory,
    *,
    run_id: str = "run-1",
    status: str = "committed",
    provider_object_id: str | None = CANDIDATE_OID,
    operation_key: str = "op-1",
) -> None:
    async with factory() as session:
        session.add(
            PublicationIntent(
                run_id=run_id,
                provider="gitlab",
                repo="1",
                target_ref="forge/mr-1",
                idempotency_scope=f"cycle-1-{operation_key}",
                operation_key=operation_key,
                status=status,
                provider_object_id=provider_object_id,
            )
        )
        await session.commit()


async def _seed_reservation(
    factory, *, run_id: str = "run-1", branch: str = "forge/mr-1-run-1", status: str = "open"
) -> None:
    async with factory() as session:
        session.add(MRReservation(flow_run_id=run_id, branch=branch, status=status))
        await session.commit()


def _command_row(**overrides) -> ControlCommandRow:
    values = {
        "id": "cmd-1",
        "work_id": "run-1",
        "run_id": "run-1",
        "kind": "steer",
        "status": "received",
        "sequence": 1,
        "dedup_key": "gitlab-note:1",
        "actor_ref": "human:reviewer-17",
        "actor_origin": "server_authenticated_human",
        "journal": [{"at": "2026-09-21T00:00:00+00:00", "to": "received"}],
    }
    values.update(overrides)
    return ControlCommandRow(**values)


async def _seed_commands(factory, *rows: ControlCommandRow) -> None:
    async with factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()


def _verdicts(results) -> dict[str, str]:
    return {result.check: result.verdict for result in results}


# ----------------------------------------------------------------------
# Pure contract checks — the meta
# ----------------------------------------------------------------------


class TestMetaSchema:
    def test_honest_meta_and_clean_diff_pass_every_contract_check(self, tmp_path):
        diff_path = _write_diff(
            tmp_path, _new_file_diff("src/new_module.py") + _modify_diff("src/exists.py")
        )
        suite = ContractCheckSuite(_meta(), diff_path, expected_attempt_base=PINNED_BASE)

        results = suite.run()

        assert _verdicts(results) == {
            "contract.meta.schema": "pass",
            "contract.meta.exit_coherence": "pass",
            "contract.meta.usage_honesty": "pass",
            "contract.attempt_base.consistency": "pass",
            "contract.diff.parseable": "pass",
            "contract.diff.path_safety": "pass",
            "contract.diff.single_representation": "pass",
        }

    def test_extra_meta_key_claiming_new_authority_fails_schema(self, tmp_path):
        diff_path = _write_diff(tmp_path, _new_file_diff("src/new.py"))
        suite = ContractCheckSuite(_meta(approved=True), diff_path)

        result = suite.check_meta_schema()

        assert result.verdict == "fail"
        assert "'approved'" in result.evidence
        assert "no authority granted" in result.evidence

    def test_missing_required_key_fails_schema(self, tmp_path):
        meta = _meta()
        del meta["exit"]
        suite = ContractCheckSuite(meta, _write_diff(tmp_path, _new_file_diff("src/x.py")))

        assert suite.check_meta_schema().verdict == "fail"
        assert "missing required key 'exit'" in suite.check_meta_schema().evidence

    def test_missing_attempt_base_in_every_spelling_fails_schema(self, tmp_path):
        meta = _meta()
        del meta["attempt_base"]
        suite = ContractCheckSuite(meta, _write_diff(tmp_path, _new_file_diff("src/x.py")))

        result = suite.check_meta_schema()

        assert result.verdict == "fail"
        assert "attempt_base" in result.evidence

    def test_wrongly_typed_key_fails_schema(self, tmp_path):
        suite = ContractCheckSuite(_meta(exit=0), _write_diff(tmp_path, _new_file_diff("src/x.py")))

        result = suite.check_meta_schema()

        assert result.verdict == "fail"
        assert "'exit' must be a string" in result.evidence

    def test_legacy_base_alias_alone_satisfies_the_base_requirement(self, tmp_path):
        meta = _meta()
        del meta["attempt_base"]
        meta["attempt_base_oid"] = BASE_OID
        suite = ContractCheckSuite(meta, _write_diff(tmp_path, _new_file_diff("src/x.py")))

        assert suite.check_meta_schema().verdict == "pass"
        assert suite.check_attempt_base().verdict == "pass"


class TestUsageHonesty:
    def test_null_usage_is_the_honest_unknown(self, tmp_path):
        suite = ContractCheckSuite(_meta(usage=None), tmp_path / "unused.diff")

        result = suite.check_usage_honesty()

        assert result.verdict == "pass"
        assert "unknown stays unknown" in result.evidence

    def test_zeroed_receipt_claiming_completeness_fails(self, tmp_path):
        suite = ContractCheckSuite(
            _meta(
                usage={
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 0,
                    "completeness": "exact",
                }
            ),
            tmp_path / "unused.diff",
        )

        result = suite.check_usage_honesty()

        assert result.verdict == "fail"
        assert "zeroed receipt" in result.evidence

    def test_claimed_completeness_without_counters_fails(self, tmp_path):
        suite = ContractCheckSuite(
            _meta(usage={"completeness": "aggregate"}), tmp_path / "unused.diff"
        )

        result = suite.check_usage_honesty()

        assert result.verdict == "fail"
        assert "no token counters" in result.evidence

    def test_present_but_empty_receipt_object_fails(self, tmp_path):
        suite = ContractCheckSuite(_meta(usage={}), tmp_path / "unused.diff")

        result = suite.check_usage_honesty()

        assert result.verdict == "fail"
        assert "never a zeroed dict" in result.evidence

    def test_negative_token_counter_fails(self, tmp_path):
        suite = ContractCheckSuite(
            _meta(usage={"input_tokens": -5, "completeness": "aggregate"}),
            tmp_path / "unused.diff",
        )

        result = suite.check_usage_honesty()

        assert result.verdict == "fail"
        assert "input_tokens=-5" in result.evidence

    def test_boolean_token_counter_fails(self, tmp_path):
        suite = ContractCheckSuite(_meta(usage={"input_tokens": True}), tmp_path / "unused.diff")

        assert suite.check_usage_honesty().verdict == "fail"

    def test_non_dict_usage_fails(self, tmp_path):
        suite = ContractCheckSuite(_meta(usage=[1, 2]), tmp_path / "unused.diff")

        assert suite.check_usage_honesty().verdict == "fail"

    def test_a_real_receipt_with_fully_cached_input_passes(self, tmp_path):
        # Anthropic-shaped: input EXCLUDES the cache — 0 input with a
        # cache read is a real turn, not a zero-lie.
        suite = ContractCheckSuite(
            _meta(usage={"input_tokens": 0, "cached_input_tokens": 900, "output_tokens": 50}),
            tmp_path / "unused.diff",
        )

        assert suite.check_usage_honesty().verdict == "pass"


class TestExitCoherence:
    @pytest.mark.parametrize(
        ("exit_value", "reason"),
        [
            ("completed", "aborted_user"),
            ("failed", ""),
            ("failed", "   "),
            ("crashed", "completed"),
        ],
    )
    def test_incoherent_exit_pairs_fail(self, tmp_path, exit_value, reason):
        suite = ContractCheckSuite(
            _meta(exit=exit_value, terminal_reason=reason), tmp_path / "unused.diff"
        )

        assert suite.check_exit_coherence().verdict == "fail"

    def test_failed_with_a_reason_is_coherent(self, tmp_path):
        suite = ContractCheckSuite(
            _meta(exit="failed", terminal_reason="budget_exceeded"), tmp_path / "unused.diff"
        )

        assert suite.check_exit_coherence().verdict == "pass"


class TestAttemptBaseConsistency:
    def test_claimed_base_not_matching_the_pinned_base_fails(self, tmp_path):
        suite = ContractCheckSuite(
            _meta(attempt_base=OTHER_OID),
            tmp_path / "unused.diff",
            expected_attempt_base=PINNED_BASE,
        )

        result = suite.check_attempt_base()

        assert result.verdict == "fail"
        assert "pinned base" in result.evidence

    def test_conflicting_base_spellings_fail_whichever_is_true(self, tmp_path):
        suite = ContractCheckSuite(_meta(attempt_base_oid=OTHER_OID), tmp_path / "unused.diff")

        result = suite.check_attempt_base()

        assert result.verdict == "fail"
        assert "disagreeing" in result.evidence

    def test_malformed_base_oid_fails(self, tmp_path):
        suite = ContractCheckSuite(_meta(attempt_base="short"), tmp_path / "unused.diff")

        result = suite.check_attempt_base()

        assert result.verdict == "fail"
        assert "not a full commit OID" in result.evidence

    def test_without_a_pinned_base_only_the_shape_is_checked(self, tmp_path):
        suite = ContractCheckSuite(_meta(), tmp_path / "unused.diff")

        result = suite.check_attempt_base()

        assert result.verdict == "pass"
        assert "shape only" in result.evidence


# ----------------------------------------------------------------------
# Pure contract checks — the diff
# ----------------------------------------------------------------------


class TestDiffChecks:
    def test_clean_diff_with_create_and_modify_passes(self, tmp_path):
        diff_path = _write_diff(
            tmp_path, _new_file_diff("src/new_module.py") + _modify_diff("src/exists.py")
        )
        suite = ContractCheckSuite(_meta(), diff_path)

        assert suite.check_diff_parseable().verdict == "pass"
        assert suite.check_diff_path_safety().verdict == "pass"
        assert suite.check_diff_single_representation().verdict == "pass"

    @pytest.mark.parametrize(
        "path",
        [
            "../outside_repo.py",
            "src/../../escape.py",
            ".forge/candidate.meta.json",
            ".forge/usage.json",
            "/etc/passwd",
            "C:/windows/evil.py",
        ],
    )
    def test_unsafe_entry_paths_fail_path_safety(self, tmp_path, path):
        diff_path = _write_diff(tmp_path, _new_file_diff(path))
        suite = ContractCheckSuite(_meta(), diff_path)

        result = suite.check_diff_path_safety()

        assert result.verdict == "fail"
        assert path in result.evidence

    def test_unparseable_diff_fails_and_dependent_checks_skip(self, tmp_path):
        broken = (
            "diff --git a/x.py b/x.py\n"
            "index 1111111..2222222\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -nonsense @@\n"
            "+x\n"
        )
        diff_path = _write_diff(tmp_path, broken)
        suite = ContractCheckSuite(_meta(), diff_path)

        parseable = suite.check_diff_parseable()
        safety = suite.check_diff_path_safety()
        representation = suite.check_diff_single_representation()

        assert parseable.verdict == "fail"
        assert "malformed_diff" in parseable.evidence
        assert safety.verdict == "skipped"
        assert "not parseable" in safety.evidence
        assert representation.verdict == "skipped"

    def test_duplicate_representation_of_one_file_fails(self, tmp_path):
        duplicated = _new_file_diff("src/twice.py") + _modify_diff("src/twice.py")
        diff_path = _write_diff(tmp_path, duplicated)
        suite = ContractCheckSuite(_meta(), diff_path)

        result = suite.check_diff_single_representation()

        assert result.verdict == "fail"
        assert "src/twice.py" in result.evidence

    def test_missing_diff_artifact_fails_parseability(self, tmp_path):
        suite = ContractCheckSuite(_meta(), tmp_path / "does-not-exist.diff")

        result = suite.check_diff_parseable()

        assert result.verdict == "fail"
        assert "missing" in result.evidence

    def test_empty_diff_is_structurally_clean(self, tmp_path):
        diff_path = _write_diff(tmp_path, "")
        suite = ContractCheckSuite(_meta(), diff_path)

        assert suite.check_diff_parseable().verdict == "pass"
        assert suite.check_diff_path_safety().verdict == "pass"
        assert suite.check_diff_single_representation().verdict == "pass"


# ----------------------------------------------------------------------
# DB integration checks — the run-side truth
# ----------------------------------------------------------------------


class TestDbRunAndAttemptBase:
    async def test_missing_run_row_fails_present_and_skips_dependents(self, session_factory):
        checks = DbIntegrationChecks(session_factory, "run-absent")

        verdicts = _verdicts(await checks.run(claimed_attempt_base=BASE_OID))

        assert verdicts["db.run.present"] == "fail"
        assert verdicts["db.attempt_base.matches_run"] == "skipped"
        assert verdicts["db.publication_intent.head"] == "skipped"
        assert verdicts["db.mr_reservation.branch_exclusive"] == "skipped"

    async def test_claimed_base_matching_the_frozen_cycle1_base_passes(self, session_factory):
        await _seed_run(session_factory, base_sha=BASE_OID)
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(
            r
            for r in await checks.run(claimed_attempt_base=BASE_OID)
            if r.check == "db.attempt_base.matches_run"
        )

        assert result.verdict == "pass"
        assert "frozen base" in result.evidence

    async def test_claimed_base_mismatching_the_run_fails(self, session_factory):
        await _seed_run(session_factory, base_sha=BASE_OID)
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(
            r
            for r in await checks.run(claimed_attempt_base=OTHER_OID)
            if r.check == "db.attempt_base.matches_run"
        )

        assert result.verdict == "fail"
        assert "frozen base" in result.evidence

    async def test_repair_cycle_freezes_on_the_last_verified_candidate(self, session_factory):
        """Cycle 1 → source base; a repair → the LAST candidate OID (ADR-0016 §4)."""
        await _seed_run(
            session_factory,
            base_sha=BASE_OID,
            candidate_shas=[CANDIDATE_OID],
            commit_cycle=2,
        )
        checks = DbIntegrationChecks(session_factory, "run-1")
        results = await checks.run(claimed_attempt_base=BASE_OID)

        by_check = {result.check: result for result in results}
        assert by_check["db.attempt_base.matches_run"].verdict == "fail"  # source base is stale

        results = await checks.run(claimed_attempt_base=CANDIDATE_OID)
        by_check = {result.check: result for result in results}
        assert by_check["db.attempt_base.matches_run"].verdict == "pass"

    async def test_no_base_claim_skips_rather_than_guessing(self, session_factory):
        await _seed_run(session_factory)
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(
            r
            for r in await checks.run(claimed_attempt_base="")
            if r.check == "db.attempt_base.matches_run"
        )

        assert result.verdict == "skipped"


class TestDbPublicationIntentHead:
    async def test_settled_intent_commit_matching_recorded_head_passes(self, session_factory):
        await _seed_run(session_factory, candidate_shas=[CANDIDATE_OID])
        await _seed_intent(session_factory, status="committed", provider_object_id=CANDIDATE_OID)
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.publication_intent.head")

        assert result.verdict == "pass"
        assert "equals" in result.evidence

    async def test_settled_intent_commit_mismatching_recorded_head_fails(self, session_factory):
        await _seed_run(session_factory, candidate_shas=[CANDIDATE_OID])
        await _seed_intent(session_factory, status="adopted", provider_object_id=OTHER_OID)
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.publication_intent.head")

        assert result.verdict == "fail"
        assert "recorded candidate head" in result.evidence

    async def test_nothing_settled_yet_skips(self, session_factory):
        await _seed_run(session_factory, candidate_shas=[CANDIDATE_OID])
        await _seed_intent(
            session_factory, status="dispatched", provider_object_id=None, operation_key="op-2"
        )
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.publication_intent.head")

        assert result.verdict == "skipped"
        assert "none settled" in result.evidence

    async def test_run_without_recorded_head_skips(self, session_factory):
        await _seed_run(session_factory, candidate_shas=[])
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.publication_intent.head")

        assert result.verdict == "skipped"


class TestDbBranchExclusivity:
    async def test_another_run_claiming_the_same_branch_fails(self, session_factory):
        await _seed_run(session_factory, run_id="run-1")
        await _seed_run(session_factory, run_id="run-2", base_sha=OTHER_OID)
        await _seed_reservation(session_factory, run_id="run-1", branch="forge/mr-1-run-1")
        await _seed_reservation(session_factory, run_id="run-2", branch="forge/mr-1-run-1")
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(
            r for r in await checks.run() if r.check == "db.mr_reservation.branch_exclusive"
        )

        assert result.verdict == "fail"
        assert "run-2" in result.evidence
        assert "forge/mr-1-run-1" in result.evidence

    async def test_exclusive_branch_claim_passes(self, session_factory):
        await _seed_run(session_factory, run_id="run-1")
        await _seed_run(session_factory, run_id="run-2", base_sha=OTHER_OID)
        await _seed_reservation(session_factory, run_id="run-1", branch="forge/mr-1-run-1")
        await _seed_reservation(session_factory, run_id="run-2", branch="forge/mr-1-run-2")
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(
            r for r in await checks.run() if r.check == "db.mr_reservation.branch_exclusive"
        )

        assert result.verdict == "pass"

    async def test_a_reservation_without_run_row_skips(self, session_factory):
        checks = DbIntegrationChecks(session_factory, "run-absent")

        result = next(
            r for r in await checks.run() if r.check == "db.mr_reservation.branch_exclusive"
        )

        assert result.verdict == "skipped"

    async def test_run_with_no_reservations_skips(self, session_factory):
        await _seed_run(session_factory)
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(
            r for r in await checks.run() if r.check == "db.mr_reservation.branch_exclusive"
        )

        assert result.verdict == "skipped"


class TestDbControlCommands:
    async def test_applied_claim_without_applied_at_fails(self, session_factory):
        await _seed_run(session_factory)
        await _seed_commands(
            session_factory,
            _command_row(
                status="applied",
                applied_at=None,
                journal=[{"to": "applied"}],
            ),
        )
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.control_commands.coherence")

        assert result.verdict == "fail"
        assert "applied_at is NULL" in result.evidence

    async def test_status_without_journal_corroboration_fails(self, session_factory):
        await _seed_run(session_factory)
        await _seed_commands(
            session_factory,
            _command_row(
                status="checkpointed",
                applied_at=datetime.now(timezone.utc),
                journal=[{"to": "dispatching"}],  # the audit stopped two rungs early
            ),
        )
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.control_commands.coherence")

        assert result.verdict == "fail"
        assert "not corroborated" in result.evidence

    async def test_coherent_commands_pass(self, session_factory):
        await _seed_run(session_factory)
        await _seed_commands(
            session_factory,
            _command_row(status="received"),
            _command_row(
                id="cmd-2",
                sequence=2,
                dedup_key="gitlab-note:2",
                status="applied",
                applied_at=datetime.now(timezone.utc),
                journal=[{"to": "authorized"}, {"to": "applied"}],
            ),
        )
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.control_commands.coherence")

        assert result.verdict == "pass"

    async def test_commands_of_other_runs_are_not_this_runs_evidence(self, session_factory):
        await _seed_run(session_factory, run_id="run-1")
        await _seed_run(session_factory, run_id="run-2", base_sha=OTHER_OID)
        await _seed_commands(
            session_factory,
            _command_row(
                work_id="run-2",
                run_id="run-2",
                status="applied",
                applied_at=None,  # would fail — but it is run-2's problem
                journal=[{"to": "applied"}],
            ),
        )
        checks = DbIntegrationChecks(session_factory, "run-1")

        result = next(r for r in await checks.run() if r.check == "db.control_commands.coherence")

        assert result.verdict == "pass"

    async def test_missing_control_commands_table_skips_clean(self):
        """No FORGE_PG_TEST_URL needed: a database without the table skips."""
        engine = create_async_engine(
            SQLITE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("DROP TABLE control_commands"))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            checks = DbIntegrationChecks(factory, "run-1")

            result = next(
                r for r in await checks.run() if r.check == "db.control_commands.coherence"
            )

            assert result.verdict == "skipped"
            assert "skip-clean" in result.evidence
        finally:
            await engine.dispose()


# ----------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------


class TestReport:
    async def test_without_a_session_factory_db_checks_skip_clean(self, tmp_path):
        diff_path = _write_diff(tmp_path, _new_file_diff("src/new.py"))

        report = await run_independent_checks(_meta(), diff_path, "run-1", session_factory=None)

        assert len(report.results) == 12
        assert [result.check for result in report.results][:7] == [
            "contract.meta.schema",
            "contract.meta.exit_coherence",
            "contract.meta.usage_honesty",
            "contract.attempt_base.consistency",
            "contract.diff.parseable",
            "contract.diff.path_safety",
            "contract.diff.single_representation",
        ]
        assert tuple(result.check for result in report.skipped()) == DB_CHECK_NAMES
        for result in report.skipped():
            assert result.suite == "db"
            assert "not cross-checked" in result.evidence
        assert report.failures() == ()

    async def test_report_shape_is_evidence_never_a_verdict(self, tmp_path):
        diff_path = _write_diff(tmp_path, _new_file_diff("../escape.py"))

        report = await run_independent_checks(_meta(), diff_path, "run-1")

        evidence = report.as_evidence()
        assert set(evidence) == {"role", "note", "checks", "failed", "skipped"}
        assert evidence["role"] == "evidence"
        assert evidence["failed"] == ["contract.diff.path_safety"]
        assert evidence["skipped"] == list(DB_CHECK_NAMES)
        assert all(
            set(item) == {"check", "suite", "verdict", "evidence"} for item in evidence["checks"]
        )
        # No aggregated verdict surface exists to misread as one.
        assert not hasattr(report, "passed") and not hasattr(report, "ok")
        assert "verdict" not in evidence and "verified" not in evidence

    async def test_failures_and_by_check_select_the_right_lines(self, tmp_path):
        diff_path = _write_diff(tmp_path, _new_file_diff("src/new.py"))

        report = IndependentCheckReport(
            results=tuple(ContractCheckSuite(_meta(approved=True), diff_path).run())
        )

        assert tuple(result.check for result in report.failures()) == ("contract.meta.schema",)
        assert report.by_check("contract.meta.schema").verdict == "fail"
        assert report.by_check("contract.diff.parseable").verdict == "pass"
        assert report.by_check("no.such.check") is None

    async def test_summary_line_reports_counts_and_failures(self, tmp_path):
        diff_path = _write_diff(tmp_path, _new_file_diff("../escape.py"))

        report = await run_independent_checks(_meta(), diff_path, "run-1")

        line = report.summary_line()
        assert "12 checks" in line
        assert "1 fail" in line
        assert "contract.diff.path_safety" in line

    async def test_end_to_end_happy_path_with_a_real_database(self, tmp_path, session_factory):
        await _seed_run(session_factory, base_sha=BASE_OID)
        diff_path = _write_diff(tmp_path, _new_file_diff("src/new_module.py"))

        report = await run_independent_checks(
            _meta(), diff_path, "run-1", session_factory=session_factory
        )

        assert report.failures() == ()
        verdicts = _verdicts(report.results)
        assert verdicts["db.run.present"] == "pass"
        assert verdicts["db.attempt_base.matches_run"] == "pass"
        assert verdicts["db.control_commands.coherence"] == "pass"
        assert verdicts["db.publication_intent.head"] == "skipped"  # nothing published yet
        assert verdicts["db.mr_reservation.branch_exclusive"] == "skipped"

    async def test_a_well_formed_artifact_diverging_from_run_truth_still_fails(
        self, tmp_path, session_factory
    ):
        """The contract suite passes; the DB suite catches the lie."""
        await _seed_run(session_factory, base_sha=BASE_OID)
        diff_path = _write_diff(tmp_path, _new_file_diff("src/new_module.py"))

        report = await run_independent_checks(
            _meta(attempt_base=OTHER_OID), diff_path, "run-1", session_factory=session_factory
        )

        assert tuple(result.check for result in report.failures()) == (
            "db.attempt_base.matches_run",
        )


# ----------------------------------------------------------------------
# R28-20: independent verification of one COMPLETE CandidateSet
# ----------------------------------------------------------------------

WORK_CONTRACT_DIGEST = "9" * 64
ORDERS_BASE = "1" * 40
ORDERS_CANDIDATE = "a" * 40
BILLING_CANDIDATE = "b" * 40
CATALOG_BASE = "2" * 40
IMAGE = f"sha256:{'e' * 64}"
POSTGRES_PIN = f"sha256:{'d' * 64}"


def _frozen_set(work_id: str = "run-1") -> CandidateSet:
    """A two-changed-one-baseline world, frozen with pins and policy refs."""
    return freeze_candidate_set(
        work_id,
        plan_revision=1,
        contract_digest=WORK_CONTRACT_DIGEST,
        per_repo={
            "orders": {
                "base_oid": ORDERS_BASE,
                "candidate_oid": ORDERS_CANDIDATE,
                "role": "changed",
                "image_digest": IMAGE,
            },
            "billing": {
                "base_oid": ORDERS_BASE,
                "candidate_oid": BILLING_CANDIDATE,
                "role": "changed",
                "image_digest": IMAGE,
            },
            "catalog": {
                "base_oid": CATALOG_BASE,
                "candidate_oid": CATALOG_BASE,
                "role": "baseline",
                "image_digest": IMAGE,
            },
        },
        environment_pins={"postgres": POSTGRES_PIN},
        policy_refs=["policy:compat-1"],
    )


def _member_meta(repository_id: str, base_oid: str) -> dict:
    """An honest published meta for ONE member, pinned to its own base."""
    meta = _meta(attempt_base=base_oid)
    meta["attempt_id"] = f"{repository_id}:1"
    return meta


def _artifacts(tmp_path: Path) -> dict:
    """Per-member (meta, diff) artifacts for the contract angle."""
    artifacts = {}
    for repository_id, base_oid, diff_text in (
        ("orders", ORDERS_BASE, _new_file_diff("src/orders.py")),
        ("billing", ORDERS_BASE, _new_file_diff("src/billing.py")),
        ("catalog", CATALOG_BASE, ""),
    ):
        member_dir = tmp_path / repository_id
        member_dir.mkdir(parents=True, exist_ok=True)
        artifacts[repository_id] = (
            _member_meta(repository_id, base_oid),
            _write_diff(member_dir, diff_text),
        )
    return artifacts


async def _seed_workpackage_world(factory, digest: str | None, *, run_id: str = "run-1") -> None:
    """A parent run coordinating a work package with *digest* active."""
    async with factory() as session:
        session.add(FlowRun(id=run_id, project_id=1, status="waiting_harness", evidence={}))
        await session.commit()
    state = {
        "schema": "forge.workpackage.state/1",
        "package_id": "pkg-1",
        "parent_run_id": run_id,
        "objective": "one change, many lanes",
        "task_brief": "brief",
        "phases": [["orders", "billing"]],
        "current_phase": 0,
        "state": "running",
        "failed_item": "",
        "tested_world_digest": digest,
        "children": {},
    }
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        run.evidence = {"workpackage": state}
        await session.commit()


class TestVerifyCandidateSet:
    async def test_a_well_formed_frozen_set_passes_every_angle(self, tmp_path, session_factory):
        await _seed_workpackage_world(session_factory, _frozen_set().tested_world_digest)
        committed = {
            (repo, oid)
            for repo, oid in (
                ("orders", ORDERS_CANDIDATE),
                ("billing", BILLING_CANDIDATE),
                ("catalog", CATALOG_BASE),
            )
        }
        artifacts = _artifacts(tmp_path)

        async def lookup(repository_id: str, oid: str) -> bool:
            return (repository_id, oid) in committed

        report = await verify_candidate_set(
            _frozen_set(), session_factory, commit_lookup=lookup, candidate_artifacts=artifacts
        )

        assert report.failures() == ()
        assert report.skipped() == ()
        set_verdicts = {result.check: result.verdict for result in report.set_checks}
        assert set_verdicts == {
            "candidateset.world_digest.recomputed": "pass",
            "candidateset.applicability_digest.recomputed": "pass",
            "candidateset.environment_compose.consistent": "pass",
            "candidateset.db.recorded_world": "pass",
        }
        # Typed per-member outcomes: every member's commit probed, every
        # published artifact through the contract suite.
        assert [member.repository_id for member in report.members] == [
            "billing",
            "catalog",
            "orders",
        ]  # sorted, deterministic
        for member in report.members:
            assert member.commit_present.verdict == "pass"
            assert all(result.verdict == "pass" for result in member.contract)
        # The evidence fragment carries no verdict surface to misread.
        evidence = report.as_evidence()
        assert evidence["role"] == "evidence"
        assert evidence["failed"] == [] and evidence["failed_members"] == []
        assert evidence["tested_world_digest"] == _frozen_set().tested_world_digest
        assert "failures:" not in report.summary_line()

    async def test_a_missing_commit_fails_exactly_that_member(self, tmp_path, session_factory):
        await _seed_workpackage_world(session_factory, _frozen_set().tested_world_digest)

        async def lookup(repository_id: str, oid: str) -> bool:
            return repository_id != "orders"  # orders' candidate is nowhere to be found

        report = await verify_candidate_set(_frozen_set(), session_factory, commit_lookup=lookup)

        assert report.failures() == (report.members[2].commit_present,)  # orders
        assert report.failed_members() == ("orders",)
        billing, catalog, orders = report.members
        assert orders.commit_present.verdict == "fail"
        assert orders.candidate_oid == ORDERS_CANDIDATE
        assert "does not exist as a commit" in orders.commit_present.evidence
        assert billing.commit_present.verdict == "pass"  # the others are untouched
        assert catalog.commit_present.verdict == "pass"
        assert "failed members: orders" in report.summary_line()

    async def test_a_world_digest_mismatch_fails_the_whole_set(self, session_factory):
        """A set whose recorded facts do not reproduce its frozen identity
        is wrong whichever side lied — set-level failure, not one member's."""
        await _seed_workpackage_world(session_factory, "0" * 64)  # a stale record too
        honest = _frozen_set()
        # A member identity silently edited after freeze: recompute differs.
        tampered_members = tuple(
            member.model_copy(
                update={
                    "candidate_oid": "9" * 40
                    if member.repository_id == "billing"
                    else member.candidate_oid
                }
            )
            for member in honest.members
        )
        tampered = honest.model_copy(
            update={
                "members": tampered_members,
                # …but the OLD digests still persisted (the lie under test):
                "tested_world_digest": honest.tested_world_digest,
                "applicability_digest": honest.applicability_digest,
            }
        )

        report = await verify_candidate_set(tampered, session_factory)

        failed = {result.check for result in report.failures()}
        assert "candidateset.world_digest.recomputed" in failed
        assert "candidateset.applicability_digest.recomputed" in failed
        assert "candidateset.db.recorded_world" in failed  # record disagrees too
        assert "does not reproduce" in report.set_checks[0].evidence
        assert report.failed_members() == ()  # a WORLD failure, not a member one

    async def test_an_unfrozen_set_skips_the_digest_angles_cleanly(self, session_factory):
        await _seed_workpackage_world(session_factory, None)
        unfrozen = freeze_candidate_set(
            "run-1",
            plan_revision=1,
            contract_digest=WORK_CONTRACT_DIGEST,
            per_repo={
                "orders": {
                    "base_oid": ORDERS_BASE,
                    "candidate_oid": ORDERS_CANDIDATE,
                    "role": "changed",
                    "image_digest": IMAGE,
                }
            },
        )

        report = await verify_candidate_set(unfrozen, session_factory)

        verdicts = {result.check: result.verdict for result in report.set_checks}
        assert verdicts["candidateset.world_digest.recomputed"] == "skipped"
        assert verdicts["candidateset.applicability_digest.recomputed"] == "skipped"
        assert verdicts["candidateset.db.recorded_world"] == "skipped"
        assert verdicts["candidateset.environment_compose.consistent"] == "pass"
        assert report.failures() == ()

    async def test_without_a_commit_lookup_the_probe_is_a_recorded_skip(self, session_factory):
        await _seed_workpackage_world(session_factory, _frozen_set().tested_world_digest)

        report = await verify_candidate_set(_frozen_set(), session_factory)

        assert report.failures() == ()
        skipped_ids = [result.check for result in report.skipped()]
        # Three members, each with a commit-probe skip and an artifact skip.
        assert skipped_ids.count(MEMBER_COMMIT_CHECK) == 3
        assert skipped_ids.count("contract.artifact.present") == 3
        for member in report.members:
            assert member.commit_present.verdict == "skipped"
            assert "no commit lookup" in member.commit_present.evidence
            assert member.contract[0].check == "contract.artifact.present"
            assert member.contract[0].verdict == "skipped"

    async def test_the_durable_world_record_disagreement_fails(self, session_factory):
        """The set froze one world; the package executes another."""
        await _seed_workpackage_world(session_factory, "f" * 64)

        report = await verify_candidate_set(_frozen_set(), session_factory)

        recorded = next(
            result
            for result in report.set_checks
            if result.check == "candidateset.db.recorded_world"
        )
        assert recorded.verdict == "fail"
        assert "active world" in recorded.evidence

    async def test_a_run_absent_entirely_fails_the_durable_record_check(self, session_factory):
        report = await verify_candidate_set(_frozen_set(), session_factory)

        recorded = next(
            result
            for result in report.set_checks
            if result.check == "candidateset.db.recorded_world"
        )
        assert recorded.verdict == "fail"
        assert "no durable coordination record" in recorded.evidence

    async def test_a_member_artifact_failing_the_contract_fails_that_member(
        self, tmp_path, session_factory
    ):
        await _seed_workpackage_world(session_factory, _frozen_set().tested_world_digest)
        artifacts = _artifacts(tmp_path)
        # billing's published diff escapes the repository — the contract
        # suite's own negative, now reached through the SET verification.
        evil_dir = tmp_path / "billing-evil"
        evil_dir.mkdir(parents=True, exist_ok=True)
        artifacts["billing"] = (
            _member_meta("billing", ORDERS_BASE),
            _write_diff(evil_dir, _new_file_diff("../escape.py")),
        )

        report = await verify_candidate_set(
            _frozen_set(), session_factory, candidate_artifacts=artifacts
        )

        assert report.failed_members() == ("billing",)
        billing_contract = {result.check: result.verdict for result in report.members[0].contract}
        assert billing_contract["contract.diff.path_safety"] == "fail"

    async def test_an_unresolvable_member_artifact_fails_the_compose_check(self, session_factory):
        """A member riding the ``unresolved`` sentinel composes flagged —
        the verifier names it, never runs whatever a tag points at."""
        await _seed_workpackage_world(session_factory, None)
        sentinel_set = freeze_candidate_set(
            "run-1",
            plan_revision=1,
            contract_digest=WORK_CONTRACT_DIGEST,
            per_repo={
                "orders": {
                    "base_oid": ORDERS_BASE,
                    "candidate_oid": ORDERS_CANDIDATE,
                    "role": "changed",
                    "image_digest": "unresolved",
                }
            },
        )

        report = await verify_candidate_set(sentinel_set, session_factory)

        compose = next(
            result
            for result in report.set_checks
            if result.check == "candidateset.environment_compose.consistent"
        )
        assert compose.verdict == "fail"
        assert "exact artifact" in compose.evidence
        assert "orders" in compose.evidence

    async def test_the_report_shape_is_evidence_never_a_verdict(self, session_factory):
        await _seed_workpackage_world(session_factory, None)

        report = await verify_candidate_set(_frozen_set(), session_factory)

        assert not hasattr(report, "passed") and not hasattr(report, "ok")
        evidence = report.as_evidence()
        assert "verdict" not in evidence and "verified" not in evidence
        assert set(evidence) == {
            "role",
            "note",
            "work_id",
            "plan_revision",
            "tested_world_digest",
            "set_checks",
            "members",
            "failed",
            "skipped",
            "failed_members",
        }
        assert list(SET_CHECK_NAMES) == [result.check for result in report.set_checks]
