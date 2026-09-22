"""The rendered driver scripts as first-class artifacts (phase 0-2 bridge).

docs/research/2026-09-22-script-rendering-architecture.md §7: the driver
scripts rendered by ``forge.harness_entry.render_driver_script`` are pinned
byte-for-byte against golden fixtures
(``tests/fixtures/rendered/<driver>[-mcp].sh``) — every rendering refactor
must keep them identical or make the change deliberately, in review.

The mechanical gates ride the same surface: every rendered combination must
parse under ``bash -n`` (and pass ``shellcheck`` where installed) — the
deterministic replacement for substring approximations of the same
guarantees — and the shipped CI template files must carry no ``<TOKEN>``
placeholders (a raw copy reaching a runner must be valid as-is).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from forge.harness_entry import DRIVERS, render_driver_script

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "rendered"

#: The deterministic render surface each fixture pins: a fixed model route
#: and the documented one-HTTP-server MCP config (the same server shape the
#: harness-entry tests use). Everything else rides the renderer defaults.
MODEL = "glm-5.3-flash"
BRIEF = ".forge/brief.md"
MCP_SERVER = {"context7": {"type": "http", "url": "https://mcp.example.com/mcp"}}

#: driver × (no MCP, one HTTP MCP server) — every combination gets a golden
#: fixture and a mechanical parse gate.
COMBINATIONS = [(driver, mcp) for driver in DRIVERS for mcp in (False, True)]

_TEMPLATE_FILES = sorted((ROOT / "ci" / "templates").glob("*.yml")) + [
    ROOT / ".github" / "workflows" / "forge-harness.yml",
]

_PLACEHOLDER_RE = re.compile(r"<[A-Z_]+>")


def render_for(driver: str, mcp: bool) -> str:
    """The rendered script for one golden combination."""
    return render_driver_script(
        driver, MODEL, BRIEF, mcp_servers=(dict(MCP_SERVER) if mcp else None)
    )


def fixture_name(driver: str, mcp: bool) -> str:
    return f"{driver}{'-mcp' if mcp else ''}.sh"


def regenerate() -> None:
    """(Re)write the golden fixtures from the CURRENT renderer.

    Run deliberately, never from the test suite::

        uv run python -c "from tests.test_script_rendering import regenerate; regenerate()"

    A regenerated diff is reviewable byte-for-byte — that is the whole
    point of the golden bridge.
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for driver, mcp in COMBINATIONS:
        target = FIXTURES_DIR / fixture_name(driver, mcp)
        target.write_bytes(render_for(driver, mcp).encode())
        print(f"wrote {target.relative_to(ROOT)}")


# ----------------------------------------------------------------------
# Phase 0 — the golden contract (byte-for-byte, every combination)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "driver,mcp", COMBINATIONS, ids=[f"{d}{'-mcp' if m else ''}" for d, m in COMBINATIONS]
)
def test_golden_fixtures(driver: str, mcp: bool):
    """The rendered script equals its golden fixture byte-for-byte."""
    fixture = FIXTURES_DIR / fixture_name(driver, mcp)
    assert fixture.is_file(), f"missing golden fixture {fixture.name}"
    assert render_for(driver, mcp).encode() == fixture.read_bytes(), (
        f"rendered {fixture_name(driver, mcp)} drifted from its golden fixture "
        "(regenerate deliberately and review the diff)"
    )


# ----------------------------------------------------------------------
# Phase 2 — mechanical gates over the same combinations
# ----------------------------------------------------------------------


class TestRenderedScriptsParse:
    def test_bash_n_accepts_every_rendered_script(self):
        """``bash -n`` (parse-only) accepts every rendered combination — the
        A09 class (silently glued shell fragments) is a parse/lex failure
        here, deterministically."""
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash unavailable")
        for driver, mcp in COMBINATIONS:
            proc = subprocess.run(
                [bash, "-n"],
                input=render_for(driver, mcp).encode(),
                capture_output=True,
                check=False,
            )
            assert proc.returncode == 0, (driver, mcp, proc.stderr.decode(errors="replace"))

    def test_shellcheck_accepts_every_rendered_script(self):
        """shellcheck (where installed) over every rendered combination —
        advisory severity stays visible, errors fail the gate."""
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            pytest.skip("shellcheck unavailable")
        for driver, mcp in COMBINATIONS:
            proc = subprocess.run(
                [shellcheck, "-"],
                input=render_for(driver, mcp).encode(),
                capture_output=True,
                check=False,
            )
            assert proc.returncode == 0, (driver, mcp, proc.stderr.decode(errors="replace"))


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=lambda p: p.name)
def test_no_placeholder_tokens_in_shipped_ci_files(path: Path):
    """Phase 3 (§4.3): a shipped template is valid AS-IS — no ``<TOKEN>``
    a human must hand-replace before the runner sees it (the LIVE
    ``<PINNED_REF>`` class: pip attempted the literal ref and the lane died
    in bootstrap). Real working defaults, environment override at runtime."""
    text = path.read_text()
    matches = _PLACEHOLDER_RE.findall(text)
    assert matches == [], f"{path.name} carries placeholder tokens {matches}"
