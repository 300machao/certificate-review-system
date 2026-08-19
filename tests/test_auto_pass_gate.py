from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import AppConfig
from app.models import AuthenticityResult, ExtractionResult, FieldValue, Issue, ReviewRecord
from app.service import AUTO_PASS_REQUIRED_FIELDS, BatchReviewService
from app.vision import VisionExtraction
from scripts.validate_anomaly_fixtures import validate


ROOT = Path(__file__).resolve().parents[1]


class _SemanticProvider:
    def __init__(
        self,
        provider: str,
        decision: str = "EQUIVALENT",
        *,
        risk: str | None = None,
    ) -> None:
        self.configured = True
        self.config = SimpleNamespace(model=f"{provider}-mock")
        self.decision = decision
        self.risk = risk or ("LOW" if decision == "EQUIVALENT" else "MEDIUM")
        self.calls: list[dict] = []

    def complete_json(self, messages, **_kwargs):
        self.calls.append({"messages": messages})
        return SimpleNamespace(
            data={
                "decision": self.decision,
                "risk": self.risk,
                "confidence": .96,
                "reason": "两个编号属于不同编号体系" if self.decision == "EQUIVALENT" else "仍需核对",
            },
            model=self.config.model,
            usage={"total_tokens": 12},
            latency_ms=5,
        )

    def close(self) -> None:
        return None


class _QwenProvider:
    configured = True
    config = SimpleNamespace(model="qwen-mock")

    def close(self) -> None:
        return None


def _eligible_record(service: BatchReviewService) -> tuple[ReviewRecord, list[dict[str, str]]]:
    service.authenticity = SimpleNamespace(authoritative=True)
    values = {
        "certificate_number": "CERT-001", "issuer_name": "机构A",
        "instrument_name": "数字表", "unified_number": "EQ-001", "model": "M1",
        "serial_number": "SN-001", "calibration_date": "2026-01-01",
        "due_date": "2027-01-01", "verification_result": "合格",
    }
    fields = {
        key: FieldValue(value=value, source="qwen_vision", confidence=.99, page=1)
        for key, value in values.items()
    }
    record = ReviewRecord(
        record_id="r", filename="r.pdf", sha256="a" * 64, size_bytes=1,
        file_type="pdf", fields=fields,
        qr_codes=[{"stable_multiscale": True, "parsed_fields": dict(values)}],
        metadata={
            "qwen_used": True,
            "qwen_warnings": [],
            "authoritative_verification_used": True,
        },
        ledger_fields=dict(values),
        authenticity=AuthenticityResult(
            status="VERIFIED",
            method="issuer_official_api",
            evidence=["official-response-id:synthetic"],
            explanation="合成权威验真证据",
        ),
    )
    return record, service.rules.compare_three_way(fields, values, values)


def _review_with_mocked_qwen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    qwen_result: VisionExtraction | Exception,
) -> tuple[BatchReviewService, ReviewRecord]:
    service = BatchReviewService(
        AppConfig(required_fields=("issue_date",), model_mode="validation"),
        root=tmp_path,
    )
    service.model_mode = "validation"
    service.providers["qwen"].close()
    service.providers["qwen"] = _QwenProvider()
    extraction = ExtractionResult(
        text_pages=["证书编号：SYNTHETIC-001"],
        text_sources=["pdf_text"],
        qr_codes=[{
            "stable_multiscale": True,
            "parsed_fields": {"certificate_number": "SYNTHETIC-001"},
        }],
    )
    local_fields = {
        "certificate_number": FieldValue(
            "SYNTHETIC-001", "pdf_text", confidence=1.0, page=1,
        ),
    }
    run_counter: list[int] = []

    monkeypatch.setattr(
        "app.service.PDFExtractor.extract", lambda _extractor, _path: extraction,
    )
    monkeypatch.setattr(
        service.fields, "extract", lambda _pages, _sources: (local_fields, {}),
    )

    def fake_qwen(_path: Path, _provider: object, chunk_size: int = 3) -> VisionExtraction:
        assert chunk_size == 3
        if isinstance(qwen_result, Exception):
            raise qwen_result
        return qwen_result

    monkeypatch.setattr("app.service.extract_with_qwen", fake_qwen)
    monkeypatch.setattr(
        service.db,
        "create_extraction_run",
        lambda *_args, **_kwargs: {"id": f"synthetic-run-{len(run_counter)}"},
    )
    monkeypatch.setattr(
        service.db,
        "finish_extraction_run",
        lambda *_args, **_kwargs: run_counter.append(1),
    )
    monkeypatch.setattr(service.db, "add_comparison", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_persist_fields", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_set_state",
        lambda target, state: setattr(target, "workflow_state", state),
    )
    monkeypatch.setattr(service, "_decide_advanced", lambda *_args, **_kwargs: None)

    record = ReviewRecord(
        record_id="synthetic-record",
        filename="synthetic.pdf",
        sha256="b" * 64,
        size_bytes=1,
        file_type="pdf",
    )
    service._review_advanced_file(tmp_path / "synthetic.pdf", record)
    return service, record


