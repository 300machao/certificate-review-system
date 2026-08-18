from __future__ import annotations

import json
from pathlib import Path
from urllib.request import urlopen

import pytest

import app.desktop as desktop_module
from app.desktop import DesktopBridge, LocalApplicationServer


class FakeWindow:
    def __init__(self, destination: Path | None) -> None:
        self.destination = destination

    def create_file_dialog(self, *args, **kwargs):
        return (str(self.destination),) if self.destination else None


class FakeService:
    def export_json(self, batch_id: str) -> bytes:
        if batch_id == "missing":
            raise KeyError(batch_id)
        return json.dumps({"batch_id": batch_id}, ensure_ascii=False).encode("utf-8")

    def export_csv(self, batch_id: str) -> bytes:
        return b"\xef\xbb\xbfbatch_id\r\n" + batch_id.encode("utf-8") + b"\r\n"


def test_local_server_starts_on_loopback_and_stops() -> None:
    server = LocalApplicationServer()
    try:
        url = server.start(timeout=10)
        assert url.startswith("http://127.0.0.1:")
        with urlopen(url + "api/capabilities", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["version"] == "2.0.0"
        assert server.running is True
    finally:
        server.stop()
    assert server.running is False


def test_desktop_bridge_uses_native_save_dialog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_module, "service", FakeService())
    destination = tmp_path / "result"
    bridge = DesktopBridge()
    bridge.attach_window(FakeWindow(destination))
    result = bridge.save_export("batch-123", "json")
    assert result == {
        "ok": True,
        "cancelled": False,
        "message": "已保存：result.json",
        "filename": "result.json",
    }
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == {
        "batch_id": "batch-123"
    }


def test_desktop_bridge_reports_cancel_and_invalid_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_module, "service", FakeService())
    bridge = DesktopBridge()
    bridge.attach_window(FakeWindow(None))
    assert bridge.save_export("batch-123", "csv")["cancelled"] is True
    invalid = bridge.save_export("batch-123", "xlsx")
    assert invalid["ok"] is False
    assert "不支持" in invalid["message"]
