from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


SEMANTIC_PROMPT_VERSION = "glm-difference-v2"
ARBITRATION_PROMPT_VERSION = "deepseek-arbitration-v2"
SEMANTIC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["EQUIVALENT", "INCONSISTENT", "UNCERTAIN"]},
        "risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["decision", "risk", "confidence", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SemanticDecision:
    decision: str
    risk: str
    confidence: float
    reason: str
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, Any]
    prompt_version: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _normalize(data: dict[str, Any], provider: str, response: Any, prompt_version: str) -> SemanticDecision:
    decision = str(data.get("decision") or "UNCERTAIN").upper()
    if decision not in {"EQUIVALENT", "INCONSISTENT", "UNCERTAIN"}:
        decision = "UNCERTAIN"
    risk = str(data.get("risk") or "MEDIUM").upper()
    if risk not in {"LOW", "MEDIUM", "HIGH"}:
        risk = "MEDIUM"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return SemanticDecision(
        decision=decision,
        risk=risk,
        confidence=confidence,
        reason=str(data.get("reason") or "模型未提供理由")[:1000],
        provider=provider,
        model=response.model,
        latency_ms=response.latency_ms,
        usage=response.usage,
        prompt_version=prompt_version,
    )


def review_differences(provider: Any, evidence: dict[str, Any]) -> SemanticDecision:
    prompt = (
        "你是外检计量证书文本差异主审模型。只判断给定字段差异是表述等价还是实质不一致，"
        "样品编号/样品号是机构或委托方内部编号，二维码验真记录号是发证机构系统记录号；"
        "二者与设备出厂编号属于不同编号体系，不要求相等，也不得仅因它们不同判为不一致或高风险。"
        "不得补造证书信息。若证据指出关键字段或冻结台账缺失，必须判UNCERTAIN，不能以其他"
        "字段替代或建议自动通过。输出JSON：decision仅EQUIVALENT/INCONSISTENT/UNCERTAIN，"
        "risk仅LOW/MEDIUM/HIGH，confidence为0到1，reason为中文理由。证据："
        + json.dumps(evidence, ensure_ascii=False)
    )
    response = provider.complete_json(
        [{"role": "user", "content": prompt}], schema=SEMANTIC_RESPONSE_SCHEMA
    )
    return _normalize(response.data, "glm", response, SEMANTIC_PROMPT_VERSION)


def arbitrate_differences(provider: Any, evidence: dict[str, Any]) -> SemanticDecision:
    prompt = (
        "你是独立的外检计量证书高风险仲裁模型。你看不到也不得推断第一模型的结论；"
        "样品编号与二维码验真记录号属于不同编号体系，不要求相等；不得把它们的差异当作设备身份冲突。"
        "只依据原始字段证据独立判断。若关键字段或冻结台账缺失，必须判UNCERTAIN，不得猜测补齐。"
        "输出JSON：decision仅EQUIVALENT/INCONSISTENT/UNCERTAIN，"
        "risk仅LOW/MEDIUM/HIGH，confidence为0到1，reason为中文理由。证据："
        + json.dumps(evidence, ensure_ascii=False)
    )
    response = provider.complete_json(
        [{"role": "user", "content": prompt}], schema=SEMANTIC_RESPONSE_SCHEMA
    )
    return _normalize(response.data, "deepseek", response, ARBITRATION_PROMPT_VERSION)