def test_qwen_completed_field_recomputes_active_rule_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, record = _review_with_mocked_qwen(
        monkeypatch,
        tmp_path,
        VisionExtraction(fields={
            "issue_date": FieldValue(
                "2026-08-01", "qwen_vision", confidence=.98, page=1,
                evidence="签发日期：2026-08-01",
            ),
        }),
    )
    try:
        extraction_codes = {
            item["code"] for item in record.metadata["extraction_stage_issues"]
        }
        active_codes = {item.code for item in record.issues}
        assert "REQUIRED_FIELD_MISSING" in extraction_codes
        assert "REQUIRED_FIELD_MISSING" not in active_codes
        assert record.fields["issue_date"].source == "qwen_vision"
    finally:
        service.close()


def test_qwen_still_missing_field_keeps_active_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, record = _review_with_mocked_qwen(
        monkeypatch, tmp_path, VisionExtraction(),
    )
    try:
        assert any(
            issue.code == "REQUIRED_FIELD_MISSING" and issue.field == "issue_date"
            for issue in record.issues
        )
    finally:
        service.close()


@pytest.mark.parametrize(
    ("qwen_result", "expected_code"),
    [
        (TimeoutError("synthetic qwen timeout"), "QWEN_VISION_FAILED"),
        (VisionExtraction(warnings=["synthetic uncertain field"]), "QWEN_VISION_WARNING"),
    ],
)
def test_qwen_timeout_or_warning_remains_an_active_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qwen_result: VisionExtraction | Exception,
    expected_code: str,
) -> None:
    service, record = _review_with_mocked_qwen(monkeypatch, tmp_path, qwen_result)
    try:
        assert any(
            issue.code == expected_code and issue.severity == "review"
            for issue in record.issues
        )
        comparisons = service.rules.compare_three_way(
            record.fields,
            record.qr_codes[0]["parsed_fields"],
            record.ledger_fields,
        )
        assert "review_or_error_issue" in service.auto_pass_blockers(record, comparisons)
    finally:
        service.close()


def test_negative_conclusions_never_pass_substring_check() -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        for conclusion in ("不合格", "不符合要求", "不满足", "不确定"):
            record, comparisons = _eligible_record(service)
            record.fields["verification_result"].value = conclusion
            assert "conclusion_not_qualified" in service.auto_pass_blockers(record, comparisons)
    finally:
        service.close()


def test_every_required_field_and_qwen_health_blocks_auto_pass() -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, comparisons = _eligible_record(service)
        assert not service.auto_pass_blockers(record, comparisons)
        for field in AUTO_PASS_REQUIRED_FIELDS:
            altered, altered_comparisons = _eligible_record(service)
            altered.fields.pop(field)
            blockers = service.auto_pass_blockers(altered, altered_comparisons)
            assert any(field in blocker for blocker in blockers)
        record, comparisons = _eligible_record(service)
        record.metadata["qwen_error"] = "timeout"
        assert "qwen_failed" in service.auto_pass_blockers(record, comparisons)
        record.metadata.pop("qwen_error")
        record.metadata["qwen_warnings"] = ["chunk conflict"]
        assert "qwen_warning_or_chunk_conflict" in service.auto_pass_blockers(record, comparisons)
    finally:
        service.close()


