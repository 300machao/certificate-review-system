from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from app.models import FieldValue


VISION_SCHEMA_VERSION = "certificate-v2"
PROMPT_VERSION = "qwen-certificate-v2"


FIELD_KEYS = (
    "certificate_number",
    "certificate_type",
    "issuer_name",
    "client_name",
    "instrument_name",
    "unified_number",
    "model",
    "serial_number",
    "sample_number",
    "manufacturer",
    "calibration_date",
    "issue_date",
    "due_date",
    "verification_result",
    "reference_document",
    "traceability",
    "seal_status",
    "signature_status",
)


@dataclass
class VisionExtraction:
    fields: dict[str, FieldValue] = field(default_factory=dict)
    measurements: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_runs: list[dict[str, Any]] = field(default_factory=list)


def _data_url(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def render_pdf_pages(path: Path, dpi: int = 150) -> list[bytes]:
    pages: list[bytes] = []
    with fitz.open(path) as document:
        if document.needs_pass:
            raise ValueError("PDF 已加密，无法提交视觉识别")
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        for page in document:
            pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
            pages.append(pixmap.tobytes("jpeg", jpg_quality=82))
    return pages


def _prompt(page_start: int, page_end: int) -> str:
    template = {
        "fields": {
            key: {
                "value": None,
                "confidence": 0.0,
                "page": None,
                "evidence": None,
            }
            for key in FIELD_KEYS
        },
        "measurements": [],
        "uncertain_fields": [],
    }
    return (
        "你是外检计量证书版面字段提取器。下面是同一份证书的"
        f"第{page_start}至第{page_end}页。逐字读取，不猜测；数字、小数点、负号、单位必须忠实。"
        "只输出合法JSON对象。每个字段必须包含value、0到1置信度、证据所在page和不超过80字的evidence。"
        "看不清时value为null、confidence为0并列入uncertain_fields。"
        "字段口径：serial_number只填写版面明确标注为出厂编号、器具编号或设备编号的设备本体编号；"
        "sample_number只填写样品编号、样品号或送检编号。二者不得混填。"
        "二维码验真网址中的数字记录号不是出厂编号，不得据此填写serial_number。"
        "样品编号与二维码验真记录号属于不同编号体系，不要求相等。"
        "不得根据二维码内容替代版面文字。结构必须与此模板一致："
        + json.dumps(template, ensure_ascii=False)
    )


def extract_with_qwen(path: Path, provider: Any, chunk_size: int = 3) -> VisionExtraction:
    pages = render_pdf_pages(path)
    result = VisionExtraction()
    candidates: dict[str, list[FieldValue]] = {}
    for offset in range(0, len(pages), chunk_size):
        chunk = pages[offset:offset + chunk_size]
        page_start = offset + 1
        page_end = offset + len(chunk)
        messages = [{"role": "user", "content": _prompt(page_start, page_end)}]
        response = provider.complete_json(
            messages,
            images=[_data_url(item) for item in chunk],
        )
        data = response.data
        result.raw_runs.append({
            "model": response.model,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "attempts": response.attempts,
            "request_id": response.request_id,
            "page_start": page_start,
            "page_end": page_end,
            "prompt_version": PROMPT_VERSION,
            "schema_version": VISION_SCHEMA_VERSION,
        })
        for key, item in (data.get("fields") or {}).items():
            if key not in FIELD_KEYS or not isinstance(item, dict) or item.get("value") is None:
                continue
            value = str(item.get("value")).strip()
            if not value:
                continue
            evidence = str(item.get("evidence") or value)[:120]
            if key == "serial_number" and any(
                label in evidence for label in ("样品编号", "样品号", "送检编号")
            ):
                key = "sample_number"
            elif key == "sample_number" and any(
                label in evidence for label in ("出厂编号", "器具编号", "设备编号")
            ):
                key = "serial_number"
            page = item.get("page")
            try:
                page = int(page) if page is not None else page_start
            except (TypeError, ValueError):
                page = page_start
            if 1 <= page <= len(chunk):
                page += offset
            confidence = item.get("confidence")
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
            except (TypeError, ValueError):
                confidence = 0.0
            candidates.setdefault(key, []).append(FieldValue(
                value=value,
                source="qwen_vision",
                page=page,
                confidence=confidence,
                evidence=evidence,
            ))
        measurements = data.get("measurements")
        if isinstance(measurements, list):
            result.measurements.extend(item for item in measurements if isinstance(item, dict))
        uncertain = data.get("uncertain_fields")
        if isinstance(uncertain, list):
            result.warnings.extend(f"千问低置信字段：{item}" for item in uncertain)

    for key, values in candidates.items():
        normalized = {item.value.strip().casefold() for item in values}
        chosen = max(values, key=lambda item: item.confidence or 0.0)
        result.fields[key] = chosen
        if len(normalized) > 1:
            result.warnings.append(f"千问分块结果冲突：{key}")
    return result
