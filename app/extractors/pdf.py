from __future__ import annotations

from pathlib import Path

import cv2
import fitz
import numpy as np

from app.config import AppConfig
from app.extractors.ocr import OCRProvider
from app.extractors.qr import QRDecoder
from app.models import ExtractionResult


class PDFExtractor:
    def __init__(self, config: AppConfig, ocr: OCRProvider, qr: QRDecoder) -> None:
        self.config = config
        self.ocr = ocr
        self.qr = qr

    def extract(self, path: Path) -> ExtractionResult:
        result = ExtractionResult()
        with fitz.open(path) as document:
            if document.needs_pass:
                raise ValueError("PDF 已加密，无法在未提供密码的情况下解析")
            if document.page_count == 0:
                raise ValueError("PDF 不含页面")
            if document.page_count > self.config.max_pdf_pages:
                raise ValueError(f"PDF 页数超过限制（{self.config.max_pdf_pages} 页）")
            result.metadata["page_count"] = document.page_count
            result.metadata["pdf_has_text_layer"] = False
            result.metadata["qr_render_scales"] = [2.0, 3.0]
            for page_index, page in enumerate(document):
                text = page.get_text("text").strip()
                text_source = "pdf_text"
                page_qr, bgr = self._decode_qr_at_fixed_scales(page)
                for item in page_qr:
                    item["page"] = page_index + 1
                result.qr_codes.extend(page_qr)

                visible_chars = len("".join(text.split()))
                if visible_chars >= self.config.pdf_text_min_chars_per_page:
                    result.metadata["pdf_has_text_layer"] = True
                elif self.ocr.available:
                    ocr_page = self.ocr.recognize(bgr)
                    if ocr_page.text.strip():
                        text = ocr_page.text.strip()
                        text_source = ocr_page.engine
                        result.metadata.setdefault("ocr_confidences", []).append(ocr_page.confidence)
                    else:
                        result.warnings.append(f"第 {page_index + 1} 页无文字层且 OCR 未提取出文字")
                else:
                    result.warnings.append(f"第 {page_index + 1} 页无可用文字层，且本地 OCR 未安装")
                result.text_pages.append(text)
                result.text_sources.append(text_source)
        return result

    def _decode_qr_at_fixed_scales(self, page: fitz.Page) -> tuple[list[dict], np.ndarray]:
        """Decode at 2x and 3x render scales and require cross-scale agreement.

        Image enhancement variants at a single resolution are useful fallbacks but
        are not independent multi-scale evidence. The raw QR payload never leaves
        ``QRDecoder``; merging is performed by its SHA-256 identifier.
        """
        found: dict[str, dict] = {}
        high_resolution: np.ndarray | None = None
        for scale in (2.0, 3.0):
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
            if pix.n == 3:
                bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            elif pix.n == 4:
                bgr = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
            else:
                bgr = image
            if scale == 3.0:
                high_resolution = bgr
            for item in self.qr.decode(bgr):
                key = str(item["payload_sha256"])
                if key not in found:
                    found[key] = dict(item)
                    found[key]["render_scales"] = [scale]
                    found[key]["enhancements"] = [
                        f"{scale}x:{method}" for method in item.get("enhancements", [])
                    ]
                else:
                    if scale not in found[key]["render_scales"]:
                        found[key]["render_scales"].append(scale)
                    found[key]["enhancements"].extend(
                        f"{scale}x:{method}" for method in item.get("enhancements", [])
                        if f"{scale}x:{method}" not in found[key]["enhancements"]
                    )
        for item in found.values():
            item["stable_multiscale"] = len(item["render_scales"]) >= 2
            item["confidence_level"] = "high" if item["stable_multiscale"] else "low"
        assert high_resolution is not None
        return list(found.values()), high_resolution
