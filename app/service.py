from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.authenticity import create_authenticity_provider
from app.config import AppConfig, load_config
from app.database import Database
from app.extractors.image import ImageExtractor
from app.extractors.ocr import create_ocr_provider
from app.extractors.pdf import PDFExtractor
from app.extractors.qr import QRDecoder
from app.ingest import FrozenLedgerRow, IngestResult
from app.model_settings import (
    ModelSettingsError,
    ModelSettingsStore,
    PROVIDER_KINDS,
    validate_base_url,
    validate_model,
)
from app.models import (
    AuthenticityResult,
    BatchResult,
    FieldComparison,
    FieldValue,
    Issue,
    ReviewRecord,
)
from app.providers import OpenAICompatibleProvider, ProviderConfig, create_model_providers
from app.rules.engine import KEY_FIELDS, RuleEngine
from app.rules.fields import FieldExtractor, normalize_field
from app.semantic import arbitrate_differences, review_differences
from app.vision import PROMPT_VERSION as QWEN_PROMPT_VERSION
from app.vision import extract_with_qwen


AUTO_PASS_REQUIRED_FIELDS = KEY_FIELDS | {"verification_result"}
ACTIVE_MODEL_MODES = {"validation", "enabled", "on"}
PROCESSING_STATES = {
    "UPLOADED", "PRECHECKED", "QR/VISION_RUNNING", "COMPARING",
    "SEMANTIC_REVIEW", "MODEL_ARBITRATION", "PROCESSING", "QUEUED",
}


class CertificateRetryInProgressError(RuntimeError):
    """Raised when a certificate already has an active retry in this process."""


class CertificateRetryReservationError(RuntimeError):
    """Raised when a retry worker does not own the certificate reservation."""


