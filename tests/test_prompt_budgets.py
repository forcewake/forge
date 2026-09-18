"""Reservable prompt sections (R32): critical instructions survive big evidence.

``_build_user_prompt`` assembles the mandatory parts first — task, output
contract, and the reserved repair diagnosis — and packs file evidence into
the remaining budget (plan-touched files first, the biggest truncated
first). With eight 6000-char evidence files and a repair context, the repair
text and the branch/commit contract MUST reach the model while some evidence
is truncated, and a machine-readable budget report must say exactly what
made it in. Small inputs keep every section, emit no report, and stay
byte-for-byte the familiar layout.
"""

import json

import pytest

from forge.durable import FlowRun
from forge.factory.implementer import (
    IMPLEMENTER_MAX_INPUT_CHARS,
    IMPLEMENTER_MIN_EVIDENCE_SLICE_CHARS,
    IMPLEMENTER_REPAIR_RESERVED_CHARS,
    LLMImplementer,
    _pack_evidence,
    _rank_evidence_paths,
)
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_llm import FakeLLM

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Tweak the widget"
BASE_SHA = "base-sha-1"
EXPECTED_BRANCH = f"factory/{ISSUE_IID}/runabc12"  # factory_branch uses run_id[:8]
EXPECTED_COMMIT = f"forge: implement {ISSUE_IID} (run runabc12)"
REPORT_HEADER = "Evidence budget report (machine-readable):"
TRUNCATION_MARKER = "[... file truncated to fit the prompt budget]"


def make_run(**overrides) -> FlowRun:
    values = dict(id="runabc123", project_id=PROJECT_ID, issue_iid=ISSUE_IID, base_sha=BASE_SHA)
    values.update(overrides)
    return FlowRun(**values)


def create_draft(path: str, content: str = "# new\n") -> str:
    return json.dumps(
        {
            "branch": "model/chose/this",
            "commit_message": "model's own message",
            "changes": [{"path": path, "operation": "create", "content": content}],
        }
    )


def evidence_file(index: int, size: int = 6000) -> tuple[str, str]:
    """A unique-marked evidence blob of exactly *size* chars."""
    path = f"src/evidence_{index}.py"
    head, tail = f"HEAD-{index}", f"TAIL-{index}"
    content = head + "x" * (size - len(head) - len(tail)) + tail
    assert len(content) == size
    return path, content


def eight_big_files() -> tuple[list[str], dict[str, str]]:
    hints, contents = [], {}
    for index in range(8):
        path, content = evidence_file(index)
        hints.append(path)
        contents[path] = content
    return hints, contents


def sent_user_prompt(llm: FakeLLM) -> str:
    calls = llm.calls_for("implementer")
    assert calls, "the implementer never called the LLM"
    return calls[0]["user"]


def parse_report(user: str) -> dict:
    assert REPORT_HEADER in user, "overflowed prompt carries no budget report"
    return json.loads(user.split(REPORT_HEADER, 1)[1].strip())


@pytest.fixture()
def repo() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_commit("main", BASE_SHA, "initial")
    return fake


