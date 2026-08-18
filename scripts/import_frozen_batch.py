from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.ingest import ingest_sources  # noqa: E402
from app.service import BatchReviewService  # noqa: E402


INGEST_EXPECTATION_KEYS = (
    "records",
    "unique_objects",
    "duplicates",
    "pages",
    "bytes",
)
ANALYSIS_EXPECTATION_KEYS = (
    "digital",
    "scan_or_no_text",
    "qr_stable",
    "qr_low_confidence",
    "qr_missing_or_undecoded",
)
EXPECTATION_KEYS = INGEST_EXPECTATION_KEYS + ANALYSIS_EXPECTATION_KEYS


def load_expectations(
    path: Path | None,
    overrides: dict[str, int | None] | None = None,
) -> dict[str, int]:
    """Load an explicitly supplied, local-only validation contract."""

    raw: dict[str, Any] = {}
    if path is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取期望门禁文件：{path}") from exc
        if not isinstance(payload, dict):
            raise ValueError("期望门禁文件必须是 JSON 对象")
        if "expected" in payload:
            payload = payload["expected"]
            if not isinstance(payload, dict):
                raise ValueError("期望门禁文件的 expected 必须是 JSON 对象")
        raw.update(payload)

    for key, value in (overrides or {}).items():
        if value is not None:
            raw[key] = value

    missing = [key for key in EXPECTATION_KEYS if key not in raw]
    if missing:
        raise ValueError(
            "缺少期望门禁：" + ", ".join(missing)
            + "；请使用 --expectations 或逐项 --expect-* 显式提供"
        )

    expected: dict[str, int] = {}
    for key in EXPECTATION_KEYS:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"期望门禁 {key} 必须是非负整数")
        expected[key] = value

    if expected["records"] <= 0:
        raise ValueError("期望门禁 records 必须大于零")
    if expected["unique_objects"] + expected["duplicates"] != expected["records"]:
        raise ValueError("期望门禁不一致：unique_objects + duplicates 必须等于 records")
    if expected["digital"] + expected["scan_or_no_text"] != expected["records"]:
        raise ValueError("期望门禁不一致：文字层分类合计必须等于 records")
    if (
        expected["qr_stable"]
        + expected["qr_low_confidence"]
        + expected["qr_missing_or_undecoded"]
        != expected["records"]
    ):
        raise ValueError("期望门禁不一致：二维码分类合计必须等于 records")
    return expected


def _expected_ingest(expected: dict[str, int]) -> dict[str, int]:
    return {key: expected[key] for key in INGEST_EXPECTATION_KEYS}


def _assert_ingest(actual: dict[str, int], expected: dict[str, int]) -> None:
    expected_ingest = _expected_ingest(expected)
    if actual != expected_ingest:
        raise RuntimeError(
            f"冻结语料门禁失败：expected={expected_ingest}, actual={actual}"
        )


def _write_report(report: dict[str, Any]) -> dict[str, Any]:
    output_dir = ROOT / "data" / "validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"frozen-validation-{report['batch_id']}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(output)
    return report


