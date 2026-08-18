from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingest import FrozenLedgerRow, parse_ledger_manifest  # noqa: E402


QUOTAS = {
    "external_scan": 4,
    "test_report": 2,
    "calibration_certificate": 7,
    "verification_certificate": 7,
}


def select_gold_set(
    rows: Iterable[FrozenLedgerRow | dict[str, Any]],
    *,
    certificate_root: Path | None = None,
) -> dict[str, Any]:
    """Select a stable 20-document validation set, keyed by unique SHA-256.

    Selection is deterministic: analysis flags are evaluated first and every tie is
    resolved by the full SHA-256. Duplicate business rows sharing bytes are never
    selected twice.
    """

    normalized = [_row_values(row) for row in rows]
    unique: dict[str, dict[str, Any]] = {}
    for values in normalized:
        sha = str(values.get("sha256") or "").lower()
        if len(sha) != 64:
            raise ValueError("every gold-set candidate must have a valid SHA-256")
        unique.setdefault(sha, values)
    if len(unique) < sum(QUOTAS.values()):
        raise ValueError("at least 20 unique certificate contents are required")

    issuer_counts = Counter(_issuer(item) for item in unique.values() if _issuer(item))
    dominant_issuer = issuer_counts.most_common(1)[0][0] if issuer_counts else ""
    candidates: list[dict[str, Any]] = []
    for sha, values in unique.items():
        analysis = _analyze_candidate(values, certificate_root)
        explicit_scan = _truthy(values.get("is_scanned") or values.get("scan_like"))
        # When files are unavailable, non-dominant issuing institutions are the
        # conservative proxy for the four heterogeneous external scanned samples.
        scan_proxy = bool(_issuer(values) and _issuer(values) != dominant_issuer)
        candidates.append(
            {
                "sha256": sha,
                "values": values,
                "issuer": _issuer(values),
                "kind": _certificate_kind(values),
                "is_scan": explicit_scan or analysis["is_scan"] or (not analysis["inspected"] and scan_proxy),
                "rotated": analysis["rotated"] or _truthy(values.get("rotated")),
                "qr_failed": analysis["qr_failed"] or _qr_failure_hint(values),
                "inspection": analysis,
            }
        )

    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    scan_pool = [item for item in candidates if item["is_scan"] and item["issuer"]]
    scan_pool.sort(key=_priority_key)
    used_issuers: set[str] = set()
    for candidate in scan_pool:
        if candidate["issuer"] in used_issuers:
            continue
        _take(candidate, "external_scan", selected, used)
        used_issuers.add(candidate["issuer"])
        if len(used_issuers) == QUOTAS["external_scan"]:
            break
    if len(used_issuers) != QUOTAS["external_scan"]:
        raise ValueError("cannot select four scanned certificates from four different institutions")

    for kind, category in (
        ("test", "test_report"),
        ("calibration", "calibration_certificate"),
        ("verification", "verification_certificate"),
    ):
        pool = [item for item in candidates if item["kind"] == kind and item["sha256"] not in used]
        pool.sort(key=_priority_key)
        for candidate in pool[: QUOTAS[category]]:
            _take(candidate, category, selected, used)
        actual = sum(1 for item in selected if item["category"] == category)
        if actual != QUOTAS[category]:
            raise ValueError(f"cannot satisfy gold-set quota for {category}: {actual}/{QUOTAS[category]}")

    category_counts = Counter(item["category"] for item in selected)
    return {
        "schema_version": "1.0",
        "selection_method": "unique_sha256_then_feature_priority_then_sha256",
        "locked": True,
        "count": len(selected),
        "category_counts": dict(sorted(category_counts.items())),
        "coverage": {
            "different_scan_institutions": len({item["issuer"] for item in selected if item["category"] == "external_scan"}),
            "rotated": sum(bool(item["rotated"]) for item in selected),
            "qr_failed": sum(bool(item["qr_failed"]) for item in selected),
        },
        "documents": selected,
    }


