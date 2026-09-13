"""Tests for the ADR-0008 quality contract and CI failure classification."""

from forge.gitlab.schemas import Job, Pipeline
from forge.runs.ci_contract import classify_failure, evaluate_quality_contract


def job(name: str, status: str, failure_reason: str | None = None) -> Job:
    return Job.model_validate(
        {"id": 1, "name": name, "status": status, "failure_reason": failure_reason}
    )


def pipeline(status: str) -> Pipeline:
    return Pipeline.model_validate({"id": 9, "status": status})


class TestClassifyFailure:
    def test_script_failure_is_code(self):
        jobs = [job("build", "success"), job("test", "failed", "script_failure")]
        assert classify_failure(jobs) == "code"

    def test_no_failure_reason_is_infrastructure(self):
        """Unknown means the evidence does not blame the code (ADR-0008) —
        e.g. a canceled or runner-killed job read with no recorded reason."""
        assert classify_failure([job("test", "failed")]) == "infrastructure"

    def test_runner_system_failure_is_infrastructure(self):
        jobs = [job("build", "failed", "runner_system_failure")]
        assert classify_failure(jobs) == "infrastructure"

    def test_stuck_job_is_infrastructure(self):
        assert classify_failure([job("test", "failed", "stuck")]) == "infrastructure"

    def test_scheduler_failure_is_infrastructure(self):
        assert classify_failure([job("test", "failed", "scheduler_failure")]) == "infrastructure"

    def test_unknown_failure_is_infrastructure(self):
        assert classify_failure([job("test", "failed", "unknown_failure")]) == "infrastructure"

    def test_config_error_is_config(self):
        assert classify_failure([job("build", "failed", "config_error")]) == "config"

    def test_yaml_error_is_config(self):
        assert classify_failure([job("build", "failed", "yaml_error")]) == "config"

    def test_infrastructure_wins_over_script_failure(self):
        """A dead runner plus a failing script means infra — never repair."""
        jobs = [
            job("test", "failed", "script_failure"),
            job("build", "failed", "runner_system_failure"),
        ]
        assert classify_failure(jobs) == "infrastructure"

    def test_config_wins_over_plain_code_when_no_infra(self):
        jobs = [
            job("test", "failed", "script_failure"),
            job("config", "failed", "config_error"),
        ]
        assert classify_failure(jobs) == "config"

    def test_no_failed_jobs_defaults_to_code(self):
        assert classify_failure([job("build", "success")]) == "code"
        assert classify_failure([]) == "code"


class TestEvaluateQualityContract:
    def test_success_without_required_jobs_is_ok(self):
        ok, reason = evaluate_quality_contract(pipeline("success"), [], [])
        assert ok is True
        assert "satisfied" in reason

    def test_failed_pipeline_never_ok(self):
        ok, reason = evaluate_quality_contract(pipeline("failed"), [], [])
        assert ok is False
        assert "failed" in reason

    def test_canceled_pipeline_never_ok(self):
        ok, _ = evaluate_quality_contract(pipeline("canceled"), [], [])
        assert ok is False

    def test_required_job_success_is_ok(self):
        jobs = [job("build", "success"), job("test", "success")]
        ok, _ = evaluate_quality_contract(pipeline("success"), jobs, ["build", "test"])
        assert ok is True

    def test_required_job_skipped_is_not_ok(self):
        """ADR-0008: a skipped required job never counts, even when green."""
        jobs = [job("build", "success"), job("test", "skipped")]
        ok, reason = evaluate_quality_contract(pipeline("success"), jobs, ["test"])
        assert ok is False
        assert "skipped" in reason

    def test_required_job_manual_is_not_ok(self):
        jobs = [job("test", "manual")]
        ok, reason = evaluate_quality_contract(pipeline("success"), jobs, ["test"])
        assert ok is False
        assert "manual" in reason

    def test_missing_required_job_is_not_ok(self):
        ok, reason = evaluate_quality_contract(pipeline("success"), [], ["test"])
        assert ok is False
        assert "not found" in reason

    def test_required_job_failed_is_not_ok_even_on_green_pipeline(self):
        """allow_failure-style greenwash: pipeline green, required job failed."""
        jobs = [job("test", "failed", "script_failure")]
        ok, reason = evaluate_quality_contract(pipeline("success"), jobs, ["test"])
        assert ok is False
        assert "failed" in reason