def test_auto_pass_requires_complete_authoritative_authenticity_evidence() -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, comparisons = _eligible_record(service)
        assert not service.auto_pass_blockers(record, comparisons)

        service.authenticity = SimpleNamespace(authoritative=False)
        assert "authenticity_provider_not_authoritative" in service.auto_pass_blockers(
            record, comparisons,
        )

        service.authenticity = SimpleNamespace(authoritative=True)
        for status in ("UNVERIFIED", "SIGNED_NOT_VERIFIED"):
            record.authenticity.status = status
            assert f"authenticity_{status.lower()}" in service.auto_pass_blockers(
                record, comparisons,
            )

        record.authenticity.status = "VERIFIED"
        record.authenticity.evidence = []
        assert "authoritative_authenticity_evidence_incomplete" in (
            service.auto_pass_blockers(record, comparisons)
        )

        record.authenticity.evidence = ["official-response-id:synthetic"]
        record.metadata["authoritative_verification_used"] = False
        assert "authoritative_authenticity_not_used" in service.auto_pass_blockers(
            record, comparisons,
        )
    finally:
        service.close()


def test_sample_number_and_qr_verification_record_only_do_not_require_serial() -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, _ = _eligible_record(service)
        record.fields.pop("serial_number")
        record.fields["sample_number"] = FieldValue(
            "13004", "qwen_vision", confidence=.99, page=1, evidence="样品编号：13004"
        )
        record.qr_codes[0]["parsed_fields"].pop("serial_number")
        record.qr_codes[0]["parsed_fields"]["verification_record_id"] = "1010554082"
        record.metadata["qr_fields"] = dict(record.qr_codes[0]["parsed_fields"])
        comparisons = service.rules.compare_three_way(
            record.fields, record.metadata["qr_fields"], record.ledger_fields
        )

        assert service._required_auto_pass_fields(record) == (
            AUTO_PASS_REQUIRED_FIELDS - {"serial_number"}
        )
        assert not service.auto_pass_blockers(record, comparisons)
        assert all(
            item["result"] != "SUBSTANTIVE_DIFF"
            for item in comparisons
            if item["field"] in {"sample_number", "verification_record_id"}
        )

        record.metadata["qr_fields"].pop("verification_record_id")
        comparisons = service.rules.compare_three_way(
            record.fields, record.metadata["qr_fields"], record.ledger_fields
        )
        assert any(
            blocker.endswith(":serial_number")
            for blocker in service.auto_pass_blockers(record, comparisons)
        )
    finally:
        service.close()