def _build_report(
    service: BatchReviewService,
    batch: Any,
    *,
    mode: str,
    source: dict[str, Any],
    expected: dict[str, int],
    require_no_external_calls: bool,
) -> dict[str, Any]:
    stored = service.db.list_certificates(batch.batch_id)
    actual = {
        "records": len(stored),
        "unique_objects": len({item["sha256"] for item in stored}),
        "duplicates": sum(1 for item in stored if item.get("duplicate_of")),
        "pages": sum(int(item.get("page_count") or 0) for item in stored),
        "bytes": sum(int(item["size_bytes"]) for item in stored),
    }
    _assert_ingest(actual, expected)

    text_layer: Counter[str] = Counter()
    qr_state: Counter[str] = Counter()
    extraction_runs: list[dict[str, Any]] = []
    model_decisions: list[dict[str, Any]] = []
    cache_hits = 0
    for record in batch.records:
        text_layer[
            "digital" if record.metadata.get("pdf_has_text_layer") else "scan_or_no_text"
        ] += 1
        if not record.qr_codes:
            qr_state["missing_or_undecoded"] += 1
        elif all(item.get("stable_multiscale") for item in record.qr_codes):
            qr_state["stable"] += 1
        else:
            qr_state["low_confidence"] += 1
        cache_hits += int(bool(record.metadata.get("cache_hit")))
        detail = service.db.get_certificate_detail(record.record_id) or {}
        extraction_runs.extend(detail.get("extraction_runs") or [])
        model_decisions.extend(detail.get("model_decisions") or [])

    analysis = {
        "digital": text_layer["digital"],
        "scan_or_no_text": text_layer["scan_or_no_text"],
        "qr_stable": qr_state["stable"],
        "qr_low_confidence": qr_state["low_confidence"],
        "qr_missing_or_undecoded": qr_state["missing_or_undecoded"],
    }
    expected_analysis = {key: expected[key] for key in analysis}
    if analysis != expected_analysis:
        raise RuntimeError(
            f"本地处理分层门禁失败：expected={expected_analysis}, actual={analysis}"
        )
    if cache_hits != expected["duplicates"]:
        raise RuntimeError(f"重复内容缓存命中异常：{cache_hits}/{expected['duplicates']}")
    if batch.summary()["total"] != expected["records"] or batch.summary()["processing"]:
        raise RuntimeError("批次统计不完整或仍有处理中记录")

    external_runs = [
        item for item in extraction_runs
        if item.get("provider") in {"qwen", "glm", "deepseek"}
    ]
    if require_no_external_calls:
        if external_runs or model_decisions:
            raise RuntimeError("离线验证期间出现外部模型运行或决定记录")
        if batch.summary()["review"] != expected["records"] or batch.summary()["failed"]:
            raise RuntimeError("离线批次出现静默失败或误自动放行")

    chain_valid, chain_error = service.db.verify_audit_chain()
    business_valid, business_error = service.db.verify_business_integrity()
    if not chain_valid or not business_valid:
        raise RuntimeError(
            f"完整性门禁失败：audit={chain_error}, business={business_error}"
        )
    report = {
        "schema_version": "1.0",
        "batch_id": batch.batch_id,
        "mode": mode,
        "source": source | {"expected": expected},
        "ingest": actual,
        "summary": batch.summary(),
        "status_counts": dict(Counter(item.status for item in batch.records)),
        "text_layer": dict(text_layer),
        "qr_state": dict(qr_state),
        "cache_hits": cache_hits,
        "model_runs": dict(Counter(item.get("provider") for item in extraction_runs)),
        "model_decisions": len(model_decisions),
        "audit_chain": {"valid": chain_valid, "error": chain_error},
        "business_integrity": {"valid": business_valid, "error": business_error},
        "authenticity_status": "UNVERIFIED",
        "ledger_writeback_performed": False,
    }
    return _write_report(report)


