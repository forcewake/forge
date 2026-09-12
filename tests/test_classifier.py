"""Tests for IntentClassifier."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.agents.classifier import IntentClassifier


@pytest.fixture()
def mock_model():
    return MagicMock()


@pytest.fixture()
def classifier(mock_model) -> IntentClassifier:
    return IntentClassifier(model=mock_model)


class TestClassify:
    async def test_returns_recognized_intent(self, classifier: IntentClassifier):
        response = MagicMock()
        response.content = "explain"
        with patch("forge.agents.classifier.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=response)
            result = await classifier.classify("what does this function do?")
        assert result == "explain"

    async def test_falls_back_to_general_on_unknown(self, classifier: IntentClassifier):
        response = MagicMock()
        response.content = "something_weird"
        with patch("forge.agents.classifier.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=response)
            result = await classifier.classify("blah blah")
        assert result == "general"

    async def test_falls_back_to_general_on_exception(self, classifier: IntentClassifier):
        with patch("forge.agents.classifier.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(side_effect=RuntimeError("LLM down"))
            result = await classifier.classify("explain this")
        assert result == "general"

    async def test_empty_message_returns_general(self, classifier: IntentClassifier):
        result = await classifier.classify("")
        assert result == "general"

    async def test_whitespace_message_returns_general(self, classifier: IntentClassifier):
        result = await classifier.classify("   ")
        assert result == "general"

    async def test_strips_and_lowercases_response(self, classifier: IntentClassifier):
        response = MagicMock()
        response.content = "  REVIEW  \n"
        with patch("forge.agents.classifier.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=response)
            result = await classifier.classify("review this code")
        assert result == "review"

    async def test_context_hint_included_in_message(self, classifier: IntentClassifier):
        response = MagicMock()
        response.content = "debug"
        with patch("forge.agents.classifier.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=response)
            result = await classifier.classify(
                "why is this failing?",
                context_hint="MR pipeline failed",
            )
        assert result == "debug"
        # Verify context was passed in the user message
        call_args = instance.arun.call_args[0][0]
        assert "MR pipeline failed" in call_args

    async def test_all_valid_intents_accepted(self, classifier: IntentClassifier):
        for intent in IntentClassifier.INTENTS:
            response = MagicMock()
            response.content = intent
            with patch("forge.agents.classifier.Agent") as MockAgent:
                instance = MockAgent.return_value
                instance.arun = AsyncMock(return_value=response)
                result = await classifier.classify("test message")
            assert result == intent

    async def test_none_response_falls_back(self, classifier: IntentClassifier):
        with patch("forge.agents.classifier.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=None)
            result = await classifier.classify("hello")
        assert result == "general"
