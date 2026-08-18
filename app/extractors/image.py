from __future__ import annotations

from pathlib import Path

import cv2
from PIL import Image

from app.extractors.ocr import OCRProvider
from app.extractors.qr import QRDecoder
from app.models import ExtractionResult


class ImageExtractor:
    def __init__(self, ocr: OCRProvider, qr: QRDecoder) -> None:
        self.ocr = ocr
        self.qr = qr

    def extract(self, path: Path) -> ExtractionResult:
        with Image.open(path) as probe:
            width, height = probe.size
            image_format = probe.format or "unknown"
            if width * height > 100_000_000:
                raise ValueError("图片像素数超过安全限制")
            probe.verify()
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("图片解码失败")
        result = ExtractionResult(metadata={
            "page_count": 1,
            "image_width": width,
            "image_height": height,
            "image_format": image_format,
        })
        for item in self.qr.decode(image):
            item["page"] = 1
            result.qr_codes.append(item)
        if self.ocr.available:
            page = self.ocr.recognize(image)
            result.text_pages = [page.text.strip()]
            result.text_sources = [page.engine]
            result.metadata["ocr_confidence"] = page.confidence
            if not page.text.strip():
                result.warnings.append("OCR 未提取出文字")
        else:
            result.text_pages = [""]
            result.text_sources = ["none"]
            result.warnings.append("本地 OCR 未安装：图片只能进行二维码识别，文字字段需人工复核")
        return result