def test_independent_identifier_pair_calls_both_models_then_auto_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, _ = _eligible_record(service)
        record.fields.pop("serial_number")
        record.fields["sample_number"] = FieldValue(
            "13004", "qwen_vision", confidence=.99, page=1, evidence="样品编号：13004"
        )
        record.qr_codes[0]["parsed_fields"].pop("serial_number")
        record.qr_codes[0]["parsed_fields"]["verification_record_id"] = "1010554082"
        record.metadata["qr_fields"] = dict(record.qr_codes[0]["parsed_fields"])
        comparisons = service.rules.compare_three_way(
            record.fields, record.metadata["qr_fields"], record.ledger_fields
        )
        glm = _SemanticProvider("glm")
        deepseek = _SemanticProvider("deepseek")
        service.providers["glm"] = glm
        service.providers["deepseek"] = deepseek
        monkeypatch.setattr(service, "_persist_model_decision", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            service, "_set_state",
            lambda target, state: setattr(target, "workflow_state", state),
        )
        monkeypatch.setattr(service.db, "add_ledger_change_proposal", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(service, "_store_record_snapshot", lambda *_args, **_kwargs: None)

        service._decide_advanced(record, comparisons)

        assert record.status == "AUTO_PASSED"
        assert record.final_decision == "PASS"
        assert len(record.model_decisions) == 2
        assert len(glm.calls) == len(deepseek.calls) == 1
        assert "GLM与DeepSeek独立确认" in record.final_reason
        deepseek_prompt = deepseek.calls[0]["messages"][0]["content"]
        assert "第一模型" in deepseek_prompt
        assert "glm-mock" not in deepseek_prompt
    finally:
        service.close()


def test_independent_identifier_pair_needs_review_without_two_model_consensus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, _ = _eligible_record(service)
        record.fields.pop("serial_number")
        record.fields["sample_number"] = FieldValue(
            "13004", "qwen_vision", confidence=.99, page=1, evidence="样品编号：13004"
        )
        record.qr_codes[0]["parsed_fields"].pop("serial_number")
        record.qr_codes[0]["parsed_fields"]["verification_record_id"] = "1010554082"
        record.metadata["qr_fields"] = dict(record.qr_codes[0]["parsed_fields"])
        comparisons = service.rules.compare_three_way(
            record.fields, record.metadata["qr_fields"], record.ledger_fields
        )
        glm = _SemanticProvider("glm")
        deepseek = _SemanticProvider("deepseek", "UNCERTAIN")
        service.providers["glm"] = glm
        service.providers["deepseek"] = deepseek
        monkeypatch.setattr(service, "_persist_model_decision", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            service, "_set_state",
            lambda target, state: setattr(target, "workflow_state", state),
        )
        monkeypatch.setattr(
            service, "_create_or_reuse_review_task",
            lambda target: setattr(target, "status", "HUMAN_REVIEW"),
        )

        service._decide_advanced(record, comparisons)

        assert record.status == "HUMAN_REVIEW"
        assert len(glm.calls) == len(deepseek.calls) == 1
        assert any(issue.code == "IDENTIFIER_MODEL_CONFIRMATION_REQUIRED" for issue in record.issues)
    finally:
        service.close()


def test_independent_identifier_models_still_run_when_another_blocker_needs_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, _ = _eligible_record(service)
        record.fields.pop("serial_number")
        record.fields["sample_number"] = FieldValue(
            "13004", "qwen_vision", confidence=.99, page=1, evidence="样品编号：13004"
        )
        record.qr_codes[0]["parsed_fields"].pop("serial_number")
        record.qr_codes[0]["parsed_fields"]["verification_record_id"] = "1010554082"
        record.metadata["qr_fields"] = dict(record.qr_codes[0]["parsed_fields"])
        record.issues.append(Issue(
            code="OTHER_REVIEW_REASON", severity="review",
            title="其他问题", detail="该问题仍需人工处理",
        ))
        comparisons = service.rules.compare_three_way(
            record.fields, record.metadata["qr_fields"], record.ledger_fields
        )
        glm = _SemanticProvider("glm")
        deepseek = _SemanticProvider("deepseek")
        service.providers["glm"] = glm
        service.providers["deepseek"] = deepseek
        monkeypatch.setattr(service, "_persist_model_decision", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            service, "_set_state",
            lambda target, state: setattr(target, "workflow_state", state),
        )
        monkeypatch.setattr(
            service, "_create_or_reuse_review_task",
            lambda target: setattr(target, "status", "HUMAN_REVIEW"),
        )

        service._decide_advanced(record, comparisons)

        assert record.status == "HUMAN_REVIEW"
        assert len(glm.calls) == len(deepseek.calls) == 1
        assert len(record.model_decisions) == 2
        assert record.metadata["independent_identifier_model_confirmed"] is True
    finally:
        service.close()


def test_non_substantive_review_blockers_are_sent_to_both_text_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, comparisons = _eligible_record(service)
        record.fields.pop("unified_number")
        record.issues.append(Issue(
            code="CRITICAL_FIELD_MISSING", severity="review",
            title="关键字段缺失", detail="缺少：unified_number",
        ))
        comparisons = service.rules.compare_three_way(
            record.fields, record.qr_codes[0]["parsed_fields"], record.ledger_fields
        )
        glm = _SemanticProvider("glm", "UNCERTAIN")
        deepseek = _SemanticProvider("deepseek", "UNCERTAIN")
        service.providers["glm"] = glm
        service.providers["deepseek"] = deepseek
        monkeypatch.setattr(service, "_persist_model_decision", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            service, "_set_state",
            lambda target, state: setattr(target, "workflow_state", state),
        )
        monkeypatch.setattr(
            service, "_create_or_reuse_review_task",
            lambda target: setattr(target, "status", "HUMAN_REVIEW"),
        )

        service._decide_advanced(record, comparisons)

        assert record.status == "HUMAN_REVIEW"
        assert len(glm.calls) == len(deepseek.calls) == 1
        assert len(record.model_decisions) == 2
        prompt = glm.calls[0]["messages"][0]["content"]
        assert "missing_document:unified_number" in prompt
        assert "不得建议自动通过" in prompt
    finally:
        service.close()


def test_missing_frozen_ledger_cannot_pass_even_with_two_equivalent_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, _ = _eligible_record(service)
        record.ledger_fields = {}
        qr_fields = record.qr_codes[0]["parsed_fields"]
        comparisons = service.rules.compare_three_way(record.fields, qr_fields, {})
        glm = _SemanticProvider("glm", "EQUIVALENT")
        deepseek = _SemanticProvider("deepseek", "EQUIVALENT")
        service.providers["glm"] = glm
        service.providers["deepseek"] = deepseek
        monkeypatch.setattr(service, "_persist_model_decision", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            service,
            "_set_state",
            lambda target, state: setattr(target, "workflow_state", state),
        )
        monkeypatch.setattr(
            service,
            "_create_or_reuse_review_task",
            lambda target: setattr(target, "status", "HUMAN_REVIEW"),
        )

        service._decide_advanced(record, comparisons)

        assert record.status == "HUMAN_REVIEW"
        assert record.final_decision is None
        assert len(glm.calls) == len(deepseek.calls) == 1
        assert any(issue.code == "FROZEN_LEDGER_MISSING" for issue in record.issues)
        assert "frozen_ledger_missing" in service.auto_pass_blockers(record, comparisons)
    finally:
        service.close()


def test_missing_frozen_ledger_cannot_auto_fail_with_two_high_risk_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        record, _ = _eligible_record(service)
        record.ledger_fields = {}
        qr_fields = dict(record.qr_codes[0]["parsed_fields"])
        qr_fields["certificate_number"] = "CONFLICTING-CERTIFICATE"
        comparisons = service.rules.compare_three_way(record.fields, qr_fields, {})
        glm = _SemanticProvider("glm", "INCONSISTENT", risk="HIGH")
        deepseek = _SemanticProvider("deepseek", "INCONSISTENT", risk="HIGH")
        service.providers["glm"] = glm
        service.providers["deepseek"] = deepseek
        monkeypatch.setattr(service, "_persist_model_decision", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            service,
            "_set_state",
            lambda target, state: setattr(target, "workflow_state", state),
        )
        monkeypatch.setattr(
            service,
            "_create_or_reuse_review_task",
            lambda target: setattr(target, "status", "HUMAN_REVIEW"),
        )
        monkeypatch.setattr(service.db, "add_ledger_change_proposal", lambda *_a, **_k: None)
        monkeypatch.setattr(service, "_store_record_snapshot", lambda *_args, **_kwargs: None)

        service._decide_advanced(record, comparisons)

        assert record.status == "HUMAN_REVIEW"
        assert record.final_decision is None
        assert len(glm.calls) == len(deepseek.calls) == 1
        assert service.auto_fail_blockers(record) == ["frozen_ledger_missing"]
        assert any(issue.code == "FROZEN_LEDGER_MISSING" for issue in record.issues)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("source_has_ledger", "target_has_ledger", "expected_missing_issue"),
    [
        (True, False, True),
        (False, True, False),
    ],
)
def test_sha_cache_regenerates_frozen_ledger_issue_from_target_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    source_has_ledger: bool,
    target_has_ledger: bool,
    expected_missing_issue: bool,
) -> None:
    service = BatchReviewService(AppConfig(model_mode="disabled"))
    try:
        fields = {
            "certificate_number": FieldValue("CERT-001", "qwen_vision", confidence=.99),
        }
        source = ReviewRecord(
            record_id="source",
            filename="source.pdf",
            sha256="c" * 64,
            size_bytes=1,
            file_type="pdf",
            fields=fields,
            qr_codes=[{
                "stable_multiscale": True,
                "parsed_fields": {"certificate_number": "CERT-001"},
            }],
            ledger_fields={"certificate_number": "CERT-001"} if source_has_ledger else {},
            issues=([] if source_has_ledger else [Issue(
                code="FROZEN_LEDGER_MISSING",
                severity="review",
                title="缺少冻结台账证据",
                detail="source-only",
            )]),
        )
        target = ReviewRecord(
            record_id="target",
            filename="target.pdf",
            sha256=source.sha256,
            size_bytes=1,
            file_type="pdf",
            duplicate_of=source.record_id,
            ledger_fields={"certificate_number": "CERT-001"} if target_has_ledger else {},
        )
        monkeypatch.setattr(
            service.db,
            "create_extraction_run",
            lambda *_args, **_kwargs: {"id": "cache-run"},
        )
        monkeypatch.setattr(service.db, "finish_extraction_run", lambda *_a, **_k: None)
        monkeypatch.setattr(service.db, "add_comparison", lambda *_a, **_k: None)
        monkeypatch.setattr(service, "_persist_fields", lambda *_a, **_k: None)
        monkeypatch.setattr(
            service,
            "_create_or_reuse_review_task",
            lambda record: setattr(record, "status", "HUMAN_REVIEW"),
        )

        service._reuse_cached_result(source, target)

        ledger_issues = [
            issue for issue in target.issues if issue.code == "FROZEN_LEDGER_MISSING"
        ]
        assert bool(ledger_issues) is expected_missing_issue
        assert len(ledger_issues) <= 1
    finally:
        service.close()


