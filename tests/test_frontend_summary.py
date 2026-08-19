from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SUMMARY_MODULE = ROOT / "static" / "review-summary.js"


def _attention_count(payload: dict) -> int:
    script = (
        "const summary=require(process.argv[1]);"
        "const payload=JSON.parse(process.argv[2]);"
        "process.stdout.write(String(summary.countActiveConcerns(payload)));"
    )
    result = subprocess.run(
        ["node", "-e", script, str(SUMMARY_MODULE), json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout)


def test_missing_comparison_and_review_issue_both_count_as_attention() -> None:
    assert _attention_count({
        "comparisons": [
            {"field": "issue_date", "result": "MISSING"},
            {"field": "certificate_number", "result": "CONSISTENT"},
        ],
        "issues": [
            {"code": "REVIEW_NEEDED", "severity": "review"},
            {"code": "AUTHENTICITY_UNVERIFIED", "severity": "info"},
        ],
    }) == 2


def test_uncertain_model_warning_and_verification_failure_count_as_attention() -> None:
    assert _attention_count({
        "comparisons": [],
        "issues": [{"code": "QWEN_VISION_WARNING", "severity": "warning"}],
        "model_decisions": [{"decision": "UNCERTAIN", "risk": "MEDIUM"}],
        "authenticity_status": "VERIFICATION_FAILED",
    }) == 3


@pytest.mark.parametrize("status", ["UNVERIFIED", "VERIFICATION_FAILED"])
def test_real_detail_authenticity_structure_counts_unresolved_status(status: str) -> None:
    assert _attention_count({
        "certificate": {
            "authenticity": {"status": status, "method": "local_evidence_only"},
            "authenticity_status": status,
        },
        "authenticity": {"status": status, "method": "local_evidence_only"},
        "authenticity_status": status,
    }) == 1


def test_same_field_comparison_issue_and_model_opinion_are_one_business_concern() -> None:
    assert _attention_count({
        "comparisons": [{"field": "issue_date", "result": "MISSING"}],
        "issues": [{
            "code": "REQUIRED_FIELD_MISSING",
            "severity": "review",
            "field": "issue_date",
        }],
        "model_decisions": [{
            "decision": "UNCERTAIN",
            "risk": "MEDIUM",
            "evidence": {"differences": [{"field": "issue_date"}]},
        }],
    }) == 1
