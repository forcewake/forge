"""Research grading retires; discovery's actual evidence boundaries remain."""

import pytest

from forge.adaptive.discovery import (
    DiscoveryRun,
    fast_path_plan_marked,
    plan_digest,
    structured_plan,
    validate_plan_citations,
)
from forge.adaptive.discovery_tools import SnapshotToolbox


def test_unanswered_question_blocks_completion_and_keeps_evidence_on_resume():
    first = DiscoveryRun("d1", "w1", "a" * 64).start().record_evidence("e1")
    waiting = first.raise_question("critical-question")
    with pytest.raises(ValueError):
        waiting.complete()
    resolved = waiting.resolve_question("critical-question").complete()
    assert DiscoveryRun.resume(resolved) == resolved
    assert resolved.evidence_bundle == ("e1",)
    assert first.status == "running"


def test_fast_path_is_never_relabelled_repository_researched():
    assert fast_path_plan_marked("docs typo")["repository_researched"] is False


def test_complete_steps_are_bound_by_digest_beyond_human_summary():
    steps = [{"step_id": str(i), "text": "long step " * 20} for i in range(30)]
    plan = structured_plan("brief summary", steps, ["assumption"], ["unknown"], ["ask owner"])
    assert len(plan["steps"]) == 30
    original = plan_digest(plan)
    changed = {**plan, "steps": [*steps[:-1], {"step_id": "29", "text": "changed"}]}
    assert plan_digest(changed) != original
    assert plan["unknowns"] == ["unknown"]


def test_missing_or_unpinned_evidence_cannot_become_a_valid_citation():
    steps = [{"step_id": "s1", "evidence_refs": ["missing"]}]
    assert validate_plan_citations(steps, {})


def test_exhausted_output_budget_is_visible_and_paging_keeps_exact_bytes():
    toolbox = SnapshotToolbox({"src/a.py": "abcdefgh"}, max_output_bytes=4)
    first = toolbox.read_file("src/a.py")
    second = toolbox.read_file("src/a.py", offset=4)
    assert first["truncated"] and not first["complete"]
    assert first["content"] + second["content"] == "abcdefgh"
