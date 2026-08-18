from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingest import FrozenLedgerRow, parse_ledger_manifest  # noqa: E402


ANOMALY_TYPES = (
    "certificate_number_mismatch",
    "factory_serial_number_mismatch",
    "date_mismatch",
    "instrument_name_mismatch",
    "model_mismatch",
    "validity_mismatch",
)


def generate_anomaly_fixtures(
    rows: Iterable[FrozenLedgerRow | dict[str, Any]],
) -> dict[str, Any]:
    """Build 12 structural mismatches; source rows and PDF bytes are never changed."""

    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = _row_values(row)
        sha = str(values.get("sha256") or "").lower()
        if len(sha) != 64:
            raise ValueError("every anomaly candidate must have a valid SHA-256")
        candidates.setdefault(sha, values)
    ordered = sorted(candidates.items())
    if len(ordered) < 12:
        raise ValueError("at least 12 unique certificate contents are required")

    fixtures: list[dict[str, Any]] = []
    definitions = [anomaly for anomaly in ANOMALY_TYPES for _ in range(2)]
    for index, (anomaly, (sha, values)) in enumerate(
        zip(definitions, ordered[: len(definitions)], strict=True), 1
    ):
        field, original = _source_value(anomaly, values)
        anomalous = _mutate_value(anomaly, original, index)
        fixtures.append(
            {
                "fixture_id": f"anomaly-{index:02d}",
                "anomaly_type": anomaly,
                "source_sha256": sha,
                "source_report_id": str(values.get("report_id") or ""),
                "source_unified_number": str(values.get("unified_number") or ""),
                "target_field": field,
                "original_value": original,
                "anomalous_value": anomalous,
                "comparison_input": {
                    "document_value": anomalous,
                    "ledger_value": original,
                },
                "expected": {
                    "must_not_auto_pass": True,
                    "acceptable_statuses": ["AUTO_FAILED", "HUMAN_REVIEW"],
                    "risk": "high",
                },
                "source_pdf_modified": False,
            }
        )
    counts = Counter(fixture["anomaly_type"] for fixture in fixtures)
    return {
        "schema_version": "1.0",
        "description": "非破坏性结构化异常输入；不生成或修改任何PDF",
        "count": len(fixtures),
        "counts": dict(sorted(counts.items())),
        "fixtures": fixtures,
    }


def _row_values(row: FrozenLedgerRow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, FrozenLedgerRow):
        return dict(row.values)
    if "values" in row and isinstance(row["values"], dict):
        return dict(row["values"])
    return dict(row)


def _source_value(anomaly: str, values: dict[str, Any]) -> tuple[str, str]:
    choices = {
        "certificate_number_mismatch": ("certificate_number", ("certificate_number", "display_name", "report_id")),
        "factory_serial_number_mismatch": ("factory_serial_number", ("factory_serial_number",)),
        "date_mismatch": ("calibrate_or_verify_date", ("calibrate_or_verify_date",)),
        "instrument_name_mismatch": ("equipment_name", ("equipment_name",)),
        "model_mismatch": ("model", ("model", "equipment_model")),
        "validity_mismatch": ("record_valid_until", ("record_valid_until", "equipment_valid_until")),
    }
    target, keys = choices[anomaly]
    for key in keys:
        value = values.get(key)
        if value not in (None, ""):
            return target, str(value)
    # Missing model/field is itself explicit evidence. The fixture injects a
    # value so rules must reject automatic pass instead of silently filling it.
    return target, "__MISSING__"


def _mutate_value(anomaly: str, original: str, index: int) -> str:
    if anomaly in {"date_mismatch", "validity_mismatch"}:
        try:
            parsed = date.fromisoformat(original)
        except ValueError:
            return f"2099-12-{index:02d}"
        return (parsed + timedelta(days=index)).isoformat()
    if anomaly == "instrument_name_mismatch":
        return f"异常器具名称-{index:02d}"
    if anomaly == "model_mismatch":
        return f"异常型号-M{index:02d}"
    if original == "__MISSING__":
        return f"异常字段值-{index:02d}"
    return f"{original}-MISMATCH-{index:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成12个非破坏性关键字段异常夹具")
    parser.add_argument("manifest", type=Path, help="下载与核验清单 JSON/CSV")
    parser.add_argument("--output", type=Path, required=True, help="输出JSON路径")
    args = parser.parse_args()

    rows = parse_ledger_manifest(args.manifest)
    result = generate_anomaly_fixtures(rows)
    result["source_manifest_sha256"] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "count": result["count"], "counts": result["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