class TestRepairSurvivesBigEvidence:
    """The R32 headline: 8 x 6000 chars of evidence must not eat the repair."""

    async def test_actual_request_keeps_repair_and_contract(self, repo):
        hints, contents = eight_big_files()
        for path, content in contents.items():
            repo.seed_file(path, content)
        repair_context = (
            "CI job 'pytest' failed on the previous commit.\n"
            "REPAIR-SIGNAL-42: AssertionError: widget count is 0\n" + "log line\n" * 40
        )
        llm = FakeLLM(script=[create_draft("forge-demo/fix.py")])
        implementer = LLMImplementer(llm, gitlab=repo)

        await implementer.propose(
            make_run(), ISSUE_TITLE, files_hint=hints, repair_context=repair_context
        )

        user = sent_user_prompt(llm)

        # The mandatory parts survived, verbatim and complete.
        assert repair_context in user
        assert "REPAIR-SIGNAL-42" in user
        assert "A previous attempt was committed and its CI failed" in user
        assert f"Use branch: {EXPECTED_BRANCH}" in user
        assert f"Use commit message: {EXPECTED_COMMIT}" in user

        # ...while the evidence could not all fit.
        assert len(user) <= IMPLEMENTER_MAX_INPUT_CHARS
        tails_present = sum(1 for i in range(8) if f"TAIL-{i}" in user)
        assert tails_present < 8, "evidence was supposed to hit the budget"

        # Order: task/contract, then the reserved repair, then evidence,
        # then the completeness report — tail truncation can only cut
        # evidence, never the diagnosis or the output contract.
        assert (
            user.index(f"Use branch: {EXPECTED_BRANCH}")
            < user.index("A previous attempt")
            < user.index("Current file contents:")
            < user.index(REPORT_HEADER)
        )

    async def test_budget_report_reflects_included_and_omitted(self, repo):
        hints, contents = eight_big_files()
        for path, content in contents.items():
            repo.seed_file(path, content)
        llm = FakeLLM(script=[create_draft("forge-demo/fix.py")])
        implementer = LLMImplementer(llm, gitlab=repo)

        await implementer.propose(make_run(), ISSUE_TITLE, files_hint=hints)

        user = sent_user_prompt(llm)
        report = parse_report(user)

        assert report["budget_chars"] == IMPLEMENTER_MAX_INPUT_CHARS
        assert report["repair_reserved_chars"] == IMPLEMENTER_REPAIR_RESERVED_CHARS
        assert report["repair_context_included_chars"] == 0

        evidence = report["evidence"]
        reported = [
            entry["path"] for key in ("included", "truncated", "omitted") for entry in evidence[key]
        ]
        assert sorted(reported) == sorted(hints), "every evidence file is accounted for"

        for entry in evidence["included"]:
            # "included" means the FULL file text is in the request.
            assert contents[entry["path"]] in user
            assert entry["chars"] == len(contents[entry["path"]])
        for entry in evidence["truncated"]:
            assert entry["kept_chars"] < entry["full_chars"]
            assert entry["kept_chars"] >= IMPLEMENTER_MIN_EVIDENCE_SLICE_CHARS
            kept_block = (
                f"--- FILE: {entry['path']} ---\n"
                + contents[entry["path"]][: entry["kept_chars"]]
                + f"\n{TRUNCATION_MARKER}"
            )
            assert kept_block in user
        for entry in evidence["omitted"]:
            assert contents[entry["path"]] not in user
            assert f"--- FILE: {entry['path']} ---" not in user


class TestSmallInputsUnchanged:
    """No overflow, no report, no gratuitous reformatting of the happy path."""

    async def test_small_prompt_keeps_all_evidence_and_emits_no_report(self, repo):
        repo.seed_file("src/a.py", "alpha = 1\n")
        repo.seed_file("src/b.py", "beta = 2\n")
        llm = FakeLLM(script=[create_draft("forge-demo/fix.py")])
        implementer = LLMImplementer(llm, gitlab=repo)

        await implementer.propose(make_run(), ISSUE_TITLE, files_hint=["src/a.py", "src/b.py"])

        user = sent_user_prompt(llm)
        assert "alpha = 1\n" in user
        assert "beta = 2\n" in user
        assert REPORT_HEADER not in user
        assert TRUNCATION_MARKER not in user
        assert len(user) <= IMPLEMENTER_MAX_INPUT_CHARS

    def test_small_prompt_layout_is_byte_stable(self):
        implementer = LLMImplementer(FakeLLM(), gitlab=FakeGitLab())

        user = implementer._build_user_prompt(
            issue="Issue title: T\n\nIssue description:\ndesc",
            plan_summary="the plan",
            paths=["src/a.py"],
            contents={"src/a.py": "ALPHA"},
            branch="b1",
            commit_message="m1",
            repair_context="",
        )
        assert user == (
            "Issue title: T\n\nIssue description:\ndesc\n"
            "\n"
            "Implementation plan (summary):\nthe plan\n"
            "\n"
            "Repository files (paths):\nsrc/a.py\n"
            "\n"
            "Use branch: b1\nUse commit message: m1\n"
            "\n"
            "Current file contents:\n--- FILE: src/a.py ---\nALPHA"
        )

    def test_small_repair_prompt_layout_is_byte_stable(self):
        implementer = LLMImplementer(FakeLLM(), gitlab=FakeGitLab())

        user = implementer._build_user_prompt(
            issue="the issue",
            plan_summary="",
            paths=[],
            contents={"src/a.py": "ALPHA"},
            branch="b1",
            commit_message="m1",
            repair_context="pytest failed: 1 == 2",
        )
        assert user == (
            "the issue\n"
            "\n"
            "Implementation plan (summary):\n(no plan summary available)\n"
            "\n"
            "Repository files (paths):\n\n"
            "\n"
            "Use branch: b1\nUse commit message: m1\n"
            "\n"
            "A previous attempt was committed and its CI failed. Propose a "
            "REPAIR: fix the failing code on top of the previous change.\n"
            "pytest failed: 1 == 2\n"
            "\n"
            "Current file contents:\n--- FILE: src/a.py ---\nALPHA"
        )


