from unittest.mock import patch

from forge.config import ForgeConfig
from forge.llm.provider import get_model, get_model_for_task


class FakeSettings:
    """Minimal Settings stand-in for tests (avoids needing real .env)."""

    LITELLM_URL = "http://localhost:4000"


def _make_config(models: dict) -> ForgeConfig:
    """Create a ForgeConfig with custom models dict."""
    config = ForgeConfig(path="nonexistent.yml")
    config._data["models"] = models
    return config


class TestGetModel:
    @patch("forge.llm.provider.LiteLLM")
    def test_string_alias(self, mock_litellm):
        config = _make_config({"fast": "ollama/llama3.1:8b"})
        get_model("fast", config, FakeSettings())
        mock_litellm.assert_called_once_with(
            id="ollama/llama3.1:8b",
            api_base="http://localhost:4000",
            api_key="not-needed-for-proxy",
            temperature=0.1,
            max_tokens=8192,
        )

    @patch("forge.llm.provider.LiteLLM")
    def test_dict_alias_with_params(self, mock_litellm):
        config = _make_config(
            {"code": {"id": "deepseek/coder-v3", "temperature": 0.0, "max_tokens": 4096}}
        )
        get_model("code", config, FakeSettings())
        mock_litellm.assert_called_once_with(
            id="deepseek/coder-v3",
            api_base="http://localhost:4000",
            api_key="not-needed-for-proxy",
            temperature=0.0,
            max_tokens=4096,
        )

    @patch("forge.llm.provider.LiteLLM")
    def test_dict_alias_defaults(self, mock_litellm):
        config = _make_config({"strong": {"id": "claude-sonnet"}})
        get_model("strong", config, FakeSettings())
        mock_litellm.assert_called_once_with(
            id="claude-sonnet",
            api_base="http://localhost:4000",
            api_key="not-needed-for-proxy",
            temperature=0.1,
            max_tokens=8192,
        )

    @patch("forge.llm.provider.LiteLLM")
    def test_unknown_alias_falls_back_to_raw(self, mock_litellm):
        config = _make_config({"fast": "fast"})
        get_model("unknown-model", config, FakeSettings())
        mock_litellm.assert_called_once()
        call_args = mock_litellm.call_args
        assert call_args.kwargs["id"] == "unknown-model"

    @patch("forge.llm.provider.LiteLLM")
    def test_api_base_from_settings(self, mock_litellm):
        settings = FakeSettings()
        settings.LITELLM_URL = "http://custom:9999"
        config = _make_config({"fast": "fast"})
        get_model("fast", config, settings)
        call_args = mock_litellm.call_args
        assert call_args.kwargs["api_base"] == "http://custom:9999"


class TestGetModelForTask:
    @patch("forge.llm.provider.LiteLLM")
    def test_review_maps_to_code(self, mock_litellm):
        config = _make_config({"code": "deepseek/coder"})
        get_model_for_task("review", config, FakeSettings())
        assert mock_litellm.call_args.kwargs["id"] == "deepseek/coder"

    @patch("forge.llm.provider.LiteLLM")
    def test_chat_maps_to_default(self, mock_litellm):
        config = _make_config({"default": "claude-sonnet"})
        get_model_for_task("chat", config, FakeSettings())
        assert mock_litellm.call_args.kwargs["id"] == "claude-sonnet"

    @patch("forge.llm.provider.LiteLLM")
    def test_pipeline_debug_maps_to_fast(self, mock_litellm):
        config = _make_config({"fast": "llama3"})
        get_model_for_task("pipeline_debug", config, FakeSettings())
        assert mock_litellm.call_args.kwargs["id"] == "llama3"

    @patch("forge.llm.provider.LiteLLM")
    def test_unknown_task_maps_to_default(self, mock_litellm):
        config = _make_config({"default": "fallback-model"})
        get_model_for_task("something_new", config, FakeSettings())
        assert mock_litellm.call_args.kwargs["id"] == "fallback-model"
