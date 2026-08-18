from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Database
from app.model_settings import ModelSettingsStore
from app.providers import create_model_providers
from app.semantic import arbitrate_differences, review_differences

def _current_cycle_start(runs: list[dict[str, Any]]) -> str | None:
    starts = [
        str(run.get("started_at"))
        for run in runs
        if run.get("provider") == "local_pdf_qr" and run.get("started_at")
    ]
    return max(starts) if starts else None


def _current_roles(raw: dict[str, Any]) -> set[str]:
    started_at = _current_cycle_start(raw.get("extraction_runs") or [])
    return {
        str(item.get("role"))
        for item in raw.get("model_decisions") or []
        if not started_at or str(item.get("created_at") or "") >= started_at
    }


def _evidence(raw: dict[str, Any]) -> dict[str, Any]:
    certificate = raw["certificate"]
    review = (certificate.get("metadata") or {}).get("review") or {}
    issues = [
        {
            "code": item.get("code"),
            "severity": item.get("severity"),
            "field": item.get("field"),
            "title": item.get("title"),
            "detail": item.get("detail"),
        }
        for item in review.get("issues") or []
        if item.get("severity") in {"review", "error"}
    ]
    comparisons = [
        item
        for item in ((review.get("metadata") or {}).get("three_way_comparisons") or [])
        if isinstance(item, dict)
        and item.get("result") not in {"CONSISTENT", "FORM_EQUIVALENT"}
    ]
    blockers: list[str] = []
    for issue in issues:
        field = str(issue.get("field") or "").strip()
        if issue.get("code") in {"CRITICAL_FIELD_MISSING", "REQUIRED_FIELD_MISSING"}:
            blockers.append(f"missing_document:{field or 'unspecified'}")
        else:
            blockers.append(f"issue:{issue.get('code') or 'UNKNOWN'}")
    return {
        "review_type": "UNRESOLVED_RULE_BLOCKERS",
        "certificate_sha256": certificate.get("sha256"),
        "blockers": list(dict.fromkeys(blockers)),
        "issues": issues,
        "differences": comparisons,
        "instruction": (
            "分析这些复核原因是否能由现有证据消除。关键字段或冻结台账缺失时必须判为"
            "UNCERTAIN，不得猜测、补造字段，也不得建议自动通过。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="为已完成的人工复核记录补充文本模型判断")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    db = Database(ROOT / "data" / "certificate-review.sqlite3")
    providers = create_model_providers(ModelSettingsStore(ROOT / "data").load())
    glm_limit = threading.BoundedSemaphore(4)
    deepseek_limit = threading.BoundedSemaphore(2)
    write_lock = threading.Lock()
    try:
        certificates = [
            item for item in db.list_certificates(args.batch_id)
            if item.get("status") == "HUMAN_REVIEW"
        ]
        jobs: list[tuple[dict[str, Any], set[str]]] = []
        for certificate in certificates:
            raw = db.get_certificate_detail(certificate["id"])
            if raw is None:
                continue
            roles = _current_roles(raw)
            if {"PRIMARY", "ARBITER"}.issubset(roles):
                continue
            jobs.append((raw, roles))

        print(json.dumps({"pending": len(jobs)}, ensure_ascii=False), flush=True)

        def process(job: tuple[dict[str, Any], set[str]]) -> dict[str, Any]:
            raw, existing_roles = job
            certificate = raw["certificate"]
            certificate_id = certificate["id"]
            evidence = _evidence(raw)
            completed: list[str] = []
            failures: list[str] = []
            specs = (
                ("PRIMARY", "glm", providers["glm"], review_differences, glm_limit),
                ("ARBITER", "deepseek", providers["deepseek"], arbitrate_differences, deepseek_limit),
            )
            for role, kind, provider, reviewer, limit in specs:
                if role in existing_roles:
                    continue
                try:
                    with limit:
                        decision = reviewer(provider, evidence)
                    with write_lock:
                        db.add_model_decision(
                            certificate_id, role, decision.provider, decision.model,
                            decision.decision, risk=decision.risk,
                            confidence=decision.confidence, reason=decision.reason,
                            evidence=evidence, usage=decision.usage,
                            latency_ms=decision.latency_ms,
                            prompt_version=decision.prompt_version,
                        )
                    completed.append(kind)
                except Exception as exc:
                    failures.append(f"{kind}:{type(exc).__name__}")
            return {
                "certificate_id": certificate_id,
                "completed": completed,
                "failures": failures,
            }

        succeeded = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
            futures = [pool.submit(process, job) for job in jobs]
            for index, future in enumerate(as_completed(futures), 1):
                result = future.result()
                if result["failures"]:
                    failed += 1
                else:
                    succeeded += 1
                if index % 5 == 0 or index == len(futures):
                    print(json.dumps({
                        "processed": index,
                        "total": len(futures),
                        "succeeded": succeeded,
                        "failed": failed,
                    }, ensure_ascii=False), flush=True)
        return 1 if failed else 0
    finally:
        db.close()
        for provider in providers.values():
            provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