class BatchRetryInProgressError(RuntimeError):
    """Raised when the same batch already has a controlled full retry running."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BatchReviewService:
    """Certificate review orchestration with a compatible legacy test surface.

    The web application uses the persistent ingest path. ``create_batch`` remains as
    a small backwards-compatible entry point for the original deterministic tests.
    """

    def __init__(self, config: AppConfig | None = None, root: Path | None = None) -> None:
        self.config = config or load_config()
        self.root = Path(root).resolve() if root else None
        self._ephemeral_root: Path | None = None
        if self.root is None:
            self._ephemeral_root = Path(tempfile.mkdtemp(prefix="certificate-review-service-"))
            self.data_root = self._ephemeral_root
            self.db = Database(":memory:")
        else:
            configured = Path(self.config.data_dir)
            self.data_root = configured if configured.is_absolute() else self.root / configured
            self.data_root.mkdir(parents=True, exist_ok=True)
            self.db = Database(self.data_root / "certificate-review.sqlite3")
        self.storage_root = self.data_root / "storage"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

        self.ocr = create_ocr_provider(self.config.ocr_provider)
        self.qr = QRDecoder()
        self.pdf = PDFExtractor(self.config, self.ocr, self.qr)
        self.image = ImageExtractor(self.ocr, self.qr)
        self.fields = FieldExtractor(self.config)
        self.rules = RuleEngine(self.config)
        self.authenticity = create_authenticity_provider(self.config.authenticity_provider)
        self.model_settings_store = ModelSettingsStore(self.data_root)
        self.providers = create_model_providers(self.model_settings_store.load())
        self._retired_providers: list[OpenAICompatibleProvider] = []
        self.model_mode = os.environ.get("CERT_MODEL_MODE", self.config.model_mode).strip().lower()
        self._provider_limits = {
            "qwen": threading.BoundedSemaphore(max(1, self.config.qwen_concurrency)),
            "glm": threading.BoundedSemaphore(max(1, self.config.glm_concurrency)),
            "deepseek": threading.BoundedSemaphore(max(1, self.config.arbiter_concurrency)),
        }

        self._batches: OrderedDict[str, BatchResult] = OrderedDict()
        self._paths: dict[str, dict[str, Path]] = {}
        self._advanced_batches: set[str] = set()
        # Reserve synchronously before a background task is queued. Persistent
        # workflow state remains in SQLite; this set only prevents duplicate work
        # inside the current process and cannot leave a stale lock after restart.
        self._certificate_retry_reservations: dict[str, tuple[str, str]] = {}
        self._batch_retry_reservations: dict[str, str] = {}
        self._recover_interrupted_work()

    def _recover_interrupted_work(self) -> None:
        """Fail closed after a crash instead of leaving records permanently PROCESSING."""
        for batch in self.db.list_batches(limit=10_000):
            certificates = self.db.list_certificates(batch["id"])
            interrupted = [item for item in certificates if item["status"] in PROCESSING_STATES]
            if not interrupted and batch["status"] not in {"PROCESSING", "QUEUED"}:
                continue
            for item in interrupted:
                self.db.update_certificate_status(
                    item["id"], "PROCESSING_FAILED",
                    error="服务在处理中断后重新启动；请使用单证重试，旧运行记录已保留。",
                    metadata={"workflow_state": "PROCESSING_FAILED", "recovered_after_restart": True},
                )
            self.db.update_batch_status(batch["id"], "PARTIAL_FAILED")
            self.db.append_audit_event(
                "batch", batch["id"], "INTERRUPTED_BATCH_RECOVERED",
                {"interrupted_certificate_count": len(interrupted)},
            )

    def close(self) -> None:
        for provider in [*self.providers.values(), *self._retired_providers]:
            provider.close()
        self.db.close()
        if self._ephemeral_root and self._ephemeral_root.exists():
            shutil.rmtree(self._ephemeral_root, ignore_errors=True)

    def capabilities(self) -> dict[str, Any]:
        models = {name: provider.health() for name, provider in self.providers.items()}
        return {
            "version": "2.0.0",
            "local_only": True,
            "model_mode": self.model_mode,
            "supported_extensions": [".pdf", ".zip", ".json", ".csv"],
            "max_file_size_mb": 512,
            "max_batch_files": self.config.max_batch_files,
            "ocr": {"provider": self.ocr.name, "available": self.ocr.available},
            "models": models,
            "vision_fallback": {
                "enabled": self.model_mode in ACTIVE_MODEL_MODES,
                "configured": models["qwen"]["configured"],
                "model": models["qwen"]["model"],
                "note": "无新凭据时不会调用外部模型，也不会产生模型费用。",
            },
            "authenticity": {
                "provider": self.config.authenticity_provider,
                "authoritative_verification_configured": self.authenticity.authoritative,
                "status": "UNVERIFIED",
                "note": "未接入官方查询前，真实性始终为未验证。",
            },
            "persistence": {
                "database": "SQLite",
                "audit_chain": "SHA-256 append-only",
                "originals": "content-addressed read-only storage",
            },
        }

    def model_health(self, *, live: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, provider in self.providers.items():
            state = provider.health()
            if live and provider.configured:
                try:
                    response = provider.complete_json([
                        {"role": "user", "content": "仅返回JSON对象：{\"ok\":true}"}
                    ], timeout_s=30)
                    state.update({
                        "status": "healthy" if bool(response.data.get("ok")) else "unhealthy",
                        "latency_ms": response.latency_ms,
                        "model": response.model,
                    })
                except Exception as exc:  # provider errors are already credential-redacted
                    state.update({"status": "error", "error": self._safe_error(exc)})
            result[name] = state
        return {"live_check": live, "models": result}

    def model_settings(self) -> dict[str, Any]:
        snapshot = self.model_settings_store.public_snapshot()
        for kind, provider in self.providers.items():
            snapshot["providers"][kind].update({
                "configured": provider.configured,
                "status": "configured" if provider.configured else "disabled",
            })
        return snapshot

    def save_model_settings(self, updates: dict[str, dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            snapshot = self.model_settings_store.save(updates)
            previous = self.providers
            self.providers = create_model_providers(self.model_settings_store.load())
            # A batch may still hold a reference to an old provider. Keep it
            # alive until service shutdown so saving settings cannot interrupt
            # an in-flight certificate run.
            self._retired_providers.extend(previous.values())
        self.db.append_audit_event(
            "system", "model-settings", "MODEL_SETTINGS_UPDATED",
            {
                "providers": {
                    kind: {
                        "base_url": values["base_url"],
                        "model": values["model"],
                        "configured": values["configured"],
                    }
                    for kind, values in snapshot["providers"].items()
                    if kind in updates
                },
                "api_keys_exported": False,
            },
            actor="local_user",
        )
        return self.model_settings()

    def test_model_settings(
        self, kind: str, candidate: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        normalized = kind.strip().lower()
        if normalized not in PROVIDER_KINDS:
            raise ModelSettingsError("不支持的模型类型")
        saved = self.model_settings_store.load()[normalized]
        candidate = candidate or {}
        base_url = validate_base_url(candidate.get("base_url") or saved["base_url"])
        model = validate_model(candidate.get("model") or saved["model"])
        draft_key = candidate.get("api_key")
        api_key = str(draft_key).strip() if draft_key is not None else str(saved["api_key"] or "")
        if not api_key:
            raise ModelSettingsError("请先填写或保存API Key")
        provider = OpenAICompatibleProvider(ProviderConfig.from_env(
            normalized, base_url=base_url, model=model, api_key=api_key,
        ))
        try:
            response = provider.complete_json(
                [{"role": "user", "content": "仅返回JSON对象：{\"ok\":true}"}],
                timeout_s=30,
            )
            if not bool(response.data.get("ok")):
                raise ModelSettingsError("接口已响应，但未返回预期的JSON结果")
            return {
                "provider": normalized,
                "status": "healthy",
                "model": response.model,
                "latency_ms": response.latency_ms,
                "message": "连接成功",
            }
        finally:
            provider.close()

    # ------------------------------------------------------------------
    # Persistent ingest path used by the website and the frozen 81 set.
    # ------------------------------------------------------------------
    def create_ingested_batch(
        self,
        ingest: IngestResult,
        *,
        name: str | None = None,
        failed_inputs: list[dict[str, Any]] | None = None,
    ) -> BatchResult:
        if len(ingest.records) + len(failed_inputs or []) > self.config.max_batch_files:
            raise ValueError(f"单批最多导入 {self.config.max_batch_files} 个证书附件")
        batch = BatchResult(
            batch_id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
            created_at=utc_now(),
            status="QUEUED",
        )
        batch_meta = {
            "name": name or f"外检证书审核-{batch.created_at[:10]}",
            "advanced": True,
            "ingest": {
                "records": len(ingest.records),
                "unique_objects": ingest.unique_objects,
                "duplicates": ingest.duplicates,
                "total_bytes": ingest.total_bytes,
                "ledger_rows": len(ingest.ledger_rows),
                "warnings": list(ingest.warnings),
            },
        }
        self.db.create_batch(batch.batch_id, batch.request_id, batch_meta, status="UPLOADED")

        ledger_queues: dict[str, deque[FrozenLedgerRow]] = defaultdict(deque)
        for row in ingest.ledger_rows:
            sha = str(row.values.get("sha256") or "").lower()
            if sha:
                ledger_queues[sha].append(row)
        unmatched_rows = list(ingest.ledger_rows)
        paths: dict[str, Path] = {}
        for item in ingest.records:
            ledger_row = self._take_ledger_row(item.original_name, item.sha256, ledger_queues, unmatched_rows)
            ledger_fields = self._ledger_fields(ledger_row.values if ledger_row else {})
            record = ReviewRecord(
                record_id=item.record_id,
                filename=item.original_name,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                file_type=Path(item.original_name).suffix.lower().lstrip(".") or item.media_type,
                duplicate_of=item.duplicate_of,
                workflow_state="UPLOADED",
                ledger_fields=ledger_fields,
                metadata={
                    "advanced": True,
                    "stored_path": item.stored_path,
                    "page_count": item.page_count,
                    "source_container": item.source_container,
                    "authenticity_status": "UNVERIFIED",
                },
            )
            batch.records.append(record)
            paths[record.record_id] = Path(item.stored_path)
            self.db.add_certificate(
                batch.batch_id,
                record.filename,
                record.sha256,
                record.size_bytes,
                record.file_type,
                certificate_id=record.record_id,
                storage_path=item.stored_path,
                page_count=item.page_count,
                status="UPLOADED",
                duplicate_of=item.duplicate_of,
                metadata={"review": record.to_dict(), "source_container": item.source_container},
            )
            if ledger_row:
                self.db.add_ledger_snapshot(
                    batch.batch_id,
                    Path(ledger_row.source).name,
                    ledger_row.values,
                    certificate_id=record.record_id,
                    source_hash=self._manifest_source_hash(ledger_row.source),
                    row_key=ledger_row.row_id,
                )

        for failure in failed_inputs or []:
            record = ReviewRecord(
                record_id=str(uuid.uuid4()),
                filename=Path(str(failure.get("filename") or "invalid-input")).name,
                sha256=str(failure.get("sha256") or ""),
                size_bytes=int(failure.get("size_bytes") or 0),
                file_type=Path(str(failure.get("filename") or "")).suffix.lstrip(".") or "unknown",
                status="PROCESSING_FAILED",
                workflow_state="PROCESSING_FAILED",
                error=str(failure.get("error") or "文件预检失败"),
                issues=[Issue(
                    code=str(failure.get("code") or "INGEST_FAILED"),
                    severity="error",
                    title="文件预检失败",
                    detail=str(failure.get("error") or "文件预检失败"),
                )],
                metadata={"advanced": True, "authenticity_status": "UNVERIFIED"},
            )
            batch.records.append(record)
            self.db.add_certificate(
                batch.batch_id, record.filename, record.sha256, record.size_bytes,
                record.file_type, certificate_id=record.record_id,
                status="PROCESSING_FAILED", metadata={"review": record.to_dict()},
            )

        # Preserve unmatched ledger rows as part of the immutable batch snapshot.
        for row in unmatched_rows:
            self.db.add_ledger_snapshot(
                batch.batch_id,
                Path(row.source).name,
                row.values,
                source_hash=self._manifest_source_hash(row.source),
                row_key=row.row_id,
            )

        with self._lock:
            self._batches[batch.batch_id] = batch
            self._paths[batch.batch_id] = paths
            self._advanced_batches.add(batch.batch_id)
            self._trim_batches()
        self.db.update_batch_status(batch.batch_id, "QUEUED")
        return batch

    @staticmethod
    def _take_ledger_row(
        filename: str,
        sha256: str,
        queues: dict[str, deque[FrozenLedgerRow]],
        unmatched: list[FrozenLedgerRow],
    ) -> FrozenLedgerRow | None:
        queue = queues.get(sha256.lower())
        row: FrozenLedgerRow | None = None
        if queue:
            wanted = unicodedata.normalize(
                "NFKC", Path(filename.replace("\\", "/")).name
            ).casefold()
            exact_index = None
            for index, candidate in enumerate(queue):
                path_value = candidate.values.get("local_path") or candidate.values.get("archive_path")
                candidate_name = unicodedata.normalize(
                    "NFKC", Path(str(path_value or "").replace("\\", "/")).name
                ).casefold()
                if candidate_name == wanted:
                    exact_index = index
                    break
            if exact_index is not None:
                row = queue[exact_index]
                del queue[exact_index]
            elif queue:
                row = queue.popleft()
        if row is not None:
            try:
                unmatched.remove(row)
            except ValueError:
                pass
        return row

    @staticmethod
    def _ledger_fields(values: dict[str, Any]) -> dict[str, str]:
        aliases = {
            "unified_number": ("unified_number",),
            "instrument_name": ("equipment_name", "instrument_name"),
            "serial_number": ("factory_serial_number", "serial_number"),
            "model": ("model", "equipment_model"),
            "calibration_date": ("calibrate_or_verify_date", "calibration_date"),
            "due_date": ("record_valid_until", "equipment_valid_until", "due_date"),
            "verification_result": ("verification_result",),
            "certificate_number": ("certificate_number",),
            "issuer_name": ("issuer_name", "institution"),
        }
        result: dict[str, str] = {}
        for target, sources in aliases.items():
            for source in sources:
                value = values.get(source)
                if value is not None and str(value).strip():
                    result[target] = str(value).strip()
                    break
        return result

    @staticmethod
    def _manifest_source_hash(source: str) -> str | None:
        path = Path(source)
        try:
            return sha256_file(path) if path.is_file() else None
        except OSError:
            return None

    def process_batch(self, batch_id: str, cleanup_dir: Path | None = None) -> None:
        if batch_id in self._advanced_batches:
            self._process_advanced_batch(batch_id)
            return
        self._process_legacy_batch(batch_id, cleanup_dir)

    def _process_advanced_batch(self, batch_id: str) -> None:
        with self._lock:
            batch = self._batches[batch_id]
            batch.status = "PROCESSING"
            paths = dict(self._paths.get(batch_id, {}))
        self.db.update_batch_status(batch_id, "PROCESSING")
        cache: dict[str, ReviewRecord] = {}
        unique_records = [
            record for record in batch.records
            if record.status != "PROCESSING_FAILED" and not record.duplicate_of
        ]
        duplicates = [
            record for record in batch.records
            if record.status != "PROCESSING_FAILED" and record.duplicate_of
        ]

        # Certificate jobs can progress concurrently. Provider-specific semaphores
        # below enforce the locked Qwen/GLM/DeepSeek limits (2/4/2 by default).
        workers = max(
            1,
            self.config.qwen_concurrency,
            self.config.glm_concurrency,
            self.config.arbiter_concurrency,
        )
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="certificate-review") as pool:
            futures = {
                pool.submit(self._process_advanced_record, paths[record.record_id], record): record
                for record in unique_records
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    future.result()
                except Exception:
                    # _process_advanced_record already persisted a redacted failure.
                    pass
                cache[record.sha256] = copy.deepcopy(record)

        for record in duplicates:
            started = time.perf_counter()
            try:
                source = cache.get(record.sha256)
                if source is None:
                    raise RuntimeError("重复内容的源记录未成功建立缓存")
                self._reuse_cached_result(source, record)
            except Exception as exc:
                record.status = "PROCESSING_FAILED"
                record.workflow_state = "PROCESSING_FAILED"
                record.error = self._safe_error(exc)
                record.issues.append(Issue(
                    code="CACHE_REUSE_FAILED", severity="error", title="重复内容缓存复用失败",
                    detail=record.error,
                ))
            finally:
                record.elapsed_ms = round((time.perf_counter() - started) * 1000)
                self._store_record_snapshot(record)
        self._refresh_advanced_batch_status(batch)

    def _refresh_advanced_batch_status(self, batch: BatchResult) -> str:
        """Atomically derive the batch status from every current certificate."""
        active = {"UPLOADED", "QUEUED", "PROCESSING"}
        with self._lock:
            if any(record.status in active for record in batch.records):
                status = "PROCESSING"
                completed_at = None
            else:
                status = (
                    "PARTIAL_FAILED"
                    if any(record.status == "PROCESSING_FAILED" for record in batch.records)
                    else "COMPLETED"
                )
                completed_at = utc_now()
            batch.status = status
            batch.completed_at = completed_at
            # Keep the database write under the same service lock so two
            # different certificate retries cannot publish terminal states out
            # of order.
            self.db.update_batch_status(
                batch.batch_id, status, completed_at=completed_at
            )
        return status

    def _process_advanced_record(self, path: Path, record: ReviewRecord) -> None:
        started = time.perf_counter()
        try:
            self._review_advanced_file(path, record)
        except Exception as exc:
            record.status = "PROCESSING_FAILED"
            record.workflow_state = "PROCESSING_FAILED"
            record.error = self._safe_error(exc)
            record.issues.append(Issue(
                code="PROCESSING_FAILED", severity="error", title="证书处理失败",
                detail=record.error,
            ))
            self._store_record_snapshot(record)
            raise
        finally:
            record.elapsed_ms = round((time.perf_counter() - started) * 1000)
            self._store_record_snapshot(record)

    def _review_advanced_file(self, path: Path, record: ReviewRecord) -> None:
        for key in ("qwen_error", "qwen_warnings", "qwen_measurements"):
            record.metadata.pop(key, None)
        record.metadata["qwen_used"] = False
        self._set_state(record, "PRECHECKED")
        self._set_state(record, "QR/VISION_RUNNING")
        local_run = self.db.create_extraction_run(
            record.record_id, "local_pdf_qr", prompt_version="deterministic-v1",
        )
        local_started = time.perf_counter()
        try:
            suffix = path.suffix.lower()
            # OpenCV's QRCodeDetector is not shared across worker threads.
            local_qr = QRDecoder()
            if suffix == ".pdf":
                extraction = PDFExtractor(self.config, self.ocr, local_qr).extract(path)
            else:
                extraction = ImageExtractor(self.ocr, local_qr).extract(path)
            extracted_fields, candidates = self.fields.extract(
                extraction.text_pages, extraction.text_sources
            )
            extraction.fields = extracted_fields
            legacy_status, legacy_issues, _ = self.rules.review(extraction, candidates)
            del legacy_status
            self.db.finish_extraction_run(
                local_run["id"], "SUCCEEDED",
                latency_ms=round((time.perf_counter() - local_started) * 1000),
                raw_response_hash=_sha256_json({
                    "fields": {key: value.value for key, value in extracted_fields.items()},
                    "qr_payload_hashes": [item.get("payload_sha256") for item in extraction.qr_codes],
                }),
            )
        except Exception as exc:
            self.db.finish_extraction_run(
                local_run["id"], "FAILED",
                latency_ms=round((time.perf_counter() - local_started) * 1000),
                error=self._safe_error(exc),
            )
            raise

        record.fields = extracted_fields
        record.qr_codes = extraction.qr_codes
        record.issues = legacy_issues
        record.authenticity = self.authenticity.verify(path)
        record.metadata.update(extraction.metadata)
        record.metadata.update({
            "text_sources": extraction.text_sources,
            "ocr_provider": self.ocr.name,
            "qwen_used": False,
            "authoritative_verification_used": self.authenticity.authoritative,
        })
        self._persist_fields(record.record_id, record.fields, local_run["id"])

        qwen = self.providers["qwen"]
        if self.model_mode in ACTIVE_MODEL_MODES and qwen.configured and path.suffix.lower() == ".pdf":
            vision_run = self.db.create_extraction_run(
                record.record_id, "qwen", model=qwen.config.model,
                prompt_version=QWEN_PROMPT_VERSION,
            )
            vision_started = time.perf_counter()
            try:
                with self._provider_limits["qwen"]:
                    vision = extract_with_qwen(path, qwen, chunk_size=3)
                for key, value in vision.fields.items():
                    value.normalized_value = normalize_field(key, value.value)
                    record.fields[key] = value
                record.metadata["qwen_used"] = True
                record.metadata["qwen_measurements"] = vision.measurements
                record.metadata["qwen_warnings"] = vision.warnings
                usage: dict[str, Any] = {}
                for run in vision.raw_runs:
                    for key, value in (run.get("usage") or {}).items():
                        if isinstance(value, (int, float)):
                            usage[key] = usage.get(key, 0) + value
                self.db.finish_extraction_run(
                    vision_run["id"], "SUCCEEDED",
                    latency_ms=round((time.perf_counter() - vision_started) * 1000),
                    usage=usage, raw_response_hash=_sha256_json(vision.raw_runs),
                )
                self._persist_fields(record.record_id, vision.fields, vision_run["id"])
                for warning in vision.warnings:
                    record.issues.append(Issue(
                        code="QWEN_VISION_WARNING", severity="review", title="千问视觉识别提示",
                        detail=warning,
                    ))
            except Exception as exc:
                self.db.finish_extraction_run(
                    vision_run["id"], "FAILED",
                    latency_ms=round((time.perf_counter() - vision_started) * 1000),
                    error=self._safe_error(exc),
                )
                record.metadata["qwen_error"] = self._safe_error(exc)
                record.issues.append(Issue(
                    code="QWEN_VISION_FAILED", severity="review", title="千问视觉识别失败",
                    detail="已转人工复核；不会因模型失败自动通过。",
                ))

        self._set_state(record, "COMPARING")
        qr_fields = self._flatten_qr_fields(record.qr_codes)
        comparisons = self.rules.compare_three_way(record.fields, qr_fields, record.ledger_fields)
        record.metadata["qr_fields"] = qr_fields
        record.metadata["three_way_comparisons"] = comparisons
        record.comparisons = [
            FieldComparison(
                field=item["field"],
                document_value=item["document_value"],
                qr_value=item["qr_value"] or item["ledger_value"],
                status=item["result"],
                basis=(
                    f"二维码={item['qr_value'] or '无'}；冻结台账={item['ledger_value'] or '无'}；"
                    f"规则={item['rule_version']}"
                ),
            )
            for item in comparisons
        ]
        for item in comparisons:
            self.db.add_comparison(
                record.record_id, item["field"], item["result"],
                document_value=item["document_value"], qr_value=item["qr_value"],
                ledger_value=item["ledger_value"],
                normalized_document_value=(normalize_field(item["field"], item["document_value"])
                                           if item["document_value"] else None),
                normalized_qr_value=(normalize_field(item["field"], item["qr_value"])
                                     if item["qr_value"] else None),
                normalized_ledger_value=(normalize_field(item["field"], item["ledger_value"])
                                         if item["ledger_value"] else None),
                risk=item["severity"], basis=item["rule_version"],
            )

        self._decide_advanced(record, comparisons)

    def _decide_advanced(self, record: ReviewRecord, comparisons: list[dict[str, str]]) -> None:
        required_fields = self._required_auto_pass_fields(record)
        critical_missing = [
            item for item in comparisons
            if item["field"] in required_fields and item["result"] == "MISSING"
        ]
        substantive = [item for item in comparisons if item["result"] == "SUBSTANTIVE_DIFF"]
        low_confidence = [
            key for key in required_fields
            if key in record.fields
            and (record.fields[key].confidence or 0.0) < self.config.auto_pass_confidence
        ]
        missing_document = sorted(required_fields - set(record.fields))
        stable_qr = bool(record.qr_codes) and all(
            bool(item.get("stable_multiscale")) for item in record.qr_codes
        )
        blockers = self.auto_pass_blockers(record, comparisons)
        identifier_evidence = self._independent_identifier_evidence(record, comparisons)
        identifier_confirmed: bool | None = None

        # This semantic question must be reviewed by both text models even when
        # some unrelated rule blocker will still send the certificate to a human.
        # Otherwise the review page misleadingly says that the text models never
        # participated, and the reviewer cannot see their independent judgment
        # about the sample-number / verification-record-id relationship.
        if identifier_evidence is not None:
            identifier_confirmed = self._models_confirm_independent_identifiers(
                record, identifier_evidence
            )

        if not blockers:
            # 样品编号与二维码验真记录号虽然属于不同编号体系，但按业务要求
            # 仍须由 GLM 和 DeepSeek 各自读取原始证据、独立确认。只有双模型
            # 都确认“无需一致”时才自动通过；任何不确定、低置信或接口失败
            # 均转人工复核，不因规则层的 NOT_COMPARABLE 直接跳过文本模型。
            if identifier_evidence is not None:
                if identifier_confirmed:
                    self._finalize_auto_pass(
                        record,
                        "GLM与DeepSeek独立确认：证书样品编号与二维码验真记录号属于"
                        "不同编号体系、无需一致；其余审核门槛全部满足。",
                    )
                else:
                    self._create_or_reuse_review_task(record)
                return

            self._finalize_auto_pass(
                record,
                "关键字段规则一致、结论合格、置信度与二维码稳定性均满足自动通过门槛。",
            )
            return

        if record.duplicate_of:
            record.issues.insert(0, Issue(
                code="DUPLICATE_FILE", severity="review", title="批次内重复内容",
                detail=f"SHA-256 与记录 {record.duplicate_of} 相同；业务附件保留，识别缓存复用。",
            ))
        if missing_document:
            record.issues.append(Issue(
                code="CRITICAL_FIELD_MISSING", severity="review", title="关键字段缺失",
                detail="缺少：" + "、".join(missing_document),
            ))
        if low_confidence:
            record.issues.append(Issue(
                code="LOW_CONFIDENCE", severity="review", title="关键字段置信度不足",
                detail="低于自动通过阈值的字段：" + "、".join(sorted(low_confidence)),
            ))
        if not stable_qr:
            record.issues.append(Issue(
                code="QR_NOT_STABLE", severity="review", title="二维码缺失或多尺度结果不稳定",
                detail="二维码异常时不得自动通过。",
            ))
        if not self._is_qualified_conclusion(
            record.fields.get("verification_result", FieldValue("", "")).value
        ):
            record.issues.append(Issue(
                code="CONCLUSION_NOT_QUALIFIED", severity="review", title="结论不是明确合格",
                detail="只有明确的合格、符合或满足结论才能自动通过；否定或不确定表述不得放行。",
            ))
        if not record.metadata.get("qwen_used"):
            record.issues.append(Issue(
                code="QWEN_EVIDENCE_REQUIRED", severity="review", title="缺少千问视觉证据",
                detail="专利验证模式要求千问完成整份证书视觉提取；未调用或失败时转人工复核。",
            ))

        if substantive:
            self._set_state(record, "SEMANTIC_REVIEW")
            glm = self.providers["glm"]
            evidence = {"differences": substantive, "certificate_sha256": record.sha256}
            if self.model_mode in ACTIVE_MODEL_MODES and glm.configured:
                try:
                    with self._provider_limits["glm"]:
                        decision = review_differences(glm, evidence)
                    record.model_decisions.append(decision.to_dict())
                    self._persist_model_decision(record.record_id, "PRIMARY", decision, evidence)
                    if (
                        decision.decision == "INCONSISTENT" and decision.risk == "HIGH"
                        and decision.confidence >= self.config.model_confidence_threshold
                    ):
                        self._set_state(record, "MODEL_ARBITRATION")
                        arbiter = self.providers["deepseek"]
                        if arbiter.configured:
                            with self._provider_limits["deepseek"]:
                                arbitration = arbitrate_differences(arbiter, evidence)
                            record.model_decisions.append(arbitration.to_dict())
                            self._persist_model_decision(
                                record.record_id, "ARBITER", arbitration, evidence
                            )
                            if (
                                arbitration.decision == "INCONSISTENT"
                                and arbitration.risk == "HIGH"
                                and arbitration.confidence >= self.config.model_confidence_threshold
                            ):
                                record.status = "AUTO_FAILED"
                                record.workflow_state = "FINALIZED"
                                record.final_decision = "FAIL"
                                record.final_reason = "GLM与DeepSeek独立一致判定为高风险实质差异。"
                                self.db.add_ledger_change_proposal(
                                    record.record_id,
                                    {"action": "人工核查后决定是否更新台账"},
                                    rationale=record.final_reason,
                                )
                                self._store_record_snapshot(record)
                                return
                        else:
                            record.issues.append(Issue(
                                code="ARBITER_NOT_CONFIGURED", severity="review",
                                title="DeepSeek仲裁未配置",
                                detail="高风险差异已转人工复核，未执行自动不通过。",
                            ))
                except Exception as exc:
                    record.issues.append(Issue(
                        code="TEXT_MODEL_FAILED", severity="review", title="文本模型调用失败",
                        detail=self._safe_error(exc),
                    ))
            else:
                record.issues.append(Issue(
                    code="GLM_NOT_CONFIGURED", severity="review", title="GLM主审未配置",
                    detail="规则发现实质差异，因模型未配置已转人工复核。",
                ))

        # Records can require human review without a SUBSTANTIVE_DIFF (for
        # example a missing frozen-ledger identifier, a Qwen chunk conflict or
        # an unqualified/uncertain conclusion).  These unresolved rule blockers
        # still need visible GLM and DeepSeek analysis.  Their opinions are
        # advisory only: they can explain the gap but may not invent missing
        # ledger evidence or turn a blocked record into an automatic pass.
        if not substantive and identifier_evidence is None:
            self._review_unresolved_blockers_with_models(record, comparisons, blockers)

        self._create_or_reuse_review_task(record)

    def _review_unresolved_blockers_with_models(
        self,
        record: ReviewRecord,
        comparisons: list[dict[str, str]],
        blockers: list[str],
    ) -> None:
        evidence = {
            "review_type": "UNRESOLVED_RULE_BLOCKERS",
            "certificate_sha256": record.sha256,
            "blockers": list(blockers),
            "issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "field": issue.field,
                    "title": issue.title,
                    "detail": issue.detail,
                }
                for issue in record.issues
                if issue.severity in {"review", "error"}
            ],
            "differences": [
                item for item in comparisons
                if item.get("result") not in {"CONSISTENT", "FORM_EQUIVALENT"}
            ],
            "instruction": (
                "分析这些复核原因是否能由现有证据消除。关键字段或冻结台账缺失时必须判为"
                "UNCERTAIN，不得猜测、补造字段，也不得建议自动通过。"
            ),
        }
        self._set_state(record, "SEMANTIC_REVIEW")
        for kind, role, reviewer, label in (
            ("glm", "PRIMARY", review_differences, "GLM"),
            ("deepseek", "ARBITER", arbitrate_differences, "DeepSeek"),
        ):
            provider = self.providers[kind]
            if not provider.configured:
                record.issues.append(Issue(
                    code=f"{kind.upper()}_NOT_CONFIGURED",
                    severity="review",
                    title=f"{label}未配置",
                    detail="规则无法自动确定，且文本模型没有可用凭据。",
                ))
                continue
            if kind == "deepseek":
                self._set_state(record, "MODEL_ARBITRATION")
            try:
                with self._provider_limits[kind]:
                    decision = reviewer(provider, evidence)
                record.model_decisions.append(decision.to_dict())
                self._persist_model_decision(record.record_id, role, decision, evidence)
            except Exception as exc:
                record.issues.append(Issue(
                    code=f"{kind.upper()}_UNRESOLVED_REVIEW_FAILED",
                    severity="review",
                    title=f"{label}复核原因分析失败",
                    detail=self._safe_error(exc),
                ))

    @staticmethod
    def _independent_identifier_evidence(
        record: ReviewRecord, comparisons: list[dict[str, str]]
    ) -> dict[str, Any] | None:
        """Build the exact evidence that requires two-model namespace confirmation.

        This route applies only when the document has a sample number, the QR
        code has an issuer verification-record id, and the document does not
        claim that the sample number is a factory serial number.
        """
        qr_fields = (record.metadata.get("qr_fields") or {}) if record.metadata else {}
        sample = record.fields.get("sample_number")
        serial = record.fields.get("serial_number")
        verification_record_id = str(qr_fields.get("verification_record_id") or "").strip()
        if (
            sample is None or not sample.value.strip()
            or verification_record_id == ""
            or (serial is not None and serial.value.strip())
        ):
            return None

        pair = [
            item for item in comparisons
            if item.get("field") in {"sample_number", "verification_record_id"}
        ]
        if not pair or any(item.get("result") != "NOT_COMPARABLE" for item in pair):
            return None

        return {
            "difference_type": "SAMPLE_NUMBER_VS_VERIFICATION_RECORD_ID",
            "certificate_sample_number": sample.value.strip(),
            "certificate_sample_number_evidence": sample.evidence or "",
            "qr_verification_record_id": verification_record_id,
            "differences": pair,
            "comparison_rule": "independent_identifier_namespaces",
            "certificate_sha256": record.sha256,
            "instruction": (
                "仅判断样品编号与二维码验真记录号是否属于无需相等的不同编号体系；"
                "不得推断或补造设备出厂编号。"
            ),
        }

    def _models_confirm_independent_identifiers(
        self, record: ReviewRecord, evidence: dict[str, Any]
    ) -> bool:
        """Require independent GLM and DeepSeek approval for the identifier pair."""
        decisions: list[Any] = []
        model_specs = (
            ("glm", "PRIMARY", review_differences, "GLM"),
            ("deepseek", "ARBITER", arbitrate_differences, "DeepSeek"),
        )
        self._set_state(record, "SEMANTIC_REVIEW")

        for kind, role, reviewer, label in model_specs:
            provider = self.providers[kind]
            if not provider.configured:
                record.issues.append(Issue(
                    code=f"{kind.upper()}_NOT_CONFIGURED",
                    severity="review",
                    title=f"{label}未配置",
                    detail="样品编号与验真记录号的特殊放行必须取得两个文本模型的独立确认。",
                ))
                continue
            if kind == "deepseek":
                self._set_state(record, "MODEL_ARBITRATION")
            try:
                # DeepSeek receives the same original evidence object and never
                # receives GLM's decision or reason.
                with self._provider_limits[kind]:
                    decision = reviewer(provider, evidence)
                decisions.append(decision)
                record.model_decisions.append(decision.to_dict())
                self._persist_model_decision(record.record_id, role, decision, evidence)
            except Exception as exc:
                record.issues.append(Issue(
                    code=f"{kind.upper()}_IDENTIFIER_REVIEW_FAILED",
                    severity="review",
                    title=f"{label}编号关系判断失败",
                    detail=self._safe_error(exc),
                ))

        confirmed = (
            len(decisions) == 2
            and all(
                item.decision == "EQUIVALENT"
                and item.risk != "HIGH"
                and item.confidence >= self.config.model_confidence_threshold
                for item in decisions
            )
        )
        if confirmed:
            record.metadata["independent_identifier_model_confirmed"] = True
            record.metadata["independent_identifier_models"] = [
                {"provider": item.provider, "model": item.model, "confidence": item.confidence}
                for item in decisions
            ]
            return True

        summaries = [
            f"{item.provider}:{item.decision}/{item.risk}/{item.confidence:.2f}"
            for item in decisions
        ]
        record.issues.append(Issue(
            code="IDENTIFIER_MODEL_CONFIRMATION_REQUIRED",
            severity="review",
            title="双模型未共同确认编号体系等价",
            detail=("；".join(summaries) if summaries else "未取得有效的双模型判断"),
        ))
        return False

    def _finalize_auto_pass(self, record: ReviewRecord, reason: str) -> None:
        record.status = "AUTO_PASSED"
        record.workflow_state = "FINALIZED"
        record.final_decision = "PASS"
        record.final_reason = reason
        self.db.add_ledger_change_proposal(
            record.record_id, {}, rationale="审核通过；首版仅生成拟处置记录，不回写平台。",
            status="NO_CHANGE",
        )
        self._store_record_snapshot(record)

    def auto_pass_blockers(
        self, record: ReviewRecord, comparisons: list[dict[str, str]]
    ) -> list[str]:
        """Return fail-closed automatic-pass reasons; an empty list is required."""
        blockers: list[str] = []
        by_field = {item["field"]: item for item in comparisons}
        for field in sorted(self._required_auto_pass_fields(record)):
            value = record.fields.get(field)
            if value is None or not value.value.strip():
                blockers.append(f"missing_document:{field}")
            elif (value.confidence or 0.0) < self.config.auto_pass_confidence:
                blockers.append(f"low_confidence:{field}")
            comparison = by_field.get(field)
            if comparison is None:
                blockers.append(f"missing_comparison:{field}")
            elif comparison["result"] not in {"CONSISTENT", "FORM_EQUIVALENT"}:
                blockers.append(f"comparison_{comparison['result'].lower()}:{field}")

        if record.duplicate_of:
            blockers.append("duplicate_content")
        if not record.qr_codes or not all(
            bool(item.get("stable_multiscale")) for item in record.qr_codes
        ):
            blockers.append("qr_missing_or_unstable")
        elif not any(item.get("parsed_fields") for item in record.qr_codes):
            blockers.append("qr_no_comparable_fields")
        if not self._is_qualified_conclusion(
            record.fields.get("verification_result", FieldValue("", "")).value
        ):
            blockers.append("conclusion_not_qualified")

        # The patent validation route requires complete Qwen evidence. A missing
        # key, an API failure, an uncertain field or a chunk conflict is not an
        # acceptable reason to fall back to the local text layer and auto-pass.
        if self.model_mode not in ACTIVE_MODEL_MODES:
            blockers.append("model_mode_disabled")
        if not record.metadata.get("qwen_used"):
            blockers.append("qwen_not_used")
        if record.metadata.get("qwen_error"):
            blockers.append("qwen_failed")
        if record.metadata.get("qwen_warnings"):
            blockers.append("qwen_warning_or_chunk_conflict")
        # RuleEngine already assigns every anomaly a severity. Automatic pass
        # is allowed only when the remaining issues are informational; keeping
        # a hand-maintained code allowlist risks silently missing a new rule.
        if any(issue.severity in {"review", "error"} for issue in record.issues):
            blockers.append("review_or_error_issue")
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _required_auto_pass_fields(record: ReviewRecord) -> set[str]:
        """Return required identity fields for this certificate type.

        A document sample number and an issuer verification-record id are
        independent internal identifiers.  When the certificate contains no
        factory serial at all, that pair must not manufacture a missing or
        conflicting factory-serial requirement.  Every other automatic-pass
        gate remains unchanged.
        """
        required = set(AUTO_PASS_REQUIRED_FIELDS)
        qr_fields = (record.metadata.get("qr_fields") or {}) if record.metadata else {}
        sample = record.fields.get("sample_number")
        serial = record.fields.get("serial_number")
        if (
            sample is not None and sample.value.strip()
            and not (serial is not None and serial.value.strip())
            and str(qr_fields.get("verification_record_id") or "").strip()
        ):
            required.discard("serial_number")
        return required

    @staticmethod
    def _is_qualified_conclusion(value: str) -> bool:
        normalized = "".join(str(value or "").split()).casefold()
        if not normalized:
            return False
        negative = (
            "不合格", "不符合", "不满足", "不通过", "不确定", "无法判定",
            "不推荐", "不适用", "待复核",
        )
        if any(term in normalized for term in negative):
            return False
        return any(term in normalized for term in ("合格", "符合", "满足"))

    def _persist_model_decision(
        self, certificate_id: str, role: str, decision: Any, evidence: dict[str, Any]
    ) -> None:
        self.db.add_model_decision(
            certificate_id, role, decision.provider, decision.model, decision.decision,
            risk=decision.risk, confidence=decision.confidence, reason=decision.reason,
            evidence=evidence, usage=decision.usage, latency_ms=decision.latency_ms,
            prompt_version=decision.prompt_version,
        )

    def _create_or_reuse_review_task(self, record: ReviewRecord) -> None:
        open_tasks = self.db.list_review_tasks(status="OPEN", certificate_id=record.record_id)
        task = open_tasks[-1] if open_tasks else self.db.create_review_task(
            record.record_id,
            "关键字段、二维码、重复内容或模型结果不满足自动处置门槛。",
        )
        record.review_task_id = str(task["id"])
        record.status = "HUMAN_REVIEW"
        record.workflow_state = "HUMAN_REVIEW"
        record.final_decision = None
        record.final_reason = "需要人工核对原件、二维码、版面字段及冻结台账。"
        self._store_record_snapshot(record)

    def _reuse_cached_result(self, source: ReviewRecord, target: ReviewRecord) -> None:
        target.fields = copy.deepcopy(source.fields)
        target.qr_codes = copy.deepcopy(source.qr_codes)
        target.issues = copy.deepcopy(source.issues)
        target.authenticity = copy.deepcopy(source.authenticity)
        target.metadata.update(copy.deepcopy(source.metadata))
        target.metadata["cache_hit"] = True
        target.metadata["cache_source_record_id"] = source.record_id
        target.model_decisions = []
        run = self.db.create_extraction_run(
            target.record_id, "content_cache", model=source.sha256,
            prompt_version="sha256-cache-v1",
        )
        self.db.finish_extraction_run(run["id"], "SUCCEEDED", latency_ms=0)
        self._persist_fields(target.record_id, target.fields, run["id"])
        qr_fields = self._flatten_qr_fields(target.qr_codes)
        comparisons = self.rules.compare_three_way(target.fields, qr_fields, target.ledger_fields)
        target.metadata["qr_fields"] = qr_fields
        target.metadata["three_way_comparisons"] = comparisons
        target.comparisons = [
            FieldComparison(
                field=item["field"], document_value=item["document_value"],
                qr_value=item["qr_value"] or item["ledger_value"], status=item["result"],
                basis="识别结果按SHA-256缓存复用；与本条冻结台账重新比较",
            )
            for item in comparisons
        ]
        for item in comparisons:
            self.db.add_comparison(
                target.record_id, item["field"], item["result"],
                document_value=item.get("document_value"), qr_value=item.get("qr_value"),
                ledger_value=target.ledger_fields.get(item["field"], item.get("ledger_value")),
                risk=item.get("severity"), basis="sha256-cache-v1",
            )
        target.issues.insert(0, Issue(
            code="DUPLICATE_FILE", severity="review", title="重复附件已复用识别缓存",
            detail=f"与记录 {target.duplicate_of} 内容相同；附件仍作为独立业务记录保留。",
        ))
        self._create_or_reuse_review_task(target)

    def _persist_fields(
        self, certificate_id: str, fields: dict[str, FieldValue], run_id: str
    ) -> None:
        for key, value in fields.items():
            self.db.add_extracted_field(
                certificate_id, key, value=value.value,
                normalized_value=value.normalized_value or normalize_field(key, value.value),
                source=value.source, extraction_run_id=run_id, page=value.page,
                evidence=value.evidence, confidence=value.confidence,
                uncertain=(value.confidence or 0.0) < self.config.auto_pass_confidence,
            )

    @staticmethod
    def _flatten_qr_fields(qr_codes: list[dict[str, Any]]) -> dict[str, str]:
        fields: dict[str, str] = {}
        conflicts: set[str] = set()
        for code in qr_codes:
            for key, raw in (code.get("parsed_fields") or {}).items():
                value = str(raw).strip()
                if key in fields and normalize_field(key, fields[key]) != normalize_field(key, value):
                    conflicts.add(key)
                else:
                    fields.setdefault(key, value)
        for key in conflicts:
            fields.pop(key, None)
        return fields

    def _set_state(self, record: ReviewRecord, state: str) -> None:
        record.workflow_state = state
        record.status = "PROCESSING" if state not in {
            "HUMAN_REVIEW", "AUTO_PASSED", "AUTO_FAILED", "PROCESSING_FAILED", "FINALIZED"
        } else state
        self.db.update_certificate_status(
            record.record_id, state, metadata={"workflow_state": state}
        )

    def _store_record_snapshot(self, record: ReviewRecord) -> None:
        self.db.update_certificate_status(
            record.record_id,
            record.status,
            error=record.error,
            authenticity_status=record.authenticity.status,
            page_count=record.metadata.get("page_count"),
            metadata={
                "review": record.to_dict(),
                "workflow_state": record.workflow_state,
                "final_decision": record.final_decision,
                "final_reason": record.final_reason,
            },
        )

    # ------------------------------------------------------------------
    # Legacy deterministic path kept for the original 11 regression tests.
    # ------------------------------------------------------------------
    def create_batch(self, incoming: list[dict[str, Any]], batch_dir: Path) -> BatchResult:
        if len(incoming) > self.config.max_batch_files:
            raise ValueError(f"单批最多导入 {self.config.max_batch_files} 个文件")
        batch = BatchResult(str(uuid.uuid4()), str(uuid.uuid4()), utc_now())
        paths: dict[str, Path] = {}
        seen_hashes: dict[str, str] = {}
        for item in incoming:
            filename = Path(str(item.get("filename") or "unnamed")).name
            path = item.get("path")
            size = int(item.get("size_bytes") or 0)
            sha256 = str(item.get("sha256") or "")
            suffix = Path(filename).suffix.lower()
            record = ReviewRecord(
                record_id=str(uuid.uuid4()), filename=filename, sha256=sha256,
                size_bytes=size, file_type=suffix.lstrip(".") or "unknown",
            )
            validation_error = item.get("error")
            if validation_error:
                record.status = "FAILED"
                record.error = str(validation_error)
                record.issues.append(Issue("FILE_VALIDATION_FAILED", "error", "文件校验失败", record.error))
            elif suffix not in self.config.allowed_extensions:
                record.status = "FAILED"
                record.error = f"不支持的文件类型：{suffix or '无扩展名'}"
            elif size <= 0:
                record.status = "FAILED"
                record.error = "文件为空"
            elif size > self.config.max_file_size_bytes:
                record.status = "FAILED"
                record.error = f"文件超过 {self.config.max_file_size_mb} MB 限制"
            elif not path or not Path(path).exists():
                record.status = "FAILED"
                record.error = "临时文件不可用"
            else:
                paths[record.record_id] = Path(path)
                if sha256 in seen_hashes:
                    record.duplicate_of = seen_hashes[sha256]
                else:
                    seen_hashes[sha256] = record.record_id
            batch.records.append(record)
        with self._lock:
            self._batches[batch.batch_id] = batch
            self._paths[batch.batch_id] = paths
            self._trim_batches()
        return batch

    def _process_legacy_batch(self, batch_id: str, cleanup_dir: Path | None = None) -> None:
        with self._lock:
            batch = self._batches[batch_id]
            batch.status = "PROCESSING"
            paths = dict(self._paths.get(batch_id, {}))
        try:
            for record in batch.records:
                if record.status == "FAILED":
                    continue
                record.status = "PROCESSING"
                started = time.perf_counter()
                try:
                    self._review_file_legacy(paths[record.record_id], record)
                    if record.duplicate_of:
                        record.issues.insert(0, Issue(
                            "DUPLICATE_FILE", "review", "批次内重复文件",
                            f"文件内容与记录 {record.duplicate_of} 完全相同（SHA-256相同）。",
                        ))
                        record.status = "REVIEW"
                except Exception as exc:
                    record.status = "FAILED"
                    record.error = self._safe_error(exc)
                    record.issues = [Issue("PARSING_FAILED", "error", "解析失败", record.error)]
                finally:
                    record.elapsed_ms = round((time.perf_counter() - started) * 1000)
            batch.status = "COMPLETED"
            batch.completed_at = utc_now()
        finally:
            with self._lock:
                self._paths.pop(batch_id, None)
            if cleanup_dir and cleanup_dir.exists():
                self._cleanup_temp_dir(cleanup_dir)

    def _review_file_legacy(self, path: Path, record: ReviewRecord) -> None:
        suffix = path.suffix.lower()
        with path.open("rb") as stream:
            signature = stream.read(12)
        if suffix == ".pdf":
            if not signature.startswith(b"%PDF-"):
                raise ValueError("扩展名为PDF，但文件头不是有效PDF")
            extraction = self.pdf.extract(path)
        else:
            extraction = self.image.extract(path)
        extracted_fields, candidates = self.fields.extract(extraction.text_pages, extraction.text_sources)
        extraction.fields = extracted_fields
        status, issues, comparisons = self.rules.review(extraction, candidates)
        record.status = status
        record.fields = extraction.fields
        record.qr_codes = extraction.qr_codes
        record.comparisons = comparisons
        record.issues = issues
        record.authenticity = self.authenticity.verify(path)
        record.metadata = extraction.metadata | {
            "text_sources": extraction.text_sources,
            "ocr_provider": self.ocr.name,
            "qwen_used": False,
            "authoritative_verification_used": self.authenticity.authoritative,
        }

    # ------------------------------------------------------------------
    # Queries, review decisions, retry and exports.
    # ------------------------------------------------------------------
    def get_batch(self, batch_id: str) -> BatchResult | None:
        with self._lock:
            existing = self._batches.get(batch_id)
        if existing is not None:
            return existing
        row = self.db.get_batch(batch_id)
        if row is None:
            return None
        batch = BatchResult(
            batch_id=row["id"], request_id=row["request_id"], created_at=row["created_at"],
            status=row["status"], completed_at=row.get("completed_at"),
        )
        paths: dict[str, Path] = {}
        for certificate in self.db.list_certificates(batch_id):
            review = (certificate.get("metadata") or {}).get("review")
            record = self._record_from_dict(review) if isinstance(review, dict) else ReviewRecord(
                certificate["id"], certificate["filename"], certificate["sha256"],
                certificate["size_bytes"], certificate["file_type"], status=certificate["status"],
            )
            batch.records.append(record)
            if certificate.get("storage_path"):
                paths[record.record_id] = Path(certificate["storage_path"])
        with self._lock:
            self._batches[batch_id] = batch
            self._paths[batch_id] = paths
            if (row.get("metadata") or {}).get("advanced"):
                self._advanced_batches.add(batch_id)
        return batch

    @staticmethod
    def _record_from_dict(data: dict[str, Any]) -> ReviewRecord:
        fields = {key: FieldValue(**value) for key, value in (data.get("fields") or {}).items()}
        comparisons = [FieldComparison(**value) for value in data.get("comparisons") or []]
        issues = [Issue(**value) for value in data.get("issues") or []]
        authenticity = AuthenticityResult(**(data.get("authenticity") or {}))
        allowed = {
            key: value for key, value in data.items()
            if key not in {"fields", "comparisons", "issues", "authenticity"}
        }
        return ReviewRecord(
            **allowed, fields=fields, comparisons=comparisons, issues=issues,
            authenticity=authenticity,
        )

    def list_batches(self) -> list[dict[str, Any]]:
        results = []
        for item in self.db.list_batches():
            batch = self.get_batch(item["id"])
            results.append(batch.to_dict() if batch else item)
        return results

    def get_certificate_detail(self, certificate_id: str) -> dict[str, Any] | None:
        raw = self.db.get_certificate_detail(certificate_id)
        if raw is None:
            return None
        certificate_row = raw["certificate"]
        review_data = (certificate_row.get("metadata") or {}).get("review") or {}
        certificate = dict(review_data)
        certificate.update({
            "id": certificate_id,
            "record_id": certificate_id,
            "batch_id": certificate_row["batch_id"],
            "storage_path": None,
            "file_url": f"/api/certificates/{certificate_id}/file",
            "page_count": certificate_row.get("page_count"),
            "status": certificate_row["status"],
            "metadata": certificate_row.get("metadata") or {},
        })
        snapshots = raw.get("ledger_snapshots") or []
        open_tasks = [task for task in raw.get("review_tasks", []) if task.get("status") == "OPEN"]
        fields = review_data.get("fields") or {}
        extraction_runs = raw.get("extraction_runs") or []
        cycle_starts = [
            str(run.get("started_at")) for run in extraction_runs
            if run.get("provider") == "local_pdf_qr" and run.get("started_at")
        ]
        current_cycle_started_at = max(cycle_starts) if cycle_starts else None

        def current_cycle(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not current_cycle_started_at:
                return rows
            return [
                item for item in rows
                if str(item.get("created_at") or "") >= current_cycle_started_at
            ]

        comparison_history = raw.get("comparisons") or []
        model_decision_history = raw.get("model_decisions") or []
        return {
            **raw,
            "certificate": certificate,
            "fields": fields,
            "qr_fields": (review_data.get("metadata") or {}).get("qr_fields", {}),
            "qr_codes": review_data.get("qr_codes") or [],
            "ledger_snapshot": snapshots[-1] if snapshots else {},
            "comparisons": current_cycle(comparison_history) or review_data.get("comparisons") or [],
            "comparison_history": comparison_history,
            "issues": review_data.get("issues") or [],
            "model_decisions": current_cycle(model_decision_history),
            "model_decision_history": model_decision_history,
            "current_cycle_started_at": current_cycle_started_at,
            "review_task": open_tasks[-1] if open_tasks else None,
            "pdf_preview_url": f"/api/certificates/{certificate_id}/file",
            "authenticity_status": "UNVERIFIED",
        }

    def certificate_file(self, certificate_id: str) -> tuple[Path, str]:
        certificate = self.db.get_certificate(certificate_id)
        if not certificate or not certificate.get("storage_path"):
            raise KeyError(certificate_id)
        path = Path(certificate["storage_path"]).resolve(strict=True)
        objects = (self.storage_root / "objects").resolve(strict=True)
        if path != objects and objects not in path.parents:
            raise ValueError("证书存储路径越界")
        return path, certificate["filename"]

    def reserve_certificate_retry(self, certificate_id: str) -> str:
        """Atomically reserve one certificate before scheduling its retry worker."""
        # Preserve the endpoint's existence, readability and storage-boundary
        # checks before accepting work.
        self.certificate_file(certificate_id)
        with self._lock:
            if certificate_id in self._certificate_retry_reservations:
                raise CertificateRetryInProgressError(certificate_id)
            token = str(uuid.uuid4())
            self._certificate_retry_reservations[certificate_id] = (token, "reserved")
            return token

    def cancel_certificate_retry_reservation(self, certificate_id: str, token: str) -> bool:
        """Cancel work that could not be queued, without unlocking a running worker."""
        with self._lock:
            current = self._certificate_retry_reservations.get(certificate_id)
            if current != (token, "reserved"):
                return False
            del self._certificate_retry_reservations[certificate_id]
            return True

    def run_reserved_certificate_retry(
        self,
        certificate_id: str,
        token: str,
    ) -> dict[str, Any]:
        """Run the unique reserved retry and release its reservation in all cases."""
        with self._lock:
            current = self._certificate_retry_reservations.get(certificate_id)
            if current is None or current[0] != token:
                raise CertificateRetryReservationError(certificate_id)
            if current[1] != "reserved":
                raise CertificateRetryInProgressError(certificate_id)
            self._certificate_retry_reservations[certificate_id] = (token, "running")
        try:
            return self._retry_certificate_once(certificate_id)
        finally:
            with self._lock:
                current = self._certificate_retry_reservations.get(certificate_id)
                if current is not None and current[0] == token:
                    del self._certificate_retry_reservations[certificate_id]

    def retry_certificate(self, certificate_id: str) -> dict[str, Any]:
        """Synchronously retry once while sharing the HTTP idempotency guard."""
        token = self.reserve_certificate_retry(certificate_id)
        return self.run_reserved_certificate_retry(certificate_id, token)

    def reserve_batch_retry(self, batch_id: str) -> str:
        """Atomically reserve one controlled full-batch retry."""
        if self.get_batch(batch_id) is None:
            raise KeyError(batch_id)
        with self._lock:
            if batch_id in self._batch_retry_reservations:
                raise BatchRetryInProgressError(batch_id)
            token = str(uuid.uuid4())
            self._batch_retry_reservations[batch_id] = token
            return token

    def cancel_batch_retry_reservation(self, batch_id: str, token: str) -> bool:
        with self._lock:
            if self._batch_retry_reservations.get(batch_id) != token:
                return False
            del self._batch_retry_reservations[batch_id]
            return True

    def run_reserved_batch_retry(self, batch_id: str, token: str) -> None:
        """Re-run a batch with the service's bounded worker pool and SHA cache."""
        with self._lock:
            if self._batch_retry_reservations.get(batch_id) != token:
                raise BatchRetryInProgressError(batch_id)
            batch = self._batches[batch_id]
            for record in batch.records:
                record.status = "QUEUED"
                record.workflow_state = "UPLOADED"
                record.error = None
                record.final_decision = None
                record.final_reason = None
                record.review_task_id = None
                self._store_record_snapshot(record)
        try:
            self._process_advanced_batch(batch_id)
        finally:
            with self._lock:
                if self._batch_retry_reservations.get(batch_id) == token:
                    del self._batch_retry_reservations[batch_id]

    def _retry_certificate_once(self, certificate_id: str) -> dict[str, Any]:
        certificate = self.db.get_certificate(certificate_id)
        if certificate is None or not certificate.get("storage_path"):
            raise KeyError(certificate_id)
        batch = self.get_batch(certificate["batch_id"])
        if batch is None:
            raise KeyError(certificate["batch_id"])
        record = next(item for item in batch.records if item.record_id == certificate_id)
        record.status = "QUEUED"
        record.workflow_state = "UPLOADED"
        record.error = None
        record.final_decision = None
        record.final_reason = None
        record.review_task_id = None
        self._store_record_snapshot(record)
        self._refresh_advanced_batch_status(batch)
        try:
            self._process_advanced_record(Path(certificate["storage_path"]), record)
        finally:
            self._refresh_advanced_batch_status(batch)
        return self.get_certificate_detail(certificate_id) or {}

    def submit_review_decision(
        self,
        task_id: str,
        decision: str,
        *,
        reason: str,
        corrected_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = decision.strip().upper()
        if normalized not in {"PASS", "FAIL", "NEEDS_EVIDENCE"}:
            raise ValueError("人工结论仅支持 PASS、FAIL 或 NEEDS_EVIDENCE")
        task = self.db.get_review_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] != "OPEN":
            raise ValueError("该复核任务已经处理")
        result = self.db.add_review_decision(
            task_id, normalized, comment=reason, corrected_fields=corrected_fields or {},
        )
        certificate_id = task["certificate_id"]
        certificate = self.db.get_certificate(certificate_id) or {}
        batch = self.get_batch(certificate.get("batch_id", ""))
        record = next((item for item in batch.records if item.record_id == certificate_id), None) if batch else None
        if normalized == "NEEDS_EVIDENCE":
            follow_up = self.db.create_review_task(certificate_id, "人工判断证据不足，等待补充证据。")
            self.db.update_certificate_status(
                certificate_id, "HUMAN_REVIEW",
                metadata={"final_decision": normalized, "review_task_id": follow_up["id"]},
            )
            if record:
                record.status = "HUMAN_REVIEW"
                record.workflow_state = "HUMAN_REVIEW"
                record.final_decision = normalized
                record.final_reason = reason
                record.review_task_id = follow_up["id"]
                self._store_record_snapshot(record)
        else:
            if record:
                record.status = "FINALIZED"
                record.workflow_state = "FINALIZED"
                record.final_decision = normalized
                record.final_reason = reason
                record.review_task_id = task_id
                self._store_record_snapshot(record)
            if normalized == "PASS":
                self.db.add_ledger_change_proposal(
                    certificate_id, corrected_fields or {},
                    rationale="人工通过；仅形成拟更新清单，不回写平台。",
                )
        return {"decision": result, "certificate": self.get_certificate_detail(certificate_id)}

    def export_json(self, batch_id: str) -> bytes:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        chain_ok, chain_error = self.db.verify_audit_chain()
        business_ok, business_error = self._verify_business_integrity()
        details = [self._exportable_certificate_detail(item.record_id) for item in batch.records]
        payload = batch.to_dict() | {
            "audit_chain": {"valid": chain_ok, "error": chain_error},
            "business_integrity": {"valid": business_ok, "error": business_error},
            "certificate_details": details,
            "authenticity_status": "UNVERIFIED",
            "ledger_writeback_performed": False,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def export_csv(self, batch_id: str) -> bytes:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow([
            "record_id", "filename", "sha256", "status", "workflow_state", "duplicate_of",
            "certificate_number", "unified_number", "instrument_name", "model",
            "serial_number", "calibration_date", "due_date", "verification_result",
            "ledger_unified_number", "issue_codes", "model_decisions", "review_task_id",
            "final_decision", "review_version", "review_reason", "corrected_fields",
            "authenticity_status", "error",
        ])
        for record in batch.records:
            values = {key: value.value for key, value in record.fields.items()}
            detail = self.db.get_certificate_detail(record.record_id) or {}
            decisions = detail.get("review_decisions") or []
            latest_review = decisions[-1] if decisions else {}
            writer.writerow([self._csv_safe(value) for value in [
                record.record_id, record.filename, record.sha256, record.status,
                record.workflow_state, record.duplicate_of or "",
                values.get("certificate_number", ""), values.get("unified_number", ""),
                values.get("instrument_name", ""), values.get("model", ""),
                values.get("serial_number", ""), values.get("calibration_date", ""),
                values.get("due_date", ""), values.get("verification_result", ""),
                record.ledger_fields.get("unified_number", ""),
                " | ".join(item.code for item in record.issues),
                json.dumps(record.model_decisions, ensure_ascii=False), record.review_task_id or "",
                record.final_decision or "", latest_review.get("version", ""),
                latest_review.get("comment", ""),
                json.dumps(latest_review.get("corrected_fields") or {}, ensure_ascii=False),
                "UNVERIFIED", record.error or "",
            ]])
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    def export_audit_zip(self, batch_id: str) -> bytes:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        chain_ok, chain_error = self.db.verify_audit_chain()
        business_ok, business_error = self._verify_business_integrity()
        certificate_ids = {record.record_id for record in batch.records}
        all_events = self.db.list_audit_events()
        batch_events = [
            item for item in all_events
            if (item["entity_type"] == "batch" and item["entity_id"] == batch_id)
            or (item["entity_type"] == "certificate" and item["entity_id"] in certificate_ids)
            or str((item.get("payload") or {}).get("certificate_id") or "") in certificate_ids
            or str((item.get("payload") or {}).get("batch_id") or "") == batch_id
        ]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("审核结果.json", self.export_json(batch_id))
            archive.writestr("审核结果.csv", self.export_csv(batch_id))
            archive.writestr(
                "批次审计事件.json",
                json.dumps(batch_events, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            archive.writestr(
                "完整审计链.json",
                json.dumps(all_events, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            archive.writestr(
                "结构化证据与人工版本.json",
                json.dumps(
                    [self._exportable_certificate_detail(item.record_id) for item in batch.records],
                    ensure_ascii=False, indent=2,
                ).encode("utf-8"),
            )
            archive.writestr(
                "审计链校验.json",
                json.dumps({
                    "valid": chain_ok,
                    "error": chain_error,
                    "business_data_valid": business_ok,
                    "business_data_error": business_error,
                    "full_chain_event_count": len(all_events),
                    "latest_event_hash": all_events[-1]["event_hash"] if all_events else None,
                    "note": "完整本机审计链和核心业务行已校验；完整链与本批次结构化证据均包含在本包。",
                }, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            for index, record in enumerate(batch.records, 1):
                try:
                    path, _ = self.certificate_file(record.record_id)
                except (KeyError, OSError, ValueError):
                    continue
                safe_name = Path(record.filename.replace("\\", "/")).name
                archive.write(path, f"原始证书/{index:04d}-{safe_name}")
        return buffer.getvalue()

    def _verify_business_integrity(self) -> tuple[bool, str | None]:
        verifier = getattr(self.db, "verify_business_integrity", None)
        if verifier is None:
            return False, "当前数据库模块不支持核心业务行完整性核验"
        return verifier()

    def _exportable_certificate_detail(self, certificate_id: str) -> dict[str, Any]:
        detail = self.db.get_certificate_detail(certificate_id) or {}
        certificate = dict(detail.get("certificate") or {})
        certificate.pop("storage_path", None)
        detail["certificate"] = certificate
        return detail

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return (str(exc).strip() or type(exc).__name__)[:500]

    @staticmethod
    def _cleanup_temp_dir(path: Path) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve()
        if path.is_symlink():
            raise ValueError("refusing linked temporary directory")
        resolved = path.resolve(strict=True)
        allowed = ("certificate-review-upload-", "certificate-review-")
        if resolved.parent != temp_root or not resolved.name.startswith(allowed):
            raise ValueError("refusing temporary directory outside owned namespace")
        shutil.rmtree(resolved)

    @staticmethod
    def _csv_safe(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    def _trim_batches(self) -> None:
        while len(self._batches) > self.config.retain_batches:
            batch_id, _ = self._batches.popitem(last=False)
            self._paths.pop(batch_id, None)


def prepare_local_files(paths: list[Path]) -> tuple[list[dict[str, Any]], Path]:
    """Create legacy service input without altering source files."""
    temp_dir = Path(tempfile.mkdtemp(prefix="certificate-review-"))
    incoming: list[dict[str, Any]] = []
    for index, source in enumerate(paths):
        if not source.exists() or not source.is_file():
            incoming.append({
                "filename": source.name, "path": None, "size_bytes": 0, "sha256": "",
                "error": "文件不存在或不是普通文件",
            })
            continue
        target = temp_dir / f"{index:04d}{source.suffix.lower()}"
        shutil.copy2(source, target)
        incoming.append({
            "filename": source.name, "path": target, "size_bytes": target.stat().st_size,
            "sha256": sha256_file(target),
        })
    return incoming, temp_dir
