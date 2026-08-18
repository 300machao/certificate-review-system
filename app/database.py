from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = 2
BUSINESS_INTEGRITY_VERSION = 1

# Every table whose contents can affect a detail view, decision, status summary, or export is
# covered.  Mutable rows are allowed to have historical hashes, but their latest integrity event
# must describe the current row.
PROTECTED_BUSINESS_TABLES: dict[str, bool] = {
    "batches": True,
    "certificates": True,
    "ledger_snapshots": False,
    "extraction_runs": True,
    "extracted_fields": False,
    "comparisons": False,
    "model_decisions": False,
    "review_tasks": True,
    "review_decisions": False,
    "ledger_change_proposals": True,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _decode_json(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return {} if fallback is None else fallback
    return json.loads(value)


def _hash_json(value: Any) -> str:
    """Return a deterministic digest without copying business values into audit logs."""
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class Database:
    """Thread-safe SQLite persistence with transactional, hash-chained audit events."""

    def __init__(self, path: str | Path, *, initialize: bool = True) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connection = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA journal_mode=WAL")
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        with self._lock:
            previous_schema_version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'UPLOADED',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS certificates (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                    file_type TEXT NOT NULL,
                    storage_path TEXT,
                    page_count INTEGER CHECK(page_count IS NULL OR page_count >= 0),
                    status TEXT NOT NULL DEFAULT 'UPLOADED',
                    duplicate_of TEXT REFERENCES certificates(id) ON DELETE RESTRICT,
                    authenticity_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS ledger_snapshots (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
                    certificate_id TEXT REFERENCES certificates(id) ON DELETE RESTRICT,
                    source_name TEXT NOT NULL,
                    source_hash TEXT,
                    row_key TEXT,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS extraction_runs (
                    id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL REFERENCES certificates(id) ON DELETE RESTRICT,
                    provider TEXT NOT NULL,
                    model TEXT,
                    prompt_version TEXT,
                    status TEXT NOT NULL DEFAULT 'RUNNING',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    latency_ms INTEGER,
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    raw_response_hash TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS extracted_fields (
                    id TEXT PRIMARY KEY,
                    extraction_run_id TEXT REFERENCES extraction_runs(id) ON DELETE RESTRICT,
                    certificate_id TEXT NOT NULL REFERENCES certificates(id) ON DELETE RESTRICT,
                    field_name TEXT NOT NULL,
                    value TEXT,
                    normalized_value TEXT,
                    source TEXT NOT NULL,
                    page INTEGER CHECK(page IS NULL OR page >= 1),
                    evidence TEXT,
                    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                    uncertain INTEGER NOT NULL DEFAULT 0 CHECK(uncertain IN (0,1)),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS comparisons (
                    id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL REFERENCES certificates(id) ON DELETE RESTRICT,
                    field_name TEXT NOT NULL,
                    document_value TEXT,
                    qr_value TEXT,
                    ledger_value TEXT,
                    normalized_document_value TEXT,
                    normalized_qr_value TEXT,
                    normalized_ledger_value TEXT,
                    status TEXT NOT NULL,
                    risk TEXT,
                    basis TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_decisions (
                    id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL REFERENCES certificates(id) ON DELETE RESTRICT,
                    extraction_run_id TEXT REFERENCES extraction_runs(id) ON DELETE RESTRICT,
                    role TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    risk TEXT,
                    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                    reason TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    latency_ms INTEGER,
                    request_id TEXT,
                    prompt_version TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_tasks (
                    id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL REFERENCES certificates(id) ON DELETE RESTRICT,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    reason TEXT NOT NULL,
                    assigned_to TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS review_decisions (
                    id TEXT PRIMARY KEY,
                    review_task_id TEXT NOT NULL REFERENCES review_tasks(id) ON DELETE RESTRICT,
                    certificate_id TEXT NOT NULL REFERENCES certificates(id) ON DELETE RESTRICT,
                    version INTEGER NOT NULL CHECK(version >= 1),
                    decision TEXT NOT NULL,
                    comment TEXT,
                    corrected_fields_json TEXT NOT NULL DEFAULT '{}',
                    reviewer TEXT NOT NULL DEFAULT 'local_user',
                    created_at TEXT NOT NULL,
                    UNIQUE(review_task_id, version)
                );

                CREATE TABLE IF NOT EXISTS ledger_change_proposals (
                    id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL REFERENCES certificates(id) ON DELETE RESTRICT,
                    status TEXT NOT NULL DEFAULT 'PROPOSED',
                    proposed_changes_json TEXT NOT NULL,
                    rationale TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );

                CREATE INDEX IF NOT EXISTS idx_certificates_batch_status
                    ON certificates(batch_id, status);
                CREATE INDEX IF NOT EXISTS idx_certificates_sha256 ON certificates(sha256);
                CREATE INDEX IF NOT EXISTS idx_ledger_batch ON ledger_snapshots(batch_id);
                CREATE INDEX IF NOT EXISTS idx_ledger_certificate ON ledger_snapshots(certificate_id);
                CREATE INDEX IF NOT EXISTS idx_runs_certificate ON extraction_runs(certificate_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_fields_certificate_name
                    ON extracted_fields(certificate_id, field_name);
                CREATE INDEX IF NOT EXISTS idx_comparisons_certificate
                    ON comparisons(certificate_id, status);
                CREATE INDEX IF NOT EXISTS idx_decisions_certificate
                    ON model_decisions(certificate_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_review_tasks_status
                    ON review_tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_review_decisions_task
                    ON review_decisions(review_task_id, version);
                CREATE INDEX IF NOT EXISTS idx_proposals_certificate
                    ON ledger_change_proposals(certificate_id, status);
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_events(entity_type, entity_id, id);

                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'audit_events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'audit_events are append-only');
                END;
                """
            )
            if previous_schema_version < SCHEMA_VERSION:
                # This runs once when a pre-v2 database is opened.  It establishes a trusted
                # starting hash for legacy rows; later opens never baseline missing events, so a
                # direct INSERT cannot be hidden by restarting the application.
                with self.transaction():
                    self._backfill_business_integrity_in_tx()
                    self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Use BEGIN IMMEDIATE at the outer boundary and savepoints when nested."""
        with self._lock:
            depth = getattr(self._local, "depth", 0)
            savepoint = f"sp_{depth}_{uuid.uuid4().hex}"
            try:
                if depth == 0:
                    self._connection.execute("BEGIN IMMEDIATE")
                else:
                    self._connection.execute(f"SAVEPOINT {savepoint}")
                self._local.depth = depth + 1
                yield self._connection
                if depth == 0:
                    self._connection.execute("COMMIT")
                else:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                if depth == 0:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                else:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._local.depth = depth

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json"):
                result[key.removesuffix("_json")] = _decode_json(result.pop(key))
        for key in ("uncertain",):
            if key in result:
                result[key] = bool(result[key])
        return result

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._row(self._connection.execute(sql, params).fetchone())

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [self._row(row) or {} for row in self._connection.execute(sql, params)]

    @staticmethod
    def _audit_hash(sequence: int, entity_type: str, entity_id: str, event_type: str,
                    actor: str, occurred_at: str, payload_json: str, prev_hash: str) -> str:
        body = _json({
            "id": sequence, "entity_type": entity_type, "entity_id": entity_id,
            "event_type": event_type, "actor": actor, "occurred_at": occurred_at,
            "payload": json.loads(payload_json), "prev_hash": prev_hash,
        })
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _append_audit_event_in_tx(self, entity_type: str, entity_id: str, event_type: str,
                                  payload: Any = None, actor: str = "system") -> dict[str, Any]:
        previous = self._connection.execute(
            "SELECT id, event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        sequence = (int(previous["id"]) + 1) if previous else 1
        prev_hash = str(previous["event_hash"]) if previous else "0" * 64
        occurred_at = utc_now()
        payload_json = _json(payload)
        event_hash = self._audit_hash(
            sequence, entity_type, entity_id, event_type, actor, occurred_at, payload_json, prev_hash
        )
        self._connection.execute(
            """INSERT INTO audit_events
               (id, entity_type, entity_id, event_type, actor, occurred_at,
                payload_json, prev_hash, event_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sequence, entity_type, entity_id, event_type, actor, occurred_at,
             payload_json, prev_hash, event_hash),
        )
        return self._row(self._connection.execute(
            "SELECT * FROM audit_events WHERE id=?", (sequence,)
        ).fetchone()) or {}

    @staticmethod
    def _integrity_identity(event_type: str, payload: Mapping[str, Any]) -> tuple[str, str] | None:
        table = payload.get("integrity_table")
        row_id = payload.get("integrity_row_id")
        if isinstance(table, str) and table in PROTECTED_BUSINESS_TABLES \
                and isinstance(row_id, str) and row_id:
            return table, row_id
        # Compatibility with the first integrity payloads written before schema v2.
        legacy = {
            "EXTRACTED_FIELD_ADDED": ("extracted_fields", "field_id"),
            "COMPARISON_ADDED": ("comparisons", "comparison_id"),
            "REVIEW_DECISION_ADDED": ("review_decisions", "decision_id"),
        }.get(event_type)
        if legacy is None:
            return None
        legacy_table, id_key = legacy
        legacy_id = payload.get(id_key)
        if isinstance(legacy_id, str) and legacy_id:
            return legacy_table, legacy_id
        return None

    def _append_business_event_in_tx(self, table: str, row_id: str, entity_type: str,
                                     entity_id: str, event_type: str,
                                     payload: Mapping[str, Any] | None = None,
                                     actor: str = "system") -> dict[str, Any]:
        if table not in PROTECTED_BUSINESS_TABLES:
            raise ValueError(f"unprotected business table: {table}")
        row = self._connection.execute(
            f"SELECT * FROM {table} WHERE id=?", (row_id,)
        ).fetchone()
        if row is None:
            raise KeyError(row_id)
        raw = dict(row)
        enriched = dict(payload or {})
        enriched.update({
            "integrity_version": BUSINESS_INTEGRITY_VERSION,
            "integrity_table": table,
            "integrity_row_id": row_id,
            "row_hash": _hash_json(raw),
        })
        if table == "certificates":
            enriched.setdefault("certificate_id", row_id)
        elif raw.get("certificate_id") is not None:
            enriched.setdefault("certificate_id", raw["certificate_id"])
        if raw.get("batch_id") is not None:
            enriched.setdefault("batch_id", raw["batch_id"])
        return self._append_audit_event_in_tx(
            entity_type, entity_id, event_type, enriched, actor
        )

    def _backfill_business_integrity_in_tx(self) -> None:
        chain_valid, chain_reason = self.verify_audit_chain()
        if not chain_valid:
            raise sqlite3.IntegrityError(
                f"cannot baseline an invalid audit chain: {chain_reason}"
            )
        covered: set[tuple[str, str, str]] = set()
        for event in self._fetchall("SELECT * FROM audit_events ORDER BY id"):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("integrity_version") != BUSINESS_INTEGRITY_VERSION:
                continue
            identity = self._integrity_identity(str(event["event_type"]), payload)
            row_hash = payload.get("row_hash")
            if identity is not None and isinstance(row_hash, str):
                covered.add((identity[0], identity[1], row_hash))

        for table in PROTECTED_BUSINESS_TABLES:
            for row in self._connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall():
                row_id = str(row["id"])
                row_hash = _hash_json(dict(row))
                if (table, row_id, row_hash) in covered:
                    continue
                self._append_business_event_in_tx(
                    table, row_id, "business_row", f"{table}:{row_id}",
                    "BUSINESS_ROW_BASELINED",
                    {"migration_from_schema": 1, "baseline": True},
                )

    def append_audit_event(self, entity_type: str, entity_id: str, event_type: str,
                           payload: Any = None, actor: str = "system") -> dict[str, Any]:
        with self.transaction():
            return self._append_audit_event_in_tx(
                entity_type, entity_id, event_type, payload, actor
            )

    def verify_audit_chain(self) -> tuple[bool, str | None]:
        rows = self._fetchall("SELECT * FROM audit_events ORDER BY id")
        expected_prev = "0" * 64
        for expected_id, row in enumerate(rows, start=1):
            if row["id"] != expected_id:
                return False, f"audit sequence gap at {expected_id}"
            if row["prev_hash"] != expected_prev:
                return False, f"previous hash mismatch at {expected_id}"
            payload_json = _json(row["payload"])
            expected_hash = self._audit_hash(
                row["id"], row["entity_type"], row["entity_id"], row["event_type"],
                row["actor"], row["occurred_at"], payload_json, row["prev_hash"]
            )
            if row["event_hash"] != expected_hash:
                return False, f"event hash mismatch at {expected_id}"
            expected_prev = row["event_hash"]
        return True, None

    def verify_business_integrity(self) -> tuple[bool, str | None]:
        """Fail closed when an audited row or its matching audit event is missing or changed.

        Schema-v1 databases are baselined once during migration.  In schema v2 every protected
        row must have a hash-bearing event, so this verifies both event-to-row and row-to-event.
        """
        with self._lock:
            chain_valid, chain_reason = self.verify_audit_chain()
            if not chain_valid:
                return False, f"audit chain invalid: {chain_reason}"

            events = self._fetchall("SELECT * FROM audit_events ORDER BY id")
            coverage: dict[tuple[str, str], list[tuple[int, str, dict[str, Any], str]]] = {}
            for event in events:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("integrity_version") != BUSINESS_INTEGRITY_VERSION:
                    continue
                event_type = str(event.get("event_type"))
                identity = self._integrity_identity(event_type, payload)
                if identity is None:
                    return False, f"integrity identity missing at audit event {event['id']}"
                table, row_id = identity
                row_hash = payload.get("row_hash")
                if not isinstance(row_hash, str) or len(row_hash) != 64:
                    return False, f"integrity row hash missing at audit event {event['id']}"
                raw_row = self._connection.execute(
                    f"SELECT * FROM {table} WHERE id=?", (row_id,)
                ).fetchone()
                if raw_row is None:
                    return False, f"audited {table} row missing: {row_id}"
                row = dict(raw_row)
                expected_row_hash = _hash_json(row)
                coverage.setdefault((table, row_id), []).append(
                    (int(event["id"]), row_hash, payload, event_type)
                )
                if not PROTECTED_BUSINESS_TABLES[table] and row_hash != expected_row_hash:
                    return False, f"business row hash mismatch: {table}/{row_id}"

                expected_certificate_id = (
                    row_id if table == "certificates" else row.get("certificate_id")
                )
                if expected_certificate_id is not None \
                        and payload.get("certificate_id") != expected_certificate_id:
                    return False, f"certificate link mismatch: {table}/{row_id}"
                expected: dict[str, Any] = {}
                if event_type == "EXTRACTED_FIELD_ADDED":
                    expected = {
                        "field_name": row["field_name"],
                        "source": row["source"],
                        "value_hash": _hash_json(row["value"]),
                        "normalized_value_hash": _hash_json(row["normalized_value"]),
                    }
                elif event_type == "COMPARISON_ADDED":
                    expected = {
                        "field_name": row["field_name"],
                        "status": row["status"],
                        "value_hashes": {
                            "document": _hash_json(row["document_value"]),
                            "qr": _hash_json(row["qr_value"]),
                            "ledger": _hash_json(row["ledger_value"]),
                            "normalized_document": _hash_json(
                                row["normalized_document_value"]
                            ),
                            "normalized_qr": _hash_json(row["normalized_qr_value"]),
                            "normalized_ledger": _hash_json(row["normalized_ledger_value"]),
                        },
                    }
                elif event_type == "REVIEW_DECISION_ADDED":
                    expected = {
                        "review_task_id": row["review_task_id"],
                        "version": row["version"],
                        "decision": row["decision"],
                    }
                for key, expected_value in expected.items():
                    if payload.get(key) != expected_value:
                        return False, f"integrity payload mismatch for {key}: {table}/{row_id}"

            # Reverse direction: every row must be represented by at least one integrity event.
            # For mutable tables only the newest event is authoritative; for append-only tables
            # all events have already been required to match above.
            for table, mutable in PROTECTED_BUSINESS_TABLES.items():
                for raw_row in self._connection.execute(f"SELECT * FROM {table}").fetchall():
                    row = dict(raw_row)
                    row_id = str(row["id"])
                    row_events = coverage.get((table, row_id), [])
                    if not row_events:
                        return False, f"missing integrity event: {table}/{row_id}"
                    current_hash = _hash_json(row)
                    if mutable:
                        latest = max(row_events, key=lambda item: item[0])
                        if latest[1] != current_hash:
                            return False, f"business row hash mismatch: {table}/{row_id}"
                    elif not any(item[1] == current_hash for item in row_events):
                        return False, f"business row hash mismatch: {table}/{row_id}"
        return True, None

    def list_audit_events(self, *, entity_type: str | None = None,
                          entity_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._fetchall(f"SELECT * FROM audit_events{where} ORDER BY id", params)

    def create_batch(self, batch_id: str, request_id: str, metadata: Mapping[str, Any] | None = None,
                     status: str = "UPLOADED") -> dict[str, Any]:
        now = utc_now()
        with self.transaction():
            self._connection.execute(
                "INSERT INTO batches VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (batch_id, request_id, status, now, now, _json(metadata)),
            )
            self._append_business_event_in_tx(
                "batches", batch_id, "batch", batch_id, "BATCH_CREATED",
                {"request_id": request_id, "status": status},
            )
        return self.get_batch(batch_id) or {}

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM batches WHERE id=?", (batch_id,))

    def list_batches(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM batches ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def update_batch_status(self, batch_id: str, status: str,
                            completed_at: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.transaction():
            cursor = self._connection.execute(
                """UPDATE batches SET status=?, updated_at=?,
                   completed_at=COALESCE(?, completed_at) WHERE id=?""",
                (status, now, completed_at, batch_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(batch_id)
            self._append_business_event_in_tx(
                "batches", batch_id, "batch", batch_id, "BATCH_STATUS_CHANGED",
                {"status": status, "completed_at": completed_at},
            )
        return self.get_batch(batch_id) or {}

    def add_certificate(self, batch_id: str, filename: str, sha256: str, size_bytes: int,
                        file_type: str, *, certificate_id: str | None = None,
                        storage_path: str | None = None, page_count: int | None = None,
                        status: str = "UPLOADED", duplicate_of: str | None = None,
                        metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        certificate_id = certificate_id or str(uuid.uuid4())
        now = utc_now()
        with self.transaction():
            self._connection.execute(
                """INSERT INTO certificates
                   (id,batch_id,filename,sha256,size_bytes,file_type,storage_path,page_count,status,
                    duplicate_of,created_at,updated_at,metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (certificate_id, batch_id, filename, sha256, size_bytes, file_type, storage_path,
                 page_count, status, duplicate_of, now, now, _json(metadata)),
            )
            self._append_business_event_in_tx(
                "certificates", certificate_id, "certificate", certificate_id,
                "CERTIFICATE_ADDED",
                {"batch_id": batch_id, "filename": filename,
                 "sha256": sha256, "status": status},
            )
        return self.get_certificate(certificate_id) or {}

    def get_certificate(self, certificate_id: str) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM certificates WHERE id=?", (certificate_id,))

    def list_certificates(self, batch_id: str, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            return self._fetchall(
                "SELECT * FROM certificates WHERE batch_id=? ORDER BY created_at,id", (batch_id,)
            )
        return self._fetchall(
            "SELECT * FROM certificates WHERE batch_id=? AND status=? ORDER BY created_at,id",
            (batch_id, status),
        )

    def update_certificate_status(self, certificate_id: str, status: str, *,
                                  error: str | None = None,
                                  authenticity_status: str | None = None,
                                  page_count: int | None = None,
                                  metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self.transaction():
            current = self._connection.execute(
                "SELECT metadata_json FROM certificates WHERE id=?", (certificate_id,)
            ).fetchone()
            if current is None:
                raise KeyError(certificate_id)
            merged_metadata = _decode_json(current["metadata_json"])
            if metadata:
                merged_metadata.update(dict(metadata))
            cursor = self._connection.execute(
                """UPDATE certificates SET status=?, error=?,
                   authenticity_status=COALESCE(?,authenticity_status),
                   page_count=COALESCE(?,page_count), metadata_json=?, updated_at=? WHERE id=?""",
                (status, error, authenticity_status, page_count, _json(merged_metadata), utc_now(),
                 certificate_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(certificate_id)
            self._append_business_event_in_tx(
                "certificates", certificate_id, "certificate", certificate_id,
                "CERTIFICATE_STATUS_CHANGED",
                {"status": status, "error": error, "authenticity_status": authenticity_status,
                 "page_count": page_count, "metadata": dict(metadata or {})},
            )
        return self.get_certificate(certificate_id) or {}

    def add_ledger_snapshot(self, batch_id: str, source_name: str, data: Mapping[str, Any], *,
                            snapshot_id: str | None = None, certificate_id: str | None = None,
                            source_hash: str | None = None, row_key: str | None = None) -> dict[str, Any]:
        snapshot_id = snapshot_id or str(uuid.uuid4())
        with self.transaction():
            self._connection.execute(
                "INSERT INTO ledger_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (snapshot_id, batch_id, certificate_id, source_name, source_hash, row_key,
                 _json(data), utc_now()),
            )
            self._append_business_event_in_tx(
                "ledger_snapshots", snapshot_id, "ledger_snapshot", snapshot_id,
                "LEDGER_SNAPSHOT_ADDED",
                {"batch_id": batch_id, "certificate_id": certificate_id,
                 "source_name": source_name, "row_key": row_key},
            )
        return self._fetchone("SELECT * FROM ledger_snapshots WHERE id=?", (snapshot_id,)) or {}

    def list_ledger_snapshots(self, *, batch_id: str | None = None,
                              certificate_id: str | None = None) -> list[dict[str, Any]]:
        if certificate_id is not None:
            return self._fetchall(
                "SELECT * FROM ledger_snapshots WHERE certificate_id=? ORDER BY created_at",
                (certificate_id,),
            )
        if batch_id is not None:
            return self._fetchall(
                "SELECT * FROM ledger_snapshots WHERE batch_id=? ORDER BY created_at", (batch_id,)
            )
        return self._fetchall("SELECT * FROM ledger_snapshots ORDER BY created_at")

    def create_extraction_run(self, certificate_id: str, provider: str, *,
                              run_id: str | None = None, model: str | None = None,
                              prompt_version: str | None = None,
                              metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        run_id = run_id or str(uuid.uuid4())
        with self.transaction():
            self._connection.execute(
                """INSERT INTO extraction_runs
                   (id,certificate_id,provider,model,prompt_version,status,started_at,metadata_json)
                   VALUES (?,?,?,?,?,'RUNNING',?,?)""",
                (run_id, certificate_id, provider, model, prompt_version, utc_now(), _json(metadata)),
            )
            self._append_business_event_in_tx(
                "extraction_runs", run_id, "extraction_run", run_id, "EXTRACTION_STARTED",
                {"certificate_id": certificate_id, "provider": provider,
                 "model": model, "prompt_version": prompt_version},
            )
        return self._fetchone("SELECT * FROM extraction_runs WHERE id=?", (run_id,)) or {}

    def finish_extraction_run(self, run_id: str, status: str, *, latency_ms: int | None = None,
                              usage: Mapping[str, Any] | None = None, error: str | None = None,
                              raw_response_hash: str | None = None) -> dict[str, Any]:
        with self.transaction():
            cursor = self._connection.execute(
                """UPDATE extraction_runs SET status=?,completed_at=?,latency_ms=?,usage_json=?,
                   error=?,raw_response_hash=? WHERE id=?""",
                (status, utc_now(), latency_ms, _json(usage), error, raw_response_hash, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            self._append_business_event_in_tx(
                "extraction_runs", run_id, "extraction_run", run_id, "EXTRACTION_FINISHED",
                {"status": status, "latency_ms": latency_ms,
                 "usage": usage or {}, "error": error},
            )
        return self._fetchone("SELECT * FROM extraction_runs WHERE id=?", (run_id,)) or {}

    def list_extraction_runs(self, certificate_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM extraction_runs WHERE certificate_id=? ORDER BY started_at,id",
            (certificate_id,),
        )

    def add_extracted_field(self, certificate_id: str, field_name: str, *, value: Any = None,
                            source: str, field_id: str | None = None,
                            extraction_run_id: str | None = None,
                            normalized_value: Any = None, page: int | None = None,
                            evidence: str | None = None, confidence: float | None = None,
                            uncertain: bool = False) -> dict[str, Any]:
        field_id = field_id or str(uuid.uuid4())
        with self.transaction():
            created_at = utc_now()
            self._connection.execute(
                "INSERT INTO extracted_fields VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (field_id, extraction_run_id, certificate_id, field_name,
                 None if value is None else str(value),
                 None if normalized_value is None else str(normalized_value), source, page, evidence,
                 confidence, int(uncertain), created_at),
            )
            row = self._connection.execute(
                "SELECT * FROM extracted_fields WHERE id=?", (field_id,)
            ).fetchone()
            self._append_business_event_in_tx(
                "extracted_fields", field_id, "extracted_field", field_id,
                "EXTRACTED_FIELD_ADDED",
                {
                    "field_id": field_id,
                    "certificate_id": certificate_id,
                    "extraction_run_id": extraction_run_id,
                    "field_name": field_name,
                    "source": source,
                    "value_hash": _hash_json(row["value"]),
                    "normalized_value_hash": _hash_json(row["normalized_value"]),
                },
            )
        return self._fetchone("SELECT * FROM extracted_fields WHERE id=?", (field_id,)) or {}

    def list_extracted_fields(self, certificate_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM extracted_fields WHERE certificate_id=? ORDER BY created_at,id",
            (certificate_id,),
        )

    def add_comparison(self, certificate_id: str, field_name: str, status: str, *,
                       comparison_id: str | None = None, document_value: Any = None,
                       qr_value: Any = None, ledger_value: Any = None,
                       normalized_document_value: Any = None,
                       normalized_qr_value: Any = None,
                       normalized_ledger_value: Any = None,
                       risk: str | None = None, basis: str | None = None) -> dict[str, Any]:
        comparison_id = comparison_id or str(uuid.uuid4())
        values = (document_value, qr_value, ledger_value, normalized_document_value,
                  normalized_qr_value, normalized_ledger_value)
        values = tuple(None if value is None else str(value) for value in values)
        with self.transaction():
            created_at = utc_now()
            self._connection.execute(
                "INSERT INTO comparisons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (comparison_id, certificate_id, field_name, *values, status, risk, basis,
                 created_at),
            )
            row = self._connection.execute(
                "SELECT * FROM comparisons WHERE id=?", (comparison_id,)
            ).fetchone()
            self._append_business_event_in_tx(
                "comparisons", comparison_id, "comparison", comparison_id,
                "COMPARISON_ADDED",
                {
                    "comparison_id": comparison_id,
                    "certificate_id": certificate_id,
                    "field_name": field_name,
                    "status": status,
                    "risk": risk,
                    "value_hashes": {
                        "document": _hash_json(row["document_value"]),
                        "qr": _hash_json(row["qr_value"]),
                        "ledger": _hash_json(row["ledger_value"]),
                        "normalized_document": _hash_json(row["normalized_document_value"]),
                        "normalized_qr": _hash_json(row["normalized_qr_value"]),
                        "normalized_ledger": _hash_json(row["normalized_ledger_value"]),
                    },
                },
            )
        return self._fetchone("SELECT * FROM comparisons WHERE id=?", (comparison_id,)) or {}

    def list_comparisons(self, certificate_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM comparisons WHERE certificate_id=? ORDER BY created_at,id",
            (certificate_id,),
        )

    def add_model_decision(self, certificate_id: str, role: str, provider: str, model: str,
                           decision: str, *, decision_id: str | None = None,
                           extraction_run_id: str | None = None, risk: str | None = None,
                           confidence: float | None = None, reason: str | None = None,
                           evidence: Any = None, usage: Mapping[str, Any] | None = None,
                           latency_ms: int | None = None, request_id: str | None = None,
                           prompt_version: str | None = None) -> dict[str, Any]:
        decision_id = decision_id or str(uuid.uuid4())
        with self.transaction():
            self._connection.execute(
                """INSERT INTO model_decisions
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, certificate_id, extraction_run_id, role, provider, model, decision,
                 risk, confidence, reason, _json(evidence), _json(usage), latency_ms, request_id,
                 prompt_version, utc_now()),
            )
            self._append_business_event_in_tx(
                "model_decisions", decision_id, "model_decision", decision_id,
                "MODEL_DECISION_RECORDED",
                {"certificate_id": certificate_id, "role": role,
                 "provider": provider, "model": model,
                 "decision": decision, "risk": risk, "confidence": confidence},
            )
        return self._fetchone("SELECT * FROM model_decisions WHERE id=?", (decision_id,)) or {}

    def list_model_decisions(self, certificate_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM model_decisions WHERE certificate_id=? ORDER BY created_at,id",
            (certificate_id,),
        )

    def create_review_task(self, certificate_id: str, reason: str, *,
                           task_id: str | None = None, assigned_to: str | None = None) -> dict[str, Any]:
        task_id = task_id or str(uuid.uuid4())
        now = utc_now()
        with self.transaction():
            self._connection.execute(
                "INSERT INTO review_tasks VALUES (?,?, 'OPEN', ?, ?, ?, ?, NULL)",
                (task_id, certificate_id, reason, assigned_to, now, now),
            )
            self._append_business_event_in_tx(
                "review_tasks", task_id, "review_task", task_id, "REVIEW_TASK_CREATED",
                {"certificate_id": certificate_id, "reason": reason},
            )
        return self.get_review_task(task_id) or {}

    def get_review_task(self, task_id: str) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM review_tasks WHERE id=?", (task_id,))

    def list_review_tasks(self, *, status: str | None = None,
                          certificate_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if certificate_id is not None:
            clauses.append("certificate_id=?")
            params.append(certificate_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._fetchall(f"SELECT * FROM review_tasks{where} ORDER BY created_at", params)

    def add_review_decision(self, review_task_id: str, decision: str, *,
                            decision_id: str | None = None, comment: str | None = None,
                            corrected_fields: Mapping[str, Any] | None = None,
                            reviewer: str = "local_user") -> dict[str, Any]:
        decision_id = decision_id or str(uuid.uuid4())
        with self.transaction():
            task = self._connection.execute(
                "SELECT certificate_id FROM review_tasks WHERE id=?", (review_task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(review_task_id)
            version = int(self._connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM review_decisions WHERE review_task_id=?",
                (review_task_id,),
            ).fetchone()[0])
            now = utc_now()
            self._connection.execute(
                "INSERT INTO review_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                (decision_id, review_task_id, task["certificate_id"], version, decision, comment,
                 _json(corrected_fields), reviewer, now),
            )
            decision_row = self._connection.execute(
                "SELECT * FROM review_decisions WHERE id=?", (decision_id,)
            ).fetchone()
            self._connection.execute(
                "UPDATE review_tasks SET status='RESOLVED',updated_at=?,resolved_at=? WHERE id=?",
                (now, now, review_task_id),
            )
            certificate_row = self._connection.execute(
                "SELECT metadata_json FROM certificates WHERE id=?", (task["certificate_id"],)
            ).fetchone()
            certificate_metadata = _decode_json(certificate_row["metadata_json"])
            certificate_metadata.update({
                "final_decision": decision,
                "review_task_id": review_task_id,
                "review_decision_id": decision_id,
                "review_decision_version": version,
            })
            self._connection.execute(
                """UPDATE certificates SET status='FINALIZED',metadata_json=?,updated_at=?
                   WHERE id=?""",
                (_json(certificate_metadata), now, task["certificate_id"]),
            )
            self._append_business_event_in_tx(
                "review_decisions", decision_id, "review_task", review_task_id,
                "REVIEW_DECISION_ADDED",
                {"decision_id": decision_id,
                 "certificate_id": task["certificate_id"],
                 "review_task_id": review_task_id,
                 "version": version,
                 "decision": decision, "comment": comment,
                 "corrected_fields": corrected_fields or {},
                 "reviewer": reviewer},
            )
            self._append_business_event_in_tx(
                "review_tasks", review_task_id, "review_task", review_task_id,
                "REVIEW_TASK_RESOLVED",
                {"certificate_id": task["certificate_id"], "decision_id": decision_id,
                 "version": version, "decision": decision},
            )
            self._append_business_event_in_tx(
                "certificates", task["certificate_id"],
                "certificate", task["certificate_id"], "HUMAN_REVIEW_FINALIZED",
                {"review_task_id": review_task_id, "review_decision_id": decision_id,
                 "version": version, "decision": decision},
            )
        return self._fetchone("SELECT * FROM review_decisions WHERE id=?", (decision_id,)) or {}

    def list_review_decisions(self, review_task_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM review_decisions WHERE review_task_id=? ORDER BY version",
            (review_task_id,),
        )

    def add_ledger_change_proposal(self, certificate_id: str,
                                   proposed_changes: Mapping[str, Any], *,
                                   proposal_id: str | None = None,
                                   rationale: str | None = None,
                                   status: str = "PROPOSED") -> dict[str, Any]:
        proposal_id = proposal_id or str(uuid.uuid4())
        with self.transaction():
            self._connection.execute(
                "INSERT INTO ledger_change_proposals VALUES (?,?,?,?,?,?,NULL)",
                (proposal_id, certificate_id, status, _json(proposed_changes), rationale, utc_now()),
            )
            self._append_business_event_in_tx(
                "ledger_change_proposals", proposal_id,
                "ledger_change_proposal", proposal_id, "LEDGER_CHANGE_PROPOSED",
                {"certificate_id": certificate_id, "status": status,
                 "proposed_changes": proposed_changes, "rationale": rationale},
            )
        return self._fetchone(
            "SELECT * FROM ledger_change_proposals WHERE id=?", (proposal_id,)
        ) or {}

    def list_ledger_change_proposals(self, certificate_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """SELECT * FROM ledger_change_proposals
               WHERE certificate_id=? ORDER BY created_at,id""",
            (certificate_id,),
        )

    def get_certificate_detail(self, certificate_id: str) -> dict[str, Any] | None:
        certificate = self.get_certificate(certificate_id)
        if certificate is None:
            return None
        return {
            "certificate": certificate,
            "ledger_snapshots": self.list_ledger_snapshots(certificate_id=certificate_id),
            "extraction_runs": self.list_extraction_runs(certificate_id),
            "extracted_fields": self.list_extracted_fields(certificate_id),
            "comparisons": self.list_comparisons(certificate_id),
            "model_decisions": self.list_model_decisions(certificate_id),
            "review_tasks": self.list_review_tasks(certificate_id=certificate_id),
            "review_decisions": self._fetchall(
                "SELECT * FROM review_decisions WHERE certificate_id=? ORDER BY created_at,version",
                (certificate_id,),
            ),
            "ledger_change_proposals": self.list_ledger_change_proposals(certificate_id),
            "audit_events": self.list_audit_events(entity_type="certificate", entity_id=certificate_id),
        }
