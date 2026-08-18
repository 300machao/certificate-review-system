from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import load_config
from app.service import BatchReviewService, prepare_local_files


def main() -> int:
    parser = argparse.ArgumentParser(description="离线批量审查证书/报告")
    parser.add_argument("inputs", nargs="+", type=Path, help="待审查 PDF 或图片")
    parser.add_argument("--output", type=Path, default=Path("review-result.json"))
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args()

    service = BatchReviewService(load_config())
    incoming, temp_dir = prepare_local_files(args.inputs)
    batch = service.create_batch(incoming, temp_dir)
    service.process_batch(batch.batch_id, temp_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(service.export_json(batch.batch_id))
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        args.csv.write_bytes(service.export_csv(batch.batch_id))
    print(json.dumps(batch.summary(), ensure_ascii=False))
    return 0 if batch.summary()["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
