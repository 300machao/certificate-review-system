from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from app.config import AppConfig
from app.extractors.qr import QRDecoder
from app.models import FieldValue
from app.rules.engine import RuleEngine
from app.semantic import arbitrate_differences, review_differences
from app.vision import extract_with_qwen


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeResponse:
    data: dict
    model: str = "mock-model"
    usage: dict | None = None
    latency_ms: int = 12
    attempts: int = 1
    request_id: str = "mock-request"

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = {"total_tokens": 10}


class FakeProvider:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.calls = []

    def complete_json(self, messages, schema=None, images=None, timeout_s=None):
        self.calls.append({"messages": messages, "images": images})
        return FakeResponse(self.data)


def test_three_way_rule_distinguishes_form_and_substantive_difference() -> None:
    engine = RuleEngine(AppConfig())
    comparisons = engine.compare_three_way(
        {
            "certificate_number": FieldValue("检定字第 2026-001 号", "pdf"),
            "calibration_date": FieldValue("2026年5月1日", "pdf"),
            "serial_number": FieldValue("ABC-001", "pdf"),
        },
        {
            "certificate_number": "检定字第2026-001号",
            "calibration_date": "2026-05-01",
            "serial_number": "ABC-999",
        },
        {
            "certificate_number": "检定字第2026-001号",
            "calibration_date": "2026/05/01",
            "serial_number": "ABC-001",
        },
    )
    by_field = {item["field"]: item for item in comparisons}
    assert by_field["certificate_number"]["result"] == "FORM_EQUIVALENT"
    assert by_field["calibration_date"]["result"] == "FORM_EQUIVALENT"
    assert by_field["serial_number"]["result"] == "SUBSTANTIVE_DIFF"
    assert by_field["serial_number"]["severity"] == "HIGH"


def test_sample_and_verification_record_numbers_are_independent() -> None:
    engine = RuleEngine(AppConfig())
    comparisons = engine.compare_three_way(
        {
            "certificate_number": FieldValue("校准字第202600001号", "pdf"),
            "sample_number": FieldValue("16011", "qwen_vision"),
        },
        {
            "certificate_number": "校准字第202600001号",
            "verification_record_id": "1010525285",
        },
        {},
    )
    by_field = {item["field"]: item for item in comparisons}

    assert by_field["sample_number"]["result"] == "NOT_COMPARABLE"
    assert by_field["verification_record_id"]["result"] == "NOT_COMPARABLE"
    assert not any(
        item["result"] == "SUBSTANTIVE_DIFF"
        for item in comparisons
        if item["field"] in {"sample_number", "verification_record_id"}
    )


def test_semantic_and_arbiter_prompts_are_independent() -> None:
    evidence = {"field": "serial_number", "left": "A1", "right": "A2"}
    glm = FakeProvider({
        "decision": "INCONSISTENT", "risk": "HIGH", "confidence": 0.96,
        "reason": "出厂编号不一致",
    })
    arbiter = FakeProvider({
        "decision": "INCONSISTENT", "risk": "HIGH", "confidence": 0.92,
        "reason": "关键身份字段冲突",
    })
    first = review_differences(glm, evidence)
    second = arbitrate_differences(arbiter, evidence)
    assert first.decision == second.decision == "INCONSISTENT"
    assert first.provider == "glm"
    assert second.provider == "deepseek"
    arbiter_prompt = arbiter.calls[0]["messages"][0]["content"]
    assert "出厂编号不一致" not in arbiter_prompt
    assert "第一模型" in arbiter_prompt


def test_qr_decoder_does_not_return_raw_payload() -> None:
    image = cv2.imdecode(
        np.fromfile(ROOT / "samples" / "01-字段一致-应通过.png", dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    decoded = QRDecoder().decode(image)
    assert decoded
    assert all(item["content"] == "[二维码载荷已脱敏]" for item in decoded)
    assert all(len(str(item["payload_sha256"])) == 64 for item in decoded)


def test_qwen_vision_schema_and_chunking_with_mock_provider() -> None:
    provider = FakeProvider({
        "fields": {
            "certificate_number": {
                "value": "校准字第2026001号",
                "confidence": 0.97,
                "page": 1,
                "evidence": "证书编号",
            }
        },
        "measurements": [],
        "uncertain_fields": [],
    })
    result = extract_with_qwen(
        ROOT / "samples" / "01-字段一致-应通过.pdf",
        provider,
        chunk_size=3,
    )
    assert result.fields["certificate_number"].confidence == 0.97
    assert result.fields["certificate_number"].source == "qwen_vision"
    assert provider.calls
    assert provider.calls[0]["images"]
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "sample_number只填写样品编号" in prompt
    assert "二维码验真记录号属于不同编号体系" in prompt


def test_qwen_sample_number_is_not_treated_as_factory_serial() -> None:
    provider = FakeProvider({
        "fields": {
            "serial_number": {
                "value": "16011",
                "confidence": 0.99,
                "page": 1,
                "evidence": "样品编号 16011",
            }
        },
        "measurements": [],
        "uncertain_fields": [],
    })

    result = extract_with_qwen(
        ROOT / "samples" / "01-字段一致-应通过.pdf",
        provider,
        chunk_size=3,
    )

    assert result.fields["sample_number"].value == "16011"
    assert "serial_number" not in result.fields
