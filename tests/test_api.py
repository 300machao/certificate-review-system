from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import app


ROOT = Path(__file__).resolve().parents[1]


def test_web_batch_and_exports() -> None:
    client = TestClient(app)
    sample_names = [
        "01-字段一致-应通过.pdf",
        "02-二维码编号不一致-需复核.pdf",
        "04-损坏文件-应失败.pdf",
    ]
    opened = []
    try:
        files = []
        for name in sample_names:
            stream = (ROOT / "samples" / name).open("rb")
            opened.append(stream)
            files.append(("files", (name, stream, "application/pdf")))
        response = client.post("/api/batches", files=files)
    finally:
        for stream in opened:
            stream.close()
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    for _ in range(50):
        current = client.get(f"/api/batches/{batch_id}")
        assert current.status_code == 200
        if current.json()["status"] == "COMPLETED":
            break
        time.sleep(0.02)
    payload = current.json()
    assert payload["summary"]["total"] == 3
    # The patent workflow is intentionally stricter than the original prototype:
    # without a frozen ledger the formerly passing sample must go to human review.
    assert payload["summary"]["passed"] == 0
    assert payload["summary"]["review"] == 2
    assert payload["summary"]["failed"] == 1
    assert client.get(f"/api/batches/{batch_id}/export.json").status_code == 200
    csv_response = client.get(f"/api/batches/{batch_id}/export.csv")
    assert csv_response.status_code == 200
    assert csv_response.content.startswith("\ufeff".encode("utf-8"))


def test_security_headers_and_capabilities() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["vision_fallback"]["configured"] is False
    assert capabilities["authenticity"]["authoritative_verification_configured"] is False
