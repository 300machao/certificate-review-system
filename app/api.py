from __future__ import annotations

import shutil
import tempfile
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import load_config
from app.ingest import (
    FrozenLedgerRow,
    IngestError,
    IngestResult,
    IngestedFile,
    ingest_sources,
    parse_ledger_manifest,
)
from app.model_settings import ModelSettingsError
from app.providers import ProviderError
from app.service import (
    BatchReviewService,
    BatchRetryInProgressError,
    CertificateRetryInProgressError,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
service = BatchReviewService(load_config(ROOT / "config" / "app.json"), ROOT)

app = FastAPI(
    title="外检计量证书智能审核系统",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
)


class ReviewDecisionRequest(BaseModel):
    decision: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=2000)
    corrected_fields: dict[str, Any] = Field(default_factory=dict)


class ProviderSettingsRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096, exclude=True)
    clear_api_key: bool = False


class ModelSettingsRequest(BaseModel):
    qwen: ProviderSettingsRequest | None = None
    glm: ProviderSettingsRequest | None = None
    deepseek: ProviderSettingsRequest | None = None


class ModelConnectionTestRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096, exclude=True)


def _require_same_origin_action(request: Request, action: str) -> None:
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin != expected or request.headers.get("x-certificate-review-action") != action:
        raise HTTPException(status_code=403, detail="该操作必须由本机网站显式发起")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/certificates/") and request.url.path.endswith("/file"):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    else:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
            "frame-ancestors 'none'"
        )
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/capabilities")
def capabilities() -> dict[str, Any]:
    return service.capabilities()


@app.get("/api/models/health")
def model_health() -> dict[str, Any]:
    """Return configuration state without making any external request."""
    return service.model_health(live=False)


@app.post("/api/models/health/check")
def model_health_live(request: Request) -> dict[str, Any]:
    """Explicit, same-origin live check; GET requests can never spend model quota."""
    _require_same_origin_action(request, "model-health-check")
    return service.model_health(live=True)


@app.get("/api/model-settings")
def get_model_settings() -> dict[str, Any]:
    """Return only non-secret model settings and key-presence metadata."""
    try:
        return service.model_settings()
    except ModelSettingsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/model-settings")
def save_model_settings(
    payload: ModelSettingsRequest,
    request: Request,
) -> dict[str, Any]:
    _require_same_origin_action(request, "model-settings-save")
    updates: dict[str, dict[str, Any]] = {}
    for kind in ("qwen", "glm", "deepseek"):
        value = getattr(payload, kind)
        if value is not None:
            updates[kind] = {
                "base_url": value.base_url,
                "model": value.model,
                "clear_api_key": value.clear_api_key,
                "api_key": value.api_key,
            }
    if not updates:
        raise HTTPException(status_code=400, detail="至少提交一项模型配置")
    try:
        return service.save_model_settings(updates)
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/model-settings/test")
def test_model_settings(
    payload: ModelConnectionTestRequest,
    request: Request,
) -> dict[str, Any]:
    _require_same_origin_action(request, "model-settings-test")
    candidate = {
        "base_url": payload.base_url,
        "model": payload.model,
        "api_key": payload.api_key,
    }
    try:
        return service.test_model_settings(payload.provider, candidate)
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _receive_upload(
    upload: UploadFile,
    target: Path,
    *,
    limit_bytes: int,
) -> tuple[int, str | None]:
    size = 0
    error: str | None = None
    try:
        with target.open("wb") as stream:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit_bytes:
                    error = f"上传文件超过 {limit_bytes // (1024 * 1024)} MB 限制"
                    break
                stream.write(chunk)
    except OSError as exc:
        error = f"接收上传文件失败：{type(exc).__name__}"
    finally:
        await upload.close()
    if error and target.exists():
        target.unlink()
    return size, error


