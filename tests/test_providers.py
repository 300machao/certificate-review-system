from __future__ import annotations

import asyncio

import httpx
import pytest

from app.providers import (
    DEFAULT_BASE_URL,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderDisabledError,
    ProviderHTTPError,
    ProviderResponseError,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _success(content: str = '{"decision":"EQUIVALENT"}') -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "gateway-request-1"},
        json={
            "id": "completion-1",
            "model": "glm-server-version",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    )


def test_provider_defaults_and_missing_key_are_safe(monkeypatch):
    monkeypatch.delenv("CERT_QWEN_API_KEY", raising=False)
    config = ProviderConfig.from_env("qwen")
    provider = OpenAICompatibleProvider(config, client=_client(lambda _: _success()))
    assert config.base_url == DEFAULT_BASE_URL
    assert config.model == "qwen35-397b-a17b-int8"
    assert provider.health() == {
        "provider": "qwen", "model": "qwen35-397b-a17b-int8",
        "base_url": DEFAULT_BASE_URL, "configured": False, "status": "disabled",
    }
    with pytest.raises(ProviderDisabledError, match="CERT_QWEN_API_KEY"):
        provider.complete_json([{"role": "user", "content": "test"}])


def test_strict_json_success_records_usage_latency_and_never_exposes_key(monkeypatch):
    secret = "super-secret-company-key"
    monkeypatch.setenv("CERT_GLM_API_KEY", secret)
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["authorization"]
        observed["body"] = __import__("json").loads(request.content)
        return _success()

    clock_values = iter([10.0, 10.125])
    config = ProviderConfig.from_env("glm")
    provider = OpenAICompatibleProvider(
        config, client=_client(handler), clock=lambda: next(clock_values)
    )
    schema = {
        "type": "object",
        "properties": {"decision": {"type": "string", "enum": ["EQUIVALENT"]}},
        "required": ["decision"],
        "additionalProperties": False,
    }
    result = provider.complete_json(
        [{"role": "user", "content": "compare"}], schema=schema
    )
    assert observed["authorization"] == f"Bearer {secret}"
    assert observed["body"]["response_format"]["json_schema"]["strict"] is True
    assert result.data == {"decision": "EQUIVALENT"}
    assert result.usage["total_tokens"] == 13
    assert result.latency_ms == 125
    assert result.attempts == 1
    assert result.request_id == "gateway-request-1"
    assert secret not in repr(config)
    assert secret not in repr(provider.health())
    assert secret not in repr(result)


def test_429_and_5xx_retry_at_most_twice(monkeypatch):
    monkeypatch.setenv("CERT_GLM_API_KEY", "key")
    statuses = iter([429, 503, 200])
    sleeps = []

    def handler(_: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 200:
            return _success()
        return httpx.Response(status, json={"error": {"message": "try later"}})

    config = ProviderConfig.from_env("glm")
    provider = OpenAICompatibleProvider(
        config, client=_client(handler), sleep=sleeps.append
    )
    result = provider.complete_json([{"role": "user", "content": "test"}])
    assert result.attempts == 3
    assert sleeps == [0.5, 1.0]


def test_non_retryable_error_is_sanitized(monkeypatch):
    secret = "must-not-leak"
    monkeypatch.setenv("CERT_ARBITER_API_KEY", secret)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad token {secret}"}})

    provider = OpenAICompatibleProvider(
        ProviderConfig.from_env("deepseek"), client=_client(handler)
    )
    with pytest.raises(ProviderHTTPError) as error:
        provider.complete_json([{"role": "user", "content": "test"}])
    assert error.value.attempts == 1
    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)


@pytest.mark.parametrize("content", ["not-json", "```json\n{}\n```", "[]"])
def test_invalid_or_schema_mismatched_output_is_rejected(monkeypatch, content):
    monkeypatch.setenv("CERT_GLM_API_KEY", "key")
    provider = OpenAICompatibleProvider(
        ProviderConfig.from_env("glm"), client=_client(lambda _: _success(content))
    )
    schema = {
        "type": "object", "properties": {"decision": {"type": "string"}},
        "required": ["decision"], "additionalProperties": False,
    }
    with pytest.raises(ProviderResponseError):
        provider.complete_json([{"role": "user", "content": "test"}], schema=schema)


def test_qwen_image_content_and_async_entrypoint(monkeypatch):
    monkeypatch.setenv("CERT_QWEN_API_KEY", "key")
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["body"] = __import__("json").loads(request.content)
        return _success('{"pages":1}')

    provider = OpenAICompatibleProvider(
        ProviderConfig.from_env("qwen"), client=_client(handler)
    )
    result = asyncio.run(provider.acomplete_json(
        [{"role": "user", "content": "read certificate"}], images=[b"fake-png"]
    ))
    content = observed["body"]["messages"][0]["content"]
    assert observed["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert observed["body"]["max_tokens"] == 4096
    assert content[0] == {"type": "text", "text": "read certificate"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result.data == {"pages": 1}
