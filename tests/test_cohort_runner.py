"""The delivery-cohort harness (A17): contract, ledger and aggregation logic.

Covers the evaluation HARNESS pieces that must hold without driving a single
live run:

- cohort integrity — 14 bounded tasks, every review axis present, unique
  unit ids, seeds and predeclared mechanical checks on each;
- the discriminating property of the acceptance contract: every task's
  checks FAIL on its own seed (the work is not done yet) and PASS on
  correct work — acceptance is mechanical, never self-reported;
- the ledger — append-only attempts, explicit human verdicts only;
- aggregation honesty — the denominator is ACCEPTED units; failed/cancelled
  attempts (and their spend) stay in the totals; token classes stay
  separate with unknown ≠ zero; the only rate built is known output tokens
  over known llm duration; rework/repair linkage survives;
- the public-surface parsers (plan-comment run id, ``/status`` reply) and
  the R23 receipt-export attachment;
- harness independence: the runner imports no ``forge`` module.
"""

from __future__ import annotations

import ast
import json
import shlex
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation.cohort import aggregate, ledger as cohort_ledger  # noqa: E402
from evaluation.cohort import runner as cohort_runner  # noqa: E402
from evaluation.cohort.tasks import (  # noqa: E402
    AXES,
    CONTRACT_VERSION,
    COHORT_TASKS,
    LARGE_FILE_BYTES,
    TASKS_BY_ID,
    AcceptanceCheck,
    CohortTask,
    materialize_seed,
    validate_cohort,
)

# ----------------------------------------------------------------------
# Cohort integrity: the predeclared contract
# ----------------------------------------------------------------------


def test_cohort_is_fourteen_tasks_covering_every_axis() -> None:
    validate_cohort()
    assert len(COHORT_TASKS) == 14
    assert {task.axis for task in COHORT_TASKS} == set(AXES)
    assert len({task.unit_id for task in COHORT_TASKS}) == 14


def test_axis_coverage_matches_the_review_set() -> None:
    by_axis = {task.axis for task in COHORT_TASKS}
    assert by_axis == {
        "create",
        "update",
        "repair",
        "large-file",
        "monorepo",
        "tests-only",
        "infra-failure",
        "cancel-mid-run",
    }


def test_large_file_units_seed_above_the_threshold() -> None:
    for unit_id in ("CU-07-large-log-analyze", "CU-08-large-dictionary"):
        size = sum(seed.size for seed in TASKS_BY_ID[unit_id].seed_files)
        assert size >= LARGE_FILE_BYTES


def test_monorepo_units_declare_a_path_scope() -> None:
    for unit_id in ("CU-09-monorepo-billing-scope", "CU-10-monorepo-shared-banner"):
        assert TASKS_BY_ID[unit_id].path_scope, unit_id


def test_procedural_units_carry_an_operator_procedure() -> None:
    assert TASKS_BY_ID["CU-13-infra-retry"].procedure
    assert TASKS_BY_ID["CU-14-cancel-mid-run"].procedure


def test_large_file_seeds_materialize_deterministically(tmp_path: Path) -> None:
    task = TASKS_BY_ID["CU-07-large-log-analyze"]
    first = materialize_seed(task, tmp_path / "a")
    second = materialize_seed(task, tmp_path / "b")
    for path_a, path_b in zip(first, second, strict=True):
        assert path_a.read_bytes() == path_b.read_bytes()


# ----------------------------------------------------------------------
# The acceptance contract discriminates: fail on seed, pass on real work
# ----------------------------------------------------------------------


@pytest.mark.parametrize("task", COHORT_TASKS, ids=lambda task: task.unit_id)
def test_no_task_is_acceptable_on_its_own_seed(task: CohortTask, tmp_path: Path) -> None:
    """At least one predeclared check must fail until the work exists.

    Untouched-file GUARD checks legitimately pass on the seed (nothing has
    been touched yet) — the invariant is that the seed cannot pass the whole
    contract, so acceptance is impossible without real work.
    """
    materialize_seed(task, tmp_path)
    results = cohort_runner.evaluate_checks(tmp_path, task.checks)
    assert [result["name"] for result in results] == [check.name for check in task.checks]
    assert not all(result["passed"] for result in results)


def test_create_checks_pass_on_correct_work(tmp_path: Path) -> None:
    task = TASKS_BY_ID["CU-01-create-greeting"]
    materialize_seed(task, tmp_path)
    (tmp_path / "greeting.py").write_text(
        'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n', encoding="utf-8"
    )
    results = cohort_runner.evaluate_checks(tmp_path, task.checks)
    assert all(result["passed"] for result in results)


