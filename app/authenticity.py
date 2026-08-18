from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pypdf import PdfReader

from app.models import AuthenticityResult


class AuthenticityProvider(Protocol):
    name: str
    authoritative: bool

    def verify(self, path: Path) -> AuthenticityResult: ...


def inspect_local_authenticity_evidence(path: Path) -> AuthenticityResult:
    """Detect local evidence without claiming cryptographic validity."""
    if path.suffix.lower() != ".pdf":
        return AuthenticityResult(method="local_evidence_only")
    evidence: list[str] = []
    try:
        reader = PdfReader(path)
        fields = reader.get_fields() or {}
        signature_names = [name for name, field in fields.items() if field.get("/FT") == "/Sig"]
        if signature_names:
            evidence.append(f"检测到 {len(signature_names)} 个 PDF 签名字段，但未验证证书链、时间戳或撤销状态")
            return AuthenticityResult(
                status="SIGNED_NOT_VERIFIED",
                method="pdf_signature_presence",
                evidence=evidence,
                explanation="检测到签名容器不等于签名有效；需可信根、时间戳及 OCSP/CRL 验证。",
            )
    except Exception as exc:
        evidence.append(f"PDF 签名字段检查失败：{type(exc).__name__}")
    return AuthenticityResult(method="local_evidence_only", evidence=evidence)


class LocalEvidenceAuthenticityProvider:
    """No-network provider that never claims authoritative validity."""

    name = "local_evidence_only"
    authoritative = False

    def verify(self, path: Path) -> AuthenticityResult:
        return inspect_local_authenticity_evidence(path)


def create_authenticity_provider(name: str) -> AuthenticityProvider:
    # Official lookup/digital-signature providers require issuer trust configuration.
    return LocalEvidenceAuthenticityProvider()