def _merge_individual_ingest(
    paths: list[Path],
    ledger_path: Path | None,
    upload_failures: list[dict[str, Any]],
) -> tuple[IngestResult, list[dict[str, Any]]]:
    """Isolate direct-file failures while preserving content-addressed storage."""
    records: list[IngestedFile] = []
    failures = list(upload_failures)
    first_by_sha: dict[str, str] = {}
    total_bytes = 0
    for path in paths:
        try:
            partial = ingest_sources([path], service.storage_root)
        except IngestError as exc:
            failures.append({
                "filename": path.name,
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "code": exc.code,
                "error": exc.message,
            })
            continue
        record = partial.records[0]
        record.duplicate_of = first_by_sha.get(record.sha256)
        if record.duplicate_of is None:
            first_by_sha[record.sha256] = record.record_id
        records.append(record)
        total_bytes += record.size_bytes
    ledger_rows: list[FrozenLedgerRow] = []
    if ledger_path:
        ledger_rows = parse_ledger_manifest(ledger_path)
        by_basename: dict[str, list[IngestedFile]] = defaultdict(list)
        for record in records:
            key = unicodedata.normalize("NFKC", Path(record.original_name).name).casefold()
            by_basename[key].append(record)
        for row in ledger_rows:
            listed_sha = str(row.values.get("sha256") or "").lower()
            listed_path = row.values.get("local_path") or row.values.get("archive_path")
            if not listed_sha or not listed_path:
                continue
            basename = unicodedata.normalize(
                "NFKC", Path(str(listed_path).replace("\\", "/")).name
            ).casefold()
            matches = by_basename.get(basename, [])
            if matches and all(item.sha256 != listed_sha for item in matches):
                raise IngestError(
                    "MANIFEST_SHA_MISMATCH",
                    f"台账行 {row.row_id} 的SHA-256与同名导入文件不一致",
                    row.source,
                )
    return IngestResult(
        records=records,
        unique_objects=len(first_by_sha),
        duplicates=len(records) - len(first_by_sha),
        total_bytes=total_bytes,
        ledger_rows=ledger_rows,
    ), failures


@app.post("/api/batches", status_code=202)
async def create_batch(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    ledger: UploadFile | None = File(None),
    name: str | None = Form(None),
) -> JSONResponse:
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个PDF或ZIP文件")
    if len(files) > service.config.max_batch_files:
        raise HTTPException(
            status_code=400,
            detail=f"浏览器一次最多选择 {service.config.max_batch_files} 个附件",
        )
    batch_dir = Path(tempfile.mkdtemp(prefix="certificate-review-upload-"))
    paths: list[Path] = []
    upload_failures: list[dict[str, Any]] = []
    ledger_path: Path | None = None
    try:
        for index, upload in enumerate(files):
            filename = Path(upload.filename or f"unnamed-{index}").name
            suffix = Path(filename).suffix.lower()
            if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".zip"}:
                await upload.close()
                upload_failures.append({
                    "filename": filename, "code": "UNSUPPORTED_FILE_TYPE",
                    "error": f"不支持的文件类型：{suffix or '无扩展名'}",
                })
                continue
            item_dir = batch_dir / f"{index:04d}"
            item_dir.mkdir()
            target = item_dir / filename
            limit = 512 * 1024 * 1024 if suffix == ".zip" else 200 * 1024 * 1024
            size, error = await _receive_upload(upload, target, limit_bytes=limit)
            if error:
                upload_failures.append({
                    "filename": filename, "size_bytes": size,
                    "code": "UPLOAD_FAILED", "error": error,
                })
            else:
                # The per-item directory prevents collisions while keeping the original basename.
                paths.append(target)

        if ledger is not None:
            ledger_name = Path(ledger.filename or "ledger.json").name
            ledger_suffix = Path(ledger_name).suffix.lower()
            if ledger_suffix not in {".json", ".csv"}:
                await ledger.close()
                raise HTTPException(status_code=400, detail="台账仅支持JSON或CSV")
            ledger_path = batch_dir / f"ledger{ledger_suffix}"
            _, error = await _receive_upload(
                ledger, ledger_path, limit_bytes=16 * 1024 * 1024
            )
            if error:
                raise HTTPException(status_code=400, detail=error)

        if not paths and upload_failures:
            ingest = IngestResult()
            failures = upload_failures
        else:
            try:
                ingest = ingest_sources(
                    paths, service.storage_root,
                    ledger_sources=[ledger_path] if ledger_path else (),
                )
                failures = upload_failures
            except IngestError as exc:
                if any(path.suffix.lower() == ".zip" for path in paths):
                    raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
                ingest, failures = _merge_individual_ingest(paths, ledger_path, upload_failures)
        batch = service.create_ingested_batch(ingest, name=name, failed_inputs=failures)
    except HTTPException:
        raise
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)

    background_tasks.add_task(service.process_batch, batch.batch_id)
    return JSONResponse(batch.to_dict(), status_code=202)


