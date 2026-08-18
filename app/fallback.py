from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class VisionFallbackResult:
    used: bool = False
    provider: str = "disabled"
    fields: dict[str, str] = field(default_factory=dict)
    warning: str | None = None


class VisionFallbackProvider(Protocol):
    name: str
    configured: bool

    def extract(self, path: Path, missing_fields: list[str]) -> VisionFallbackResult: ...


class DisabledVisionFallback:
    """Safe default: never performs network calls or consumes model quota."""

    name = "disabled"
    configured = False

    def extract(self, path: Path, missing_fields: list[str]) -> VisionFallbackResult:
        return VisionFallbackResult()


def create_vision_fallback(enabled: bool) -> VisionFallbackProvider:
    # A paid/custom endpoint adapter must be added deliberately with credentials kept outside reports.
    # Merely setting enabled=true never activates an unconfigured external call.
    return DisabledVisionFallback()