def test_repair_checks_pass_once_the_code_is_fixed(tmp_path: Path) -> None:
    task = TASKS_BY_ID["CU-05-repair-temperature"]
    materialize_seed(task, tmp_path)
    (tmp_path / "temperature.py").write_text(
        "def celsius_to_fahrenheit(c: float) -> float:\n    return c * 9 / 5 + 32\n",
        encoding="utf-8",
    )
    results = cohort_runner.evaluate_checks(tmp_path, task.checks)
    assert all(result["passed"] for result in results), results


def test_repair_checks_catch_test_file_edits(tmp_path: Path) -> None:
    """Fixing the TEST instead of the code must fail the byte-identity check."""
    task = TASKS_BY_ID["CU-05-repair-temperature"]
    materialize_seed(task, tmp_path)
    test_path = tmp_path / "test_temperature.py"
    test_path.write_text(
        test_path.read_text(encoding="utf-8").replace(", 212)", ", 213)"),
        encoding="utf-8",
    )
    results = cohort_runner.evaluate_checks(tmp_path, task.checks)
    by_name = {result["name"]: result["passed"] for result in results}
    assert by_name["test-file-untouched"] is False


def test_large_file_check_passes_when_the_whole_file_is_read(tmp_path: Path) -> None:
    task = TASKS_BY_ID["CU-07-large-log-analyze"]
    materialize_seed(task, tmp_path)
    (tmp_path / "analyze.py").write_text(
        "def count_errors(path: str) -> int:\n"
        '    with open(path, encoding="utf-8") as handle:\n'
        "        return sum(1 for line in handle if line.startswith('ERROR'))\n",
        encoding="utf-8",
    )
    results = cohort_runner.evaluate_checks(tmp_path, task.checks)
    assert all(result["passed"] for result in results), results


def test_monorepo_scope_guard_fails_on_out_of_scope_edits(tmp_path: Path) -> None:
    task = TASKS_BY_ID["CU-09-monorepo-billing-scope"]
    materialize_seed(task, tmp_path)
    price = tmp_path / "services" / "billing" / "price.py"
    price.write_text(
        price.read_text(encoding="utf-8") + "\n\ndef discount_cents(subtotal_cents: int) -> int:\n"
        "    return subtotal_cents // 10\n",
        encoding="utf-8",
    )
    # The billing work is correct — but shipping was also touched.
    shipping = tmp_path / "services" / "shipping" / "rate.py"
    shipping.write_text(shipping.read_text(encoding="utf-8") + "\n# stray edit\n", encoding="utf-8")
    results = cohort_runner.evaluate_checks(tmp_path, task.checks)
    by_name = {result["name"]: result["passed"] for result in results}
    assert by_name["billing-discount-contract"] is True
    assert by_name["shipping-bytes-untouched"] is False
    assert by_name["shipping-behavior-intact"] is True


def test_tests_only_checks_pass_on_a_real_suite(tmp_path: Path) -> None:
    task = TASKS_BY_ID["CU-11-tests-stringutil"]
    materialize_seed(task, tmp_path)
    (tmp_path / "test_stringutil.py").write_text(
        "import unittest\n\n"
        "from stringutil import is_palindrome, squeeze, truncate\n\n\n"
        "class StringUtilTest(unittest.TestCase):\n"
        "    def test_squeeze(self):\n"
        "        self.assertEqual(squeeze('  a   b  '), 'a b')\n\n"
        "    def test_palindrome(self):\n"
        "        self.assertTrue(is_palindrome('abba'))\n"
        "        self.assertFalse(is_palindrome('abc'))\n\n"
        "    def test_truncate(self):\n"
        "        self.assertEqual(truncate('abcdef', 3), 'abc')\n"
        "        self.assertEqual(truncate('abcdef', -1), '')\n\n\n"
        'if __name__ == "__main__":\n    unittest.main()\n',
        encoding="utf-8",
    )
    results = cohort_runner.evaluate_checks(tmp_path, task.checks)
    assert all(result["passed"] for result in results), results


def test_a_crashing_check_fails_never_passes(tmp_path: Path) -> None:
    check = AcceptanceCheck(
        name="boom", argv=("/nonexistent/cohort-binary",), description="crashes"
    )
    results = cohort_runner.evaluate_checks(tmp_path, [check])
    assert results == [{"name": "boom", "passed": False, "detail": results[0]["detail"]}]
    assert results[0]["detail"]


# ----------------------------------------------------------------------
# The ledger: append-only attempts, human verdicts only
# ----------------------------------------------------------------------


