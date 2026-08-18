from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import AppConfig  # noqa: E402
from app.models import FieldValue, ReviewRecord  # noqa: E402
from app.service import AUTO_PASS_REQUIRED_FIELDS, BatchReviewService  # noqa: E402


FIELD_MAP = {
    "certificate_number": "certificate_number",
    "factory_serial_number": "serial_number",
    "calibrate_or_verify_date": "calibration_date",
    "equipment_name": "instrument_name",
    "model": "model",
    "record_valid_until": "due_date",
}


def validate(fixtures: dict[str, Any]) -> dict[str, Any]:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    results: list[dict[str, Any]] = []
    try:
        baseline = {
            "certificate_number": "BASE-CERT-001",
            "issuer_name": "示例计量机构",
            "instrument_name": "示例计量器具",
            "unified_number": "EQ-001",
            "model": "MODEL-001",
            "serial_number": "SN-001",
            "calibration_date": "2026-01-01",
            "due_date": "2027-01-01",
            "verification_result": "合格",
        }
        for fixture in fixtures.get("fixtures", []):
            field = FIELD_MAP[str(fixture["target_field"])]
            original = str(fixture["original_value"])
            if original == "__MISSING__":
                original = baseline[field]
            document = dict(baseline)
            qr = dict(baseline)
            ledger = dict(baseline)
            document[field] = str(fixture["anomalous_value"])
            qr[field] = original
            ledger[field] = original
            fields = {
                key: FieldValue(value=value, normalized_value=value, source="qwen_vision",
                                confidence=0.99, page=1, evidence=value)
                for key, value in document.items()
            }
            record = ReviewRecord(
                record_id=str(fixture["fixture_id"]), filename="synthetic-evidence.pdf",
                sha256=str(fixture["source_sha256"]), size_bytes=1, file_type="pdf",
                fields=fields,
                qr_codes=[{"stable_multiscale": True, "payload_sha256": "0" * 64}],
                ledger_fields=ledger,
                metadata={"qwen_used": True, "qwen_warnings": []},
            )
            comparisons = service.rules.compare_three_way(fields, qr, ledger)
            target = next(item for item in comparisons if item["field"] == field)
            blockers = service.auto_pass_blockers(record, comparisons)
            passed = target["result"] == "SUBSTANTIVE_DIFF" and bool(blockers)
            results.append({
                "fixture_id": fixture["fixture_id"],
                "anomaly_type": fixture["anomaly_type"],
                "canonical_field": field,
                "comparison_result": target["result"],
                "risk": target["severity"],
                "auto_pass_eligible": not blockers,
                "blockers": blockers,
                "passed": passed,
            })
    finally:
        service.close()
    passed_count = sum(1 for item in results if item["passed"])
    return {
        "schema_version": "1.0",
        "fixture_count": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "zero_false_release": passed_count == len(results) == 12,
        "note": "非破坏性结构化异常门禁；不修改原始PDF，不调用外部模型。",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="验证12个关键字段异常绝不满足自动通过门槛")
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate(json.loads(args.fixtures.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("fixture_count", "passed", "failed", "zero_false_release")}, ensure_ascii=False))
    return 0 if result["zero_false_release"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
