from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
import warnings
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Sequence

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader


DOCUMENT_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})
MANIFEST_EXTENSIONS = frozenset({".json", ".csv"})
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class IngestLimits:
    """Fail-closed limits applied before archive extraction is committed."""

    max_files: int = 500
    max_archive_members: int = 1_000
    max_single_file_bytes: int = 200 * 1024 * 1024
    max_total_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_archive_bytes: int = 512 * 1024 * 1024
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_path_chars: int = 240
    max_pdf_pages: int = 500
    max_image_pixels: int = 100_000_000

    def __post_init__(self) -> None:
        numeric = (
            self.max_files,
            self.max_archive_members,
            self.max_single_file_bytes,
            self.max_total_uncompressed_bytes,
            self.max_archive_bytes,
            self.max_manifest_bytes,
            self.max_path_chars,
            self.max_pdf_pages,
            self.max_image_pixels,
        )
        if any(value <= 0 for value in numeric) or self.max_compression_ratio <= 0:
            raise ValueError("all ingest limits must be positive")


class IngestError(ValueError):
    def __init__(self, code: str, message: str, member: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.member = member

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.member is not None:
            result["member"] = self.member
        return result


@dataclass(frozen=True)
class FrozenLedgerRow:
    row_id: str
    source_index: int
    values: dict[str, Any]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestedFile:
    record_id: str
    original_name: str
    sha256: str
    size_bytes: int
    media_type: str
    stored_path: str
    source_container: str | None = None
    duplicate_of: str | None = None
    page_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestResult:
    records: list[IngestedFile] = field(default_factory=list)
    unique_objects: int = 0
    duplicates: int = 0
    total_bytes: int = 0
    ledger_rows: list[FrozenLedgerRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [record.to_dict() for record in self.records],
            "unique_objects": self.unique_objects,
            "duplicates": self.duplicates,
            "total_bytes": self.total_bytes,
            "ledger_rows": [row.to_dict() for row in self.ledger_rows],
            "warnings": list(self.warnings),
        }


@dataclass
class _StagedDocument:
    path: Path
    original_name: str
    source_container: str | None
    sha256: str
    size_bytes: int
    media_type: str
    page_count: int | None


_LEDGER_ALIASES = {
    "序号": "sequence",
    "统一编号": "unified_number",
    "设备名称": "equipment_name",
    "器具名称": "equipment_name",
    "出厂编号": "factory_serial_number",
    "设备状态": "equipment_status",
    "器具有效期": "equipment_valid_until",
    "检定记录ID": "record_id",
    "检修日期": "calibrate_or_verify_date",
    "记录有效期": "record_valid_until",
    "检修类型": "maintenance_type",
    "检定结论": "verification_result",
    "校准检定地点": "calibrate_or_verify_location",
    "校准人": "inspector",
    "证书附件ID": "report_id",
    "证书显示名": "display_name",
    "服务端文件类型": "server_file_type",
    "服务端文件大小": "server_file_size",
    "解压文件": "local_path",
    "本地字节数": "bytes",
    "压缩字节数": "compressed_bytes",
    "检测文件类型": "detected_kind",
    "PDF可读": "pdf_readable",
    "PDF页数": "pdf_pages",
    "重复内容组": "duplicate_content_group",
    "重复组文件数": "duplicate_content_group_size",
}


def ingest_sources(
    sources: Sequence[Path],
    storage_root: Path,
    *,
    ledger_sources: Sequence[Path] = (),
    limits: IngestLimits | None = None,
) -> IngestResult:
    """Validate and ingest documents without collapsing duplicate business records.

    Documents are staged first. Only after every source passes validation are unique
    byte streams committed to ``storage_root/objects`` under their SHA-256 digest.
    Existing objects are re-hashed before reuse. Any failure removes staging files and
    leaves already-existing content-addressed objects untouched.
    """

    active_limits = limits or IngestLimits()
    root = Path(storage_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    objects_root = root / "objects"
    objects_root.mkdir(parents=True, exist_ok=True)

    expanded_sources = _expand_sources(sources, root)
    explicit_ledgers = _expand_sources(ledger_sources, root, manifests_only=True)
    all_sources = expanded_sources + explicit_ledgers
    if not all_sources:
        raise IngestError("NO_INPUT", "未提供可导入的文件")

    staged: list[_StagedDocument] = []
    ledger_rows: list[FrozenLedgerRow] = []
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix="certificate-ingest-stage-", dir=root) as stage_name:
        stage_root = Path(stage_name)
        for source in all_sources:
            _reject_link_or_non_file(source)
            suffix = source.suffix.lower()
            if suffix == ".zip":
                docs, rows = _stage_zip(source, stage_root, active_limits, len(staged))
                staged.extend(docs)
                ledger_rows.extend(rows)
            elif suffix in MANIFEST_EXTENSIONS:
                ledger_rows.extend(parse_ledger_manifest(source, limits=active_limits))
            elif suffix in DOCUMENT_EXTENSIONS:
                staged.append(
                    _stage_regular_document(source, stage_root, active_limits, len(staged))
                )
            else:
                raise IngestError(
                    "UNSUPPORTED_FILE_TYPE",
                    f"不支持的导入类型：{suffix or '无扩展名'}",
                    str(source),
                )
            if len(staged) > active_limits.max_files:
                raise IngestError("TOO_MANY_FILES", f"证书文件数超过 {active_limits.max_files} 个限制")
            total_bytes = sum(item.size_bytes for item in staged)
            if total_bytes > active_limits.max_total_uncompressed_bytes:
                raise IngestError("TOTAL_SIZE_LIMIT", "批次解压后总大小超过限制")

        if not staged:
            raise IngestError("NO_DOCUMENTS", "未找到PDF或受支持的图片文件")
        _reconcile_manifest(staged, ledger_rows)

        records: list[IngestedFile] = []
        first_by_sha: dict[str, str] = {}
        for item in staged:
            object_path = _commit_object(item, objects_root)
            record_id = str(uuid.uuid4())
            duplicate_of = first_by_sha.get(item.sha256)
            if duplicate_of is None:
                first_by_sha[item.sha256] = record_id
            records.append(
                IngestedFile(
                    record_id=record_id,
                    original_name=item.original_name,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    media_type=item.media_type,
                    stored_path=str(object_path),
                    source_container=item.source_container,
                    duplicate_of=duplicate_of,
                    page_count=item.page_count,
                )
            )

    return IngestResult(
        records=records,
        unique_objects=len(first_by_sha),
        duplicates=len(records) - len(first_by_sha),
        total_bytes=total_bytes,
        ledger_rows=ledger_rows,
        warnings=[],
    )


def parse_ledger_manifest(
    path: Path,
    *,
    limits: IngestLimits | None = None,
) -> list[FrozenLedgerRow]:
    active_limits = limits or IngestLimits()
    source = Path(path)
    _reject_link_or_non_file(source)
    size = source.stat().st_size
    if size > active_limits.max_manifest_bytes:
        raise IngestError("MANIFEST_TOO_LARGE", "台账清单超过大小限制", str(source))
    data = source.read_bytes()
    return _parse_manifest_bytes(data, source.suffix.lower(), str(source))


def _expand_sources(
    sources: Sequence[Path],
    storage_root: Path,
    *,
    manifests_only: bool = False,
) -> list[Path]:
    expanded: list[Path] = []
    for raw in sources:
        source = Path(raw)
        if source.is_symlink():
            raise IngestError("SYMLINK_INPUT", "不允许导入符号链接", str(source))
        if source.is_dir():
            resolved_source = source.resolve()
            try:
                storage_root.relative_to(resolved_source)
            except ValueError:
                pass
            else:
                raise IngestError("STORAGE_INSIDE_SOURCE", "存储目录不能位于待导入目录内", str(source))
            expanded.extend(_walk_directory(source, manifests_only=manifests_only))
        else:
            expanded.append(source)
    return expanded


def _walk_directory(root: Path, *, manifests_only: bool) -> list[Path]:
    result: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        entries = sorted(os.scandir(current), key=lambda entry: unicodedata.normalize("NFKC", entry.name).casefold())
        for entry in entries:
            if entry.is_symlink():
                raise IngestError("SYMLINK_INPUT", "目录中包含符号链接", entry.path)
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                raise IngestError("NON_REGULAR_INPUT", "目录中包含非普通文件", entry.path)
            suffix = Path(entry.name).suffix.lower()
            allowed = MANIFEST_EXTENSIONS if manifests_only else DOCUMENT_EXTENSIONS | MANIFEST_EXTENSIONS | {".zip"}
            if suffix not in allowed:
                raise IngestError("UNSUPPORTED_FILE_TYPE", f"目录中存在不支持的文件：{entry.name}", entry.path)
            result.append(Path(entry.path))
    return sorted(result, key=lambda item: unicodedata.normalize("NFKC", str(item)).casefold())


def _reject_link_or_non_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise IngestError("INPUT_NOT_FOUND", "导入文件不存在", str(path)) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise IngestError("SYMLINK_INPUT", "不允许导入符号链接", str(path))
    if not stat.S_ISREG(metadata.st_mode):
        raise IngestError("NON_REGULAR_INPUT", "仅允许导入普通文件", str(path))


def _stage_regular_document(
    source: Path,
    stage_root: Path,
    limits: IngestLimits,
    index: int,
) -> _StagedDocument:
    size = source.stat().st_size
    if size <= 0:
        raise IngestError("EMPTY_FILE", "文件为空", str(source))
    if size > limits.max_single_file_bytes:
        raise IngestError("FILE_SIZE_LIMIT", "单个文件超过大小限制", str(source))
    target = stage_root / f"{index:06d}.bin"
    with source.open("rb") as incoming, target.open("xb") as output:
        digest, copied = _copy_and_hash(incoming, output, limits.max_single_file_bytes)
    if copied != size:
        raise IngestError("SOURCE_CHANGED", "读取期间文件大小发生变化", str(source))
    media_type, page_count = _validate_document(target, source.suffix.lower(), limits, str(source))
    return _StagedDocument(target, source.name, None, digest, copied, media_type, page_count)


def _stage_zip(
    source: Path,
    stage_root: Path,
    limits: IngestLimits,
    start_index: int,
) -> tuple[list[_StagedDocument], list[FrozenLedgerRow]]:
    archive_size = source.stat().st_size
    if archive_size <= 0:
        raise IngestError("EMPTY_ARCHIVE", "ZIP文件为空", str(source))
    if archive_size > limits.max_archive_bytes:
        raise IngestError("ARCHIVE_SIZE_LIMIT", "ZIP文件超过大小限制", str(source))
    staged: list[_StagedDocument] = []
    ledger_rows: list[FrozenLedgerRow] = []
    try:
        archive = zipfile.ZipFile(source, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise IngestError("INVALID_ZIP", "ZIP结构无效", str(source)) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_archive_members:
            raise IngestError("ARCHIVE_MEMBER_LIMIT", "ZIP条目数超过限制", str(source))
        normalized_names: set[str] = set()
        file_count = 0
        declared_total = 0
        for info in infos:
            normalized = _validate_zip_member(info, limits)
            original_basename = PurePosixPath(info.filename.replace("\\", "/").rstrip("/")).name
            collision_key = normalized.casefold()
            if collision_key in normalized_names:
                raise IngestError("NORMALIZED_NAME_COLLISION", "ZIP中存在规范化后重名条目", info.filename)
            normalized_names.add(collision_key)
            if info.is_dir():
                continue
            file_count += 1
            declared_total += info.file_size
            if file_count > limits.max_files:
                raise IngestError("TOO_MANY_FILES", f"ZIP文件条目超过 {limits.max_files} 个限制")
            if info.file_size <= 0:
                raise IngestError("EMPTY_MEMBER", "ZIP中包含空文件", info.filename)
            suffix = Path(PurePosixPath(normalized).name).suffix.lower()
            if suffix not in DOCUMENT_EXTENSIONS | MANIFEST_EXTENSIONS:
                raise IngestError("UNSUPPORTED_ARCHIVE_MEMBER", "ZIP包含不支持的文件类型", info.filename)
            allowed_size = limits.max_manifest_bytes if suffix in MANIFEST_EXTENSIONS else limits.max_single_file_bytes
            if info.file_size > allowed_size:
                raise IngestError("FILE_SIZE_LIMIT", "ZIP内单个文件超过大小限制", info.filename)
            if declared_total > limits.max_total_uncompressed_bytes:
                raise IngestError("TOTAL_SIZE_LIMIT", "ZIP声明的解压后总大小超过限制", str(source))
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > limits.max_compression_ratio:
                raise IngestError("ZIP_BOMB_RATIO", "ZIP条目压缩比超过限制", info.filename)
            if info.flag_bits & 0x1:
                raise IngestError("ENCRYPTED_ARCHIVE_MEMBER", "不支持加密ZIP条目", info.filename)

            target = stage_root / f"{start_index + len(staged):06d}-{file_count:06d}.bin"
            try:
                with archive.open(info, "r") as incoming, target.open("xb") as output:
                    digest, copied = _copy_and_hash(incoming, output, allowed_size)
            except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
                raise IngestError("ZIP_CRC_FAILED", "ZIP条目CRC或数据完整性校验失败", info.filename) from exc
            if copied != info.file_size:
                raise IngestError("ZIP_SIZE_MISMATCH", "ZIP条目实际大小与目录记录不一致", info.filename)
            source_label = f"{source}!{normalized}"
            if suffix in MANIFEST_EXTENSIONS:
                ledger_rows.extend(_parse_manifest_bytes(target.read_bytes(), suffix, source_label))
                target.unlink()
                continue
            media_type, page_count = _validate_document(target, suffix, limits, source_label)
            staged.append(
                _StagedDocument(
                    target,
                    original_basename,
                    source.name,
                    digest,
                    copied,
                    media_type,
                    page_count,
                )
            )
    return staged, ledger_rows


def _validate_zip_member(info: zipfile.ZipInfo, limits: IngestLimits) -> str:
    raw = info.filename
    if not raw or "\x00" in raw:
        raise IngestError("INVALID_ARCHIVE_PATH", "ZIP条目名称为空或包含NUL", raw)
    if len(raw) > limits.max_path_chars:
        raise IngestError("ARCHIVE_PATH_TOO_LONG", "ZIP条目路径过长", raw)
    normalized_raw = unicodedata.normalize("NFKC", raw)
    if normalized_raw.startswith(("/", "\\")):
        raise IngestError("ABSOLUTE_ARCHIVE_PATH", "ZIP条目不得使用绝对或UNC路径", raw)
    normalized = normalized_raw.replace("\\", "/")
    if ":" in normalized:
        raise IngestError("ARCHIVE_ADS_OR_DRIVE", "ZIP条目不得包含盘符或ADS冒号", raw)
    parts = normalized.rstrip("/").split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise IngestError("ZIP_SLIP", "ZIP条目包含不安全的相对路径", raw)
    for part in parts:
        if part.endswith((" ", ".")):
            raise IngestError("WINDOWS_PATH_ALIAS", "ZIP条目含Windows尾随空格或句点", raw)
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise IngestError("WINDOWS_RESERVED_NAME", "ZIP条目使用Windows保留名称", raw)
        if any(ord(character) < 32 for character in part):
            raise IngestError("INVALID_ARCHIVE_PATH", "ZIP条目包含控制字符", raw)
        if any(character in '<>"|?*' for character in part):
            raise IngestError("INVALID_WINDOWS_PATH", "ZIP条目包含Windows非法字符", raw)
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        raise IngestError("ZIP_SLIP", "ZIP条目可能逃逸目标目录", raw)

    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_kind = stat.S_IFMT(unix_mode)
    if file_kind and file_kind not in {stat.S_IFREG, stat.S_IFDIR}:
        raise IngestError("ARCHIVE_SPECIAL_FILE", "ZIP不得包含符号链接或特殊文件", raw)
    if (info.external_attr & 0xFFFF) & 0x0400:
        raise IngestError("ARCHIVE_REPARSE_POINT", "ZIP不得包含Windows重解析点", raw)
    return "/".join(parts)


def _copy_and_hash(incoming: BinaryIO, output: BinaryIO, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    copied = 0
    while True:
        chunk = incoming.read(1024 * 1024)
        if not chunk:
            break
        copied += len(chunk)
        if copied > maximum:
            raise IngestError("FILE_SIZE_LIMIT", "流式读取超过大小限制")
        output.write(chunk)
        digest.update(chunk)
    output.flush()
    os.fsync(output.fileno())
    return digest.hexdigest(), copied


def _validate_document(path: Path, suffix: str, limits: IngestLimits, label: str) -> tuple[str, int | None]:
    if suffix == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise IngestError("PDF_MAGIC_MISMATCH", "PDF扩展名与文件头不一致", label)
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                raise IngestError("ENCRYPTED_PDF", "不支持加密PDF", label)
            pages = len(reader.pages)
            for page in reader.pages:
                _ = page.mediabox
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError("INVALID_PDF", "PDF结构不可读", label) from exc
        if pages <= 0:
            raise IngestError("EMPTY_PDF", "PDF不包含页面", label)
        if pages > limits.max_pdf_pages:
            raise IngestError("PDF_PAGE_LIMIT", "PDF页数超过限制", label)
        return "application/pdf", pages

    expected_formats = {
        ".png": {"PNG"},
        ".jpg": {"JPEG"},
        ".jpeg": {"JPEG"},
        ".tif": {"TIFF"},
        ".tiff": {"TIFF"},
        ".bmp": {"BMP"},
        ".webp": {"WEBP"},
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                detected = str(image.format or "").upper()
                width, height = image.size
                frame_count = int(getattr(image, "n_frames", 1))
                if width <= 0 or height <= 0 or width * height > limits.max_image_pixels:
                    raise IngestError("IMAGE_PIXEL_LIMIT", "图片像素数量超过限制", label)
                if detected not in expected_formats.get(suffix, set()):
                    raise IngestError("IMAGE_MAGIC_MISMATCH", "图片扩展名与实际格式不一致", label)
                image.verify()
    except IngestError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombWarning) as exc:
        raise IngestError("INVALID_IMAGE", "图片结构不可读", label) from exc
    mime = Image.MIME.get(detected, f"image/{detected.lower()}")
    return mime, frame_count


def _commit_object(item: _StagedDocument, objects_root: Path) -> Path:
    parent = objects_root / item.sha256[:2]
    parent.mkdir(parents=True, exist_ok=True)
    canonical_suffix = {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/tiff": ".tiff",
        "image/bmp": ".bmp",
        "image/webp": ".webp",
    }.get(item.media_type, ".bin")
    target = parent / f"{item.sha256}{canonical_suffix}"
    if target.exists():
        _verify_existing_object(target, item)
        return target
    temporary = parent / f".{item.sha256}.{uuid.uuid4().hex}.tmp"
    try:
        with item.path.open("rb") as incoming, temporary.open("xb") as output:
            digest, copied = _copy_and_hash(incoming, output, item.size_bytes)
        if digest != item.sha256 or copied != item.size_bytes:
            raise IngestError("STAGING_HASH_CHANGED", "提交期间暂存对象内容发生变化", item.original_name)
        try:
            # A hard link publishes the fully-written object atomically without
            # replacing a valid object committed by a concurrent batch.
            os.link(temporary, target)
        except FileExistsError:
            _verify_existing_object(target, item)
        temporary.unlink()
        target.chmod(stat.S_IREAD)
    except Exception:
        if temporary.exists():
            try:
                temporary.chmod(stat.S_IWRITE | stat.S_IREAD)
                temporary.unlink()
            except OSError:
                pass
        raise
    return target


def _verify_existing_object(target: Path, item: _StagedDocument) -> None:
    if not target.is_file() or target.is_symlink():
        raise IngestError("OBJECT_STORE_CONFLICT", "内容寻址对象路径不是普通文件", str(target))
    if target.stat().st_size != item.size_bytes or _sha256_path(target) != item.sha256:
        raise IngestError("OBJECT_STORE_CORRUPT", "已有内容寻址对象校验失败", str(target))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_manifest_bytes(data: bytes, suffix: str, source: str) -> list[FrozenLedgerRow]:
    if not data:
        raise IngestError("EMPTY_MANIFEST", "台账清单为空", source)
    if b"\x00" in data:
        raise IngestError("INVALID_MANIFEST_ENCODING", "台账清单包含NUL字节", source)
    if suffix == ".json":
        try:
            payload = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_json_object_without_duplicates)
        except IngestError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise IngestError("INVALID_MANIFEST_JSON", "台账JSON不可解析", source) from exc
        if isinstance(payload, list):
            raw_rows = payload
        elif isinstance(payload, dict):
            raw_rows = next(
                (payload[key] for key in ("records", "rows", "data") if isinstance(payload.get(key), list)),
                [payload],
            )
        else:
            raise IngestError("INVALID_MANIFEST_JSON", "台账JSON顶层必须是对象或数组", source)
    elif suffix == ".csv":
        text = _decode_csv(data, source)
        try:
            matrix = list(csv.reader(io.StringIO(text)))
        except csv.Error as exc:
            raise IngestError("INVALID_MANIFEST_CSV", "台账CSV不可解析", source) from exc
        if not matrix:
            raise IngestError("EMPTY_MANIFEST", "台账清单没有表头", source)
        headers = matrix[0]
        if not headers or any(not str(header).strip() for header in headers):
            raise IngestError("INVALID_MANIFEST_CSV", "台账CSV包含空表头", source)
        canonical_headers = [_LEDGER_ALIASES.get(unicodedata.normalize("NFKC", header).strip(), _snake_case(header)) for header in headers]
        if len(set(canonical_headers)) != len(canonical_headers):
            raise IngestError("MANIFEST_FIELD_COLLISION", "台账CSV包含重复或规范化重名表头", source)
        raw_rows = []
        for row_number, row in enumerate(matrix[1:], 2):
            if len(row) != len(headers):
                raise IngestError("INVALID_MANIFEST_CSV", f"台账CSV第 {row_number} 行列数与表头不一致", source)
            raw_rows.append(dict(zip(headers, row, strict=True)))
    else:
        raise IngestError("UNSUPPORTED_MANIFEST", "仅支持JSON或CSV台账清单", source)
    if not raw_rows:
        raise IngestError("EMPTY_MANIFEST", "台账清单没有数据行", source)

    result: list[FrozenLedgerRow] = []
    for index, raw in enumerate(raw_rows, 1):
        if not isinstance(raw, dict):
            raise IngestError("INVALID_MANIFEST_ROW", f"台账第 {index} 行不是对象", source)
        values = _canonicalize_row(raw, source, index)
        sha = str(values.get("sha256") or "").strip()
        if sha and not _SHA256_RE.fullmatch(sha):
            raise IngestError("INVALID_MANIFEST_SHA256", f"台账第 {index} 行SHA256无效", source)
        if sha:
            values["sha256"] = sha.lower()
        row_id_value = next(
            (values.get(key) for key in ("report_id", "record_id", "unified_number", "sequence") if values.get(key) not in (None, "")),
            None,
        )
        row_id = str(row_id_value) if row_id_value is not None else f"{Path(source).name}:{index}"
        result.append(FrozenLedgerRow(row_id=row_id, source_index=index, values=values, source=source))
    return result


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IngestError("MANIFEST_DUPLICATE_JSON_KEY", f"台账JSON包含重复键：{key}")
        result[key] = value
    return result


def _decode_csv(data: bytes, source: str) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError("INVALID_MANIFEST_ENCODING", "台账CSV编码不是UTF-8或GB18030", source)


def _canonicalize_row(raw: dict[Any, Any], source: str, index: int) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for original_key, value in raw.items():
        if original_key is None:
            continue
        key_text = unicodedata.normalize("NFKC", str(original_key)).strip()
        if not key_text:
            continue
        key = _LEDGER_ALIASES.get(key_text, _snake_case(key_text))
        if key in values and values[key] != value:
            raise IngestError("MANIFEST_FIELD_COLLISION", f"台账第 {index} 行存在规范化重名字段：{key}", source)
        values[key] = value.strip() if isinstance(value, str) else value
    if not values:
        raise IngestError("EMPTY_MANIFEST_ROW", f"台账第 {index} 行为空", source)
    return values


def _snake_case(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "_", value, flags=re.UNICODE)
    return value.strip("_").lower()


def _reconcile_manifest(staged: Sequence[_StagedDocument], rows: Sequence[FrozenLedgerRow]) -> None:
    documents_by_name: dict[str, list[_StagedDocument]] = {}
    for document in staged:
        key = unicodedata.normalize("NFKC", Path(document.original_name).name).casefold()
        documents_by_name.setdefault(key, []).append(document)
    for row in rows:
        listed_sha = str(row.values.get("sha256") or "").lower()
        path_value = row.values.get("local_path") or row.values.get("archive_path")
        if not listed_sha or not path_value:
            continue
        basename = str(path_value).replace("\\", "/").rsplit("/", 1)[-1]
        key = unicodedata.normalize("NFKC", basename).casefold()
        matching = documents_by_name.get(key, [])
        if matching and all(document.sha256 != listed_sha for document in matching):
            raise IngestError(
                "MANIFEST_SHA_MISMATCH",
                f"台账行 {row.row_id} 的SHA256与同名导入文件不一致",
                row.source,
            )
