from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from app.config import AppConfig, load_config
from app.ingest import FrozenLedgerRow, IngestResult, ingest_sources
from app.service import BatchReviewService, prepare_local_files


ROOT = Path(__file__).resolve().parents[1]


def test_ledger_row_filename_match_uses_nfkc_without_changing_display_name() -> None:
    wrong = FrozenLedgerRow(
        "wrong", 0, {"local_path": "DEMO-REPORT-001(OTHER-EQ).pdf"}, "ledger.json"
    )
    expected = FrozenLedgerRow(
        "expected", 1, {"local_path": "DEMO-REPORT-001(DEMO-EQ-001).pdf"}, "ledger.json"
    )
    queue = deque([wrong, expected])
    unmatched = [wrong, expected]

    selected = BatchReviewService._take_ledger_row(
        "DEMO-REPORT-001（DEMO-EQ-001）.pdf", "a" * 64, {"a" * 64: queue}, unmatched
    )

    assert selected is expected
    assert list(queue) == [wrong]
    assert unmatched == [wrong]


def test_advanced_batch_with_technical_failure_is_partial_failed(tmp_path: Path) -> None:
    service = BatchReviewService(AppConfig(model_mode="disabled"), root=tmp_path)
    try:
        batch = service.create_ingested_batch(
            IngestResult(),
            failed_inputs=[{
                "filename": "broken.pdf",
                "size_bytes": 10,
                "code": "INVALID_PDF",
                "error": "PDF不可读",
            }],
        )

        service.process_batch(batch.batch_id)

        assert batch.status == "PARTIAL_FAILED"
        assert service.db.get_batch(batch.batch_id)["status"] == "PARTIAL_FAILED"
    finally:
        service.close()


def test_successful_certificate_retry_reaggregates_partial_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = BatchReviewService(AppConfig(model_mode="disabled"), root=tmp_path)
    try:
        sample = ROOT / "samples" / "01-字段一致-应通过.pdf"
        ingest = ingest_sources([sample], service.storage_root)
        batch = service.create_ingested_batch(ingest)
        record = batch.records[0]
        record.status = "PROCESSING_FAILED"
        record.workflow_state = "PROCESSING_FAILED"
        service._store_record_snapshot(record)
        service._refresh_advanced_batch_status(batch)
        assert batch.status == "PARTIAL_FAILED"

        def successful_review(_path: Path, target) -> None:
            target.status = "HUMAN_REVIEW"
            target.workflow_state = "HUMAN_REVIEW"
            target.error = None

        monkeypatch.setattr(service, "_review_advanced_file", successful_review)
        service.retry_certificate(record.record_id)

        assert batch.status == "COMPLETED"
        assert service.db.get_batch(batch.batch_id)["status"] == "COMPLETED"
    finally:
        service.close()


def run_batch(paths: list[Path]):
    service = BatchReviewService(load_config(ROOT / "config" / "app.json"))
    incoming, temp_dir = prepare_local_files(paths)
    batch = service.create_batch(incoming, temp_dir)
    service.process_batch(batch.batch_id, temp_dir)
    return service, batch


def test_batch_isolates_failure_and_classifies_records() -> None:
    samples = ROOT / "samples"
    service, batch = run_batch([
        samples / "01-字段一致-应通过.pdf",
        samples / "02-二维码编号不一致-需复核.pdf",
        samples / "04-损坏文件-应失败.pdf",
    ])
    assert batch.status == "COMPLETED"
    assert [record.status for record in batch.records] == ["PASS", "REVIEW", "FAILED"]
    assert batch.summary() == {
        "total": 3, "queued": 0, "processing": 0, "passed": 1, "review": 1, "failed": 1,
    }
    mismatch = batch.records[1]
    assert any(item.code == "QR_TEXT_MISMATCH" for item in mismatch.issues)
    assert batch.records[2].error

    exported = json.loads(service.export_json(batch.batch_id))
    assert exported["records"][0]["authenticity"]["status"] == "UNVERIFIED"
    assert "真实性未核验" in [item["title"] for item in exported["records"][0]["issues"]]


def test_duplicate_is_marked_for_review() -> None:
    sample = ROOT / "samples" / "01-字段一致-应通过.pdf"
    _, batch = run_batch([sample, sample])
    assert batch.records[0].status == "PASS"
    assert batch.records[1].status == "REVIEW"
    assert batch.records[1].duplicate_of == batch.records[0].record_id
    assert any(item.code == "DUPLICATE_FILE" for item in batch.records[1].issues)


def test_csv_formula_values_are_neutralized() -> None:
    assert BatchReviewService._csv_safe("=HYPERLINK('x')") == "'=HYPERLINK('x')"
    assert BatchReviewService._csv_safe("safe") == "safe"


def test_cleanup_refuses_non_temporary_project_directory() -> None:
    with pytest.raises(ValueError):
        BatchReviewService._cleanup_temp_dir(ROOT)
