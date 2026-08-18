from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppConfig:
    max_file_size_mb: int = 25
    max_batch_files: int = 100
    max_pdf_pages: int = 50
    pdf_text_min_chars_per_page: int = 20
    render_dpi: int = 200
    allowed_extensions: tuple[str, ...] = (
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".webp",
    )
    required_fields: tuple[str, ...] = (
        "certificate_number",
        "issuer_name",
        "issue_date",
    )
    ocr_provider: str = "none"
    vision_fallback_enabled: bool = False
    authenticity_provider: str = "local_evidence_only"
    retain_batches: int = 20
    data_dir: str = "data"
    model_mode: str = "disabled"
    auto_pass_confidence: float = 0.90
    model_confidence_threshold: float = 0.85
    qwen_concurrency: int = 2
    glm_concurrency: int = 4
    arbiter_concurrency: int = 2
    issuer_aliases: tuple[str, ...] = (
        "中国测试技术研究院",
        "中国计量科学研究院",
    )
    field_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


def _to_tuple(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return fallback


def load_config(path: Path | None = None) -> AppConfig:
    default = AppConfig()
    if path is None:
        path = Path(__file__).resolve().parents[1] / "config" / "app.json"
    if not path.exists():
        return default
    raw = json.loads(path.read_text(encoding="utf-8"))
    aliases: dict[str, tuple[str, ...]] = {}
    for key, value in raw.get("field_aliases", {}).items():
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            aliases[key] = tuple(value)
    return AppConfig(
        max_file_size_mb=int(raw.get("max_file_size_mb", default.max_file_size_mb)),
        max_batch_files=int(raw.get("max_batch_files", default.max_batch_files)),
        max_pdf_pages=int(raw.get("max_pdf_pages", default.max_pdf_pages)),
        pdf_text_min_chars_per_page=int(
            raw.get("pdf_text_min_chars_per_page", default.pdf_text_min_chars_per_page)
        ),
        render_dpi=int(raw.get("render_dpi", default.render_dpi)),
        allowed_extensions=_to_tuple(raw.get("allowed_extensions"), default.allowed_extensions),
        required_fields=_to_tuple(raw.get("required_fields"), default.required_fields),
        ocr_provider=str(raw.get("ocr_provider", default.ocr_provider)),
        vision_fallback_enabled=bool(
            raw.get("vision_fallback_enabled", default.vision_fallback_enabled)
        ),
        authenticity_provider=str(
            raw.get("authenticity_provider", default.authenticity_provider)
        ),
        retain_batches=int(raw.get("retain_batches", default.retain_batches)),
        data_dir=os.environ.get("CERT_DATA_DIR", str(raw.get("data_dir", default.data_dir))),
        model_mode=str(raw.get("model_mode", default.model_mode)),
        auto_pass_confidence=float(
            raw.get("auto_pass_confidence", default.auto_pass_confidence)
        ),
        model_confidence_threshold=float(
            raw.get("model_confidence_threshold", default.model_confidence_threshold)
        ),
        qwen_concurrency=int(raw.get("qwen_concurrency", default.qwen_concurrency)),
        glm_concurrency=int(raw.get("glm_concurrency", default.glm_concurrency)),
        arbiter_concurrency=int(
            raw.get("arbiter_concurrency", default.arbiter_concurrency)
        ),
        issuer_aliases=_to_tuple(raw.get("issuer_aliases"), default.issuer_aliases),
        field_aliases=aliases,
    )
