from forge.context.log_parser import (
    ParsedJobLog,
    format_parsed_log,
    parse_job_log,
)


class TestParseJobLog:
    def test_empty_log(self):
        result = parse_job_log("")
        assert result.full_length == 0
        assert result.error_type is None

    def test_python_test_failure(self):
        log = (
            "$ pytest tests/\n"
            "collecting ...\n"
            "tests/test_foo.py::test_bar FAILED\n"
            "E       AssertionError: expected 1, got 2\n"
            "FAILED tests/test_foo.py::test_bar - AssertionError\n"
            "=== 1 failed, 5 passed ===\n"
        )
        result = parse_job_log(log)
        assert result.error_type == "test_failure"
        assert result.full_length == 6
        assert any("AssertionError" in line for line in result.key_lines)

    def test_npm_dependency_error(self):
        log = (
            "$ npm install\n"
            "npm ERR! code ERESOLVE\n"
            "npm ERR! Could not resolve dependency: react@^19.0.0\n"
            "npm ERR! Fix the upstream dependency conflict\n"
        )
        result = parse_job_log(log)
        assert result.error_type == "dependency"

    def test_docker_build_failure(self):
        log = (
            "Step 1/10: FROM node:18\n"
            "Sending build context...\n"
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n"
        )
        result = parse_job_log(log)
        assert result.error_type == "docker"

    def test_timeout(self):
        log = "Running tests...\n...\nJob exceeded time limit\n"
        result = parse_job_log(log)
        assert result.error_type == "timeout"

    def test_syntax_error(self):
        log = (
            "$ python main.py\n"
            '  File "main.py", line 10\n'
            "    def foo(:\n"
            "SyntaxError: invalid syntax\n"
        )
        result = parse_job_log(log)
        assert result.error_type == "syntax"

    def test_permission_denied(self):
        log = "Permission denied: /usr/local/bin/script.sh\n"
        result = parse_job_log(log)
        assert result.error_type == "permission"

    def test_network_error(self):
        log = "Could not resolve host: registry.example.com\n"
        result = parse_job_log(log)
        assert result.error_type == "network"

    def test_config_error(self):
        log = ".gitlab-ci.yml error: jobs config should contain at least one job\n"
        result = parse_job_log(log)
        assert result.error_type == "config"

    def test_clean_log_returns_unknown(self):
        log = "Step 1: Building...\nStep 2: Done.\nAll good!\n"
        result = parse_job_log(log)
        assert result.error_type == "unknown"

    def test_exit_code_extraction(self):
        log = "Running script...\nERROR: Job failed: exit status 137\n"
        result = parse_job_log(log)
        assert result.exit_code == 137

    def test_long_log_truncated(self):
        lines = [f"Line {i}: some output" for i in range(1000)]
        lines[500] = "FAILED: test_something"
        log = "\n".join(lines)
        result = parse_job_log(log, max_lines=100)
        assert result.truncated
        assert result.full_length == 1000

    def test_error_section_includes_context(self):
        lines = ["line " + str(i) for i in range(100)]
        lines[50] = "FAILED tests/test_foo.py - AssertionError"
        log = "\n".join(lines)
        result = parse_job_log(log)
        # Should include lines around the error
        assert "FAILED tests/test_foo.py" in result.error_section
        # Error type should be detected
        assert result.error_type == "test_failure"

    def test_tail_included(self):
        lines = ["line " + str(i) for i in range(50)]
        log = "\n".join(lines)
        result = parse_job_log(log)
        assert "line 49" in result.tail


class TestFormatParsedLog:
    def test_format_with_error_type(self):
        parsed = ParsedJobLog(
            full_length=100,
            truncated=True,
            error_type="test_failure",
            exit_code=1,
            error_section="FAILED test_foo",
            key_lines=["FAILED test_foo"],
            tail="=== 1 failed ===",
        )
        formatted = format_parsed_log(parsed)
        assert "test_failure" in formatted
        assert "Exit Code" in formatted
        assert "FAILED test_foo" in formatted

    def test_format_empty_log(self):
        parsed = ParsedJobLog(full_length=0)
        formatted = format_parsed_log(parsed)
        assert "0 lines" in formatted

    def test_format_falls_back_to_tail(self):
        parsed = ParsedJobLog(
            full_length=10,
            error_section="",
            tail="last line of output",
        )
        formatted = format_parsed_log(parsed)
        assert "last line of output" in formatted
