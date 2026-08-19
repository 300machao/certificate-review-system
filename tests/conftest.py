from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_TEMP = Path(tempfile.gettempdir()).resolve()


def _project_data_directories() -> set[Path]:
    directories = {(ROOT / "data").resolve()}
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                directories.add((Path(line.removeprefix("worktree ")) / "data").resolve())
    return directories


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents

# API imports create the application service at module load time. Point it at a
# session-scoped temporary root before test modules import app.api, so automated
# tests can never write the delivered production SQLite database.
_requested_data_dir = os.environ.get("CERT_DATA_DIR")
if _requested_data_dir:
    _test_data_dir = Path(_requested_data_dir).expanduser().resolve()
    if any(_is_within(_test_data_dir, item) for item in _project_data_directories()):
        raise RuntimeError("CERT_DATA_DIR for tests must never target a project data directory")
    if _test_data_dir.parent != SYSTEM_TEMP:
        raise RuntimeError("CERT_DATA_DIR for tests must be a direct child of the system Temp directory")
    if _test_data_dir.exists():
        raise RuntimeError("CERT_DATA_DIR for tests must be a new unique path that does not exist")
    _test_data_dir.mkdir()
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


@pytest.fixture(autouse=True)
def reject_real_network_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow connections only to loopback ports bound inside the current test.

    Provider contract tests use in-memory mock transports, while API tests use
    Starlette's in-process TestClient.  A test that needs a real loopback socket
    must bind its own listener first; pre-existing services such as 8766 remain
    unreachable even though they listen on loopback.
    """

    original_bind = socket.socket.bind
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    allowed_ports: set[int] = set()

    def loopback(address: object) -> bool:
        return (
            isinstance(address, tuple)
            and bool(address)
            and str(address[0]).casefold() in {"127.0.0.1", "::1", "localhost"}
        )

    def guarded_bind(sock: socket.socket, address: object) -> object:
        result = original_bind(sock, address)
        if loopback(address):
            bound = sock.getsockname()
            if isinstance(bound, tuple) and len(bound) > 1:
                allowed_ports.add(int(bound[1]))
        return result

    def allowed_connection(address: object) -> bool:
        return (
            loopback(address)
            and isinstance(address, tuple)
            and len(address) > 1
            and int(address[1]) in allowed_ports
        )

    def guarded_connect(sock: socket.socket, address: object) -> object:
        if not allowed_connection(address):
            raise AssertionError(
                f"tests may only connect to loopback ports bound by the current test: {address!r}"
            )
        return original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: object) -> int:
        if not allowed_connection(address):
            raise AssertionError(
                f"tests may only connect to loopback ports bound by the current test: {address!r}"
            )
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "bind", guarded_bind)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
