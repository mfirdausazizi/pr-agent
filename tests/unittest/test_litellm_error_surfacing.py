"""Errors from the model call must reach the caller with their message intact (issue #2576)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
import tenacity

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler


class FakeBox:
    def __init__(self, values=None, **attrs):
        self._values = values or {}
        for key, value in attrs.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return self._values.get(key, default)


class FakeSettings:
    def __init__(self, config_values=None, settings_values=None):
        self.config = FakeBox(
            config_values or {},
            reasoning_effort=None,
            ai_timeout=30,
            custom_reasoning_model=False,
            max_model_tokens=32000,
            verbosity_level=0,
            model="gpt-4o",
        )
        self.litellm = FakeBox()
        self._settings_values = settings_values or {}

    def get(self, key, default=None):
        return self._settings_values.get(key, default)


def _api_error(message):
    return openai.APIError(message, request=httpx.Request("POST", "http://test"), body=None)


# what litellm raises when it cannot infer a provider
PROVIDER_ERROR = (
    "litellm.BadRequestError: LLM Provider NOT provided. Pass in the LLM provider you are "
    "trying to call. You passed model=gpt-5.6-terra Learn more: "
    "https://docs.litellm.ai/docs/providers"
)


@pytest.mark.asyncio
async def test_provider_error_survives_retry_instead_of_becoming_retryerror(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = _api_error(PROVIDER_ERROR)
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(openai.APIError) as exc_info:
            await handler.chat_completion(model="gpt-5.6-terra", system="sys", user="usr")

    assert not isinstance(exc_info.value, tenacity.RetryError)
    assert "gpt-5.6-terra" in str(exc_info.value)
    assert "LLM Provider NOT provided" in str(exc_info.value)
    assert mock_call.call_count == litellm_handler.MODEL_RETRIES  # it did retry first


@pytest.mark.asyncio
async def test_empty_choices_raises_usable_api_error_not_typeerror(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    empty = MagicMock()
    response = {"choices": []}
    empty.__getitem__.side_effect = response.__getitem__

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = empty
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(openai.APIError) as exc_info:
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert not isinstance(exc_info.value, TypeError)
    assert "gpt-4o" in str(exc_info.value)


@pytest.mark.asyncio
async def test_non_api_error_is_wrapped_but_keeps_message_and_cause(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    original = ValueError("upstream exploded: bad endpoint")

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = original
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(openai.APIError) as exc_info:
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert not isinstance(exc_info.value, TypeError)
    assert "upstream exploded: bad endpoint" in str(exc_info.value)
    assert exc_info.value.__cause__ is original


# Ported from upstream 2ab06df9: empty non-streaming content is a failure, not a success.
def _content_response(content, finish_reason="stop"):
    mock = MagicMock()
    response = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


def _bare_handler():
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler.streaming_required_models = []
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", None])
async def test_get_completion_raises_on_empty_content(content):
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _content_response(content, finish_reason="length")
        with pytest.raises(openai.APIError) as exc_info:
            await _bare_handler()._get_completion(model="openai/claude-opus-5-5", messages=[])
    assert "length" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_completion_returns_non_empty_content():
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _content_response("ok")
        resp, finish_reason, _ = await _bare_handler()._get_completion(model="openai/gpt-6-sol", messages=[])
    assert (resp, finish_reason) == ("ok", "stop")


@pytest.mark.asyncio
async def test_empty_content_is_retried_then_surfaced(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _content_response("")
        with pytest.raises(openai.APIError):
            await litellm_handler.LiteLLMAIHandler().chat_completion(model="gpt-4o", system="s", user="u")
    assert mock_call.call_count == litellm_handler.MODEL_RETRIES


@pytest.mark.asyncio
async def test_rate_limit_is_not_retried(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)
    error = openai.RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://t")),
                                  body=None)
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = error
        with pytest.raises(openai.RateLimitError):
            await litellm_handler.LiteLLMAIHandler().chat_completion(model="gpt-4o", system="s", user="u")
    assert mock_call.call_count == 1
