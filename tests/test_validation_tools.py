from __future__ import annotations

import copy
from collections import Counter

from scripts.generate_anomaly_fixtures import ANOMALY_TYPES, generate_anomaly_fixtures
from scripts.select_gold_set import QUOTAS, select_gold_set


def sha(index: int) -> str:
    return f"{index:064x}"


def gold_candidates() -> list[dict]:
    rows: list[dict] = []
    for index in range(1, 5):
        rows.append(
            {
                "sha256": sha(index),
                "unified_number": f"SCAN-{index}",
                "display_name": f"外部扫描-{index}",
                "calibrate_or_verify_location": f"外部机构-{index}",
                "is_scanned": True,
                "rotated": index == 1,
                "qr_status": "failed",
            }
        )
    for index in range(5, 7):
        rows.append(
            {
                "sha256": sha(index),
                "unified_number": f"TEST-{index}",
                "display_name": f"测试字第{index}号",
                "calibrate_or_verify_location": "主机构",
                "certificate_kind": "test",
            }
        )
    for index in range(7, 14):
        rows.append(
            {
                "sha256": sha(index),
                "unified_number": f"CAL-{index}",
                "display_name": f"校准字第{index}号",
                "calibrate_or_verify_location": "主机构",
                "certificate_kind": "calibration",
            }
        )
    for index in range(14, 21):
        rows.append(
            {
                "sha256": sha(index),
                "unified_number": f"VER-{index}",
                "display_name": f"检定字第{index}号",
                "calibrate_or_verify_location": "主机构",
                "certificate_kind": "verification",
            }
        )
    # Same bytes, second business row: it must not consume a gold-set slot.
    rows.append(dict(rows[-1], report_id="duplicate-business-row"))
    return rows


def test_gold_set_is_deterministic_and_meets_locked_quotas() -> None:
    rows = gold_candidates()
    first = select_gold_set(rows)
    second = select_gold_set(list(reversed(rows)))

    assert first == second
    assert first["count"] == 20
    assert first["category_counts"] == dict(sorted(QUOTAS.items()))
    assert first["coverage"]["different_scan_institutions"] == 4
    assert first["coverage"]["rotated"] >= 1
    assert first["coverage"]["qr_failed"] >= 4
    assert len({item["sha256"] for item in first["documents"]}) == 20


def test_gold_set_fails_closed_when_a_quota_is_impossible() -> None:
    rows = gold_candidates()
    rows = [row for row in rows if row.get("certificate_kind") != "test"]
    rows.extend(
        {
            "sha256": sha(index),
            "unified_number": f"UNKNOWN-{index}",
            "display_name": f"未知-{index}",
            "calibrate_or_verify_location": "主机构",
        }
        for index in (100, 101)
    )
    try:
        select_gold_set(rows)
    except ValueError as exc:
        assert "test_report" in str(exc)
    else:
        raise AssertionError("missing category quota must be rejected")


def anomaly_candidates() -> list[dict]:
    return [
        {
            "sha256": sha(index),
            "report_id": f"R{index}",
            "unified_number": f"EQ{index}",
            "display_name": f"证书-{index}",
            "factory_serial_number": f"SN{index}",
            "calibrate_or_verify_date": "2026-01-01",
            "record_valid_until": "2027-01-01",
            "equipment_name": f"器具-{index}",
            "model": f"MODEL-{index}",
        }
        for index in range(1, 14)
    ]


def test_anomaly_fixtures_are_structured_non_destructive_and_balanced() -> None:
    rows = anomaly_candidates()
    before = copy.deepcopy(rows)
    result = generate_anomaly_fixtures(rows)

    assert rows == before
    assert result["count"] == 12
    assert result["counts"] == {name: 2 for name in sorted(ANOMALY_TYPES)}
    assert Counter(item["anomaly_type"] for item in result["fixtures"]) == Counter({name: 2 for name in ANOMALY_TYPES})
    assert all(item["source_pdf_modified"] is False for item in result["fixtures"])
    assert all(item["original_value"] != item["anomalous_value"] for item in result["fixtures"])
    assert all(item["expected"]["must_not_auto_pass"] is True for item in result["fixtures"])
    assert len({item["source_sha256"] for item in result["fixtures"]}) == 12


def test_missing_model_becomes_explicit_missing_vs_value_fixture() -> None:
    rows = anomaly_candidates()
    for row in rows:
        row.pop("model")
    result = generate_anomaly_fixtures(rows)
    model_fixtures = [item for item in result["fixtures"] if item["anomaly_type"] == "model_mismatch"]
    assert len(model_fixtures) == 2
    assert all(item["original_value"] == "__MISSING__" for item in model_fixtures)
    assert all(item["anomalous_value"].startswith("异常型号-") for item in model_fixtures)