def small_ledger() -> dict:
    data = cohort_ledger.new_ledger("owner/lab", {"driver": "claude-code"})
    cohort_ledger.open_unit(data, TASKS_BY_ID["CU-01-create-greeting"])
    return data


def test_record_attempt_appends_and_never_rewrites() -> None:
    data = small_ledger()
    first = cohort_ledger.attempt_record(run_id="a" * 32, terminal_status="failed")
    second = cohort_ledger.attempt_record(run_id="b" * 32, kind="retry")
    assert cohort_ledger.record_attempt(data, "CU-01-create-greeting", first) == 1
    assert cohort_ledger.record_attempt(data, "CU-01-create-greeting", second) == 2
    unit = data["units"]["CU-01-create-greeting"]
    assert [attempt["run_id"] for attempt in unit["attempts"]] == ["a" * 32, "b" * 32]
    assert unit["attempts"][0]["terminal_status"] == "failed"


def test_attempt_record_rejects_unknown_kind() -> None:
    with pytest.raises(cohort_ledger.CohortError):
        cohort_ledger.attempt_record(run_id="a" * 32, kind="winged-it")


def test_acceptance_requires_a_known_verdict_and_an_open_unit() -> None:
    data = small_ledger()
    with pytest.raises(cohort_ledger.CohortError):
        cohort_ledger.record_acceptance(
            data, "CU-01-create-greeting", "looks-fine", decided_by="op"
        )
    cohort_ledger.record_acceptance(
        data, "CU-01-create-greeting", "accepted", decided_by="operator"
    )
    assert data["units"]["CU-01-create-greeting"]["acceptance"]["verdict"] == "accepted"
    with pytest.raises(cohort_ledger.CohortError):
        cohort_ledger.record_acceptance(data, "CU-99-missing", "accepted", decided_by="operator")


def test_ledger_roundtrip_and_schema_guards(tmp_path: Path) -> None:
    data = small_ledger()
    cohort_ledger.record_attempt(
        data, "CU-01-create-greeting", cohort_ledger.attempt_record(run_id="a" * 32)
    )
    path = tmp_path / "ledgers" / "pass.ledger.json"
    cohort_ledger.save_ledger(data, path)
    loaded = cohort_ledger.load_ledger(path)
    assert loaded["units"]["CU-01-create-greeting"]["attempts"][0]["run_id"] == "a" * 32

    loaded["contract_version"] = "someone-elses-contract/9"
    path.write_text(json.dumps(loaded), encoding="utf-8")
    with pytest.raises(cohort_ledger.CohortError, match="contract"):
        cohort_ledger.load_ledger(path)

    loaded["contract_version"] = CONTRACT_VERSION
    loaded["schema"] = "not-the-right-schema"
    path.write_text(json.dumps(loaded), encoding="utf-8")
    with pytest.raises(cohort_ledger.CohortError, match="not a forge.cohort.ledger"):
        cohort_ledger.load_ledger(path)


def test_load_rejects_an_unknown_verdict(tmp_path: Path) -> None:
    data = small_ledger()
    data["units"]["CU-01-create-greeting"]["acceptance"]["verdict"] = "vibes"
    path = tmp_path / "pass.ledger.json"
    cohort_ledger.save_ledger(data, path)
    with pytest.raises(cohort_ledger.CohortError, match="verdict"):
        cohort_ledger.load_ledger(path)


# ----------------------------------------------------------------------
# Aggregation: the honest denominator and its numerators
# ----------------------------------------------------------------------

RUN_A1 = "a" * 32
RUN_A2 = "b" * 32
RUN_B = "c" * 32
RUN_C = "d" * 32
RUN_D = "e" * 32

T0 = "2026-09-17T00:00:00+00:00"
T_PLAN = "2026-09-17T00:01:00+00:00"
T_GO = "2026-09-17T00:02:00+00:00"
T_CANDIDATE = "2026-09-17T00:20:00+00:00"
T_CI = "2026-09-17T00:30:00+00:00"
T_VERDICT = "2026-09-17T01:00:00+00:00"


def receipt(**overrides: object) -> dict:
    values: dict = {
        "receipt_id": "r" * 64,
        "attempt_id": "501:1",
        "model": "glm-test",
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "completeness": "aggregate",
    }
    values.update(overrides)
    return values


def llm_call(duration_ms: int, first_token_ms: int | None = None, status: str = "ok") -> dict:
    return {"duration_ms": duration_ms, "first_token_ms": first_token_ms, "status": status}


