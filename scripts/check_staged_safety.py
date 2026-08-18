from __future__ import annotations

import codecs
import hashlib
import re
import subprocess
import sys
from pathlib import PurePosixPath


# These are the six reviewed, generated acceptance artifacts. A matching path
# with different bytes is rejected so a real document cannot replace a demo
# file under an allowed name.
ALLOWED_SYNTHETIC_MEDIA = {
    "samples/01-字段一致-应通过.pdf": "90d7ced9ede4d155fa80a008814391db98c60431e400d8146a29caa0b930b980",
    "samples/01-字段一致-应通过.png": "4aae4216a9f2a35dd1e835f360ecadc295d62f41d784e6cd05f809fef5b5ebfb",
    "samples/02-二维码编号不一致-需复核.pdf": "1f9fecd4b33dfca633d593f3d405ad794b5686e3ca36eb071a0faaa60143ed84",
    "samples/02-二维码编号不一致-需复核.png": "7832a72ca79cf30aef9caa8e4ef060012ef3216a11d3d98d525a6a5bb46f3941",
    "samples/03-无二维码-需复核.pdf": "132363c97936a5de1717ac23ef80d072a381034257b0a519e0e50e2148e37ddf",
    "samples/04-损坏文件-应失败.pdf": "a1cdf563f0775e542a642b400aad002132b9d58345813311e3586fe316058199",
}
_ALLOWED_MEDIA_CASEFOLD = {
    path.casefold(): digest for path, digest in ALLOWED_SYNTHETIC_MEDIA.items()
}

FORBIDDEN_PREFIXES = ("data/", "validation/")
FORBIDDEN_DIRECTORY_NAMES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "logs",
    "node_modules",
    "venv",
}
DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".wal", ".shm")
DATABASE_SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")
SECRET_SUFFIXES = (
    ".cer",
    ".credentials",
    ".crt",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".secret",
    ".token",
)
MEDIA_AND_ARCHIVE_SUFFIXES = {
    ".7z",
    ".avi",
    ".avif",
    ".bmp",
    ".bz2",
    ".cab",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".heic",
    ".ico",
    ".iso",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".tar",
    ".tgz",
    ".tif",
    ".tiff",
    ".webp",
    ".webm",
    ".wav",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".xz",
    ".zip",
}
BINARY_BUILD_SUFFIXES = (".dll", ".exe", ".pyc", ".pyd")
MAX_TEXT_BLOB_BYTES = 4 * 1024 * 1024

