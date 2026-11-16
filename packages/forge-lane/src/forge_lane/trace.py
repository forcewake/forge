"""Bounded, credential-redacted execution traces with native CI folding."""

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from forge_protocol import Usage

from .driver import DriverEvent

Platform = Literal["actions", "gitlab", "flat"]
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CREDENTIAL = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+\S+|\b(?:sk-|ghp_|github_pat_|glpat-)[\w.-]+)"
)


class Trace:
    def __init__(
        self,
        driver: str,
        model: str,
        *,
        platform: Platform = "flat",
        secrets: tuple[str, ...] = (),
        emit: Callable[[str], None] = print,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.platform, self.secrets, self.sink, self.now = platform, secrets, emit, now
        self.group: str | None = None
        self.hidden = 0
        self.messages = 0
        self.closed = False
        self.emit(f"── {self.clean(driver)} · model {self.clean(model)} ──")

    def emit(self, text: str) -> None:
        try:
            self.sink(text)
        except (OSError, ValueError, UnicodeError):
            pass

    def clean(self, text: str, limit: int = 4096) -> str:
        text = _CONTROL.sub("", _ANSI.sub("", text))
        for secret in sorted(set(self.secrets) - {""}, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        text = _CREDENTIAL.sub("[REDACTED]", text)
        text = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)
        text = text.replace("##[", "## [")
        return " ".join(text.split())[:limit]

    def line(self, glyph: str, text: str) -> None:
        self.emit(f"{self.now():%H:%M:%S} {glyph} {self.clean(text)}")

    def close_group(self) -> None:
        if self.group is not None:
            if self.platform == "actions":
                self.emit("::endgroup::")
            elif self.platform == "gitlab":
                self.emit(f"\x1b[0Ksection_end:{int(self.now().timestamp())}:{self.group}\r\x1b[0K")
            self.group = None

    def event(self, event: DriverEvent) -> None:
        if self.closed:
            return
        if event.kind == "tool_start":
            self.close_group()
            self.group = "forge_tool_" + str(self.messages)
            self.messages += 1
            title = self.clean(f"🔧 {event.tool_name} {event.text}", 160)
            if self.platform == "actions":
                self.emit("::group::" + title)
            elif self.platform == "gitlab":
                self.emit(
                    f"\x1b[0Ksection_start:{int(self.now().timestamp())}:{self.group}"
                    f"[collapsed=true]\r\x1b[0K{title}"
                )
            else:
                self.line("🔧", f"{event.tool_name} {event.text}")
        elif event.kind == "tool_result":
            self.line("❌" if event.is_error else "✅", event.text or event.tool_name)
            self.close_group()
        elif event.kind == "message" and event.text:
            self.messages += 1
            if self.messages <= 100:
                self.line("💬", event.text)
            else:
                self.hidden += 1
                if self.hidden % 100 == 0:
                    self.line("…", f"{self.hidden} prose events hidden")
        elif event.kind not in ("completed", "failed"):
            self.hidden += 1

    def finish(self, classification: str, usage: Usage | None = None, *, detail: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_group()
        summary = (
            classification
            if not detail or detail == classification
            else f"{classification}: {detail}"
        )
        self.line("■", f"{summary} · hidden {self.hidden}")
        if usage is not None:
            self.emit(
                "FORGE_USAGE:"
                + json.dumps(
                    {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "completeness": "aggregate",
                        "source": "forge-lane:driver",
                    },
                    separators=(",", ":"),
                )
            )
