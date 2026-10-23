"""Applicable context contracts on the current read-only reactive reviewer."""

from unittest.mock import AsyncMock
from forge.reactive.github_review import GitHubReactiveReviewer, REVIEWER_MAX_DIFF_CHARS


def reviewer(files):
    client = AsyncMock()
    client.get_pr_files.return_value = files
    return GitHubReactiveReviewer(None, client), client


async def test_diff_context_comes_from_the_actual_native_read_owner():
    owner, client = reviewer([{"filename": "app.py", "patch": "+value=1"}])
    text, files = await owner.build_diff("acme", "app", 7)
    client.get_pr_files.assert_awaited_once_with("acme", "app", 7)
    assert "+value=1" in text and files[0]["filename"] == "app.py"


async def test_context_is_deterministically_largest_first():
    owner, _ = reviewer(
        [{"filename": "small.py", "patch": "+a"}, {"filename": "large.py", "patch": "+bbbbbb"}]
    )
    text, _ = await owner.build_diff("acme", "app", 7)
    assert text.index("large.py") < text.index("small.py")


async def test_rendered_diff_has_a_hard_character_bound():
    owner, _ = reviewer(
        [{"filename": "large.py", "patch": "+" + "x" * (REVIEWER_MAX_DIFF_CHARS * 2)}]
    )
    text, _ = await owner.build_diff("acme", "app", 7)
    assert len(text) <= REVIEWER_MAX_DIFF_CHARS


async def test_failed_diff_read_is_explicitly_unavailable():
    owner, client = reviewer([])
    client.get_pr_files.side_effect = ConnectionError("unavailable")
    text, files = await owner.build_diff("acme", "app", 7)
    assert text == "(diff unavailable)" and files == []


async def test_incremental_context_reads_the_exact_commit_pair():
    owner, client = reviewer([])
    client.get_compare.return_value = {"files": [{"filename": "app.py", "patch": "+new"}]}
    text, _ = await owner.build_diff("acme", "app", 7, before_sha="a" * 40, after_sha="b" * 40)
    client.get_compare.assert_awaited_once_with("acme", "app", "a" * 40, "b" * 40)
    client.get_pr_files.assert_not_awaited()
    assert "+new" in text


async def test_an_omitted_native_patch_does_not_invent_diff_lines():
    owner, _ = reviewer([{"filename": "binary.bin", "patch": None}])
    text, files = await owner.build_diff("acme", "app", 7)
    assert text == "" and files[0]["patch"] is None
