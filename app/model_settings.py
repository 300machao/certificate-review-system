from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from app.providers import DEFAULT_BASE_URL, provider_defaults


PROVIDER_KINDS = ("qwen", "glm", "deepseek")
_ENTROPY = b"certificate-review-model-settings-v1"


class ModelSettingsError(ValueError):
    pass


def validate_base_url(raw: object) -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise ModelSettingsError("API基础地址不能为空或包含非法字符")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelSettingsError("API基础地址必须是有效的 http 或 https 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelSettingsError("API基础地址不得包含账号、密码、查询参数或片段")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ModelSettingsError("非本机API必须使用 https")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        raise ModelSettingsError("请填写API基础地址，不要包含 /chat/completions")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def validate_model(raw: object) -> str:
    value = str(raw or "").strip()
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise ModelSettingsError("模型名称不能为空或包含非法字符")
    return value


class ModelSettingsStore:
    """Store non-secrets as JSON and API keys with Windows user-scoped DPAPI."""

    def __init__(self, data_root: Path) -> None:
        self.directory = Path(data_root) / "settings"
        self.public_path = self.directory / "model-providers.json"
        self.secret_path = self.directory / "model-api-keys.dpapi.json"

    def load(self) -> dict[str, dict[str, str | None]]:
        defaults = provider_defaults()
        public = self._load_json(self.public_path)
        encrypted = self._load_json(self.secret_path)
        result: dict[str, dict[str, str | None]] = {}
        for kind in PROVIDER_KINDS:
            saved = public.get(kind) if isinstance(public.get(kind), dict) else {}
            base_url = validate_base_url(saved.get("base_url") or os.environ.get(
                defaults[kind]["base_url_env"], os.environ.get("CERT_MODEL_BASE_URL", DEFAULT_BASE_URL)
            ) or DEFAULT_BASE_URL)
            model = validate_model(saved.get("model") or os.environ.get(
                defaults[kind]["model_env"], defaults[kind]["model"]
            ) or defaults[kind]["model"])
            api_key: str | None = None
            source = "not_configured"
            ciphertext = encrypted.get(kind)
            if isinstance(ciphertext, str) and ciphertext:
                try:
                    api_key = _unprotect(ciphertext).decode("utf-8").strip() or None
                except Exception as exc:
                    raise ModelSettingsError(f"{kind} 的加密API Key无法由当前Windows用户解密") from exc
                source = "secure_store" if api_key else "not_configured"
            if not api_key:
                env_key = os.environ.get(defaults[kind]["api_key_env"], "").strip()
                if env_key:
                    api_key = env_key
                    source = "environment"
            result[kind] = {
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
                "api_key_source": source,
            }
        return result

    def public_snapshot(self) -> dict[str, Any]:
        settings = self.load()
        return {
            "providers": {
                kind: {
                    "base_url": item["base_url"],
                    "model": item["model"],
                    "configured": bool(item["api_key"]),
                    "api_key_source": item["api_key_source"],
                }
                for kind, item in settings.items()
            },
            "storage": {
                "non_secret": "data/settings/model-providers.json",
                "api_keys": "data/settings/model-api-keys.dpapi.json",
                "protection": "Windows DPAPI（当前用户）",
                "effective": "保存后立即生效，无需重启",
            },
        }

    def save(self, updates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        unknown = set(updates) - set(PROVIDER_KINDS)
        if unknown:
            raise ModelSettingsError("不支持的模型配置：" + "、".join(sorted(unknown)))
        current_public = self._load_json(self.public_path)
        encrypted = self._load_json(self.secret_path)
        self.directory.mkdir(parents=True, exist_ok=True)
        for kind, raw in updates.items():
            if not isinstance(raw, Mapping):
                raise ModelSettingsError(f"{kind} 配置格式无效")
            current_public[kind] = {
                "base_url": validate_base_url(raw.get("base_url")),
                "model": validate_model(raw.get("model")),
            }
            if bool(raw.get("clear_api_key")):
                encrypted.pop(kind, None)
            else:
                api_key = raw.get("api_key")
                if api_key is not None and str(api_key).strip():
                    key_text = str(api_key).strip()
                    if len(key_text) > 4096 or any(ord(char) < 32 for char in key_text):
                        raise ModelSettingsError("API Key长度或格式无效")
                    encrypted[kind] = _protect(key_text.encode("utf-8"))
        self._atomic_json(self.public_path, current_public)
        self._atomic_json(self.secret_path, encrypted)
        return self.public_snapshot()

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ModelSettingsError(f"配置文件无法读取：{path.name}") from exc
        if not isinstance(value, dict):
            raise ModelSettingsError(f"配置文件格式无效：{path.name}")
        return value

    @staticmethod
    def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _crypt32() -> tuple[Any, Any]:
    if os.name != "nt":
        raise ModelSettingsError("安全保存API Key需要Windows DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return crypt32, kernel32


def _protect(data: bytes) -> str:
    crypt32, kernel32 = _crypt32()
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "CertificateReviewApiKey", ctypes.byref(entropy),
        None, None, 0x1, ctypes.byref(output),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(ctypes.string_at(output.pbData, output.cbData)).decode("ascii")
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(value: str) -> bytes:
    crypt32, kernel32 = _crypt32()
    source, source_buffer = _blob(base64.b64decode(value, validate=True))
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(output)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
