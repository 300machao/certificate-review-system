from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass
class OCRPage:
    text: str
    confidence: float | None
    engine: str


class OCRProvider(Protocol):
    name: str
    available: bool

    def recognize(self, image: np.ndarray) -> OCRPage: ...


class UnavailableOCR:
    name = "unavailable"
    available = False

    def recognize(self, image: np.ndarray) -> OCRPage:
        return OCRPage(text="", confidence=None, engine=self.name)


class PaddleOCRProvider:
    """Lazy adapter supporting common PaddleOCR 2.x/3.x result shapes."""

    name = "paddleocr"

    def __init__(self) -> None:
        self.available = False
        self._engine = None
        try:
            from paddleocr import PaddleOCR

            try:
                self._engine = PaddleOCR(
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    lang="ch",
                )
            except TypeError:
                self._engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
            self.available = True
        except Exception:
            self.available = False

    def recognize(self, image: np.ndarray) -> OCRPage:
        if not self.available or self._engine is None:
            return OCRPage(text="", confidence=None, engine=self.name)
        texts: list[str] = []
        scores: list[float] = []
        try:
            if hasattr(self._engine, "ocr"):
                raw = self._engine.ocr(image, cls=True)
                for page in raw or []:
                    for item in page or []:
                        if len(item) >= 2 and isinstance(item[1], (list, tuple)):
                            texts.append(str(item[1][0]))
                            scores.append(float(item[1][1]))
            else:
                raw = self._engine.predict(image)
                for item in raw or []:
                    data = getattr(item, "json", item)
                    if callable(data):
                        data = data()
                    if isinstance(data, dict):
                        payload = data.get("res", data)
                        texts.extend(str(x) for x in payload.get("rec_texts", []))
                        scores.extend(float(x) for x in payload.get("rec_scores", []))
        except Exception as exc:
            return OCRPage(text="", confidence=None, engine=f"{self.name}:error:{type(exc).__name__}")
        confidence = sum(scores) / len(scores) if scores else None
        return OCRPage(text="\n".join(texts), confidence=confidence, engine=self.name)


def create_ocr_provider(name: str) -> OCRProvider:
    if name.lower() in {"auto", "paddleocr"}:
        provider = PaddleOCRProvider()
        if provider.available:
            return provider
    return UnavailableOCR()