def run(
    corpus: Path,
    expected: dict[str, int],
    *,
    enable_models: bool = False,
    batch_name: str = "本地冻结语料验证",
) -> dict[str, Any]:
    archive_candidates = sorted(corpus.glob("*.zip"))
    manifest_candidates = sorted(corpus.glob("*.json"))
    if len(archive_candidates) != 1 or len(manifest_candidates) != 1:
        raise RuntimeError("冻结语料目录必须恰好包含一个原始ZIP和一个JSON清单")

    service = BatchReviewService(load_config(ROOT / "config" / "app.json"), ROOT)
    if not enable_models:
        service.model_mode = "disabled"
    elif not all(provider.configured for provider in service.providers.values()):
        service.close()
        raise RuntimeError("--enable-models 要求千问、GLM和DeepSeek三枚新凭据全部配置")
    try:
        ingest = ingest_sources(
            [archive_candidates[0]],
            service.storage_root,
            ledger_sources=[manifest_candidates[0]],
        )
        ingest_actual = {
            "records": len(ingest.records),
            "unique_objects": ingest.unique_objects,
            "duplicates": ingest.duplicates,
            "pages": sum(item.page_count or 0 for item in ingest.records),
            "bytes": ingest.total_bytes,
        }
        _assert_ingest(ingest_actual, expected)
        batch = service.create_ingested_batch(
            ingest,
            name=batch_name + ("（模型联调）" if enable_models else "（离线）"),
        )
        service.db.append_audit_event(
            "batch",
            batch.batch_id,
            "FROZEN_VALIDATION_STARTED",
            {"expected": expected, "model_calls_enabled": enable_models},
        )
        service.process_batch(batch.batch_id)
        return _build_report(
            service,
            batch,
            mode="model_validation" if enable_models else "offline_no_external_calls",
            source={
                "archive": archive_candidates[0].name,
                "manifest": manifest_candidates[0].name,
            },
            expected=expected,
            require_no_external_calls=not enable_models,
        )
    finally:
        service.close()


def validate_existing(batch_id: str, expected: dict[str, int]) -> dict[str, Any]:
    """Revalidate a completed persisted batch without reprocessing or model calls."""
    service = BatchReviewService(load_config(ROOT / "config" / "app.json"), ROOT)
    service.model_mode = "disabled"
    try:
        batch = service.get_batch(batch_id)
        if batch is None:
            raise RuntimeError(f"数据库中不存在批次：{batch_id}")
        if batch.status != "COMPLETED":
            raise RuntimeError(f"批次尚未完成：{batch.status}")
        return _build_report(
            service,
            batch,
            mode="offline_no_external_calls_persisted_revalidation",
            source={"database_batch": batch_id},
            expected=expected,
            require_no_external_calls=True,
        )
    finally:
        service.close()


def _add_expectation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expectations",
        type=Path,
        help="受 Git 忽略的本地 JSON 门禁文件；也可逐项使用 --expect-*",
    )
    for key in EXPECTATION_KEYS:
        parser.add_argument(
            f"--expect-{key.replace('_', '-')}",
            dest=f"expect_{key}",
            type=int,
            help=f"显式期望值：{key}",
        )


def _expectation_overrides(args: argparse.Namespace) -> dict[str, int | None]:
    return {key: getattr(args, f"expect_{key}") for key in EXPECTATION_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser(description="导入并验证显式指定的冻结外检证书语料")
    parser.add_argument(
        "--corpus",
        type=Path,
        help="私有冻结语料目录；新导入时必须显式提供",
    )
    parser.add_argument(
        "--batch-name",
        help="新批次显示名称；默认使用不含现场数量的通用名称",
    )
    parser.add_argument(
        "--enable-models",
        action="store_true",
        help="显式允许使用已配置的新凭据调用模型；默认完全离线",
    )
    parser.add_argument("--existing-batch", help="只复核已完成批次，不重新处理证书")
    _add_expectation_arguments(parser)
    args = parser.parse_args()

    if args.existing_batch and (args.corpus or args.enable_models or args.batch_name):
        parser.error("--existing-batch 不能与 --corpus/--enable-models/--batch-name 同时使用")
    if not args.existing_batch and args.corpus is None:
        parser.error("新导入必须显式提供 --corpus；脚本不会搜索默认真实语料")
    if args.batch_name is not None and not args.batch_name.strip():
        parser.error("--batch-name 不能为空")

    try:
        expected = load_expectations(
            args.expectations,
            _expectation_overrides(args),
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.existing_batch:
        report = validate_existing(args.existing_batch, expected)
    else:
        report = run(
            args.corpus.resolve(),
            expected,
            enable_models=args.enable_models,
            batch_name=args.batch_name or "本地冻结语料验证",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
