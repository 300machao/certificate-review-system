from __future__ import annotations

import json
import os
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.ingest import IngestError, IngestLimits, ingest_sources, parse_ledger_manifest


def pdf_bytes() -> bytes:
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(stream)
    return stream.getvalue()


def png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (16, 16), "white").save(stream, format="PNG")
    return stream.getvalue()


def write_zip(path: Path, members: list[tuple[str | zipfile.ZipInfo, bytes]], compression=zipfile.ZIP_DEFLATED) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, data in members:
            archive.writestr(name, data)


def assert_error(code: str, func) -> None:
    with pytest.raises(IngestError) as caught:
        func()
    assert caught.value.code == code
    assert caught.value.to_dict()["code"] == code


def test_duplicate_business_records_are_retained_with_one_object(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(pdf_bytes())
    second.write_bytes(first.read_bytes())

    result = ingest_sources([first, second], tmp_path / "store")

    assert len(result.records) == 2
    assert result.unique_objects == 1
    assert result.duplicates == 1
    assert result.records[0].record_id != result.records[1].record_id
    assert result.records[1].duplicate_of == result.records[0].record_id
    assert result.records[0].stored_path == result.records[1].stored_path
    assert result.records[0].page_count == 1
    assert Path(result.records[0].stored_path).read_bytes() == first.read_bytes()


def test_concurrent_batches_publish_one_complete_object(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes())
    store = tmp_path / "store"
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ingest_sources([source], store), range(8)))
    stored_paths = {result.records[0].stored_path for result in results}
    assert len(stored_paths) == 1
    assert Path(stored_paths.pop()).read_bytes() == source.read_bytes()


def test_image_and_zip_manifest_ingest(tmp_path: Path) -> None:
    archive = tmp_path / "batch.zip"
    manifest = (
        "序号,统一编号,出厂编号,解压文件,SHA256\n"
        "1,EQ-1,SN-1,证书文件\\a.pdf,\n"
    ).encode("utf-8-sig")
    write_zip(archive, [("证书/a.pdf", pdf_bytes()), ("images/page.png", png_bytes()), ("下载与核验清单.csv", manifest)])

    result = ingest_sources([archive], tmp_path / "store")

    assert len(result.records) == 2
    assert {record.media_type for record in result.records} == {"application/pdf", "image/png"}
    assert result.ledger_rows[0].values["unified_number"] == "EQ-1"
    assert result.ledger_rows[0].values["factory_serial_number"] == "SN-1"


@pytest.mark.parametrize(
    ("member", "code"),
    [
        ("../escape.pdf", "ZIP_SLIP"),
        ("dir/../../escape.pdf", "ZIP_SLIP"),
        ("/absolute.pdf", "ABSOLUTE_ARCHIVE_PATH"),
        (r"\\server\share\x.pdf", "ABSOLUTE_ARCHIVE_PATH"),
        ("＼＼server＼share＼x.pdf", "ABSOLUTE_ARCHIVE_PATH"),
        ("C:/drive.pdf", "ARCHIVE_ADS_OR_DRIVE"),
        ("safe.pdf:stream", "ARCHIVE_ADS_OR_DRIVE"),
        ("CON.pdf", "WINDOWS_RESERVED_NAME"),
        ("dir/NUL.txt", "WINDOWS_RESERVED_NAME"),
        ("trailing. /x.pdf", "WINDOWS_PATH_ALIAS"),
    ],
)
def test_rejects_unsafe_zip_paths(tmp_path: Path, member: str, code: str) -> None:
    archive = tmp_path / "bad.zip"
    write_zip(archive, [(member, pdf_bytes())])
    assert_error(code, lambda: ingest_sources([archive], tmp_path / "store"))


def test_rejects_normalized_name_collision(tmp_path: Path) -> None:
    archive = tmp_path / "collision.zip"
    write_zip(archive, [("Folder/A.pdf", pdf_bytes()), ("folder/a.PDF", pdf_bytes())])
    assert_error("NORMALIZED_NAME_COLLISION", lambda: ingest_sources([archive], tmp_path / "store"))


def test_zip_preserves_original_unicode_display_name(tmp_path: Path) -> None:
    archive = tmp_path / "unicode-name.zip"
    original_name = "DEMO-REPORT-001（DEMO-EQ-001）.pdf"
    write_zip(archive, [(original_name, pdf_bytes())])

    result = ingest_sources([archive], tmp_path / "store")

    assert result.records[0].original_name == original_name


