"""Aimaxhug uploader using the official example repository's wire contract.

The documentation-linked Aimaxhug repository confirms the upload host/path,
``Authorization: Bearer`` authentication, multipart field ``file``, and the
``status``/``data`` response wrapper containing ``tmp_url``. The local request
matches that example. Live synthetic uploads have nevertheless returned HTTP 401,
then succeeded with HTTP 200 after the credential was replaced; the observed
response matched the documented ``status``/``data`` wrapper. The current
credential's upload permission is therefore verified, while video creation and
result delivery remain separate contracts. No upload is performed on construction.

Only immutable bytes from a stable, SHA-verified local snapshot are sent. The
256 MiB cap is a local limit, not a claim about the service. MIME is inferred from
the filename; this module does not transcode, decode, or alter the source. Returned
name/MIME/size come from that local snapshot, not untrusted server metadata.
URLs remain in memory and are excluded from repr. Public-URL validation is local
syntax/IP checking, not a DNS or reachability guarantee. Upload ambiguity has its
own exception, separate from paid video creation, and is never retried here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import stat

import httpx

from .client import KlingError, _TIMEOUT, _capture_directory_snapshot, _verify_directory_snapshot
from .seedance_client import _public_url, _quiet_transport_logs, _unique_object


UPLOAD_URL = "https://base-api.aimaxhug.com/api/v2/upload/file"
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
_REPARSE_POINT = 0x400


class SeedanceUploadError(KlingError):
    """Safe upload error; never carries raw response, URL, key, or source path."""


class UploadUnknownOutcome(SeedanceUploadError):
    """An upload may have reached the service; this is not a paid creation error."""

    def __init__(self) -> None:
        super().__init__("Aimaxhug 上传结果未知，禁止自动重试；请人工核查上传结果", kind="upload_unknown")


@dataclass(frozen=True)
class UploadResult:
    url: str = field(repr=False, compare=False)
    sha256: str
    name: str
    mime_type: str
    size: int


def _source_error() -> SeedanceUploadError:
    return SeedanceUploadError(
        "上传源文件无效、不在允许目录、超过本地限制、发生变化或 SHA-256 不匹配",
        kind="invalid_source",
    )


def _local_path(value: str | Path) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or raw.startswith(("\\\\", "//")):
        raise _source_error()
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise _source_error()
    path = Path(raw)
    if (".." in path.parts or (path.drive and not path.is_absolute())
            or any(":" in part for part in path.parts if part != path.anchor)):
        raise _source_error()
    return Path(os.path.abspath(path))


def _file_stamp(info: os.stat_result) -> tuple[int, int, int, int, int]:
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
            or (info.st_dev, info.st_ino) == (0, 0)):
        raise _source_error()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_snapshot(path: str | Path, allowed_root: str | Path, expected_sha256: str) -> tuple[bytes, str, str, str]:
    """Check paths and file identity before reading; verify stability before send."""
    try:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise _source_error()
        source = _local_path(path)
        root = _local_path(allowed_root)
        source.relative_to(root)  # Lexical boundary before any file open.
        snapshot = _capture_directory_snapshot(root, source.parent)
        before = _file_stamp(source.lstat())
        if not 0 < before[2] <= MAX_UPLOAD_BYTES:
            raise _source_error()
        mime, encoding = mimetypes.guess_type(source.name)
        if encoding is not None or mime is None or mime.split("/", 1)[0] not in {"image", "video", "audio"}:
            raise _source_error()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            opened = _file_stamp(os.fstat(descriptor))
            # Windows path stat and descriptor stat can expose different ctime
            # semantics. Compare identity/size/mtime across APIs; compare each
            # API's full stamp (including ctime) against its own before value.
            if opened[:4] != before[:4]:
                raise _source_error()
            _verify_directory_snapshot(snapshot)
            content = bytearray()
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_UPLOAD_BYTES - len(content) + 1))
                if not chunk:
                    break
                if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
                    raise _source_error()
                content.extend(chunk)
                digest.update(chunk)
            if _file_stamp(os.fstat(descriptor)) != opened or _file_stamp(source.lstat()) != before:
                raise _source_error()
            _verify_directory_snapshot(snapshot)
            sha256 = digest.hexdigest()
            if len(content) != before[2] or sha256 != expected_sha256.lower():
                raise _source_error()
            return bytes(content), sha256, source.name, mime
        finally:
            os.close(descriptor)
    except Exception:
        # Includes errors from the shared directory helpers (whose text is for
        # downloads), filesystem exceptions, and invalid path values.
        raise _source_error() from None


def _response_url(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise UploadUnknownOutcome()
    scopes = [payload]
    if "data" in payload:
        if not isinstance(payload["data"], Mapping):
            raise UploadUnknownOutcome()
        scopes.append(payload["data"])
    urls = [scope["tmp_url"] for scope in scopes if "tmp_url" in scope]
    if not urls or any(not _public_url(url) for url in urls) or len(set(urls)) != 1:
        raise UploadUnknownOutcome()
    return urls[0]


class SeedanceUploader:
    def __init__(self, api_key_provider: Callable[[], str], transport: httpx.BaseTransport | None = None) -> None:
        if not callable(api_key_provider):
            raise SeedanceUploadError("上传器需要 API Key 提供函数", kind="credential")
        if transport is not None and not isinstance(transport, httpx.BaseTransport):
            raise SeedanceUploadError("上传器只接受低层 HTTP transport", kind="configuration")
        self._api_key_provider = api_key_provider
        self._client = httpx.Client(
            transport=transport, auth=None, headers=None, cookies=None,
            event_hooks=None, trust_env=False, proxy=None, mounts=None, follow_redirects=False,
        )

    def upload_file(self, path: str | Path, *, allowed_root: str | Path, expected_sha256: str) -> UploadResult:
        content, sha256, name, mime = _read_snapshot(path, allowed_root, expected_sha256)
        try:
            api_key = self._api_key_provider()
        except Exception:
            raise SeedanceUploadError("无法取得 Aimaxhug 上传 API Key", kind="credential") from None
        if not isinstance(api_key, str) or not api_key or any(ord(c) <= 32 or ord(c) >= 127 for c in api_key):
            raise SeedanceUploadError("Aimaxhug 上传 API Key 无效", kind="credential")
        try:
            # Official example wire contract. No raw path/file handle is passed to httpx.
            # Fresh Request bypasses inherited cookie/auth state on every upload.
            request = httpx.Request(
                "POST", UPLOAD_URL, headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (name, content, mime)}, extensions={"timeout": _TIMEOUT.as_dict()},
            )
            with _quiet_transport_logs():
                try:
                    response = self._client.send(request, auth=None, follow_redirects=False)
                except (Exception, KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise UploadUnknownOutcome() from None
                try:
                    code = response.status_code
                    if not 200 <= code < 300:
                        if code == 408 or code >= 500:
                            raise UploadUnknownOutcome()
                        kind = "auth" if code in {401, 403} else "http"
                        raise SeedanceUploadError(f"Aimaxhug 上传请求失败（HTTP {code}）", kind=kind)
                    try:
                        url = _response_url(response.json(object_pairs_hook=_unique_object))
                    except Exception:
                        raise UploadUnknownOutcome() from None
                    return UploadResult(url=url, sha256=sha256, name=name, mime_type=mime, size=len(content))
                finally:
                    response.close()
        except SeedanceUploadError:
            raise
        except Exception:
            raise UploadUnknownOutcome() from None

    def close(self) -> None:
        try:
            with _quiet_transport_logs():
                self._client.close()
        except Exception:
            raise SeedanceUploadError("无法安全关闭 Aimaxhug 上传连接", kind="close") from None