def test_every_review_or_error_issue_and_unparseable_qr_blocks_auto_pass() -> None:
    service = BatchReviewService(AppConfig(model_mode="validation"))
    service.model_mode = "validation"
    try:
        for code in ("QR_NO_COMPARABLE_FIELDS", "FUTURE_DATE", "DATE_ORDER_INVALID", "INVALID_DATE_FORMAT"):
            record, comparisons = _eligible_record(service)
            record.issues.append(Issue(code=code, severity="review", title=code, detail=code))
            assert "review_or_error_issue" in service.auto_pass_blockers(record, comparisons)

        record, comparisons = _eligible_record(service)
        record.qr_codes[0]["parsed_fields"] = {}
        assert "qr_no_comparable_fields" in service.auto_pass_blockers(record, comparisons)

        record, comparisons = _eligible_record(service)
        record.issues.append(Issue(code="AUTHENTICITY_UNVERIFIED", severity="info", title="info", detail="info"))
        assert "review_or_error_issue" not in service.auto_pass_blockers(record, comparisons)
    finally:
        service.close()


def test_all_twelve_anomaly_fixtures_have_zero_false_release() -> None:
    fixtures = json.loads(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "anomaly-fixtures-12.synthetic.json"
        ).read_text(encoding="utf-8")
    )
    result = validate(fixtures)
    assert result["fixture_count"] == 12
    assert result["zero_false_release"] is True
    assert all(item["comparison_result"] == "SUBSTANTIVE_DIFF" for item in result["results"])
