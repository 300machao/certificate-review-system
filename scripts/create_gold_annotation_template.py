from __future__ import annotations

import argparse
import json
from pathlib import Path


FIELDS = (
    "certificate_type", "certificate_number", "issuer_name", "client_name",
    "instrument_name", "unified_number", "model", "serial_number", "manufacturer",
    "calibration_date", "issue_date", "due_date", "verification_result",
    "reference_document", "traceability", "seal_status", "signature_status",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="为锁定的20份金标集创建人工标注空模板")
    parser.add_argument("selection", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    documents = []
    for item in selection["documents"]:
        documents.append({
            "gold_id": item["gold_id"],
            "sha256": item["sha256"],
            "category": item["category"],
            "local_path": item["local_path"],
            "annotation_status": "PENDING_HUMAN_ANNOTATION",
            "annotator": None,
            "annotated_at": None,
            "fields": {
                key: {"value": None, "page": None, "evidence": None, "confirmed": False}
                for key in FIELDS
            },
            "notes": None,
        })
    payload = {
        "schema_version": "1.0",
        "locked_selection": True,
        "prompt_tuning_allowed": False,
        "annotation_status": "PENDING_HUMAN_ANNOTATION",
        "document_count": len(documents),
        "documents": documents,
        "metric_note": "全部人工字段确认后方可计算关键字段准确率和拒答率。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "count": len(documents)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
