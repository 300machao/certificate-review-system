from __future__ import annotations

import io
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app, service


ROOT = Path(__file__).resolve().parents[1]


def _upload_one(client: TestClient) -> dict:
    sample = ROOT / "samples" / "01-字段一致-应通过.pdf"
    with sample.open("rb") as stream:
        response = client.post(
            "/api/batches",
            files={"files": (sample.name, stream, "application/pdf")},
        )
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    for _ in range(100):
        payload = client.get(f"/api/batches/{batch_id}").json()
        if payload["status"] == "COMPLETED":
            return payload
        time.sleep(0.02)
    raise AssertionError("batch did not finish")


def test_missing_ledger_never_auto_passes_and_pdf_is_same_origin_previewable() -> None:
    client = TestClient(app)
    batch = _upload_one(client)
    assert batch["summary"] == {
        "total": 1, "queued": 0, "processing": 0,
        "passed": 0, "review": 1, "failed": 0,
    }
    record = batch["records"][0]
    assert record["status"] == "HUMAN_REVIEW"
    assert any(item["code"] == "CRITICAL_FIELD_MISSING" for item in record["issues"])

    detail = client.get(f"/api/certificates/{record['record_id']}")
    assert detail.status_code == 200
    assert detail.json()["authenticity_status"] == "UNVERIFIED"
    preview = client.get(f"/api/certificates/{record['record_id']}/file")
    assert preview.status_code == 200
    assert preview.headers["x-frame-options"] == "SAMEORIGIN"
    assert "inline" in preview.headers["content-disposition"]
    assert preview.content.startswith(b"%PDF-")


def test_human_review_is_versioned_and_audit_package_contains_original() -> None:
    client = TestClient(app)
    batch = _upload_one(client)
    record = batch["records"][0]
    task_id = record["review_task_id"]
    response = client.post(
        f"/api/review-tasks/{task_id}/decision",
        json={
            "decision": "PASS",
            "reason": "人工逐页核对后确认通过",
            "corrected_fields": {"serial_number": "人工确认值"},
        },
    )
    assert response.status_code == 200
    detail = response.json()["certificate"]
    assert detail["certificate"]["final_decision"] == "PASS"
    assert detail["review_decisions"][0]["version"] == 1

    archive_response = client.get(f"/api/batches/{batch['batch_id']}/audit.zip")
    assert archive_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
        names = archive.namelist()
        assert "审核结果.json" in names
        assert "审核结果.csv" in names
        assert "审计链校验.json" in names
        assert any(name.startswith("原始证书/") and name.endswith(".pdf") for name in names)


def test_health_check_does_not_call_unconfigured_models() -> None:
    client = TestClient(app)
    response = client.get("/api/models/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["live_check"] is False
    assert set(payload["models"]) == {"qwen", "glm", "deepseek"}


def test_concurrent_retry_posts_queue_only_one_worker(monkeypatch) -> None:
    first_client = TestClient(app)
    second_client = TestClient(app)
    batch = _upload_one(first_client)
    certificate_id = batch["records"][0]["record_id"]

    started = threading.Event()
    release = threading.Event()
    calls_lock = threading.Lock()
    call_count = 0
    original_review = service._review_advanced_file

    def blocking_review(path, record):
        nonlocal call_count
        with calls_lock:
            call_count += 1
        started.set()
        if not release.wait(timeout=10):
            raise AssertionError("retry worker was not released by the test")
        return original_review(path, record)

    monkeypatch.setattr(service, "_review_advanced_file", blocking_review)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            first_client.post,
            f"/api/certificates/{certificate_id}/retry",
        )
        assert started.wait(timeout=10), "first retry worker did not start"

        duplicate = second_client.post(f"/api/certificates/{certificate_id}/retry")
        assert duplicate.status_code == 409
        assert "重试任务" in duplicate.json()["detail"]
        with calls_lock:
            assert call_count == 1

        release.set()
        accepted = first.result(timeout=20)

    assert accepted.status_code == 202
    assert accepted.json()["status"] == "RETRY_QUEUED"
    with calls_lock:
        assert call_count == 1

    # Completion releases the reservation, so a later intentional retry is valid.
    later = second_client.post(f"/api/certificates/{certificate_id}/retry")
    assert later.status_code == 202
    with calls_lock:
        assert call_count == 2


def test_retry_reservation_is_released_after_worker_failure(monkeypatch) -> None:
    client = TestClient(app)
    batch = _upload_one(client)
    certificate_id = batch["records"][0]["record_id"]

    def fail_retry(_certificate_id: str):
        raise RuntimeError("synthetic retry failure")

    token = service.reserve_certificate_retry(certificate_id)
    monkeypatch.setattr(service, "_retry_certificate_once", fail_retry)
    with pytest.raises(RuntimeError, match="synthetic retry failure"):
        service.run_reserved_certificate_retry(certificate_id, token)

    replacement = service.reserve_certificate_retry(certificate_id)
    assert service.cancel_certificate_retry_reservation(certificate_id, replacement) is True


def test_batch_retry_uses_one_reservation_and_resets_all_records(monkeypatch) -> None:
    client = TestClient(app)
    batch = _upload_one(client)
    batch_id = batch["batch_id"]
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def controlled_retry(target_batch_id: str):
        nonlocal calls
        assert target_batch_id == batch_id
        calls += 1
        started.set()
        assert release.wait(timeout=10)

    monkeypatch.setattr(service, "_process_advanced_batch", controlled_retry)
    first = threading.Thread(
        target=lambda: client.post(f"/api/batches/{batch_id}/retry"), daemon=True
    )
    first.start()
    assert started.wait(timeout=10)
    duplicate = client.post(f"/api/batches/{batch_id}/retry")
    assert duplicate.status_code == 409
    assert calls == 1
    release.set()
    first.join(timeout=10)
    replacement = service.reserve_batch_retry(batch_id)
    assert service.cancel_batch_retry_reservation(batch_id, replacement) is True
