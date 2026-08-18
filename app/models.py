from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FieldValue:
    value: str
    source: str
    page: int | None = None
    confidence: float | None = None
    evidence: str | None = None
    normalized_value: str | None = None
    bbox: list[float] | None = None


@dataclass
class Issue:
    code: str
    severity: str
    title: str
    detail: str
    field: str | None = None
    page: int | None = None
    evidence: str | None = None


@dataclass
class FieldComparison:
    field: str
    document_value: str
    qr_value: str
    status: str
    basis: str


@dataclass
class AuthenticityResult:
    status: str = "UNVERIFIED"
    method: str = "none"
    evidence: list[str] = field(default_factory=list)
    explanation: str = (
        "仅完成内容一致性检查；未连接发证机构官方查询、可信证书状态库，"
        "也未完成数字签名信任链验证。"
    )


@dataclass
class ExtractionResult:
    text_pages: list[str] = field(default_factory=list)
    text_sources: list[str] = field(default_factory=list)
    qr_codes: list[dict[str, Any]] = field(default_factory=list)
    fields: dict[str, FieldValue] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReviewRecord:
    record_id: str
    filename: str
    sha256: str
    size_bytes: int
    file_type: str
    status: str = "QUEUED"
    workflow_state: str = "UPLOADED"
    duplicate_of: str | None = None
    fields: dict[str, FieldValue] = field(default_factory=dict)
    qr_codes: list[dict[str, Any]] = field(default_factory=list)
    comparisons: list[FieldComparison] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    authenticity: AuthenticityResult = field(default_factory=AuthenticityResult)
    metadata: dict[str, Any] = field(default_factory=dict)
    ledger_fields: dict[str, str] = field(default_factory=dict)
    model_decisions: list[dict[str, Any]] = field(default_factory=list)
    review_task_id: str | None = None
    final_decision: str | None = None
    final_reason: str | None = None
    error: str | None = None
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchResult:
    batch_id: str
    request_id: str
    created_at: str
    status: str = "QUEUED"
    records: list[ReviewRecord] = field(default_factory=list)
    completed_at: str | None = None

    def summary(self) -> dict[str, int]:
        counts = {"total": len(self.records), "queued": 0, "processing": 0,
                  "passed": 0, "review": 0, "failed": 0}
        mapping = {
            "QUEUED": "queued", "PROCESSING": "processing", "PASS": "passed",
            "AUTO_PASSED": "passed", "MANUAL_PASSED": "passed",
            "AUTO_FAILED": "failed", "MANUAL_FAILED": "failed",
            "REVIEW": "review", "HUMAN_REVIEW": "review", "FAILED": "failed",
            "PROCESSING_FAILED": "failed",
        }
        for record in self.records:
            key = mapping.get(record.status)
            if record.status == "FINALIZED":
                if record.final_decision == "PASS":
                    key = "passed"
                elif record.final_decision == "FAIL":
                    key = "failed"
                else:
                    key = "review"
            if key:
                counts[key] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["summary"] = self.summary()
        return data
