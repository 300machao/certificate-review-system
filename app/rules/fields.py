from __future__ import annotations

import re
import unicodedata
from datetime import date

from app.config import AppConfig
from app.models import FieldValue


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":")


def normalize_certificate_number(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s:：,，。]", "", value).replace("–", "-").replace("—", "-").upper()


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s:：,，。·•]", "", value).replace("–", "-").replace("—", "-").lower()


def normalize_date(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    numbers = re.findall(r"\d+", value)
    if len(numbers) >= 3:
        try:
            parsed = date(int(numbers[0]), int(numbers[1]), int(numbers[2]))
            return parsed.isoformat()
        except ValueError:
            return value.strip()
    return value.strip()


NORMALIZERS = {
    "certificate_number": normalize_certificate_number,
    "issue_date": normalize_date,
    "calibration_date": normalize_date,
    "receive_date": normalize_date,
    "due_date": normalize_date,
}


def normalize_field(field: str, value: str) -> str:
    return NORMALIZERS.get(field, normalize_name)(value)


class FieldExtractor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def extract(self, pages: list[str], sources: list[str]) -> tuple[dict[str, FieldValue], dict[str, list[str]]]:
        fields: dict[str, FieldValue] = {}
        candidates: dict[str, list[str]] = {}
        for page_number, raw in enumerate(pages, start=1):
            text = compact(raw)
            if not text:
                continue
            source = sources[page_number - 1] if page_number <= len(sources) else "unknown"
            page_fields = self._extract_page(text)
            for key, value in page_fields.items():
                value = value.strip(" :：;,；")
                if not value:
                    continue
                candidates.setdefault(key, []).append(value)
                if key not in fields:
                    fields[key] = FieldValue(
                        value=value,
                        normalized_value=normalize_field(key, value),
                        source=source,
                        page=page_number,
                        confidence=1.0 if source == "pdf_text" else None,
                        evidence=value[:120],
                    )
        unique_candidates = {
            key: list(dict.fromkeys(normalize_field(key, item) for item in values))
            for key, values in candidates.items()
        }
        return fields, unique_candidates

    def _extract_page(self, text: str) -> dict[str, str]:
        found: dict[str, str] = {}

        patterns = {
            "certificate_number": [
                r"(?:证书编号|报告编号|CertificateNo\.?)[:：]?((?:校准|检定|检测|测试)?字?第?[A-Za-z0-9\-]+号)",
                r"(?:证书编号|报告编号)[:：]?([A-Za-z0-9][A-Za-z0-9\-/]{4,})",
            ],
            "issue_date": [
                r"(?:签发日期|发证日期|IssueDate)[:：]?(\d{4})年?(\d{1,2})月?(\d{1,2})日?",
            ],
            "calibration_date": [
                r"(?:校准日期|检定日期)(?:CalibrateDate)?[:：]?(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
            ],
            "receive_date": [
                r"(?:接收日期|ReceiveDate)[:：]?(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
            ],
            "due_date": [
                r"(?:有效期至|有效日期至|下次检定日期|有效期)[:：]?(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
            ],
            "client_name": [
                r"客户名称(.{2,100}?)(?:ClientName|联络信息|联系信息|ContactInformation)",
                r"客户名称[:：]?(.{2,80}?)(?:器具名称|产品名称|型号/规格|型号)",
                r"(?:委托单位|送检单位|持有人)[:：]?([\u4e00-\u9fffA-Za-z0-9（）()·\-]{2,80})",
            ],
            "instrument_name": [
                r"器具名称[:：]?(.{2,60}?)(?:器具编号|产品编号|型号/规格|型号)",
                r"器具名称(.{2,80}?)(?:InstrumentName|型号/规格|型号)",
                r"(?:产品名称|样品名称)[:：]?([\u4e00-\u9fffA-Za-z0-9（）()·\-]{2,60})",
            ],
            "model": [
                r"型号/规格[:：]?(.{1,40}?)(?:制造单位|器具编号|产品编号|Manufacturer)",
                r"型号/规格(.{1,60}?)(?:Model|器具编号|SerialNo)",
                r"(?:型号|规格)[:：]?([\u4e00-\u9fffA-Za-z0-9（）()·/\-]{1,40})",
            ],
            "serial_number": [
                r"器具编号[:：]?(.{1,40}?)(?:型号/规格|型号|制造单位|Manufacturer)",
                r"器具编号(.{1,60}?)(?:SerialNo|制造单位|Manufacturer)",
                r"(?:出厂编号|设备编号)[:：]?([A-Za-z0-9\-/]{2,40})",
            ],
            "sample_number": [
                r"(?:样品编号|样品号|送检编号)[:：]?([A-Za-z0-9\-/]{2,40})",
            ],
            "unified_number": [
                r"(?:统一编号|资产编号|管理编号)[:：]?([A-Za-z0-9\-/]{2,40})",
            ],
            "manufacturer": [
                r"制造单位[:：]?(.{2,80}?)(?:本文件|声明|校准专用章|Stamp|Manufacturer)",
                r"制造单位(.{2,100}?)(?:Manufacturer|校准专用章|Stamp)",
            ],
            "verification_result": [
                r"(?:检定结论|校准结论|结论)[:：]?(.{1,30}?)(?:有效期|签发日期|$)",
            ],
            "reference_document": [
                r"(?:检定依据|校准依据|依据规程|技术依据)[:：]?(.{2,80}?)(?:环境条件|使用的计量标准|$)",
            ],
        }
        for field, variants in patterns.items():
            for pattern in variants:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if not match:
                    continue
                groups = match.groups()
                if field.endswith("date") and len(groups) >= 3:
                    found[field] = f"{int(groups[0]):04d}-{int(groups[1]):02d}-{int(groups[2]):02d}"
                else:
                    found[field] = groups[0]
                break

        for issuer in self.config.issuer_aliases:
            if issuer in text:
                found["issuer_name"] = issuer
                break
        if "issuer_name" not in found:
            generic = re.search(
                r"(?:发证机构|签发机构|机构名称)[:：]?(.{3,80}?)(?:签发日期|发证日期|IssueDate|证书编号|报告编号)",
                text,
            )
            if generic:
                found["issuer_name"] = generic.group(1)
        return found
