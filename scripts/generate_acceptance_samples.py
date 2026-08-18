from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def make_qr(payload: dict[str, str], output: Path) -> None:
    encoder = cv2.QRCodeEncoder_create()
    encoded = encoder.encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    encoded = cv2.copyMakeBorder(encoded, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    encoded = cv2.resize(encoded, (600, 600), interpolation=cv2.INTER_NEAREST)
    if not cv2.imwrite(str(output), encoded):
        raise RuntimeError("无法写入二维码样例")


def make_certificate(output: Path, certificate_number: str, qr_certificate_number: str | None) -> None:
    font_name = "STSong-Light"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    page_width, page_height = A4
    doc = canvas.Canvas(str(output), pagesize=A4, pageCompression=1)
    doc.setTitle("离线审查验收样例")
    doc.setFont(font_name, 22)
    doc.drawCentredString(page_width / 2, page_height - 60, "测试计量技术中心 校准证书")
    doc.setFont(font_name, 12)
    fields = [
        ("证书编号", certificate_number),
        ("发证机构", "测试计量技术中心"),
        ("签发日期", "2026年08月12日"),
        ("校准日期", "2026-08-11"),
        ("客户名称", "示例制造有限公司"),
        ("器具名称", "数字温度计"),
        ("器具编号", "DEMO-TM-001"),
        ("型号/规格", "TM-100"),
        ("制造单位", "示例仪器厂"),
    ]
    y = page_height - 125
    for label, value in fields:
        doc.drawString(72, y, f"{label}：{value}")
        y -= 30
    doc.setStrokeColorRGB(0.6, 0.65, 0.63)
    doc.line(72, y - 6, page_width - 72, y - 6)
    doc.setFont(font_name, 10)
    doc.drawString(72, y - 32, "本文件仅用于本地批量审查功能验收，不代表任何真实证书。")

    if qr_certificate_number:
        payload = {
            "certificateNo": qr_certificate_number,
            "issueDate": "2026-08-12",
            "serialNo": "DEMO-TM-001",
        }
        with TemporaryDirectory() as temp_dir:
            qr_path = Path(temp_dir) / "qr.png"
            make_qr(payload, qr_path)
            doc.drawImage(str(qr_path), page_width - 210, 72, width=130, height=130, mask="auto")
    doc.setFont(font_name, 9)
    doc.drawCentredString(page_width / 2, 40, "第 1 页 / 共 1 页")
    doc.save()


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    make_certificate(SAMPLES / "01-字段一致-应通过.pdf", "DEMO-CERT-2026-001", "DEMO-CERT-2026-001")
    make_certificate(SAMPLES / "02-二维码编号不一致-需复核.pdf", "DEMO-CERT-2026-002", "DEMO-CERT-2026-999")
    make_certificate(SAMPLES / "03-无二维码-需复核.pdf", "DEMO-CERT-2026-003", None)
    (SAMPLES / "04-损坏文件-应失败.pdf").write_bytes(b"not-a-valid-pdf")
    print(f"generated 4 samples in {SAMPLES}")


if __name__ == "__main__":
    main()