def test_rejects_zip_symlink(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("linked.pdf")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = tmp_path / "linked.zip"
    write_zip(archive, [(link, b"target")], compression=zipfile.ZIP_STORED)
    assert_error("ARCHIVE_SPECIAL_FILE", lambda: ingest_sources([archive], tmp_path / "store"))


def test_rejects_zip_bomb_ratio_and_size_limits(tmp_path: Path) -> None:
    archive = tmp_path / "bomb.zip"
    write_zip(archive, [("large.png", b"0" * 20_000)])
    limits = IngestLimits(max_compression_ratio=2, max_single_file_bytes=30_000)
    assert_error("ZIP_BOMB_RATIO", lambda: ingest_sources([archive], tmp_path / "store", limits=limits))

    direct = tmp_path / "too-large.pdf"
    direct.write_bytes(pdf_bytes())
    limits = IngestLimits(max_single_file_bytes=10)
    assert_error("FILE_SIZE_LIMIT", lambda: ingest_sources([direct], tmp_path / "store2", limits=limits))


def test_rejects_zip_count_single_and_total_limits(tmp_path: Path) -> None:
    payload = pdf_bytes()
    archive = tmp_path / "limits.zip"
    write_zip(archive, [("a.pdf", payload), ("b.pdf", payload)], compression=zipfile.ZIP_STORED)

    assert_error(
        "ARCHIVE_MEMBER_LIMIT",
        lambda: ingest_sources([archive], tmp_path / "member-store", limits=IngestLimits(max_archive_members=1)),
    )
    assert_error(
        "TOO_MANY_FILES",
        lambda: ingest_sources([archive], tmp_path / "count-store", limits=IngestLimits(max_files=1)),
    )
    assert_error(
        "FILE_SIZE_LIMIT",
        lambda: ingest_sources(
            [archive], tmp_path / "single-store", limits=IngestLimits(max_single_file_bytes=len(payload) - 1)
        ),
    )
    assert_error(
        "TOTAL_SIZE_LIMIT",
        lambda: ingest_sources(
            [archive],
            tmp_path / "total-store",
            limits=IngestLimits(max_single_file_bytes=len(payload) + 1, max_total_uncompressed_bytes=len(payload) * 2 - 1),
        ),
    )


def test_rejects_bad_crc(tmp_path: Path) -> None:
    archive = tmp_path / "crc.zip"
    payload = pdf_bytes()
    write_zip(archive, [("a.pdf", payload)], compression=zipfile.ZIP_STORED)
    raw = bytearray(archive.read_bytes())
    offset = raw.find(payload)
    assert offset >= 0
    raw[offset + 10] ^= 0xFF
    archive.write_bytes(raw)
    assert_error("ZIP_CRC_FAILED", lambda: ingest_sources([archive], tmp_path / "store"))


def test_rejects_extension_magic_mismatch(tmp_path: Path) -> None:
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(png_bytes())
    assert_error("PDF_MAGIC_MISMATCH", lambda: ingest_sources([fake], tmp_path / "store"))


def test_manifest_json_and_csv_are_frozen_and_canonical(tmp_path: Path) -> None:
    json_path = tmp_path / "ledger.json"
    json_path.write_text(
        json.dumps(
            {
                "summary": {"ignored": True},
                "records": [
                    {
                        "reportId": "R1",
                        "unifiedNumber": "EQ1",
                        "factorySerialNumber": "SN1",
                        "recordValidUntil": "2027-01-01",
                        "sha256": "a" * 64,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rows = parse_ledger_manifest(json_path)
    assert rows[0].row_id == "R1"
    assert rows[0].values["unified_number"] == "EQ1"
    assert rows[0].values["factory_serial_number"] == "SN1"
    assert rows[0].values["record_valid_until"] == "2027-01-01"

    csv_path = tmp_path / "ledger.csv"
    csv_path.write_text("证书附件ID,统一编号,PDF页数\nR2,EQ2,3\n", encoding="utf-8-sig")
    csv_rows = parse_ledger_manifest(csv_path)
    assert csv_rows[0].values == {"report_id": "R2", "unified_number": "EQ2", "pdf_pages": "3"}


def test_manifest_rejects_duplicate_fields_and_primitive_json(tmp_path: Path) -> None:
    duplicate_json = tmp_path / "duplicate.json"
    duplicate_json.write_text('{"records": [], "records": []}', encoding="utf-8")
    assert_error("MANIFEST_DUPLICATE_JSON_KEY", lambda: parse_ledger_manifest(duplicate_json))

    primitive_json = tmp_path / "primitive.json"
    primitive_json.write_text("42", encoding="utf-8")
    assert_error("INVALID_MANIFEST_JSON", lambda: parse_ledger_manifest(primitive_json))

    duplicate_csv = tmp_path / "duplicate.csv"
    duplicate_csv.write_text("统一编号,unifiedNumber\nA,B\n", encoding="utf-8-sig")
    assert_error("MANIFEST_FIELD_COLLISION", lambda: parse_ledger_manifest(duplicate_csv))


def test_manifest_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    document = tmp_path / "a.pdf"
    document.write_bytes(pdf_bytes())
    manifest = tmp_path / "ledger.json"
    manifest.write_text(
        json.dumps({"records": [{"reportId": "R1", "localPath": "a.pdf", "sha256": "0" * 64}]}),
        encoding="utf-8",
    )
    assert_error(
        "MANIFEST_SHA_MISMATCH",
        lambda: ingest_sources([document], tmp_path / "store", ledger_sources=[manifest]),
    )


def test_folder_import_rejects_link_or_unknown_file(tmp_path: Path) -> None:
    folder = tmp_path / "input"
    folder.mkdir()
    (folder / "notes.exe").write_bytes(b"no")
    assert_error("UNSUPPORTED_FILE_TYPE", lambda: ingest_sources([folder], tmp_path / "store"))

    if hasattr(os, "symlink"):
        (folder / "notes.exe").unlink()
        target = folder / "real.pdf"
        target.write_bytes(pdf_bytes())
        link = folder / "link.pdf"
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        assert_error("SYMLINK_INPUT", lambda: ingest_sources([folder], tmp_path / "store2"))
