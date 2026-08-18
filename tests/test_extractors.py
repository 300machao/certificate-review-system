from __future__ import annotations

from pathlib import Path

from app.config import load_config
from app.extractors.ocr import create_ocr_provider
from app.extractors.pdf import PDFExtractor
from app.extractors.qr import QRDecoder, parse_qr_payload
from app.rules.fields import FieldExtractor


ROOT = Path(__file__).resolve().parents[1]


def test_qr_payload_parses_json_and_url_fragment() -> None:
    json_fields = parse_qr_payload(
        '{"certificateNo":"CERT-001","issueDate":"2026-08-12","serialNo":"SN-9"}'
    )
    assert json_fields == {
        "certificate_number": "CERT-001",
        "issue_date": "2026-08-12",
        "serial_number": "SN-9",
    }
    url_fields = parse_qr_payload(
        "https://example.invalid/#/verify/10001/校准字第DEMO202600001号"
    )
    assert url_fields["certificate_number"] == "校准字第DEMO202600001号"


def test_verification_route_record_id_is_not_factory_serial_number() -> None:
    fields = parse_qr_payload(
        "https://example.invalid/CertificateVerification/1010525285/校准字第202600001号"
    )

    assert fields["verification_record_id"] == "1010525285"
    assert fields["certificate_number"] == "校准字第202600001号"
    assert "serial_number" not in fields


def test_matching_sample_extracts_fields_and_qr() -> None:
    config = load_config(ROOT / "config" / "app.json")
    result = PDFExtractor(config, create_ocr_provider("none"), QRDecoder()).extract(
        ROOT / "samples" / "01-字段一致-应通过.pdf"
    )
    fields, _ = FieldExtractor(config).extract(result.text_pages, result.text_sources)
    assert fields["certificate_number"].value == "DEMO-CERT-2026-001"
    assert fields["issue_date"].value == "2026-08-12"
    assert fields["issuer_name"].value == "测试计量技术中心"
    assert result.qr_codes[0]["parsed_fields"]["certificate_number"] == "DEMO-CERT-2026-001"
    assert result.qr_codes[0]["render_scales"] == [2.0, 3.0]
    assert result.qr_codes[0]["stable_multiscale"] is True
