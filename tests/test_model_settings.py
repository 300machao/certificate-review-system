from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app, service
from app.config import AppConfig
from app.model_settings import ModelSettingsError, ModelSettingsStore, validate_base_url
from app.service import BatchReviewService


def test_dpapi_store_roundtrip_never_writes_plaintext_key(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path)
    secret = "unit-test-secret-key-that-must-not-be-plaintext"

    snapshot = store.save({
        "qwen": {
            "base_url": "https://gateway.example/v1",
            "model": "qwen-test",
            "api_key": secret,
        }
    })

    assert snapshot["providers"]["qwen"]["configured"] is True
    assert store.load()["qwen"]["api_key"] == secret
    assert secret not in store.public_path.read_text(encoding="utf-8")
    assert secret not in store.secret_path.read_text(encoding="utf-8")
    assert '"api_key":' not in json.dumps(snapshot)


def test_model_setting_url_validation_rejects_unsafe_or_wrong_shape() -> None:
    assert validate_base_url("https://gateway.example/v1/") == "https://gateway.example/v1"
    assert validate_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"
    for value in (
        "http://gateway.example/v1",
        "https://user:pass@gateway.example/v1",
        "https://gateway.example/v1?token=x",
        "https://gateway.example/v1/chat/completions",
    ):
        with pytest.raises(ModelSettingsError):
            validate_base_url(value)


def test_service_save_reloads_provider_without_leaking_secret(tmp_path: Path) -> None:
    reviewed = BatchReviewService(AppConfig(model_mode="disabled"), root=tmp_path)
    secret = "service-secret-key-not-for-database"
    try:
        response = reviewed.save_model_settings({
            "glm": {
                "base_url": "https://gateway.example/v1",
                "model": "glm-test",
                "api_key": secret,
            }
        })
        assert response["providers"]["glm"]["configured"] is True
        assert reviewed.providers["glm"].config.api_key == secret
        assert reviewed.providers["glm"].config.model == "glm-test"
    finally:
        reviewed.close()

    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "model-api-keys.dpapi.json":
            assert secret.encode() not in path.read_bytes()


def test_settings_api_is_same_origin_and_never_returns_key(monkeypatch) -> None:
    client = TestClient(app)
    payload = {
        "qwen": {
            "base_url": "https://gateway.example/v1",
            "model": "qwen-test",
            "api_key": "api-secret-for-endpoint-test",
        }
    }
    blocked = client.put("/api/model-settings", json=payload)
    assert blocked.status_code == 403

    saved = client.put(
        "/api/model-settings",
        json=payload,
        headers={
            "Origin": "http://testserver",
            "X-Certificate-Review-Action": "model-settings-save",
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["providers"]["qwen"]["configured"] is True
    assert "api-secret-for-endpoint-test" not in saved.text

    fetched = client.get("/api/model-settings")
    assert fetched.status_code == 200
    assert "api-secret-for-endpoint-test" not in fetched.text

    monkeypatch.setattr(
        service,
        "test_model_settings",
        lambda kind, candidate: {
            "provider": kind, "status": "healthy", "model": candidate["model"],
            "latency_ms": 8, "message": "连接成功",
        },
    )
    tested = client.post(
        "/api/model-settings/test",
        json={
            "provider": "qwen",
            "base_url": "https://gateway.example/v1",
            "model": "qwen-test",
            "api_key": "draft-secret",
        },
        headers={
            "Origin": "http://testserver",
            "X-Certificate-Review-Action": "model-settings-test",
        },
    )
    assert tested.status_code == 200
    assert "draft-secret" not in tested.text
