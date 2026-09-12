from unittest.mock import AsyncMock

from forge.agents.tools.gitlab_tools import GitLabToolkit


def _make_diff_refs():
    mock = AsyncMock()
    mock.base_sha = "aaa111"
    mock.head_sha = "bbb222"
    mock.start_sha = "ccc333"
    return mock


def _make_toolkit(diff_refs=None):
    gitlab = AsyncMock()
    gitlab.create_mr_discussion.return_value = AsyncMock(id="disc_123")
    gitlab.create_mr_note.return_value = AsyncMock(id=456)
    gitlab.add_mr_labels.return_value = AsyncMock()
    gitlab.remove_mr_labels.return_value = AsyncMock()
    gitlab.get_file.return_value = AsyncMock(content="file content", encoding="utf-8")

    toolkit = GitLabToolkit(
        gitlab=gitlab,
        project_id=1,
        mr_iid=42,
        diff_refs=diff_refs or _make_diff_refs(),
    )
    return toolkit, gitlab


class TestPostInlineComment:
    async def test_posts_with_position(self):
        toolkit, gitlab = _make_toolkit()
        result = await toolkit.post_inline_comment(
            file="src/main.py",
            line=10,
            line_type="new",
            body="This looks wrong",
        )
        assert "disc_123" in result
        assert len(toolkit.discussion_ids) == 1

        # Check the position dict
        call_kwargs = gitlab.create_mr_discussion.call_args
        position = call_kwargs.kwargs.get("position") or call_kwargs[1].get("position")
        assert position["new_line"] == 10
        assert position["new_path"] == "src/main.py"
        assert position["position_type"] == "text"
        assert position["base_sha"] == "aaa111"

    async def test_posts_old_line(self):
        toolkit, gitlab = _make_toolkit()
        await toolkit.post_inline_comment(
            file="src/main.py",
            line=5,
            line_type="old",
            body="This was better before",
        )
        call_kwargs = gitlab.create_mr_discussion.call_args
        position = call_kwargs.kwargs.get("position") or call_kwargs[1].get("position")
        assert position["old_line"] == 5
        assert "new_line" not in position

    async def test_includes_suggestion(self):
        toolkit, gitlab = _make_toolkit()
        await toolkit.post_inline_comment(
            file="src/main.py",
            line=10,
            line_type="new",
            body="Use a list comprehension",
            suggestion="items = [x for x in data]",
        )
        call_args = gitlab.create_mr_discussion.call_args
        body = call_args[0][2] if len(call_args[0]) > 2 else call_args.kwargs.get("body")
        assert "```suggestion" in body

    async def test_falls_back_to_note_without_diff_refs(self):
        toolkit, gitlab = _make_toolkit(diff_refs=AsyncMock(base_sha=None))
        result = await toolkit.post_inline_comment(
            file="src/main.py",
            line=10,
            line_type="new",
            body="Issue here",
        )
        # Should have called create_mr_note instead
        gitlab.create_mr_note.assert_called_once()
        assert "Posted note" in result

    async def test_falls_back_on_api_error(self):
        toolkit, gitlab = _make_toolkit()
        gitlab.create_mr_discussion.side_effect = Exception("API error")
        result = await toolkit.post_inline_comment(
            file="src/main.py",
            line=10,
            line_type="new",
            body="Issue",
        )
        gitlab.create_mr_note.assert_called_once()
        assert "Posted note" in result


class TestPostNote:
    async def test_posts_note(self):
        toolkit, gitlab = _make_toolkit()
        result = await toolkit.post_note(body="Great work!")
        gitlab.create_mr_note.assert_called_once_with(1, 42, "Great work!")
        assert "Posted note" in result


class TestLabels:
    async def test_add_label(self):
        toolkit, gitlab = _make_toolkit()
        result = await toolkit.add_label("ai-reviewed")
        gitlab.add_mr_labels.assert_called_once_with(1, 42, ["ai-reviewed"])
        assert "Added label" in result

    async def test_remove_label(self):
        toolkit, gitlab = _make_toolkit()
        result = await toolkit.remove_label("needs-review")
        gitlab.remove_mr_labels.assert_called_once_with(1, 42, ["needs-review"])
        assert "Removed label" in result


class TestGetFileContent:
    async def test_fetches_file(self):
        toolkit, gitlab = _make_toolkit()
        result = await toolkit.get_file_content("README.md")
        gitlab.get_file.assert_called_once_with(1, "README.md", "HEAD")
        assert result == "file content"

    async def test_decodes_base64(self):
        toolkit, gitlab = _make_toolkit()
        import base64 as b64

        encoded = b64.b64encode(b"decoded content").decode()
        gitlab.get_file.return_value = AsyncMock(content=encoded, encoding="base64")
        result = await toolkit.get_file_content("src/lib.py", ref="main")
        assert result == "decoded content"