ABSOLUTE_PATH_PATTERNS = (
    (
        "Windows 用户目录绝对路径",
        re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\"']+"),
    ),
    (
        "POSIX 用户目录绝对路径",
        re.compile(r"(?i)(?<![A-Za-z0-9])/(?:Users|home)/[A-Za-z0-9._-]+/[^\s\"']*"),
    ),
)
SECRET_PATTERNS = (
    (
        "私钥头",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("OpenAI 风格密钥", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b")),
    ("AWS 访问密钥", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub 令牌", re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("Google API 密钥", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("Slack 令牌", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
    ("Bearer 令牌", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|password)\b"
    r"\s*[:=]\s*[\"']([^\"'\r\n]{16,})[\"']"
)
PLACEHOLDER_MARKERS = (
    "changeme",
    "demo",
    "dummy",
    "example",
    "fake",
    "must-never-be-written",
    "not-a-real",
    "placeholder",
    "redacted",
    "test",
)


def _git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"git {' '.join(args)} failed")
    return result.stdout


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def staged_paths() -> list[str]:
    raw = _git(
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
        "--",
    )
    paths = [
        _normalize_path(item.decode("utf-8", errors="surrogateescape"))
        for item in raw.split(b"\0")
        if item
    ]
    return sorted(set(paths), key=str.casefold)


def staged_mode(path: str) -> str:
    raw = _git("ls-files", "--stage", "-z", "--", path)
    entry = raw.split(b"\0", 1)[0]
    if not entry:
        raise RuntimeError(f"暂存区中找不到路径：{path}")
    return entry.split(b" ", 1)[0].decode("ascii", errors="replace")


def staged_blob(path: str) -> bytes:
    return _git("show", "--no-textconv", f":{path}")


def path_violations(path: str) -> list[str]:
    normalized = _normalize_path(path)
    folded = normalized.casefold()
    parts = tuple(part for part in folded.split("/") if part)
    name = parts[-1] if parts else ""
    suffix = PurePosixPath(name).suffix
    violations: list[str] = []

    if any(folded.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        violations.append("正式 data/validation 路径禁止暂存")
    if name == "acceptance-result" or name.startswith("acceptance-result."):
        violations.append("acceptance-result 运行产物禁止暂存")
    if any(part in FORBIDDEN_DIRECTORY_NAMES for part in parts[:-1]):
        violations.append("缓存、虚拟环境、日志或构建目录禁止暂存")
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        violations.append("环境变量文件禁止暂存")
    if "dpapi" in name:
        violations.append("DPAPI 凭据文件禁止暂存")
    if any(marker in name for marker in ("api-key", "api_key", "apikey")):
        violations.append("API 密钥文件禁止暂存")
    if name in {"credentials.json", "secrets.json"} or name.startswith(
        ("credentials.", "secrets.")
    ):
        violations.append("凭据或秘密文件禁止暂存")
    if suffix in DATABASE_SUFFIXES or name.endswith(DATABASE_SIDECAR_SUFFIXES):
        violations.append("数据库、WAL、SHM 或 journal 文件禁止暂存")
    if suffix == ".log" or suffix == ".pid":
        violations.append("日志或 PID 文件禁止暂存")
    if suffix in SECRET_SUFFIXES:
        violations.append("密钥或证书文件禁止暂存")
    if suffix in BINARY_BUILD_SUFFIXES:
        violations.append("编译或二进制构建产物禁止暂存")
    if suffix in MEDIA_AND_ARCHIVE_SUFFIXES and folded not in _ALLOWED_MEDIA_CASEFOLD:
        violations.append("媒体、办公文档或压缩包不在精确合成白名单")
    return violations


def decode_text(blob: bytes) -> str | None:
    try:
        if blob.startswith(codecs.BOM_UTF8):
            return blob.decode("utf-8-sig")
        if blob.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return blob.decode("utf-16")
        if b"\0" in blob:
            return None
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def content_violations(path: str, blob: bytes) -> list[str]:
    folded = _normalize_path(path).casefold()
    expected_hash = _ALLOWED_MEDIA_CASEFOLD.get(folded)
    if expected_hash is not None:
        actual_hash = hashlib.sha256(blob).hexdigest()
        if actual_hash != expected_hash:
            return ["合成样例路径内容已变化，固定 SHA-256 不匹配"]
        return []

    if len(blob) > MAX_TEXT_BLOB_BYTES:
        return ["非白名单暂存文本超过 4 MiB，拒绝绕过内容扫描"]
    text = decode_text(blob)
    if text is None:
        return ["非白名单二进制或未知编码内容禁止暂存"]

    violations: list[str] = []
    for label, pattern in ABSOLUTE_PATH_PATTERNS:
        if pattern.search(text):
            violations.append(label)
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            violations.append(label)
    for match in CREDENTIAL_ASSIGNMENT.finditer(text):
        value = match.group(2).casefold()
        if not any(marker in value for marker in PLACEHOLDER_MARKERS):
            violations.append("疑似明文凭据赋值")
            break
    return violations


def main() -> int:
    try:
        paths = staged_paths()
    except RuntimeError as exc:
        print(f"暂存安全检查无法读取 Git 索引：{exc}", file=sys.stderr)
        return 2

    failures: list[tuple[str, str]] = []
    for path in paths:
        path_failures = path_violations(path)
        if path_failures:
            failures.extend((path, reason) for reason in path_failures)
            continue
        try:
            mode = staged_mode(path)
            if mode not in {"100644", "100755"}:
                failures.append((path, f"不允许的 Git 文件模式：{mode}"))
                continue
            blob = staged_blob(path)
        except RuntimeError as exc:
            failures.append((path, f"无法读取暂存 blob：{exc}"))
            continue
        failures.extend((path, reason) for reason in content_violations(path, blob))

    if failures:
        print("暂存安全检查失败；未读取工作区正式数据。", file=sys.stderr)
        for path, reason in failures:
            print(f"- {path}: {reason}", file=sys.stderr)
        return 2

    print(f"暂存安全检查通过：{len(paths)} 个 staged blob。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
