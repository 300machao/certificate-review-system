from __future__ import annotations

import ctypes
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
import webview

from app.api import app, service


APP_TITLE = "证书与报告批量审查"
APP_ID = "CertificateReportReview"


def application_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_ID
    return Path(os.environ.get("TEMP", ".")) / APP_ID


def configure_logging() -> Path:
    log_dir = application_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desktop.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
        force=True,
    )
    return log_path


def show_startup_error(message: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, message, f"{APP_TITLE} - 启动失败", 0x10)
    else:
        print(message, file=sys.stderr)


class LocalApplicationServer:
    """Runs the existing FastAPI app on a process-private random loopback port."""

    def __init__(self) -> None:
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("本地服务尚未启动")
        return f"http://127.0.0.1:{self.port}/"

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.started and self._thread and self._thread.is_alive())

    def start(self, timeout: float = 15.0) -> str:
        if self.running:
            return self.url
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        self.port = int(listener.getsockname()[1])
        self._socket = listener
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_config=None,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._run,
            name="certificate-review-local-server",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.running:
                return self.url
            if not self._thread.is_alive():
                break
            time.sleep(0.05)
        self.stop()
        raise RuntimeError("本地审查服务未能在规定时间内启动")

    def _run(self) -> None:
        assert self._server is not None and self._socket is not None
        try:
            self._server.run(sockets=[self._socket])
        except Exception:
            logging.getLogger(__name__).exception("Local application server failed")

    def stop(self, timeout: float = 8.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        self._thread = None
        self._server = None
        self.port = None


class DesktopBridge:
    """Narrow JS bridge for native save dialogs; it cannot read arbitrary files."""

    def __init__(self) -> None:
        self.window: Any | None = None

    def attach_window(self, window: Any) -> None:
        self.window = window

    def app_info(self) -> dict[str, Any]:
        return {
            "desktop": True,
            "version": "1.1.0",
            "authoritative_verification_configured": service.authenticity.authoritative,
        }

    def save_export(self, batch_id: str, export_format: str) -> dict[str, Any]:
        if self.window is None:
            return {"ok": False, "cancelled": False, "message": "桌面窗口尚未就绪"}
        export_format = export_format.lower().strip()
        if export_format not in {"json", "csv"}:
            return {"ok": False, "cancelled": False, "message": "不支持的导出格式"}
        try:
            payload = (
                service.export_json(batch_id)
                if export_format == "json"
                else service.export_csv(batch_id)
            )
        except KeyError:
            return {"ok": False, "cancelled": False, "message": "批次不存在或已过期"}
        except Exception:
            logging.getLogger(__name__).exception("Export preparation failed")
            return {"ok": False, "cancelled": False, "message": "生成导出内容失败"}

        filters = (
            "JSON 文件 (*.json)",
            "所有文件 (*.*)",
        ) if export_format == "json" else (
            "CSV 文件 (*.csv)",
            "所有文件 (*.*)",
        )
        selection = self.window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=f"review-{batch_id}.{export_format}",
            file_types=filters,
        )
        if not selection:
            return {"ok": False, "cancelled": True, "message": "已取消保存"}
        destination = Path(selection[0])
        if destination.suffix.lower() != f".{export_format}":
            destination = destination.with_suffix(f".{export_format}")
        try:
            destination.write_bytes(payload)
        except OSError:
            logging.getLogger(__name__).exception("Export write failed")
            return {"ok": False, "cancelled": False, "message": "写入文件失败，请检查目录权限或文件占用"}
        return {
            "ok": True,
            "cancelled": False,
            "message": f"已保存：{destination.name}",
            "filename": destination.name,
        }


def run_desktop() -> int:
    log_path = configure_logging()
    logger = logging.getLogger(__name__)
    server = LocalApplicationServer()
    try:
        url = server.start()
        bridge = DesktopBridge()
        webview.settings["ALLOW_DOWNLOADS"] = False
        window = webview.create_window(
            APP_TITLE,
            url=url,
            js_api=bridge,
            width=1280,
            height=820,
            min_size=(960, 680),
            resizable=True,
            background_color="#F3F6F4",
            text_select=True,
            zoomable=False,
        )
        if window is None:
            raise RuntimeError("无法创建桌面窗口")
        bridge.attach_window(window)
        window.events.closed += lambda: server.stop()
        logger.info("Desktop application started")
        webview.start(gui="edgechromium", debug=False, private_mode=True)
        return 0
    except Exception as exc:
        logger.exception("Desktop application startup failed")
        show_startup_error(
            f"{APP_TITLE}无法启动。\n\n{str(exc) or type(exc).__name__}\n\n"
            f"诊断日志：{log_path}"
        )
        return 1
    finally:
        server.stop()


def main() -> None:
    raise SystemExit(run_desktop())


if __name__ == "__main__":
    main()
