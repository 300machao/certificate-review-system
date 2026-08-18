from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# API imports create the application service at module load time. Point it at a
# session-scoped temporary root before test modules import app.api, so automated
# tests can never write the delivered production SQLite database.
_requested_data_dir = os.environ.get("CERT_DATA_DIR")
if _requested_data_dir:
    _test_data_dir = Path(_requested_data_dir).expanduser().resolve()
    if _test_data_dir == ROOT or ROOT in _test_data_dir.parents:
        raise RuntimeError("CERT_DATA_DIR for tests must be outside the project tree")
    _test_data_dir.mkdir(parents=True, exist_ok=True)
else:
    _test_data_dir = Path(tempfile.mkdtemp(prefix="certificate-review-tests-")).resolve()

os.environ["CERT_DATA_DIR"] = str(_test_data_dir)
os.environ["CERT_MODEL_MODE"] = "disabled"
for _secret_name in (
    "CERT_QWEN_API_KEY",
    "CERT_GLM_API_KEY",
    "CERT_ARBITER_API_KEY",
):
    os.environ.pop(_secret_name, None)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
