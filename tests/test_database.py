from __future__ import annotations

import json
import sqlite3

import pytest

from app.database import Database


def _seed(db: Database) -> None:
    db.create_batch("batch-1", "request-1", {"source": "test"})
    db.add_certificate(
        "batch-1", "certificate.pdf", "a" * 64, 123, "pdf",
        certificate_id="certificate-1", storage_path="objects/a.pdf", page_count=3,
    )


def _seed_integrity_records(db: Database) -> None:
    _seed(db)
    db.add_ledger_snapshot(
        "batch-1", "frozen.csv", {"instrument_id": "3TA000007"},
        snapshot_id="ledger-1", certificate_id="certificate-1",
    )
    db.add_extracted_field(
        "certificate-1", "serial_number", field_id="field-1", value="A-01",
        normalized_value="A01", source="VISION", confidence=0.98,
    )
    db.add_comparison(
        "certificate-1", "serial_number", "MATCH", comparison_id="comparison-1",
        document_value="A-01", qr_value="A01", ledger_value="A01",
        normalized_document_value="A01", normalized_qr_value="A01",
        normalized_ledger_value="A01", risk="LOW",
    )
    db.add_model_decision(
        "certificate-1", "primary", "glm", "glm-5.1", "EQUIVALENT",
        decision_id="model-1", confidence=0.95,
    )
    db.create_review_task("certificate-1", "manual check", task_id="task-1")
    db.add_review_decision(
        "task-1", "PASS", decision_id="review-1",
        corrected_fields={"serial_number": "A-01"},
    )


