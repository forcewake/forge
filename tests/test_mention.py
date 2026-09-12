"""Tests for @mention extraction from GitLab note bodies."""

import pytest

from forge.gateway.mention import extract_mention


class TestExtractMention:
    """Test the extract_mention function."""

    def test_basic_mention(self):
        result = extract_mention("@forge explain this function")
        assert result.is_mention is True
        assert result.mention_text == "explain this function"
        assert result.slash_command is None
        assert result.command_args is None

    def test_no_mention(self):
        result = extract_mention("No mention here")
        assert result.is_mention is False
        assert result.mention_text == ""
        assert result.slash_command is None

    def test_trailing_mention(self):
        result = extract_mention("Thanks @forge")
        assert result.is_mention is True
        assert result.mention_text == ""
        assert result.slash_command is None

    def test_mention_in_middle(self):
        result = extract_mention("Hey @forge can you explain this?")
        assert result.is_mention is True
        assert result.mention_text == "can you explain this?"

    def test_case_insensitive_upper(self):
        result = extract_mention("@FORGE explain")
        assert result.is_mention is True
        assert result.mention_text == "explain"

    def test_case_insensitive_mixed(self):
        result = extract_mention("@Forge explain")
        assert result.is_mention is True
        assert result.mention_text == "explain"

    def test_slash_review(self):
        result = extract_mention("@forge /review focus on security")
        assert result.is_mention is True
        assert result.slash_command == "/review"
        assert result.command_args == "focus on security"
        assert result.mention_text == "focus on security"

    def test_slash_debug_no_args(self):
        result = extract_mention("@forge /debug")
        assert result.is_mention is True
        assert result.slash_command == "/debug"
        assert result.command_args is None

    def test_slash_help(self):
        result = extract_mention("@forge /help")
        assert result.is_mention is True
        assert result.slash_command == "/help"
        assert result.command_args is None

    def test_slash_explain(self):
        result = extract_mention("@forge /explain this function")
        assert result.is_mention is True
        assert result.slash_command == "/explain"
        assert result.command_args == "this function"

    def test_slash_security(self):
        result = extract_mention("@forge /security")
        assert result.is_mention is True
        assert result.slash_command == "/security"

    def test_slash_summarize(self):
        result = extract_mention("@forge /summarize the discussion")
        assert result.is_mention is True
        assert result.slash_command == "/summarize"
        assert result.command_args == "the discussion"

    def test_slash_command_case_insensitive(self):
        result = extract_mention("@forge /REVIEW check this")
        assert result.slash_command == "/review"
        assert result.command_args == "check this"

    def test_unknown_slash_command_treated_as_text(self):
        result = extract_mention("@forge /unknown do something")
        assert result.is_mention is True
        assert result.slash_command is None
        assert result.mention_text == "/unknown do something"

    def test_custom_pattern(self):
        result = extract_mention("@mybot explain this", mention_pattern="@mybot")
        assert result.is_mention is True
        assert result.mention_text == "explain this"

    def test_custom_pattern_no_match(self):
        result = extract_mention("@forge explain this", mention_pattern="@mybot")
        assert result.is_mention is False

    def test_custom_pattern_with_slash(self):
        result = extract_mention(
            "@forge-bot /review this MR",
            mention_pattern="@forge-bot",
        )
        assert result.is_mention is True
        assert result.slash_command == "/review"
        assert result.command_args == "this MR"

    def test_raw_text_preserved(self):
        text = "@forge explain this function"
        result = extract_mention(text)
        assert result.raw_text == text

    def test_empty_string(self):
        result = extract_mention("")
        assert result.is_mention is False

    def test_multiline_note(self):
        text = "Hey team\n\n@forge can you explain\nthis change?"
        result = extract_mention(text)
        assert result.is_mention is True
        assert "can you explain" in result.mention_text
        assert "this change?" in result.mention_text

    def test_mention_info_is_frozen(self):
        result = extract_mention("@forge hello")
        with pytest.raises(AttributeError):
            result.is_mention = False  # type: ignore[misc]