def fixture_ledger() -> dict:
    """One pass: 1 accepted (2 attempts, first failed), 1 rejected, 1 cancelled,
    1 pending/unmeasured — every honesty rule has a subject."""
    data = cohort_ledger.new_ledger(
        "owner/lab", {"driver": "claude-code", "model": "glm-test", "budget_class": "standard"}
    )
    for unit_id in (
        "CU-01-create-greeting",
        "CU-02-create-calculator",
        "CU-14-cancel-mid-run",
        "CU-11-tests-stringutil",
    ):
        cohort_ledger.open_unit(data, TASKS_BY_ID[unit_id])
    units = data["units"]

    units["CU-01-create-greeting"]["attempts"] = [
        {
            **cohort_ledger.attempt_record(
                run_id=RUN_A1,
                started_at=T0,
                plan_seen_at=T_PLAN,
                go_posted_at=T_GO,
                terminal_seen_at=T_CANDIDATE,
                terminal_status="failed",
                commit_cycle=1,
            ),
            "receipts": [
                receipt(
                    receipt_id="1" * 64,
                    input_tokens=100,
                    cached_input_tokens=40,
                    cache_write_tokens=25,
                    output_tokens=50,
                )
            ],
            "llm_calls": [llm_call(2000, 120), llm_call(3000, 180)],
            "export_attached": True,
        },
        {
            **cohort_ledger.attempt_record(
                run_id=RUN_A2,
                kind="retry",
                started_at=T0,
                go_posted_at=T_GO,
                candidate_seen_at=T_CANDIDATE,
                ci_concluded_at=T_CI,
                terminal_status="ready_for_human",
                rework_of=RUN_A1,
                commit_cycle=3,
            ),
            "receipts": [
                receipt(
                    receipt_id="2" * 64,
                    attempt_id="501:2",
                    input_tokens=200,
                    cached_input_tokens=10,
                    output_tokens=150,
                )
            ],
            "llm_calls": [llm_call(4000)],
            "export_attached": True,
        },
    ]
    units["CU-01-create-greeting"]["acceptance"] = {
        "verdict": "accepted",
        "decided_by": "operator",
        "decided_at": T_VERDICT,
        "notes": "merged the draft PR",
        "checks": [{"name": "greet-contract", "passed": True, "detail": ""}],
    }

    units["CU-02-create-calculator"]["attempts"] = [
        {
            **cohort_ledger.attempt_record(
                run_id=RUN_B, started_at=T0, terminal_status="failed", commit_cycle=2
            ),
            "receipts": [receipt(receipt_id="3" * 64, input_tokens=500, output_tokens=None)],
            "llm_calls": [llm_call(1000, status="error")],
            "export_attached": True,
        }
    ]
    units["CU-02-create-calculator"]["acceptance"] = {
        "verdict": "rejected",
        "decided_by": "operator",
        "decided_at": T_VERDICT,
        "notes": "closed without merge",
        "checks": [{"name": "calculator-contract", "passed": False, "detail": "no module"}],
    }

    units["CU-14-cancel-mid-run"]["attempts"] = [
        {
            **cohort_ledger.attempt_record(
                run_id=RUN_C, started_at=T0, terminal_status="cancelled"
            ),
            "receipts": [receipt(receipt_id="4" * 64, input_tokens=1000, output_tokens=800)],
            "llm_calls": [llm_call(8000)],
            "export_attached": True,
        }
    ]
    units["CU-14-cancel-mid-run"]["acceptance"] = {
        "verdict": "cancelled",
        "decided_by": "operator",
        "decided_at": T_VERDICT,
        "notes": "cancelled mid-run by contract",
        "checks": [],
    }

    units["CU-11-tests-stringutil"]["attempts"] = [
        cohort_ledger.attempt_record(run_id=RUN_D, started_at=T0, terminal_status=None)
    ]
    return data


def test_denominator_is_accepted_units_not_attempts() -> None:
    report = aggregate.cohort_report(fixture_ledger())
    counts = report["counts"]
    assert counts["units"] == 4
    assert counts["attempts"] == 5
    assert counts["accepted_units"] == 1
    assert counts["rejected_units"] == 1
    assert counts["cancelled_units"] == 1
    assert counts["pending_units"] == 1
    assert counts["rejected_attempts"] == 3  # failed first try + rejected + cancelled (R24 set)
    assert counts["rework_count"] == 1


def test_failed_and_cancelled_spend_stays_in_the_totals() -> None:
    report = aggregate.cohort_report(fixture_ledger())
    classes = report["all_attempt_spend"]["usage"]["token_classes"]
    # 300 (accepted unit, both attempts) + 500 (rejected) + 1000 (cancelled)
    assert classes["input_tokens"] == 1800
    assert classes["cached_input_tokens"] == 50
    assert classes["cache_write_tokens"] == 25
    # 200 + 800 known; the rejected unit's output stays UNKNOWN, not zero
    assert classes["output_tokens"] == 1000
    assert report["all_attempt_spend"]["usage"]["unknown_counts"]["output_tokens"] == 1
    assert report["honesty"]["attempts_retained"] is True