@app.get("/api/batches")
def list_batches() -> list[dict[str, Any]]:
    return service.list_batches()


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch.to_dict()


@app.get("/api/certificates/{certificate_id}")
def get_certificate(certificate_id: str) -> dict[str, Any]:
    detail = service.get_certificate_detail(certificate_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="证书记录不存在")
    return detail


@app.get("/api/certificates/{certificate_id}/file")
def certificate_file(certificate_id: str) -> FileResponse:
    try:
        path, original_name = service.certificate_file(certificate_id)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="证书原件不存在") from None
    except ValueError:
        raise HTTPException(status_code=403, detail="证书原件路径无效") from None
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else None
    encoded = quote(Path(original_name).name)
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename=certificate{path.suffix}; filename*=UTF-8''{encoded}",
        },
    )


@app.post("/api/certificates/{certificate_id}/retry", status_code=202)
def retry_certificate(
    certificate_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    try:
        reservation_token = service.reserve_certificate_retry(certificate_id)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="证书记录或原件不存在") from None
    except ValueError:
        raise HTTPException(status_code=403, detail="证书原件路径无效") from None
    except CertificateRetryInProgressError:
        raise HTTPException(
            status_code=409,
            detail="该证书已有重试任务在运行，请勿重复提交",
        ) from None
    try:
        background_tasks.add_task(
            service.run_reserved_certificate_retry,
            certificate_id,
            reservation_token,
        )
    except Exception:
        service.cancel_certificate_retry_reservation(certificate_id, reservation_token)
        raise
    return {
        "accepted": True,
        "certificate_id": certificate_id,
        "status": "RETRY_QUEUED",
    }


@app.post("/api/batches/{batch_id}/retry", status_code=202)
def retry_batch(batch_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        reservation_token = service.reserve_batch_retry(batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="批次不存在") from None
    except BatchRetryInProgressError:
        raise HTTPException(status_code=409, detail="该批次已有整批重跑任务") from None
    try:
        background_tasks.add_task(
            service.run_reserved_batch_retry,
            batch_id,
            reservation_token,
        )
    except Exception:
        service.cancel_batch_retry_reservation(batch_id, reservation_token)
        raise
    return {"accepted": True, "batch_id": batch_id, "status": "RETRY_QUEUED"}


@app.get("/api/review-tasks")
def review_tasks(status: str | None = None) -> list[dict[str, Any]]:
    return service.db.list_review_tasks(status=status)


@app.post("/api/review-tasks/{task_id}/decision")
def submit_review(task_id: str, request: ReviewDecisionRequest) -> dict[str, Any]:
    try:
        return service.submit_review_decision(
            task_id,
            request.decision,
            reason=request.reason,
            corrected_fields=request.corrected_fields,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="复核任务不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/batches/{batch_id}/export.json")
def export_json(batch_id: str) -> Response:
    try:
        payload = service.export_json(batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="批次不存在") from None
    return Response(
        payload,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="review-{batch_id}.json"'},
    )


@app.get("/api/batches/{batch_id}/export.csv")
def export_csv(batch_id: str) -> Response:
    try:
        payload = service.export_csv(batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="批次不存在") from None
    return Response(
        payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="review-{batch_id}.csv"'},
    )


@app.get("/api/batches/{batch_id}/audit.zip")
def export_audit(batch_id: str) -> Response:
    try:
        payload = service.export_audit_zip(batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="批次不存在") from None
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="audit-{batch_id}.zip"'},
    )


app.mount("/static", StaticFiles(directory=STATIC), name="static")