class TestReservationAndRanking:
    """The repair reservation is real budget; ranking puts the plan first."""

    async def test_full_reserved_repair_context_survives_eight_big_files(self, repo):
        hints, contents = eight_big_files()
        for path, content in contents.items():
            repo.seed_file(path, content)
        repair_context = "R" * IMPLEMENTER_REPAIR_RESERVED_CHARS
        llm = FakeLLM(script=[create_draft("forge-demo/fix.py")])
        implementer = LLMImplementer(llm, gitlab=repo)

        await implementer.propose(
            make_run(), ISSUE_TITLE, files_hint=hints, repair_context=repair_context
        )

        user = sent_user_prompt(llm)
        # Every reserved char of the diagnosis reached the model.
        assert repair_context in user
        assert parse_report(user)["repair_context_included_chars"] == len(repair_context)
        assert len(user) <= IMPLEMENTER_MAX_INPUT_CHARS

    def test_rank_puts_plan_files_first_and_biggest_at_the_boundary(self):
        contents = {
            "aaa_big.py": "x" * 9000,
            "zzz_small.py": "x" * 10,
            "mmm_small.py": "x" * 10,
            "plan_touched.py": "y" * 5000,
        }

        ranked = _rank_evidence_paths(contents, ["plan_touched.py"])

        assert ranked[0] == "plan_touched.py"
        # Non-hinted files come smallest-first: the biggest sit at the
        # budget boundary and are truncated/omitted first.
        assert ranked[1:] == ["mmm_small.py", "zzz_small.py", "aaa_big.py"]
        assert _rank_evidence_paths(contents, []) == [
            "mmm_small.py",
            "zzz_small.py",
            "plan_touched.py",
            "aaa_big.py",
        ]

    def test_packing_truncates_the_boundary_file_and_reports_it(self):
        marker = "\n[... file truncated to fit the prompt budget]"
        contents = {"small.py": "s" * 50, "big.py": "B" * 5000}
        header = len("Current file contents:") + 1
        small_block = len("--- FILE: small.py ---\n") + 50
        big_prefix = len("--- FILE: big.py ---\n")
        kept = 250  # above IMPLEMENTER_MIN_EVIDENCE_SLICE_CHARS
        budget = header + small_block + 2 + big_prefix + kept + len(marker)

        section, included, truncated, omitted = _pack_evidence(contents, ["small.py"], budget)

        assert section == (
            "Current file contents:\n"
            "--- FILE: small.py ---\n" + "s" * 50 + "\n\n"
            "--- FILE: big.py ---\n" + "B" * kept + marker
        )
        assert included == [{"path": "small.py", "chars": 50}]
        assert truncated == [{"path": "big.py", "kept_chars": kept, "full_chars": 5000}]
        assert omitted == []
        assert len(section) <= budget

    def test_packing_omits_instead_of_leaving_a_sliver(self):
        contents = {"small.py": "s" * 50, "big.py": "B" * 5000}
        header = len("Current file contents:") + 1
        small_block = len("--- FILE: small.py ---\n") + 50
        # Room for the big file's prefix leaves less than the minimum slice.
        budget = header + small_block + 2 + len("--- FILE: big.py ---\n") + 10

        section, included, truncated, omitted = _pack_evidence(contents, ["small.py"], budget)

        assert "big.py" not in section
        assert included == [{"path": "small.py", "chars": 50}]
        assert truncated == []
        assert omitted == [{"path": "big.py", "full_chars": 5000}]

    def test_packing_never_revisits_a_spent_budget(self):
        # Regression guard: after a truncation consumed the budget, a later
        # file must be reported omitted, never partially included twice.
        contents = {
            "a.py": "A" * 1000,
            "b.py": "B" * 1000,
            "c.py": "C" * 1000,
        }

        _, included, truncated, omitted = _pack_evidence(contents, [], 1000)

        reported = len(included) + len(truncated) + len(omitted)
        assert reported == 3
        kept = sum(int(entry["chars"]) for entry in included)
        kept += sum(int(entry["kept_chars"]) for entry in truncated)
        assert kept <= 1000