def test_token_classes_stay_separate_and_the_only_rate_is_decode() -> None:
    report = aggregate.cohort_report(fixture_ledger())
    assert set(report["all_attempt_spend"]["usage"]["token_classes"]) == set(
        aggregate.TOKEN_CLASSES
    )
    # 200+150+50... output known = 1000 over 18s of llm activity (2+3+4+1+8)
    # B09: receipt tokens (2 receipts) over llm-call durations (5 calls) is
    # an UNMATCHED population pair — no proven join, no rate.
    assert report["latency"]["decode_output_tokens_per_s"] is None
    assert report["all_attempt_spend"]["llm"]["llm_active_s"] == pytest.approx(18.0)
    assert "total_tokens" not in json.dumps(report["all_attempt_spend"]["usage"])


def test_ttft_uses_only_reporting_calls() -> None:
    report = aggregate.cohort_report(fixture_ledger())
    ttft = report["all_attempt_spend"]["llm"]["ttft"]
    assert ttft["known_calls"] == 2
    assert ttft["unknown_calls"] == 3
    assert ttft["p50_ms"] == 120
    assert ttft["p95_ms"] == 180


def test_per_accepted_unit_prices_all_of_the_units_attempts() -> None:
    report = aggregate.cohort_report(fixture_ledger())
    per_unit = report["per_accepted_unit"]
    assert per_unit["denominator"] == 1
    # The accepted unit's BOTH attempts: 100+200 input, 40+10 cached, 25 write,
    # 50+150 output — the failed first attempt is part of the unit's cost.
    assert per_unit["token_classes_per_unit"] == {
        "input_tokens": 300.0,
        "cached_input_tokens": 50.0,
        "cache_write_tokens": 25.0,
        "output_tokens": 200.0,
    }
    assert per_unit["llm_active_s_per_unit"] == pytest.approx(9.0)
    assert per_unit["repairs_per_unit"] == 2.0
    assert per_unit["wall_s_mean"] == pytest.approx(3600.0)
    assert per_unit["wall_known_units"] == 1


def test_pricebook_costs_only_fully_priced_receipts() -> None:
    pricebook = {
        "glm-test": {
            "input_tokens": 1.0,
            "cached_input_tokens": 0.1,
            "cache_write_tokens": 2.0,
            "output_tokens": 3.0,
        }
    }
    report = aggregate.cohort_report(fixture_ledger(), pricebook)
    per_unit = report["per_accepted_unit"]
    # The accepted unit's second attempt never reported its cache-write count
    # — the unit cost is UNKNOWN (no fabricated remainder averages in).
    assert per_unit["cost_known_units"] == 0
    assert per_unit["cost_usd_mean"] is None


def test_pricebook_costs_sum_every_attempt_of_accepted_units() -> None:
    pricebook = {
        "glm-test": {
            "input_tokens": 1.0,
            "cached_input_tokens": 0.1,
            "cache_write_tokens": 2.0,
            "output_tokens": 3.0,
        }
    }
    data = fixture_ledger()
    # Give the second attempt its missing cache-write count so it can price.
    data["units"]["CU-01-create-greeting"]["attempts"][1]["receipts"][0]["cache_write_tokens"] = 0
    report = aggregate.cohort_report(data, pricebook)
    per_unit = report["per_accepted_unit"]
    expected = (100 * 1 + 40 * 0.1 + 25 * 2 + 50 * 3) / 1e6 + (
        200 * 1 + 10 * 0.1 + 0 * 2 + 150 * 3
    ) / 1e6
    assert per_unit["cost_known_units"] == 1
    assert per_unit["cost_usd_mean"] == pytest.approx(expected)


def test_unmeasured_attempts_are_counted_never_zeroed() -> None:
    report = aggregate.cohort_report(fixture_ledger())
    assert report["honesty"]["unknown_usage_attempts"] == 1
    rollup = next(u for u in report["units"] if u["verdict"] == "pending")
    assert rollup["usage"]["receipt_count"] == 0
    assert rollup["attempts"][0]["export_attached"] is False


def test_zero_accepted_units_yield_no_per_accepted_block() -> None:
    data = fixture_ledger()
    data["units"]["CU-01-create-greeting"]["acceptance"]["verdict"] = "pending"
    report = aggregate.cohort_report(data)
    assert report["per_accepted_unit"] is None
    assert report["honesty"]["zero_accepted_note"]
    assert report["all_attempt_spend"]["usage"]["token_classes"]["input_tokens"] == 1800


