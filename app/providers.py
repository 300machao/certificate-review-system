from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx


DEFAULT_BASE_URL = "https://gpu-api.dongfang.com/v1"

_PROVIDERS: dict[str, dict[str, str]] = {
    "qwen": {
        "api_key_env": "CERT_QWEN_API_KEY",
        "base_url_env": "CERT_QWEN_BASE_URL",
        "model_env": "CERT_QWEN_MODEL",
        "model": "qwen35-397b-a17b-int8",
    },
    "glm": {
        "api_key_env": "CERT_GLM_API_KEY",
        "base_url_env": "CERT_GLM_BASE_URL",
        "model_env": "CERT_GLM_MODEL",
        "model": "glm-5.1",
    },
    "deepseek": {
        "api_key_env": "CERT_ARBITER_API_KEY",
        "base_url_env": "CERT_ARBITER_BASE_URL",
        "model_env": "CERT_ARBITER_MODEL",
        "model": "deepseek-v4",
    },
}


class ProviderError(RuntimeError):
    pass


class ProviderDisabledError(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code: int, message: str, *, attempts: int) -> None:
        super().__init__(f"model gateway returned HTTP {status_code}: {message}")
        self.status_code = status_code
        self.attempts = attempts


class ProviderResponseError(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    kind: str
    api_key_env: str
    base_url: str
    model: str
    timeout_s: float = 120.0
    max_retries: int = 2
    retry_base_s: float = 0.5
    api_key_value: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_env(
        cls,
        kind: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> "ProviderConfig":
        normalized = kind.strip().lower()
        if normalized not in _PROVIDERS:
            raise ValueError(f"unknown provider: {kind}")
        defaults = _PROVIDERS[normalized]
        base_url = (base_url or os.environ.get(
            defaults["base_url_env"], os.environ.get("CERT_MODEL_BASE_URL", DEFAULT_BASE_URL)
        )).strip().rstrip("/")
        model = (model or os.environ.get(defaults["model_env"], defaults["model"])).strip()
        timeout = _safe_float(os.environ.get("CERT_MODEL_TIMEOUT_S"), 120.0)
        return cls(
            kind=normalized,
            api_key_env=defaults["api_key_env"],
            base_url=base_url or DEFAULT_BASE_URL,
            model=model or defaults["model"],
            timeout_s=max(1.0, timeout),
            api_key_value=api_key,
        )

    @property
    def api_key(self) -> str | None:
        if self.api_key_value is not None:
            return self.api_key_value.strip() or None
        value = os.environ.get(self.api_key_env, "").strip()
        return value or None

    @property
    def configured(self) -> bool:
        return self.api_key is not None


@dataclass(frozen=True)
class ModelResponse:
    data: Any
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    attempts: int = 1
    request_id: str | None = None
    finish_reason: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "usage": dict(self.usage),
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "request_id": self.request_id,
            "finish_reason": self.finish_reason,
        }


class OpenAICompatibleProvider:
    """Strict-JSON chat-completions client for the internal OpenAI-compatible gateway."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        # The internal gateway is contacted directly. Inheriting a user-level proxy
        # could both break startup (for example, missing SOCKS support) and expose
        # the Authorization header to an unintended intermediary.
        self._client = client or httpx.Client(timeout=config.timeout_s, trust_env=False)
        self._sleep = sleep
        self._clock = clock

    @property
    def configured(self) -> bool:
        return self.config.configured

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.config.kind,
            "model": self.config.model,
            "base_url": self.config.base_url,
            "configured": self.configured,
            "status": "configured" if self.configured else "disabled",
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OpenAICompatibleProvider":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def complete_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        schema: Mapping[str, Any] | None = None,
        images: Sequence[str | bytes | Path | Mapping[str, Any]] | None = None,
        timeout_s: float | None = None,
        temperature: float = 0.0,
    ) -> ModelResponse:
        api_key = self.config.api_key
        if not api_key:
            raise ProviderDisabledError(
                f"{self.config.kind} provider is disabled; set {self.config.api_key_env}"
            )
        prepared_messages = [dict(message) for message in messages]
        if not prepared_messages:
            raise ValueError("messages must not be empty")
        if images:
            prepared_messages = _attach_images(prepared_messages, images)

        response_format: dict[str, Any]
        if schema is None:
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "certificate_review_response",
                    "strict": True,
                    "schema": dict(schema),
                },
            }
        body = {
            "model": self.config.model,
            "messages": prepared_messages,
            "temperature": temperature,
            "max_tokens": max(256, _safe_int(os.environ.get("CERT_MODEL_MAX_TOKENS"), 4096)),
            "response_format": response_format,
        }
        if self.config.kind == "qwen":
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = f"{self.config.base_url}/chat/completions"
        started = self._clock()
        attempts = 0
        response: httpx.Response | None = None
        while attempts <= self.config.max_retries:
            attempts += 1
            try:
                response = self._client.post(
                    url, headers=headers, json=body, timeout=timeout_s or self.config.timeout_s
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempts > self.config.max_retries:
                    raise ProviderError(
                        f"model gateway request failed after {attempts} attempts: "
                        f"{type(exc).__name__}"
                    ) from exc
                self._sleep(self.config.retry_base_s * (2 ** (attempts - 1)))
                continue
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempts <= self.config.max_retries:
                    retry_after = _retry_after_seconds(response)
                    delay = max(self.config.retry_base_s * (2 ** (attempts - 1)), retry_after)
                    self._sleep(delay)
                    continue
            break

        assert response is not None
        latency_ms = max(0, round((self._clock() - started) * 1000))
        if response.status_code < 200 or response.status_code >= 300:
            message = _safe_http_message(response, api_key)
            raise ProviderHTTPError(response.status_code, message, attempts=attempts)
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderResponseError("model gateway returned a non-JSON envelope") from exc
        try:
            choice = payload["choices"][0]
            content = _message_text(choice["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError("model gateway response is missing choices/message/content") from exc
        try:
            data = json.loads(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderResponseError("model returned invalid strict JSON") from exc
        if schema is not None:
            _validate_schema(data, schema)
        request_id = response.headers.get("x-request-id") or payload.get("id")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return ModelResponse(
            data=data,
            model=str(payload.get("model") or self.config.model),
            usage=dict(usage),
            latency_ms=latency_ms,
            attempts=attempts,
            request_id=str(request_id) if request_id is not None else None,
            finish_reason=(str(choice.get("finish_reason"))
                           if choice.get("finish_reason") is not None else None),
        )

    async def acomplete_json(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return await asyncio.to_thread(self.complete_json, *args, **kwargs)


def create_qwen_provider(**kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(ProviderConfig.from_env("qwen"), **kwargs)


def create_glm_provider(**kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(ProviderConfig.from_env("glm"), **kwargs)


def create_deepseek_provider(**kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(ProviderConfig.from_env("deepseek"), **kwargs)


def provider_defaults() -> dict[str, dict[str, str]]:
    return {kind: dict(values) for kind, values in _PROVIDERS.items()}


def create_model_providers(
    settings: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, OpenAICompatibleProvider]:
    settings = settings or {}
    def build(kind: str) -> OpenAICompatibleProvider:
        values = settings.get(kind) or {}
        return OpenAICompatibleProvider(ProviderConfig.from_env(
            kind,
            base_url=str(values.get("base_url") or "") or None,
            model=str(values.get("model") or "") or None,
            api_key=(str(values["api_key"]) if values.get("api_key") is not None else None),
        ))
    return {
        "qwen": build("qwen"),
        "glm": build("glm"),
        "deepseek": build("deepseek"),
    }


def _safe_float(raw: str | None, fallback: float) -> float:
    try:
        return float(raw) if raw is not None else fallback
    except ValueError:
        return fallback


def _safe_int(raw: str | None, fallback: int) -> int:
    try:
        return int(raw) if raw is not None else fallback
    except ValueError:
        return fallback


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content
                 if isinstance(item, dict) and item.get("type") in {"text", "output_text"}]
        if parts:
            return "".join(str(part) for part in parts)
    raise ProviderResponseError("model message content is not textual JSON")


def _attach_images(messages: list[dict[str, Any]],
                   images: Sequence[str | bytes | Path | Mapping[str, Any]]) -> list[dict[str, Any]]:
    last_user = next((index for index in range(len(messages) - 1, -1, -1)
                      if messages[index].get("role") == "user"), None)
    if last_user is None:
        raise ValueError("images require at least one user message")
    message = dict(messages[last_user])
    existing = message.get("content", "")
    content = list(existing) if isinstance(existing, list) else [{"type": "text", "text": str(existing)}]
    for image in images:
        if isinstance(image, Mapping):
            if "url" in image:
                url = str(image["url"])
                detail = str(image.get("detail", "high"))
            elif "image_url" in image:
                nested = image["image_url"]
                if isinstance(nested, Mapping):
                    url = str(nested.get("url", ""))
                    detail = str(nested.get("detail", "high"))
                else:
                    url, detail = str(nested), "high"
            else:
                raise ValueError("image mapping requires url or image_url")
        elif isinstance(image, bytes):
            url, detail = _data_url(image, "image/png"), "high"
        elif isinstance(image, Path):
            mime = mimetypes.guess_type(image.name)[0] or "image/png"
            url, detail = _data_url(image.read_bytes(), mime), "high"
        else:
            url, detail = str(image), "high"
        content.append({"type": "image_url", "image_url": {"url": url, "detail": detail}})
    message["content"] = content
    messages[last_user] = message
    return messages


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("retry-after")
    try:
        return max(0.0, min(float(value), 30.0)) if value is not None else 0.0
    except ValueError:
        return 0.0


def _safe_http_message(response: httpx.Response, api_key: str) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                text = str(error.get("message") or error.get("code") or "request failed")
            else:
                text = str(error or payload.get("message") or "request failed")
        else:
            text = "request failed"
    except ValueError:
        text = response.text[:300] or "request failed"
    return text.replace(api_key, "[REDACTED]")[:500]


def _validate_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise ProviderResponseError(f"strict JSON schema mismatch at {path}: value is not in enum")
    expected = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected, list):
        if not any(valid.get(item, True) for item in expected):
            raise ProviderResponseError(f"strict JSON schema type mismatch at {path}")
    elif expected in valid and not valid[expected]:
        raise ProviderResponseError(f"strict JSON schema type mismatch at {path}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ProviderResponseError(
                f"strict JSON schema missing required field at {path}: {missing[0]}"
            )
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = [name for name in value if name not in properties]
            if unexpected:
                raise ProviderResponseError(
                    f"strict JSON schema unexpected field at {path}: {unexpected[0]}"
                )
        for name, child in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, Mapping):
                _validate_schema(child, child_schema, f"{path}.{name}")
    elif isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, child in enumerate(value):
            _validate_schema(child, schema["items"], f"{path}[{index}]")
