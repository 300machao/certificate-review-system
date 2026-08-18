from __future__ import annotations

import json
import hashlib
import re
from urllib.parse import parse_qs, unquote, urlparse

import cv2
import numpy as np


FIELD_ALIASES = {
    "certificate_number": {
        "certificatenumber", "certificateno", "certno", "certificate_no",
        "证书编号", "报告编号", "编号",
    },
    "issuer_name": {"issuer", "issuername", "机构名称", "发证机构", "签发机构"},
    "issue_date": {"issuedate", "issue_date", "签发日期", "发证日期"},
    "calibration_date": {"calibrationdate", "calibratedate", "校准日期", "检定日期"},
    "client_name": {"client", "clientname", "客户名称", "委托单位", "持有人"},
    "instrument_name": {"instrument", "instrumentname", "器具名称", "产品名称"},
    "serial_number": {"serial", "serialno", "器具编号", "出厂编号"},
    "verification_record_id": {
        "verificationrecordid", "verificationid", "recordid", "验真记录号", "查询记录号",
    },
}


def _canonical_key(key: str) -> str | None:
    normalized = re.sub(r"[\s.\-:]", "", key).lower()
    for field, aliases in FIELD_ALIASES.items():
        if normalized in {re.sub(r"[\s.\-:]", "", item).lower() for item in aliases}:
            return field
    return None


def parse_qr_payload(payload: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    text = unquote(payload.strip())
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key, value in data.items():
                canonical = _canonical_key(str(key))
                if canonical and value is not None:
                    fields[canonical] = str(value).strip()
    except (json.JSONDecodeError, TypeError):
        pass

    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        for key, values in parse_qs(parsed.query).items():
            canonical = _canonical_key(key)
            if canonical and values:
                fields.setdefault(canonical, values[0].strip())
        route = "/".join(part for part in (parsed.path, parsed.fragment) if part)
        segments = [unquote(part) for part in route.split("/") if part]
        lowered = [part.casefold() for part in segments]
        if "certificateverification" in lowered:
            index = lowered.index("certificateverification")
            if index + 1 < len(segments):
                # The first route parameter is the issuer's verification-system
                # record id.  It is not the instrument factory serial number and
                # must never be compared with a certificate/sample identifier.
                fields.setdefault("verification_record_id", segments[index + 1].strip())
            if index + 2 < len(segments):
                fields.setdefault("certificate_number", segments[index + 2].strip())
        for segment in reversed(segments):
            if re.search(r"(?:校准|检定|检测|测试|证书).{0,4}第?[A-Za-z0-9\-]+号?", segment):
                fields.setdefault("certificate_number", segment)
                break

    for key, value in re.findall(
        r"([A-Za-z_\-]+|证书编号|报告编号|发证机构|签发日期|校准日期|客户名称|器具编号)"
        r"\s*[:=：]\s*([^;；|&\r\n]+)",
        text,
    ):
        canonical = _canonical_key(key)
        if canonical:
            fields.setdefault(canonical, value.strip())
    return fields


class QRDecoder:
    """Offline OpenCV decoder with conservative image-enhancement fallbacks."""

    def __init__(self) -> None:
        self._detector = cv2.QRCodeDetector()

    def decode(self, image: np.ndarray) -> list[dict[str, object]]:
        if image.ndim == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image
        variants: list[tuple[str, np.ndarray]] = [("original", gray)]
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        variants.append(("clahe", clahe))
        variants.append(("otsu", cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]))
        if min(gray.shape[:2]) < 1600:
            variants.append(("upscale_2x", cv2.resize(clahe, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)))

        found: dict[str, dict[str, object]] = {}

        def store(value: str, method: str, points: object) -> None:
            if not value:
                return
            if value not in found:
                found[value] = {
                    "payload_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    "content": "[二维码载荷已脱敏]",
                    "parsed_fields": parse_qr_payload(value),
                    "enhancement": method,
                    "enhancements": [method],
                    "stable_multiscale": False,
                    "points": points,
                }
            elif method not in found[value]["enhancements"]:
                found[value]["enhancements"].append(method)

        for method, variant in variants:
            try:
                ok, decoded, points, _ = self._detector.detectAndDecodeMulti(variant)
                if ok:
                    for index, value in enumerate(decoded):
                        store(
                            value,
                            method,
                            points[index].round(1).tolist() if points is not None else None,
                        )
                value, points, _ = self._detector.detectAndDecode(variant)
                store(value, method, points.round(1).tolist() if points is not None else None)
            except cv2.error:
                continue
        for item in found.values():
            item["stable_multiscale"] = len(item["enhancements"]) >= 2
        return list(found.values())