def test_rework_and_repairs_survive_the_rollup() -> None:
    report = aggregate.cohort_report(fixture_ledger())
    unit = next(u for u in report["units"] if u["verdict"] == "accepted")
    assert unit["rework_count"] == 1
    assert unit["repairs"] == 2
    assert unit["attempts"][0]["rejected"] is True
    assert unit["attempts"][1]["rework_of"] == RUN_A1
    assert unit["attempts"][1]["attempt_no"] == 2


def test_phase_seconds_present_only_when_both_stamps_known() -> None:
    phases = aggregate.phase_seconds(
        {
            "started_at": T0,
            "plan_seen_at": T_PLAN,
            "go_posted_at": T_GO,
            "candidate_seen_at": T_CANDIDATE,
            "ci_concluded_at": T_CI,
        }
    )
    assert phases == {
        "plan_wait_s": 60.0,
        "gate_wait_s": 60.0,
        "execution_wait_s": 1080.0,
        "ci_wait_s": 600.0,
    }
    partial = aggregate.phase_seconds({"started_at": T0, "go_posted_at": T_GO})
    assert partial == {}  # plan_seen unknown → gate_wait absent, not zero
    skew = aggregate.phase_seconds({"started_at": T_GO, "plan_seen_at": T_PLAN})
    assert skew == {"plan_wait_s": 0.0}  # clock skew clamps, never negative


def test_percentile_nearest_rank() -> None:
    assert aggregate.percentile([], 50) is None
    assert aggregate.percentile([3.0], 50) == 3.0
    assert aggregate.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    values = [float(n) for n in range(1, 101)]
    assert aggregate.percentile(values, 95) == 95.0
    assert aggregate.percentile(values, 100) == 100.0


def test_sum_known_and_count_unknown() -> None:
    assert aggregate.sum_known([1, None, 2]) == 3
    assert aggregate.sum_known([None, None]) is None
    assert aggregate.sum_known([]) is None
    assert aggregate.count_unknown([1, None, None]) == 2


# ----------------------------------------------------------------------
# Public-surface parsers and the R23 export attachment
# ----------------------------------------------------------------------


def test_extract_run_id_from_a_real_plan_comment() -> None:
    run_id = "9f" * 16
    comment = (
        f"## Forge plan — run `{run_id[:8]}`\n\nThe plan body.\n\n"
        f"**Plan digest:** `{'d' * 64}`\n\n"
        f"Approve this exact plan by commenting `@forge /go {run_id}`.\n\n"
        "Approvers: @operator.\n\n*This is an automated message.*"
    )
    assert cohort_runner.extract_run_id(comment) == run_id
    assert cohort_runner.extract_run_id("Approve with `/go 12345` — truncated id.") is None
    assert cohort_runner.extract_run_id("") is None


def test_parse_status_reply_from_the_public_probe() -> None:
    body = (
        "## Forge — run `9f9f9f9f` status\n\n"
        "- **Status:** `ready_for_human` — checks passed; merge is a human decision\n"
        "- **Commit cycle:** 2\n"
    )
    assert cohort_runner.parse_status_reply(body) == "ready_for_human"
    blocked = "## Forge — run `9f9f9f9f` status\n\n- **Status:** `blocked` — dispatch failed\n"
    assert cohort_runner.parse_status_reply(blocked) == "blocked"
    assert cohort_runner.parse_status_reply("no status line here") is None


def test_attach_export_fills_the_attempt_and_honestly_skips_missing_runs() -> None:
    data = small_ledger()
    cohort_ledger.record_attempt(
        data,
        "CU-01-create-greeting",
        cohort_ledger.attempt_record(run_id=RUN_A1, terminal_status=None),
    )
    export = {
        "schema": "forge.cohort.export/1",
        "runs": {
            RUN_A1: {
                "status": "ready_for_human",
                "commit_cycle": 3,
                "usage_receipts": [receipt(input_tokens=10, output_tokens=5)],
                "llm_calls": [llm_call(1500, 90)],
            }
        },
    }
    attached = cohort_runner.attach_export(data, "CU-01-create-greeting", 1, export)
    attempt = data["units"]["CU-01-create-greeting"]["attempts"][0]
    assert attached == 1
    assert attempt["export_attached"] is True
    assert attempt["commit_cycle"] == 3
    assert attempt["terminal_status"] == "ready_for_human"
    assert attempt["receipts"][0]["output_tokens"] == 5

    export["runs"] = {}
    assert cohort_runner.attach_export(data, "CU-01-create-greeting", 1, export) == 0
    assert data["units"]["CU-01-create-greeting"]["attempts"][0]["export_attached"] is False

    with pytest.raises(cohort_runner.RunnerError):
        cohort_runner.attach_export(data, "CU-01-create-greeting", 7, export)
    with pytest.raises(cohort_runner.RunnerError):
        cohort_runner.attach_export(data, "CU-99-missing", 1, export)


