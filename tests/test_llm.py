"""The Anthropic client is tested against a stub of the SDK: no network, no API key."""

from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from app.config import Settings
from app.llm import (
    AnthropicClient,
    ConfigurationError,
    FakeClient,
    LLMError,
    ToolCall,
    build_llm_client,
)
from app.main import create_app

ANSWER_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class StubMessages:
    """Stands in for `anthropic.Anthropic().messages`: records the request, returns or raises."""

    def __init__(self, response: Message | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Message | None:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class StubSDK:
    def __init__(self, messages: StubMessages) -> None:
        self.messages = messages


def message(content: list[dict[str, Any]], stop_reason: str) -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 120, "output_tokens": 40},
        }
    )


def client_with(stub: StubMessages) -> AnthropicClient:
    client = AnthropicClient(api_key="test-key", model="claude-opus-5-5", effort="medium")
    client._client = StubSDK(stub)  # type: ignore[assignment]
    return client


def test_tool_use_response_becomes_tool_calls_and_keeps_thinking_blocks() -> None:
    response = message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "match_canonical_supplier",
                "input": {"supplier_name": "Uber"},
            },
        ],
        "tool_use",
    )
    stub = StubMessages(response)

    turn = client_with(stub).complete(
        "system", [{"role": "user", "content": "{}"}], [], ANSWER_SCHEMA
    )

    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls == [
        ToolCall("toolu_1", "match_canonical_supplier", {"supplier_name": "Uber"})
    ]
    assert turn.text == ""
    assert turn.raw_content == list(response.content)  # thinking block included, unchanged


def test_request_uses_structured_output_and_no_forced_tool_choice() -> None:
    stub = StubMessages(message([{"type": "text", "text": '{"ok": true}'}], "end_turn"))

    turn = client_with(stub).complete(
        "system", [{"role": "user", "content": "{}"}], [], ANSWER_SCHEMA
    )

    request = stub.requests[0]
    assert turn.text == '{"ok": true}'
    assert request["model"] == "claude-opus-5-5"
    assert request["max_tokens"] == 16000
    assert request["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": ANSWER_SCHEMA},
    }
    for not_sent in ("tool_choice", "thinking", "temperature", "top_p", "top_k"):
        assert not_sent not in request


def test_sdk_errors_become_llm_errors() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    stub = StubMessages(error=anthropic.APIConnectionError(request=request))

    with pytest.raises(LLMError, match="APIConnectionError: Connection error."):
        client_with(stub).complete("system", [], [], ANSWER_SCHEMA)


def test_provider_must_be_chosen(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Set LLM_PROVIDER"):
        build_llm_client(Settings(_env_file=None))
    with pytest.raises(ConfigurationError, match="Set LLM_PROVIDER"):
        create_app(Settings(_env_file=None, operational_db_path=tmp_path / "op.db"))


def test_anthropic_provider_needs_an_api_key() -> None:
    settings = Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key=None)

    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        build_llm_client(settings)


def test_an_empty_key_in_the_env_file_counts_as_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=anthropic\nANTHROPIC_API_KEY=\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.llm_provider == "anthropic"
    assert settings.anthropic_api_key is None


def test_clients_are_built_from_settings() -> None:
    real = build_llm_client(
        Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="test-key")
    )
    fake = build_llm_client(Settings(_env_file=None, llm_provider="fake"))

    assert isinstance(real, AnthropicClient)
    assert (real.provider, real.model) == ("anthropic", "claude-opus-5-5")
    assert isinstance(fake, FakeClient)