def _take(candidate: dict[str, Any], category: str, selected: list[dict[str, Any]], used: set[str]) -> None:
    sha = candidate["sha256"]
    if sha in used:
        return
    values = candidate["values"]
    selected.append(
        {
            "gold_id": f"gold-{len(selected) + 1:02d}",
            "category": category,
            "sha256": sha,
            "unified_number": values.get("unified_number", ""),
            "display_name": values.get("display_name", ""),
            "issuer": candidate["issuer"],
            "local_path": values.get("local_path", ""),
            "pdf_pages": values.get("pdf_pages"),
            "rotated": bool(candidate["rotated"]),
            "qr_failed": bool(candidate["qr_failed"]),
            "selection_evidence": candidate["inspection"],
        }
    )
    used.add(sha)


def _priority_key(candidate: dict[str, Any]) -> tuple[int, int, str]:
    return (-int(bool(candidate["rotated"])), -int(bool(candidate["qr_failed"])), candidate["sha256"])


def _row_values(row: FrozenLedgerRow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, FrozenLedgerRow):
        return dict(row.values)
    if "values" in row and isinstance(row["values"], dict):
        return dict(row["values"])
    return dict(row)


def _issuer(values: dict[str, Any]) -> str:
    return str(values.get("calibrate_or_verify_location") or values.get("issuer") or "").strip()


def _certificate_kind(values: dict[str, Any]) -> str:
    explicit = str(values.get("certificate_kind") or values.get("document_type") or "").lower()
    if explicit in {"test", "calibration", "verification"}:
        return explicit
    name = str(values.get("display_name") or values.get("original_name") or "").strip()
    if name.startswith("测试") or name.startswith("测") or "测试报告" in name:
        return "test"
    if name.startswith("校") or "校准" in name:
        return "calibration"
    if name.startswith("检") or "检定" in name:
        return "verification"
    return "unknown"


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "y", "是", "扫描"}


def _qr_failure_hint(values: dict[str, Any]) -> bool:
    value = values.get("qr_status")
    if value is False:
        return True
    return str(value or "").strip().lower() in {"failed", "failure", "not_found", "decode_failed", "失败", "未识别"}


def _analyze_candidate(values: dict[str, Any], certificate_root: Path | None) -> dict[str, Any]:
    result = {"inspected": False, "is_scan": False, "rotated": False, "qr_failed": False}
    if certificate_root is None:
        return result
    relative = str(values.get("local_path") or "").replace("\\", os.sep).replace("/", os.sep)
    if not relative:
        return result
    path = certificate_root / relative
    if not path.is_file() or path.suffix.lower() != ".pdf":
        return result
    try:
        import cv2
        import fitz
        import numpy as np

        document = fitz.open(path)
        total_text = sum(len(page.get_text("text").strip()) for page in document)
        rotations = [int(page.rotation or 0) % 360 for page in document]
        detector = cv2.QRCodeDetector()
        qr_found = False
        for page in list(document)[:3]:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
            image = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR) if pixmap.n == 3 else pixels
            decoded, _, _ = detector.detectAndDecode(image)
            if decoded:
                qr_found = True
                break
        document.close()
        return {
            "inspected": True,
            "is_scan": total_text < 20,
            "rotated": any(rotation != 0 for rotation in rotations),
            "qr_failed": not qr_found,
        }
    except Exception:
        # Selection remains deterministic and conservative if optional visual
        # inspection dependencies cannot analyze a particular document.
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性选择20份外检证书人工金标集")
    parser.add_argument("manifest", type=Path, help="下载与核验清单 JSON/CSV")
    parser.add_argument("--certificate-root", type=Path, help="包含清单 local_path 文件的根目录")
    parser.add_argument("--output", type=Path, required=True, help="输出JSON路径")
    args = parser.parse_args()

    rows = parse_ledger_manifest(args.manifest)
    result = select_gold_set(rows, certificate_root=args.certificate_root)
    result["source_manifest_sha256"] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "count": result["count"], "coverage": result["coverage"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