def test_schema_wal_foreign_keys_and_core_persistence(tmp_path):
    path = tmp_path / "review.sqlite3"
    db = Database(path)
    try:
        assert db._connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {row[0] for row in db._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {
            "batches", "certificates", "ledger_snapshots", "extraction_runs",
            "extracted_fields", "comparisons", "model_decisions", "review_tasks",
            "review_decisions", "audit_events", "ledger_change_proposals",
        } <= tables
        _seed(db)
        db.add_ledger_snapshot(
            "batch-1", "frozen.csv", {"instrument_id": "3TA000007"},
            snapshot_id="ledger-1", certificate_id="certificate-1", row_key="3TA000007",
        )
        run = db.create_extraction_run(
            "certificate-1", "qwen", run_id="run-1", model="qwen-test", prompt_version="p1"
        )
        db.add_extracted_field(
            "certificate-1", "serial_number", field_id="field-1", value="A-01",
            normalized_value="A01", source="VISION", extraction_run_id=run["id"],
            page=1, evidence="出厂编号 A-01", confidence=0.98,
        )
        db.add_comparison(
            "certificate-1", "serial_number", "MATCH", comparison_id="comparison-1",
            document_value="A-01", ledger_value="A01", normalized_document_value="A01",
            normalized_ledger_value="A01", risk="LOW", basis="normalized equality",
        )
        db.finish_extraction_run("run-1", "SUCCEEDED", latency_ms=80,
                                 usage={"prompt_tokens": 10})
        db.add_model_decision(
            "certificate-1", "primary", "glm", "glm-5.1", "EQUIVALENT",
            decision_id="model-1", confidence=0.95, usage={"total_tokens": 18}, latency_ms=40,
        )
        task = db.create_review_task("certificate-1", "manual check", task_id="task-1")
        first = db.add_review_decision(
            task["id"], "PASS", decision_id="review-1", comment="checked",
            corrected_fields={"serial_number": "A-01"},
        )
        second = db.add_review_decision(task["id"], "FAIL", decision_id="review-2")
        db.add_ledger_change_proposal(
            "certificate-1", {"valid_until": "2027-01-01"}, proposal_id="proposal-1"
        )

        assert first["version"] == 1
        assert second["version"] == 2
        assert db.get_review_task("task-1")["status"] == "RESOLVED"
        finalized = db.get_certificate("certificate-1")
        assert finalized["status"] == "FINALIZED"
        assert finalized["metadata"]["final_decision"] == "FAIL"
        detail = db.get_certificate_detail("certificate-1")
        assert detail is not None
        assert detail["certificate"]["authenticity_status"] == "UNVERIFIED"
        assert detail["extracted_fields"][0]["confidence"] == pytest.approx(0.98)
        assert detail["model_decisions"][0]["usage"]["total_tokens"] == 18
        assert db.verify_audit_chain() == (True, None)
        assert db.verify_business_integrity() == (True, None)
    finally:
        db.close()

    reopened = Database(path)
    try:
        assert reopened.get_batch("batch-1")["metadata"] == {"source": "test"}
        assert reopened.get_certificate("certificate-1")["page_count"] == 3
        assert len(reopened.list_review_decisions("task-1")) == 2
        assert reopened.verify_audit_chain() == (True, None)
    finally:
        reopened.close()


def test_nested_transaction_rolls_back_business_data_and_audit():
    db = Database(":memory:")
    try:
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.create_batch("rolled-back", "request-rollback")
                raise RuntimeError("abort")
        assert db.get_batch("rolled-back") is None
        assert db.list_audit_events() == []
    finally:
        db.close()


def test_foreign_keys_and_append_only_audit_are_enforced():
    db = Database(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.add_certificate("missing", "a.pdf", "b" * 64, 1, "pdf")
        db.create_batch("batch-1", "request-1")
        event = db.list_audit_events()[0]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db._connection.execute(
                "UPDATE audit_events SET event_type='TAMPERED' WHERE id=?", (event["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db._connection.execute("DELETE FROM audit_events WHERE id=?", (event["id"],))
    finally:
        db.close()


def test_hash_chain_detects_out_of_band_tampering():
    db = Database(":memory:")
    try:
        db.create_batch("batch-1", "request-1")
        db.append_audit_event("batch", "batch-1", "CHECKED", {"safe": True})
        assert db.verify_audit_chain() == (True, None)
        db._connection.execute("DROP TRIGGER audit_events_no_update")
        db._connection.execute(
            "UPDATE audit_events SET payload_json=? WHERE id=2",
            (json.dumps({"safe": False}),),
        )
        valid, reason = db.verify_audit_chain()
        assert valid is False
        assert reason == "event hash mismatch at 2"
    finally:
        db.close()


def test_status_changes_are_audited_atomically():
    db = Database(":memory:")
    try:
        _seed(db)
        db.update_batch_status("batch-1", "FINALIZED", completed_at="2026-08-13T00:00:00+00:00")
        db.update_certificate_status(
            "certificate-1", "HUMAN_REVIEW", error=None,
            authenticity_status="UNVERIFIED", page_count=4, metadata={"phase": "comparison"},
        )
        assert db.get_batch("batch-1")["status"] == "FINALIZED"
        assert db.get_certificate("certificate-1")["status"] == "HUMAN_REVIEW"
        assert db.get_certificate("certificate-1")["metadata"]["phase"] == "comparison"
        event_types = [event["event_type"] for event in db.list_audit_events()]
        assert "BATCH_STATUS_CHANGED" in event_types
        assert "CERTIFICATE_STATUS_CHANGED" in event_types
        assert db.verify_audit_chain() == (True, None)
    finally:
        db.close()


def test_core_business_writes_add_certificate_scoped_integrity_events():
    db = Database(":memory:")
    try:
        _seed_integrity_records(db)
        field_event = db.list_audit_events(
            entity_type="extracted_field", entity_id="field-1"
        )[0]
        assert field_event["event_type"] == "EXTRACTED_FIELD_ADDED"
        assert field_event["payload"]["certificate_id"] == "certificate-1"
        assert field_event["payload"]["field_name"] == "serial_number"
        assert field_event["payload"]["source"] == "VISION"
        assert len(field_event["payload"]["value_hash"]) == 64
        assert len(field_event["payload"]["row_hash"]) == 64

        comparison_event = db.list_audit_events(
            entity_type="comparison", entity_id="comparison-1"
        )[0]
        assert comparison_event["event_type"] == "COMPARISON_ADDED"
        assert comparison_event["payload"]["certificate_id"] == "certificate-1"
        assert comparison_event["payload"]["field_name"] == "serial_number"
        assert comparison_event["payload"]["status"] == "MATCH"
        assert set(comparison_event["payload"]["value_hashes"]) == {
            "document", "qr", "ledger", "normalized_document", "normalized_qr",
            "normalized_ledger",
        }

        review_event = next(
            event for event in db.list_audit_events(entity_type="review_task", entity_id="task-1")
            if event["event_type"] == "REVIEW_DECISION_ADDED"
        )
        assert review_event["payload"]["certificate_id"] == "certificate-1"
        assert review_event["payload"]["decision_id"] == "review-1"
        assert db.verify_business_integrity() == (True, None)
    finally:
        db.close()


def test_field_and_comparison_audit_failure_rolls_back_business_row():
    db = Database(":memory:")
    try:
        _seed(db)
        db._connection.executescript(
            """
            CREATE TRIGGER reject_field_audit BEFORE INSERT ON audit_events
            WHEN NEW.event_type='EXTRACTED_FIELD_ADDED'
            BEGIN SELECT RAISE(ABORT, 'reject field audit'); END;
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="reject field audit"):
            db.add_extracted_field(
                "certificate-1", "serial_number", field_id="field-rollback",
                value="A-01", source="VISION",
            )
        assert db._connection.execute(
            "SELECT COUNT(*) FROM extracted_fields WHERE id='field-rollback'"
        ).fetchone()[0] == 0
        db._connection.execute("DROP TRIGGER reject_field_audit")

        db._connection.executescript(
            """
            CREATE TRIGGER reject_comparison_audit BEFORE INSERT ON audit_events
            WHEN NEW.event_type='COMPARISON_ADDED'
            BEGIN SELECT RAISE(ABORT, 'reject comparison audit'); END;
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="reject comparison audit"):
            db.add_comparison(
                "certificate-1", "serial_number", "MATCH",
                comparison_id="comparison-rollback", document_value="A-01",
            )
        assert db._connection.execute(
            "SELECT COUNT(*) FROM comparisons WHERE id='comparison-rollback'"
        ).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("tamper_sql", "expected_table"),
    [
        ("UPDATE extracted_fields SET value='TAMPERED' WHERE id='field-1'", "extracted_fields"),
        ("UPDATE comparisons SET status='TAMPERED' WHERE id='comparison-1'", "comparisons"),
        ("UPDATE ledger_snapshots SET data_json='{}' WHERE id='ledger-1'", "ledger_snapshots"),
        ("UPDATE model_decisions SET reason='TAMPERED' WHERE id='model-1'", "model_decisions"),
        ("UPDATE certificates SET metadata_json='{}' WHERE id='certificate-1'", "certificates"),
        ("UPDATE review_decisions SET decision='FAIL' WHERE id='review-1'", "review_decisions"),
    ],
)
def test_business_integrity_detects_direct_core_table_tampering(tamper_sql, expected_table):
    db = Database(":memory:")
    try:
        _seed_integrity_records(db)
        assert db.verify_business_integrity() == (True, None)
        db._connection.execute(tamper_sql)
        valid, reason = db.verify_business_integrity()
        assert valid is False
        assert reason == f"business row hash mismatch: {expected_table}/" + {
            "extracted_fields": "field-1",
            "comparisons": "comparison-1",
            "ledger_snapshots": "ledger-1",
            "model_decisions": "model-1",
            "certificates": "certificate-1",
            "review_decisions": "review-1",
        }[expected_table]
    finally:
        db.close()


def test_business_integrity_rejects_direct_insert_without_event():
    db = Database(":memory:")
    try:
        _seed(db)
        db._connection.execute(
            """INSERT INTO extracted_fields
               VALUES ('legacy-field',NULL,'certificate-1','serial_number','OLD',NULL,
                       'VISION',NULL,NULL,NULL,0,'2026-01-01T00:00:00+00:00')"""
        )
        assert db.verify_business_integrity() == (
            False, "missing integrity event: extracted_fields/legacy-field"
        )
    finally:
        db.close()


def test_business_integrity_rejects_direct_delete_of_audited_row():
    db = Database(":memory:")
    try:
        _seed_integrity_records(db)
        db._connection.execute("DELETE FROM comparisons WHERE id='comparison-1'")
        assert db.verify_business_integrity() == (
            False, "audited comparisons row missing: comparison-1"
        )
    finally:
        db.close()


def test_schema_v1_rows_are_baselined_once_then_enforced(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = Database(path)
    try:
        _seed(db)
        db._connection.execute(
            """INSERT INTO extracted_fields
               VALUES ('legacy-field',NULL,'certificate-1','serial_number','OLD',NULL,
                       'VISION',NULL,NULL,NULL,0,'2026-01-01T00:00:00+00:00')"""
        )
        db.append_audit_event(
            "extracted_field", "legacy-field", "EXTRACTED_FIELD_ADDED",
            {"field_id": "legacy-field", "certificate_id": "certificate-1"},
        )
        db._connection.execute("PRAGMA user_version=1")
    finally:
        db.close()

    migrated = Database(path)
    try:
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert migrated.verify_business_integrity() == (True, None)
        baseline = next(
            event for event in migrated.list_audit_events()
            if event["event_type"] == "BUSINESS_ROW_BASELINED"
            and event["payload"]["integrity_row_id"] == "legacy-field"
        )
        assert baseline["payload"]["integrity_table"] == "extracted_fields"

        # Migration is not repeated at schema v2, so a later out-of-band row stays detectable.
        migrated._connection.execute(
            """INSERT INTO extracted_fields
               VALUES ('post-migration',NULL,'certificate-1','serial_number','NEW',NULL,
                       'VISION',NULL,NULL,NULL,0,'2026-01-02T00:00:00+00:00')"""
        )
    finally:
        migrated.close()

    reopened = Database(path)
    try:
        assert reopened.verify_business_integrity() == (
            False, "missing integrity event: extracted_fields/post-migration"
        )
    finally:
        reopened.close()
