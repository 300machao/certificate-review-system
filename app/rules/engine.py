from __future__ import annotations

from datetime import date

from app.config import AppConfig
from app.models import ExtractionResult, FieldComparison, Issue
from app.rules.fields import normalize_date, normalize_field


FIELD_LABELS = {
    "certificate_number": "证书编号",
    "issuer_name": "机构名称",
    "issue_date": "签发日期",
    "calibration_date": "校准日期",
    "client_name": "客户/持有人",
    "instrument_name": "器具/产品名称",
    "serial_number": "器具/产品编号",
    "sample_number": "样品编号（机构内部）",
    "verification_record_id": "二维码验真记录号",
    "manufacturer": "制造单位",
    "model": "型号/规格",
    "unified_number": "统一编号",
    "due_date": "有效期",
    "verification_result": "检定/校准结论",
    "reference_document": "检定/校准依据",
    "traceability": "溯源性信息",
    "seal_status": "印章状态",
    "signature_status": "签名状态",
}

KEY_FIELDS = {
    "certificate_number", "instrument_name", "unified_number", "model",
    "serial_number", "calibration_date", "due_date", "issuer_name",
}

THREE_WAY_FIELDS = KEY_FIELDS | {"verification_result"}
INDEPENDENT_IDENTIFIER_FIELDS = {"sample_number", "verification_record_id"}


class RuleEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def review(
        self,
        extraction: ExtractionResult,
        candidate_values: dict[str, list[str]],
    ) -> tuple[str, list[Issue], list[FieldComparison]]:
        issues: list[Issue] = []
        comparisons: list[FieldComparison] = []
        fields = extraction.fields

        if not any(page.strip() for page in extraction.text_pages):
            issues.append(Issue(
                code="EMPTY_TEXT",
                severity="review",
                title="未提取到版面文字",
                detail="文件可打开，但没有可用文字层；请安装本地 OCR 或人工复核。",
            ))

        for field in self.config.required_fields:
            if field not in fields:
                issues.append(Issue(
                    code="REQUIRED_FIELD_MISSING",
                    severity="review",
                    title=f"缺少必填字段：{FIELD_LABELS.get(field, field)}",
                    detail="规则未从报告版面中提取到该字段。",
                    field=field,
                ))

        for field, values in candidate_values.items():
            if len(values) > 1:
                issues.append(Issue(
                    code="DOCUMENT_FIELD_CONFLICT",
                    severity="review",
                    title=f"报告内部字段冲突：{FIELD_LABELS.get(field, field)}",
                    detail=f"不同位置出现 {len(values)} 个不一致值。",
                    field=field,
                    evidence=" | ".join(values[:5]),
                ))

        for field in ("issue_date", "calibration_date", "receive_date"):
            value = fields.get(field)
            if not value:
                continue
            normalized = normalize_date(value.value)
            try:
                parsed = date.fromisoformat(normalized)
                if parsed > date.today():
                    issues.append(Issue(
                        code="FUTURE_DATE",
                        severity="review",
                        title=f"日期晚于当前日期：{FIELD_LABELS.get(field, field)}",
                        detail=f"提取值为 {normalized}，请确认报告是否为未来签发/录入。",
                        field=field,
                        page=value.page,
                        evidence=value.value,
                    ))
            except ValueError:
                issues.append(Issue(
                    code="INVALID_DATE_FORMAT",
                    severity="review",
                    title=f"日期格式异常：{FIELD_LABELS.get(field, field)}",
                    detail=f"无法解析日期 {value.value}。",
                    field=field,
                    page=value.page,
                    evidence=value.value,
                ))

        if "issue_date" in fields and "calibration_date" in fields:
            issue_date = normalize_date(fields["issue_date"].value)
            calibration_date = normalize_date(fields["calibration_date"].value)
            try:
                if date.fromisoformat(issue_date) < date.fromisoformat(calibration_date):
                    issues.append(Issue(
                        code="DATE_ORDER_INVALID",
                        severity="review",
                        title="签发日期早于校准日期",
                        detail=f"校准日期 {calibration_date}，签发日期 {issue_date}。",
                        field="issue_date",
                    ))
            except ValueError:
                pass

        if not extraction.qr_codes:
            issues.append(Issue(
                code="QR_NOT_FOUND",
                severity="review",
                title="未识别到二维码",
                detail="无法执行二维码内容与版面字段的交叉核验。",
            ))
        else:
            qr_fields: dict[str, str] = {}
            conflicting_qr_fields: set[str] = set()
            for qr in extraction.qr_codes:
                for key, value in qr.get("parsed_fields", {}).items():
                    if key in qr_fields and normalize_field(key, qr_fields[key]) != normalize_field(key, str(value)):
                        conflicting_qr_fields.add(key)
                    else:
                        qr_fields.setdefault(key, str(value))
            for field in sorted(conflicting_qr_fields):
                issues.append(Issue(
                    code="QR_FIELD_CONFLICT",
                    severity="review",
                    title=f"多个二维码字段冲突：{FIELD_LABELS.get(field, field)}",
                    detail="同一文件内二维码给出了不一致的值。",
                    field=field,
                ))

            common = sorted(set(qr_fields) & set(fields))
            for field in common:
                document_value = fields[field].value
                qr_value = qr_fields[field]
                matched = normalize_field(field, document_value) == normalize_field(field, qr_value)
                comparisons.append(FieldComparison(
                    field=field,
                    document_value=document_value,
                    qr_value=qr_value,
                    status="MATCH" if matched else "MISMATCH",
                    basis="规范化后精确匹配（仅证明内容一致，不证明真实有效）",
                ))
                if not matched:
                    issues.append(Issue(
                        code="QR_TEXT_MISMATCH",
                        severity="review",
                        title=f"二维码与版面不一致：{FIELD_LABELS.get(field, field)}",
                        detail=f"版面值“{document_value}”，二维码值“{qr_value}”。",
                        field=field,
                        page=fields[field].page,
                    ))
            if not common:
                issues.append(Issue(
                    code="QR_NO_COMPARABLE_FIELDS",
                    severity="review",
                    title="二维码缺少可比字段",
                    detail="二维码已解码，但未解析出可与版面字段直接比较的证书编号、日期或主体信息。",
                ))

        for warning in extraction.warnings:
            issues.append(Issue(
                code="EXTRACTION_WARNING",
                severity="review",
                title="提取警告",
                detail=warning,
            ))

        issues.append(Issue(
            code="AUTHENTICITY_UNVERIFIED",
            severity="info",
            title="真实性未核验",
            detail="字段一致只表示二维码内容与版面文字相符，不能单独证明证书真实有效。",
        ))
        status = "REVIEW" if any(item.severity in {"review", "error"} for item in issues) else "PASS"
        return status, issues, comparisons

    def compare_three_way(
        self,
        document_fields: dict[str, object],
        qr_fields: dict[str, str],
        ledger_fields: dict[str, str],
    ) -> list[dict[str, str]]:
        """Compare document, QR and frozen ledger without fuzzy guessing."""
        results: list[dict[str, str]] = []
        # Include every mandatory comparison field even when all three channels
        # omit it. Otherwise an entirely missing key would disappear from the
        # result set and could accidentally bypass an automatic-pass gate.
        all_fields = sorted(
            set(document_fields) | set(qr_fields) | set(ledger_fields) | THREE_WAY_FIELDS
        )
        for field in all_fields:
            document_item = document_fields.get(field)
            document_value = str(getattr(document_item, "value", document_item or ""))
            qr_value = str(qr_fields.get(field) or "")
            ledger_value = str(ledger_fields.get(field) or "")
            if field in INDEPENDENT_IDENTIFIER_FIELDS:
                results.append({
                    "field": field,
                    "document_value": document_value,
                    "qr_value": qr_value,
                    "ledger_value": ledger_value,
                    "result": "NOT_COMPARABLE",
                    "severity": "LOW",
                    "rule_version": "three-way-v2",
                })
                continue
            present = [value for value in (document_value, qr_value, ledger_value) if value]
            if len(present) < 2:
                outcome = "MISSING"
            else:
                normalized = [normalize_field(field, value) for value in present]
                if len(set(normalized)) == 1:
                    outcome = "CONSISTENT" if len(set(present)) == 1 else "FORM_EQUIVALENT"
                else:
                    outcome = "SUBSTANTIVE_DIFF"
            results.append({
                "field": field,
                "document_value": document_value,
                "qr_value": qr_value,
                "ledger_value": ledger_value,
                "result": outcome,
                "severity": "HIGH" if field in KEY_FIELDS and outcome == "SUBSTANTIVE_DIFF" else (
                    "MEDIUM" if outcome in {"SUBSTANTIVE_DIFF", "MISSING"} else "LOW"
                ),
                "rule_version": "three-way-v2",
            })
        return results
