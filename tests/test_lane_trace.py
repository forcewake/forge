"""CI trace folding never changes execution or exposes authority."""

from datetime import datetime, timezone

import pytest

from forge_lane.driver import DriverEvent
from forge_lane.errors import ExitCode
from forge_lane.trace import Trace
from forge_protocol import Usage
from tests.test_lane_runner_v1 import FakeDriver, WireServer, checkout, execute, inputs

NOW = datetime(2026, 10, 23, 12, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("platform", ["actions", "gitlab", "flat"])
def test_native_groups_have_bounded_redacted_output_and_close_once(platform):
    output = []
    trace = Trace("driver", "model", platform=platform, emit=output.append, now=lambda: NOW)
    trace.event(DriverEvent("tool_start", text="src/main.py", tool_name="Read"))
    trace.event(DriverEvent("tool_result", text="file contents", is_error=False))
    trace.finish("completed", Usage(input_tokens=12, output_tokens=3))
    trace.finish("failed")
    trace.event(DriverEvent("message", text="late"))
    assert output[0] == "── driver · model model ──"
    assert output[-2] == "12:30:00 ■ completed · hidden 0"
    assert output[-1] == (
        'FORGE_USAGE:{"input_tokens":12,"output_tokens":3,'
        '"completeness":"aggregate","source":"forge-lane:driver"}'
    )
    assert "late" not in "\n".join(output)
    if platform == "actions":
        assert output[1] == "::group::🔧 Read src/main.py"
        assert output[3] == "::endgroup::"
    elif platform == "gitlab":
        assert output[1] == (
            f"\x1b[0Ksection_start:{int(NOW.timestamp())}:forge_tool_0"
            "[collapsed=true]\r\x1b[0K🔧 Read src/main.py"
        )
        assert output[3] == f"\x1b[0Ksection_end:{int(NOW.timestamp())}:forge_tool_0\r\x1b[0K"
    else:
        assert output[1] == "12:30:00 🔧 Read src/main.py"
        assert len(output) == 5


def test_untrusted_text_cannot_emit_commands_or_credentials_and_suppression_is_counted():
    output = []
    trace = Trace(
        "driver",
        "model",
        platform="actions",
        secrets=("attempt-key", "session-key"),
        emit=output.append,
        now=lambda: NOW,
    )
    payload = (
        "attempt-key session-key Bearer abc-secret glpat-private "
        "https://user:password@example.test/x\n::error::forged\r\x1b[31mRED\x1b[0m"
    )
    trace.event(DriverEvent("message", text=payload))
    for _ in range(200):
        trace.event(DriverEvent("message", text="routine"))
    trace.event(DriverEvent("tool_start", tool_name="Read", text=payload * 100))
    trace.finish("failed")
    joined = "\n".join(output)
    for secret in ("attempt-key", "session-key", "abc-secret", "glpat-private", "password"):
        assert secret not in joined
    assert not any(line.startswith("::error::") for line in output)
    assert all("\n" not in line and "\r" not in line and "\x1b" not in line for line in output)
    assert "100 prose events hidden" in joined and "hidden 101" in joined
    assert len(output[-3]) <= len("::group::") + 160
    assert "FORGE_USAGE:" not in joined


async def test_real_runner_trace_reports_empty_diff_instead_of_driver_success(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    dispatch, brief = inputs(checkout(workspace))
    server = WireServer(dispatch, brief)
    result = await execute(dispatch, server, FakeDriver(), workspace, trace_platform="actions")
    assert result.exit_code == ExitCode.EMPTY_DIFF and result.report_accepted
    output = capsys.readouterr().out
    assert "■ empty_diff" in output and "■ completed" not in output
    assert "FORGE_USAGE:" not in output


def test_closed_log_output_is_not_an_execution_failure():
    def closed(_):
        raise BrokenPipeError("closed log")

    trace = Trace("driver", "model", emit=closed)
    trace.event(DriverEvent("tool_start", tool_name="Read"))
    trace.event(DriverEvent("message", text="visible"))
    trace.finish("completed", Usage(input_tokens=1, output_tokens=2))
    assert trace.closed