def test_report_cli_writes_the_artifact_without_any_live_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "pass.ledger.json"
    cohort_ledger.save_ledger(fixture_ledger(), ledger_path)
    out_path = tmp_path / "artifacts" / "report.json"
    exit_code = cohort_runner.main(["report", "--ledger", str(ledger_path), "--out", str(out_path)])
    assert exit_code == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["schema"] == aggregate.REPORT_SCHEMA
    assert report["counts"]["accepted_units"] == 1
    assert report["per_accepted_unit"]["denominator"] == 1
    assert "accepted=1" in capsys.readouterr().err


def test_observe_cli_attaches_receipts_from_an_export(tmp_path: Path) -> None:
    data = small_ledger()
    cohort_ledger.record_attempt(
        data,
        "CU-01-create-greeting",
        cohort_ledger.attempt_record(run_id=RUN_A1, terminal_status=None),
    )
    ledger_path = tmp_path / "pass.ledger.json"
    cohort_ledger.save_ledger(data, ledger_path)
    export_path = tmp_path / "export.json"
    export_path.write_text(
        json.dumps(
            {
                "schema": "forge.cohort.export/1",
                "runs": {
                    RUN_A1: {
                        "status": "ready_for_human",
                        "commit_cycle": 1,
                        "usage_receipts": [receipt(input_tokens=7, output_tokens=3)],
                        "llm_calls": [llm_call(900, 40)],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    exit_code = cohort_runner.main(
        [
            "observe",
            "--ledger",
            str(ledger_path),
            "--export",
            str(export_path),
            "CU-01-create-greeting",
        ]
    )
    assert exit_code == 0
    reloaded = cohort_ledger.load_ledger(ledger_path)
    attempt = reloaded["units"]["CU-01-create-greeting"]["attempts"][0]
    assert attempt["export_attached"] is True
    assert attempt["receipts"][0]["input_tokens"] == 7
    # the saved ledger must remain a valid, loadable artifact
    assert reloaded["contract_version"] == CONTRACT_VERSION


def test_the_runner_imports_no_forge_module() -> None:
    """The harness stands beside the service — it drives the public surface."""
    source = (REPO_ROOT / "evaluation" / "cohort" / "runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    assert not any(name == "forge" or name.startswith("forge.") for name in imported)


# ---------------------------------------------------------------------------
# The seed's ci.yml proves the UNIT's contract, never forge's (2026-09-20)
# ---------------------------------------------------------------------------


def test_rendered_ci_workflow_runs_exactly_the_predeclared_checks() -> None:
    task = TASKS_BY_ID["CU-01-create-greeting"]
    workflow = yaml.safe_load(cohort_runner._render_ci_workflow(task))
    steps = workflow["jobs"]["checks"]["steps"]
    check_steps = [s for s in steps if "run" in s]
    assert [s["name"] for s in check_steps] == [c.name for c in task.checks]
    for step, check in zip(check_steps, task.checks):
        argv = ["python3" if a == "{python}" else a for a in check.argv]
        assert step["run"].strip() == shlex.join(argv)


def test_every_units_checks_render_to_a_valid_workflow() -> None:
    for task in COHORT_TASKS:
        workflow = yaml.safe_load(cohort_runner._render_ci_workflow(task))
        # pyyaml applies YAML 1.1: a bare ``on:`` key parses as True
        assert workflow[True] == {"pull_request": None, "push": None}
        assert workflow["jobs"]["checks"]["steps"][0]["uses"] == "actions/checkout@v4"
        rendered = cohort_runner._render_ci_workflow(task)
        assert "pyproject" not in rendered  # forge's ci.yml must never leak in


# ---------------------------------------------------------------------------
# Attempt rows record once per drive; the cancel procedure hits the live row
# ---------------------------------------------------------------------------


def _open(tmp_path: Path) -> tuple[Path, dict]:
    path = tmp_path / "ledger.json"
    data = cohort_ledger.new_ledger(repo="acme/lab")
    task = TASKS_BY_ID["CU-14-cancel-mid-run"]
    cohort_ledger.open_unit(data, task)
    return path, data


def test_drive_re_stamp_does_not_duplicate_the_attempt_row(tmp_path: Path) -> None:
    path, data = _open(tmp_path)
    task = TASKS_BY_ID["CU-14-cancel-mid-run"]
    attempt = cohort_ledger.attempt_record(run_id="", started_at="2026-09-20T00:00:00+00:00")
    cohort_runner._record_attempt(path, data, task.unit_id, attempt)
    attempt["run_id"] = "abc"
    attempt["go_posted_at"] = "2026-09-20T00:01:00+00:00"
    cohort_runner._record_attempt(path, data, task.unit_id, attempt)
    reloaded = cohort_ledger.load_ledger(path)
    rows = reloaded["units"][task.unit_id]["attempts"]
    assert len(rows) == 1  # one drive, one row
    assert rows[0]["run_id"] == "abc"  # the stamps persisted


def test_cancel_unit_stamps_the_in_flight_row(tmp_path: Path) -> None:
    path, data = _open(tmp_path)
    task = TASKS_BY_ID["CU-14-cancel-mid-run"]
    old = cohort_ledger.attempt_record(run_id="old", started_at="2026-09-20T00:00:00+00:00")
    cohort_runner._record_attempt(path, data, task.unit_id, old)
    live = cohort_ledger.attempt_record(run_id="live", started_at="2026-09-20T00:05:00+00:00")
    cohort_runner._record_attempt(path, data, task.unit_id, live)

    class FakeGh:
        def __init__(self) -> None:
            self.comments: list[tuple[int, str]] = []

        def add_comment(self, issue_number: int, body: str) -> None:
            self.comments.append((issue_number, body))

    cohort_runner.cancel_unit(FakeGh(), path, data, task.unit_id, 1, 99)
    rows = cohort_ledger.load_ledger(path)["units"][task.unit_id]["attempts"]
    assert rows[-1]["terminal_status"] == "cancelled"  # the in-flight row
    assert rows[-1]["run_id"] == "live"
    assert rows[0]["terminal_status"] != "cancelled"  # history untouched


# ----------------------------------------------------------------------
# B09: unknown stays unknown in the cohort economics
# ----------------------------------------------------------------------


def test_all_unknown_durations_are_none_not_zero() -> None:
    calls = [{"status": "ok", "first_token_ms": 12}, {"status": "ok"}]  # no duration_ms
    agg = aggregate.aggregate_llm_calls(calls)
    assert agg["llm_active_s"] is None  # unknown activity, not 0.0

    merged = aggregate.merge_llm([agg, agg])
    assert merged["llm_active_s"] is None  # a known-0 merge cannot dilute it


def test_partially_priced_unit_costs_report_a_lower_bound_not_a_mean() -> None:
    """B09: attempt 1 priced $1, attempt 2 receipt-less → exact cost is
    UNKNOWN; the report carries a known lower bound and cost_exact=False,
    never the priced subtotal as the unit's cost."""
    unit = {
        "unit_id": "CU-X",
        "axis": "create",
        "title": "t",
        "acceptance": {"verdict": "accepted", "decided_by": "h", "decided_at": "x", "notes": ""},
        "attempts": [
            {
                "receipts": [
                    {
                        "model": "test-model",
                        "input_tokens": 1000,
                        "cached_input_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 0,
                    }
                ]
            },
            {"receipts": []},  # export missing — spend UNKNOWN
        ],
    }
    pricebook = {
        "test-model": {
            "input_tokens": 1.0,
            "cached_input_tokens": 0.0,
            "cache_write_tokens": 0.0,
            "output_tokens": 0.0,
        }
    }
    report = aggregate.cohort_report(
        {
            "schema": "forge.cohort.ledger/2",
            "repo": "a/b",
            "units": {"CU-X": unit},
        },
        pricebook=pricebook,
    )
    per = report["per_accepted_unit"]
    assert per["cost_usd_mean"] is None  # exact unknown
    assert per["cost_exact"] is False
    assert per["cost_known_units"] == 0
    assert per["cost_lower_bound_usd_mean"] == pytest.approx(0.001)


def test_decode_rate_requires_matched_populations() -> None:
    """B09: receipt tokens over llm-call durations is only a rate when the
    populations match — fewer receipts than calls means no proven join."""
    usage = {
        "token_classes": {"output_tokens": 1000},
        "receipt_count": 1,
        "unknown_counts": {},
    }
    llm = {"call_count": 3, "failed_calls": 0, "llm_active_s": 10.0, "ttft": {}}
    assert aggregate._decode_rate(usage, llm) is None  # 1 receipt over 3 calls

    matched = dict(usage, receipt_count=3)
    assert aggregate._decode_rate(matched, llm) == pytest.approx(100.0)
