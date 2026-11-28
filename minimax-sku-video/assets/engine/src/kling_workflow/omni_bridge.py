"""Fail-closed project bridge to the installed Aimaxhug Omni client.

The bridge keeps private prompts, local images, credentials, and provider URLs
out of console output and processing logs. Paid creation is available only via
an explicit caller flag; unknown creation outcomes permanently block resubmit
for that SKU/version until an operator resolves the task outside this bridge.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import httpx
import importlib.util
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from PIL import Image

from .processing_log import ProcessingLog, ProcessingLogError
from .seedance_credentials import SeedanceCredentialStore
from .validation import validate_video


_MODELS = ("omni-fast-v2v-no-water", "omni-fast", "omni-fast-no-water")
_ASSET_ROLES = ("model_image", "reverse_flat", "action_storyboard")
_IMAGE_MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_PROMPT_BYTES = 128 * 1024
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEO_BYTES = 512 * 1024 * 1024
_SKU = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION = re.compile(r"^v(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,5})$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,180}$")
_SAFE_REMOTE_STATES = frozenset({
    "queued", "pending", "processing", "running", "in_progress", "created",
    "completed", "complete", "succeeded", "success", "done", "finished",
    "failed", "failure", "error", "cancelled", "canceled", "rejected",
})
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_CREATION_FAILURE_KINDS = frozenset({
    "none", "timeout", "connection", "response_parse", "http_rejection",
    "missing_task_id", "unknown",
})
_QUERY_FAILURE_CATEGORIES = frozenset({
    "upload_timeout", "busy", "auth", "rate_limit", "server_error",
    "parse", "connection", "timeout", "unknown",
})
_REMOTE_DONE_STATES = frozenset({
    "completed", "complete", "succeeded", "success", "done", "finished",
})
_REMOTE_FAILED_STATES = frozenset({
    "failed", "failure", "error", "cancelled", "canceled", "rejected",
})


def _safe_http_status(value: object) -> int | None:
    if type(value) is int and 100 <= value <= 599:
        return value
    return None


def _exception_chain(exc: BaseException, *, limit: int = 5) -> list[BaseException]:
    chain: list[BaseException] = []
    pending = [exc]
    seen: set[int] = set()
    while pending and len(chain) < limit:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        cause = current.__cause__
        context = current.__context__
        if cause is not None:
            pending.append(cause)
        if context is not None and context is not cause:
            pending.append(context)
    return chain


def _creation_failure_kind(
    *, client: Any, omni: Any, status: int | None, exc: BaseException | None = None,
    has_task_id: bool = False,
) -> str:
    if exc is not None:
        chain = _exception_chain(exc)
        requests_module = getattr(client, "_requests", None)
        exceptions_module = getattr(requests_module, "exceptions", None)
        timeout_type = getattr(exceptions_module, "Timeout", None)
        connection_type = getattr(exceptions_module, "ConnectionError", None)
        if isinstance(timeout_type, type) and any(isinstance(item, timeout_type) for item in chain):
            return "timeout"
        if isinstance(connection_type, type) and any(isinstance(item, connection_type) for item in chain):
            return "connection"

        aimaxhug_error = getattr(omni, "AimaxhugError", None)
        known_provider_error = isinstance(aimaxhug_error, type) and any(
            isinstance(item, aimaxhug_error) for item in chain
        )
        if known_provider_error:
            if status is not None and any(isinstance(item, ValueError) for item in chain):
                return "response_parse"
            if status is not None and status >= 400:
                return "http_rejection"
        return "unknown"

    if status is not None and not 200 <= status < 300:
        return "http_rejection"
    if has_task_id:
        return "none"
    if status is not None and status >= 400:
        return "http_rejection"
    return "missing_task_id"


def _exception_http_status(exc: BaseException, omni: Any) -> int | None:
    aimaxhug_error = getattr(omni, "AimaxhugError", None)
    if not isinstance(aimaxhug_error, type):
        return None
    for item in _exception_chain(exc):
        if isinstance(item, aimaxhug_error):
            return _safe_http_status(getattr(item, "status_code", None))
    return None


def _serialized_request_json_bytes(client: Any, body: dict[str, Any]) -> int | None:
    requests_module = getattr(client, "_requests", None)
    models = getattr(requests_module, "models", None)
    complexjson = getattr(models, "complexjson", None)
    serializer = getattr(complexjson, "dumps", None)
    if not callable(serializer):
        serializer = json.dumps
    try:
        serialized = serializer(body, allow_nan=False)
        return len(serialized.encode("utf-8")) if type(serialized) is str else None
    except Exception:
        return None


def _record_creation_diagnostic(
    log: ProcessingLog, *, result: str, failure_kind: str, elapsed_ms: int,
    reference_count: int, request_json_bytes: int | None, http_status: int | None,
    sku: str, version: str,
) -> None:
    if failure_kind not in _CREATION_FAILURE_KINDS:
        failure_kind = "unknown"
    evidence = (
        f"phase=create_post;elapsed_ms={max(0, int(elapsed_ms))};attempt_count=1;"
        f"reference_count={max(0, int(reference_count))};"
        f"request_json_bytes={request_json_bytes if type(request_json_bytes) is int and request_json_bytes >= 0 else 'null'};"
        f"http_status={http_status if _safe_http_status(http_status) is not None else 'null'}"
    )
    log.record_step(
        model="local", role="query", reason="Omni创建调用诊断", result=result,
        evidence=evidence, exception=failure_kind, sku=sku, version=version,
    )


def _query_http_failure_category(status: int | None) -> str | None:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status == 408:
        return "timeout"
    if status is not None and status >= 500:
        return "server_error"
    if status is not None and not 200 <= status < 300:
        return "unknown"
    return None


def _query_structured_failure_category(response: object) -> str | None:
    code_categories = {
        "context_deadline_exceeded": "timeout",
        "reference_upload_timeout": "upload_timeout",
        "upload_timeout": "upload_timeout",
        "busy": "busy",
        "task_busy": "busy",
        "server_busy": "busy",
        "unauthorized": "auth",
        "forbidden": "auth",
        "invalid_api_key": "auth",
        "authentication_failed": "auth",
        "rate_limit": "rate_limit",
        "too_many_requests": "rate_limit",
        "server_error": "server_error",
        "internal_error": "server_error",
        "internal_server_error": "server_error",
        "parse_error": "parse",
        "invalid_json": "parse",
    }
    if not isinstance(response, dict):
        return None
    nodes = [response.get("data"), response.get("error"), response]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("error_code", "errorCode", "code"):
            value = node.get(key)
            if type(value) is str:
                normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
                category = code_categories.get(normalized)
                if category in _QUERY_FAILURE_CATEGORIES:
                    return category
    return None


def _query_exception_failure_category(
    client: Any, omni: Any, exc: BaseException
) -> tuple[str, int | None]:
    chain = _exception_chain(exc)
    requests_module = getattr(client, "_requests", None)
    exceptions_module = getattr(requests_module, "exceptions", None)
    timeout_type = getattr(exceptions_module, "Timeout", None)
    connection_type = getattr(exceptions_module, "ConnectionError", None)
    if isinstance(timeout_type, type) and any(isinstance(item, timeout_type) for item in chain):
        return "timeout", _exception_http_status(exc, omni)
    if isinstance(connection_type, type) and any(isinstance(item, connection_type) for item in chain):
        return "connection", _exception_http_status(exc, omni)
    if any(isinstance(item, ValueError) for item in chain):
        return "parse", _exception_http_status(exc, omni)
    status = _exception_http_status(exc, omni)
    return _query_http_failure_category(status) or "unknown", status


def _task_id_echo_state(omni: Any, response: object, task_id: str) -> str:
    if not isinstance(response, dict):
        return "absent"
    try:
        echoed = omni.task_id_of(response)
    except Exception:
        return "invalid"
    if not echoed:
        return "absent"
    return "matched" if echoed == task_id else "mismatch"


def _record_query_diagnostic(
    log: ProcessingLog, *, result: str, failure_category: str | None,
    http_status: int | None, task_state: str, task_id_echo: str,
    video_field_present: bool, sku: str, version: str,
) -> None:
    category = failure_category if failure_category in _QUERY_FAILURE_CATEGORIES else None
    safe_state = task_state if task_state in _SAFE_REMOTE_STATES else "unknown"
    safe_echo = task_id_echo if task_id_echo in {"matched", "mismatch", "absent", "invalid"} else "invalid"
    evidence = (
        f"phase=status_get;http_status={http_status if _safe_http_status(http_status) is not None else 'null'};"
        f"task_state={safe_state};task_id_echo={safe_echo};"
        f"video_field_present={'true' if video_field_present else 'false'};"
        f"failure_category={category or 'none'}"
    )
    log.record_step(
        model="local", role="query", reason="Omni状态查询诊断", result=result,
        evidence=evidence, exception=category or "", sku=sku, version=version,
    )


class OmniBridgeError(RuntimeError):
    """A user-safe bridge error; never includes provider or credential text."""


class _OmniArgumentError(Exception):
    pass


class _OmniArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse embeds rejected values and unknown arguments in `message`.
        raise _OmniArgumentError


@dataclass(frozen=True)
class _Prepared:
    sku: str
    version: str
    model: str
    prompt: str
    prompt_sha256: str
    images: dict[str, tuple[Path, bytes | None, str, int, int]]


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _reject_reparse_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    for component in reversed((absolute, *absolute.parents)):
        if _is_reparse(component):
            raise OmniBridgeError("路径包含链接或重解析点")


def _ensure_directory(path: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise OmniBridgeError("目标路径超出项目目录") from None
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            try:
                cursor.mkdir()
            except FileExistsError:
                pass
            except OSError:
                raise OmniBridgeError("无法安全创建Omni本地目录") from None
            try:
                info = cursor.lstat()
            except OSError:
                raise OmniBridgeError("无法核验Omni本地目录") from None
        if not stat.S_ISDIR(info.st_mode) or _is_reparse(cursor):
            raise OmniBridgeError("Omni本地目录不是普通目录")


def _safe_json_read(path: Path, maximum: int) -> Any:
    try:
        _reject_reparse_components(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise OmniBridgeError("输入必须是大小合规的普通文件")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > maximum or (before.st_size, before.st_mtime_ns) != (
            after.st_size, after.st_mtime_ns
        ):
            raise OmniBridgeError("输入文件在读取期间发生变化")
        return json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except OmniBridgeError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise OmniBridgeError("无法安全读取Omni输入文件") from None


def _stable_file_bytes(path: Path, maximum: int) -> bytes:
    try:
        _reject_reparse_components(path)
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise OmniBridgeError("输入文件不是合规的普通文件")
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if (
            len(payload) > maximum
            or identity(before) != identity(after)
            or identity(after) != identity(current)
            or _is_reparse(path)
        ):
            raise OmniBridgeError("输入文件在读取期间发生变化")
        return payload
    except OmniBridgeError:
        raise
    except OSError:
        raise OmniBridgeError("无法安全读取Omni输入文件") from None


def _public_https_url(value: object) -> str:
    if type(value) is not str or len(value) > 4096 or any(c.isspace() for c in value) or "\\" in value:
        raise OmniBridgeError("服务端结果地址无效")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise OmniBridgeError("服务端结果地址无效") from None
    if (
        parts.scheme != "https" or not host
        or parts.username is not None or parts.password is not None
        or port == 0 or "#" in value
    ):
        raise OmniBridgeError("服务端结果地址无效")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        lowered = host.lower().rstrip(".")
        if (
            "." not in lowered
            or lowered.endswith((".localhost", ".local", ".internal", ".home", ".lan"))
            or all(c.isdigit() or c == "." for c in lowered)
        ):
            raise OmniBridgeError("服务端结果地址无效") from None
    else:
        if not ip.is_global:
            raise OmniBridgeError("服务端结果地址无效")
    try:
        addresses = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError
    except (OSError, ValueError):
        raise OmniBridgeError("服务端结果地址无法通过公网地址检查") from None
    return value


def _download_video(url: str, staging_path: Path) -> Path:
    checked = _public_https_url(url)
    total = 0
    try:
        with httpx.stream(
            "GET", checked, timeout=120.0, follow_redirects=False,
            headers={"Accept": "video/mp4,application/octet-stream"},
        ) as response:
            if response.status_code != 200:
                raise OmniBridgeError("候选视频下载未成功")
            declared = response.headers.get("content-length")
            if declared and int(declared) > _MAX_VIDEO_BYTES:
                raise OmniBridgeError("候选视频超过本地大小上限")
            with staging_path.open("xb") as stream:
                for block in response.iter_bytes(1024 * 1024):
                    total += len(block)
                    if total > _MAX_VIDEO_BYTES:
                        raise OmniBridgeError("候选视频超过本地大小上限")
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
        if total == 0:
            raise OmniBridgeError("候选视频为空")
        return staging_path
    except OmniBridgeError:
        staging_path.unlink(missing_ok=True)
        raise
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise OmniBridgeError("候选视频下载失败；远程任务号已保留") from None


def _load_omni_module() -> Any:
    path = Path.home() / ".codex" / "skills" / "aimaxhug-generation" / "scripts" / "omni_video.py"
    if not path.is_file():
        raise OmniBridgeError("未找到已安装的Aimaxhug Omni模块")
    dotenv_disabled = os.environ.get("PYTHON_DOTENV_DISABLED")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    dotenv_module = None
    original_load_dotenv = None
    try:
        try:
            dotenv_module = importlib.import_module("dotenv")
            original_load_dotenv = getattr(dotenv_module, "load_dotenv", None)
            if callable(original_load_dotenv):
                dotenv_module.load_dotenv = lambda *args, **kwargs: False
        except ImportError:
            dotenv_module = None
        spec = importlib.util.spec_from_file_location("_codex_aimaxhug_omni_video", path)
        if spec is None or spec.loader is None:
            raise ImportError
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        raise OmniBridgeError("无法安全载入已安装的Aimaxhug Omni模块") from None
    finally:
        if dotenv_module is not None and callable(original_load_dotenv):
            dotenv_module.load_dotenv = original_load_dotenv
        if dotenv_disabled is None:
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)
        else:
            os.environ["PYTHON_DOTENV_DISABLED"] = dotenv_disabled


def _extract_listed_models(value: Any, allowed: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 8:
            continue
        if isinstance(node, str):
            if node in allowed:
                found.add(node)
        elif isinstance(node, dict):
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
    return found


class OmniBridge:
    def __init__(
        self,
        project_root: str | Path,
        *,
        credential_store: Any | None = None,
        omni_module: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
        video_validator: Callable[[Path], Any] | None = None,
        downloader: Callable[[str, Path], Path] | None = None,
    ) -> None:
        try:
            self.root = Path(project_root).resolve(strict=True)
            if not self.root.is_dir() or _is_reparse(self.root):
                raise OSError
        except OSError:
            raise OmniBridgeError("Omni项目目录无效") from None
        self.credentials = credential_store or SeedanceCredentialStore()
        self.omni = omni_module
        self.client_factory = client_factory
        self.video_validator = video_validator or (
            lambda path: validate_video(
                path,
                write_report=False,
                expected_aspect_ratio=9 / 16,
                decode_all_frames=True,
                expected_duration_seconds=10.0,
            )
        )
        self.downloader = downloader or _download_video

    def _load_omni(self) -> Any:
        if self.omni is None:
            self.omni = _load_omni_module()
        return self.omni

    def _load_client_factory(self) -> Callable[..., Any]:
        if self.client_factory is not None:
            return self.client_factory
        return lambda *, api_key: self._load_omni().AimaxhugClient(api_key=api_key)

    def _manifest_file(self, manifest: str | Path) -> Path:
        candidate = Path(manifest)
        if candidate.is_absolute() or PureWindowsPath(str(manifest)).is_absolute() or ".." in candidate.parts:
            raise OmniBridgeError("manifest必须使用项目内相对路径")
        path = self.root / candidate
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
            _reject_reparse_components(path)
            if not resolved.is_file():
                raise ValueError
            return resolved
        except (OSError, ValueError):
            raise OmniBridgeError("manifest必须是项目目录内的普通文件") from None

    def _asset_path(self, value: object) -> Path:
        if type(value) is not str or not value or len(value) > 1024:
            raise OmniBridgeError("图片路径无效")
        relative = Path(value)
        if relative.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in relative.parts:
            raise OmniBridgeError("图片路径必须位于项目目录内")
        path = self.root / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
            _reject_reparse_components(path)
            if not resolved.is_file():
                raise ValueError
            return resolved
        except (OSError, ValueError):
            raise OmniBridgeError("图片必须是项目目录内的普通文件") from None

    def _prepare(self, manifest: str | Path, *, include_image_bytes: bool) -> _Prepared:
        path = self._manifest_file(manifest)
        data = _safe_json_read(path, _MAX_MANIFEST_BYTES)
        if not isinstance(data, dict) or set(data) != {
            "schema_version", "sku", "version", "model", "prompt_file", "prompt_review", "images"
        }:
            raise OmniBridgeError("Omni manifest字段不符合当前输入规则")
        sku, version, model = data["sku"], data["version"], data["model"]
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise OmniBridgeError("Omni manifest版本无效")
        if type(sku) is not str or not _SKU.fullmatch(sku) or type(version) is not str or not _VERSION.fullmatch(version):
            raise OmniBridgeError("款号或版本号格式无效")
        omni = self._load_omni()
        supported = tuple(getattr(omni, "MODELS", _MODELS))
        if type(model) is not str or model not in supported:
            raise OmniBridgeError("所选Omni模型不在本地已知范围")

        review = data["prompt_review"]
        if (
            not isinstance(review, dict)
            or set(review) != {"reviewer", "decision", "sha256"}
            or review.get("reviewer") != "Sol"
            or review.get("decision") != "approved"
            or type(review.get("sha256")) is not str
            or not _SHA256.fullmatch(review["sha256"])
        ):
            raise OmniBridgeError("Omni提示词尚未通过Sol审核绑定")
        prompt_path = self._asset_path(data["prompt_file"])
        prompt_bytes = _stable_file_bytes(prompt_path, _MAX_PROMPT_BYTES)
        try:
            prompt = prompt_bytes.decode("utf-8")
        except UnicodeError:
            raise OmniBridgeError("Omni提示词必须是UTF-8文本") from None
        prompt_sha = hashlib.sha256(prompt_bytes).hexdigest()
        if not prompt.strip() or prompt_sha.lower() != review["sha256"].lower():
            raise OmniBridgeError("Omni提示词与审核记录不匹配")

        raw_images = data["images"]
        if not isinstance(raw_images, dict) or set(raw_images) != set(_ASSET_ROLES):
            raise OmniBridgeError("Omni必须且只能提供模特图、反面平铺和动作分镜")
        images: dict[str, tuple[Path, bytes | None, str, int, int]] = {}
        for role in _ASSET_ROLES:
            image_path = self._asset_path(raw_images[role])
            blob = _stable_file_bytes(image_path, _MAX_IMAGE_BYTES)
            try:
                with Image.open(__import__("io").BytesIO(blob)) as image:
                    image_format = image.format
                    width, height = image.size
                    frames = getattr(image, "n_frames", 1)
                    image.verify()
                if image_format not in _IMAGE_MIMES or width <= 0 or height <= 0 or frames != 1:
                    raise ValueError
            except Exception:
                raise OmniBridgeError("Omni输入图片必须是有效的JPEG、PNG或WEBP静态图片") from None
            payload = blob if include_image_bytes else None
            images[role] = (image_path, payload, _IMAGE_MIMES[image_format], width, height)
        return _Prepared(sku, version, model, prompt, prompt_sha, images)

    @staticmethod
    def _preview(prepared: _Prepared) -> dict[str, Any]:
        return {
            "state": "offline_preflight_passed",
            "sku": prepared.sku,
            "version": prepared.version,
            "model": prepared.model,
            "aspect_ratio": "9:16",
            "duration_seconds": 10,
            "prompt_review": "approved_hash_match",
            "inputs": [
                {"role": role, "width": prepared.images[role][3], "height": prepared.images[role][4]}
                for role in _ASSET_ROLES
            ],
        }

    def preview(self, manifest: str | Path) -> dict[str, Any]:
        with self._processing_log(
            command="omni-preview", sku=None, version=None, mode="offline", paid=False
        ) as log:
            prepared = self._prepare(manifest, include_image_bytes=False)
            log.record_step(
                model="local", role="query", reason="离线检查Omni图片输入和视频参数",
                result="offline_preflight_passed", evidence="三图角色、审核绑定、9比16和10秒字段已核对",
                sku=prepared.sku, version=prepared.version,
            )
            return self._preview(prepared)

    def _task_path(self, sku: str, version: str) -> Path:
        if not _SKU.fullmatch(sku) or not _VERSION.fullmatch(version):
            raise OmniBridgeError("款号或版本号格式无效")
        path = self.root / ".omni_bridge" / "tasks" / sku / f"{version}.json"
        _ensure_directory(path.parent, self.root)
        try:
            _reject_reparse_components(path.parent)
        except OmniBridgeError:
            raise OmniBridgeError("Omni任务记录目录不安全") from None
        return path

    @staticmethod
    def _task_receipt_paths(path: Path) -> tuple[Path, Path]:
        receipt = path.with_name(f"{path.stem}.task.json")
        staged = path.with_name(f".{path.stem}.task-recovery.tmp")
        return receipt, staged

    @contextlib.contextmanager
    def _submission_lock(self, path: Path) -> Iterator[None]:
        lock_path = path.with_name(f".{path.stem}.submit.lock")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            handle = os.open(lock_path, flags, 0o600)
        except FileExistsError:
            raise OmniBridgeError(
                "此款号和版本存在提交互斥锁；请人工核对，禁止再次提交"
            ) from None
        except OSError:
            raise OmniBridgeError("无法安全锁定Omni提交；没有发出请求") from None
        try:
            os.close(handle)
        except OSError:
            try:
                os.close(handle)
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise OmniBridgeError("无法安全锁定Omni提交；没有发出请求") from None
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise OmniBridgeError(
                    "Omni提交互斥锁无法安全释放；请人工核对"
                ) from None

    @classmethod
    def _reject_existing_submission_artifacts(cls, path: Path) -> None:
        receipt, staged = cls._task_receipt_paths(path)
        for candidate in (path, receipt, staged):
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise OmniBridgeError(
                    "无法确认Omni任务记录或回执状态；请人工核对，禁止提交"
                ) from None
            raise OmniBridgeError(
                "此款号和版本已有任务记录或回执；请人工核对，禁止再次提交"
            )

    @staticmethod
    def _task_receipt(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "sku": record["sku"],
            "version": record["version"],
            "model": record["model"],
            "state": "task_known",
            "task_id": record["task_id"],
        }

    @classmethod
    def _write_task_receipt(cls, path: Path, record: dict[str, Any]) -> None:
        receipt, staged = cls._task_receipt_paths(path)
        expected = cls._task_receipt(record)
        try:
            with staged.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(expected, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(staged, receipt)
            except FileExistsError:
                _reject_reparse_components(receipt)
                existing = _safe_json_read(receipt, 32 * 1024)
                if existing != expected:
                    raise OmniBridgeError("Omni任务号回执冲突；禁止继续查询或重试") from None
            staged.unlink(missing_ok=True)
        except OmniBridgeError:
            raise
        except OSError:
            # Keep the fsynced staged receipt: resume can recover it if publishing
            # the canonical sidecar or replacing the main record failed.
            raise OmniBridgeError("Omni任务号回执保存失败；原任务已锁定，禁止重试") from None

    @staticmethod
    def _write_new_record(path: Path, record: dict[str, Any]) -> None:
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(record, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise OmniBridgeError("此款号和版本已有任务记录；禁止再次提交") from None
        except OSError:
            raise OmniBridgeError("无法安全创建Omni任务记录；未提交请求") from None

    @staticmethod
    def _replace_record(path: Path, record: dict[str, Any]) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(record, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise OmniBridgeError("Omni任务状态保存失败；禁止自动重试") from None

    def _read_record(self, sku: str, version: str) -> tuple[Path, dict[str, Any]]:
        path = self._task_path(sku, version)
        try:
            _reject_reparse_components(path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024:
                raise ValueError
            record = _safe_json_read(path, 32 * 1024)
            if (
                not isinstance(record, dict)
                or record.get("schema_version") != 1
                or record.get("sku") != sku
                or record.get("version") != version
                or record.get("model") not in _MODELS
                or record.get("state") not in {"creation_unknown", "task_known", "pending_human_review", "remote_failed"}
            ):
                raise ValueError
            task_id = record.get("task_id")
            if task_id is not None and (type(task_id) is not str or not _TASK_ID.fullmatch(task_id)):
                raise ValueError
            if record["state"] == "creation_unknown" and task_id is None:
                receipt, staged = self._task_receipt_paths(path)
                recovered = []
                expected_keys = {"schema_version", "sku", "version", "model", "state", "task_id"}
                for candidate in (receipt, staged):
                    try:
                        _reject_reparse_components(candidate)
                        candidate_info = candidate.lstat()
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(candidate_info.st_mode) or candidate_info.st_size > 32 * 1024:
                        raise ValueError
                    saved = _safe_json_read(candidate, 32 * 1024)
                    if (
                        not isinstance(saved, dict)
                        or set(saved) != expected_keys
                        or saved.get("schema_version") != 1
                        or saved.get("sku") != sku
                        or saved.get("version") != version
                        or saved.get("model") != record.get("model")
                        or saved.get("model") not in _MODELS
                        or saved.get("state") != "task_known"
                        or type(saved.get("task_id")) is not str
                        or not _TASK_ID.fullmatch(saved["task_id"])
                    ):
                        raise ValueError
                    recovered.append(saved)
                if recovered:
                    if len({item["task_id"] for item in recovered}) != 1:
                        raise ValueError
                    receipt_record = recovered[0]
                    record.update(state="task_known", model=receipt_record["model"], task_id=receipt_record["task_id"])
            if record["state"] == "pending_human_review":
                local_path = record.get("local_path")
                relative = Path(local_path) if type(local_path) is str else Path("..")
                if (
                    relative.is_absolute()
                    or PureWindowsPath(str(local_path)).is_absolute()
                    or ".." in relative.parts
                    or len(relative.parts) != 2
                    or relative.parts[0] != "02_待审核"
                    or not relative.name.startswith(f"Omni_{sku}_{version}_")
                    or relative.suffix.lower() != ".mp4"
                ):
                    raise ValueError
                review_dir = self.root / "02_待审核"
                output_candidate = self.root / relative
                _reject_reparse_components(review_dir)
                _reject_reparse_components(output_candidate)
                resolved_output = output_candidate.resolve(strict=False)
                if resolved_output.parent != review_dir.resolve(strict=False):
                    raise ValueError
                if resolved_output.exists() and not resolved_output.is_file():
                    raise ValueError
                record["local_path"] = relative.as_posix()
            return path, record
        except FileNotFoundError:
            raise OmniBridgeError("未找到对应Omni本地任务记录") from None
        except OmniBridgeError:
            raise
        except (OSError, ValueError):
            raise OmniBridgeError("Omni本地任务记录无效或不安全") from None

    @contextlib.contextmanager
    def _processing_log(
        self, *, command: str, sku: str | None, version: str | None, mode: str, paid: bool
    ) -> Iterator[ProcessingLog]:
        try:
            entry = ProcessingLog.start(
                self.root, command=command, sku=sku, version=version,
                network_mode=mode, paid_authorization_present=paid,
            )
        except ProcessingLogError:
            raise OmniBridgeError("无法安全写入Omni处理日志；操作未执行") from None
        try:
            yield entry
        except BaseException:
            try:
                entry.finish(status="failure", exit_code=1)
            except ProcessingLogError:
                pass
            raise
        else:
            try:
                entry.finish(status="success", exit_code=0)
            except ProcessingLogError:
                raise OmniBridgeError("Omni操作完成但结束日志写入失败；请核对本地任务记录") from None

    def _client(self) -> Any:
        try:
            if self.client_factory is None:
                self._load_omni()
                try:
                    requests_spec = importlib.util.find_spec("requests")
                except (ImportError, ValueError):
                    requests_spec = None
                if requests_spec is None:
                    raise OmniBridgeError("项目环境缺少requests依赖；没有读取凭据")
            status = self.credentials.status()
            if status.get("api-key") != "PRESENT":
                raise OmniBridgeError("Aimaxhug凭据状态为ABSENT；没有发出请求")
            key = self.credentials.read()
            client = self._load_client_factory()(api_key=key)
            del key
            return client
        except OmniBridgeError:
            raise
        except SystemExit:
            raise OmniBridgeError("Aimaxhug客户端依赖不可用") from None
        except Exception:
            raise OmniBridgeError("无法安全读取Aimaxhug凭据或创建客户端") from None

    def read_only_check(self, model: str) -> dict[str, Any]:
        with self._processing_log(
            command="omni-read-only", sku=None, version=None, mode="read_only", paid=False
        ) as log:
            omni = self._load_omni()
            supported = tuple(getattr(omni, "MODELS", _MODELS))
            if model not in supported:
                raise OmniBridgeError("所选Omni模型不在本地已知范围")
            client = self._client()
            try:
                models_endpoint = self._load_omni().VIDEO_ENDPOINT.rstrip("/") + "/models"
                status_code, response = client.get_with_status(models_endpoint, timeout=30)
            except Exception:
                raise OmniBridgeError("只读模型查询失败；没有提交生成请求") from None
            models = _extract_listed_models(response, supported)
            if type(status_code) is int and status_code in (401, 403):
                access = "rejected"
            elif type(status_code) is int and 200 <= status_code < 300:
                access = "http_2xx"
            else:
                access = "unavailable"
            result = {
                "state": "read_only_check_complete",
                "endpoint_access": access,
                "http_status": status_code if type(status_code) is int and 100 <= status_code <= 599 else None,
                "selected_model_listed": model in models,
                "capability_status": "listed" if model in models else "not_established",
                "scope_note": "只证明只读模型端点的响应，不证明Omni生成、上传或结果下载合同",
            }
            log.record_step(
                model="local", role="query", reason="只读核验Aimaxhug模型端点",
                result=access, evidence="仅保留已知模型白名单状态",
            )
            return result

    def _body(self, prepared: _Prepared) -> dict[str, Any]:
        role_payloads = {}
        for role, (_, blob, mime, _, _) in prepared.images.items():
            if blob is None:
                raise OmniBridgeError("图片数据未准备")
            role_payloads[role] = f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")
        args = SimpleNamespace(
            model=prepared.model,
            prompt=prepared.prompt,
            ratio="9:16",
            seconds=None,
            duration=10,
            image_url=role_payloads["model_image"],
            images=[role_payloads["reverse_flat"], role_payloads["action_storyboard"]],
            first_image_url=None,
            last_image_url=None,
            video=None,
            video_url=None,
            videos=None,
        )
        try:
            body = self._load_omni().build_body(args)
        except Exception:
            raise OmniBridgeError("Omni请求体未通过已安装模块校验") from None
        expected = {"model", "prompt", "aspect_ratio", "duration", "image_url", "images"}
        if set(body) != expected or body.get("aspect_ratio") != "9:16" or body.get("duration") != 10:
            raise OmniBridgeError("Omni请求字段超出已核验的输入合同")
        if len(body.get("images", [])) != 2:
            raise OmniBridgeError("Omni请求必须包含且只能包含三个图片角色")
        if type(body.get("prompt")) is not str:
            raise OmniBridgeError("Omni请求提示词未通过审核字节校验")
        # The installed builder strips leading/trailing whitespace; restore the
        # exact reviewed text and verify it still hashes to the approved bytes.
        body["prompt"] = prepared.prompt
        if hashlib.sha256(body["prompt"].encode("utf-8")).hexdigest() != prepared.prompt_sha256:
            raise OmniBridgeError("Omni请求提示词未通过审核字节校验")
        return body

    def submit(self, manifest: str | Path, *, paid_confirmation: bool) -> dict[str, Any]:
        with self._processing_log(
            command="omni-submit", sku=None, version=None,
            mode="paid", paid=paid_confirmation is True,
        ) as log:
            if paid_confirmation is not True:
                raise OmniBridgeError("Omni付费提交必须显式提供--confirm-paid-request")
            prepared = self._prepare(manifest, include_image_bytes=True)
            record_path = self._task_path(prepared.sku, prepared.version)
            with self._submission_lock(record_path):
                self._reject_existing_submission_artifacts(record_path)
                client = self._client()
                body = self._body(prepared)
                record: dict[str, Any] = {
                    "schema_version": 1,
                    "sku": prepared.sku,
                    "version": prepared.version,
                    "model": prepared.model,
                    "state": "creation_unknown",
                    "task_id": None,
                    "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                }
                self._write_new_record(record_path, record)
                log.record_step(
                    model="local", role="query", reason="提交前持久化Omni未知状态",
                    result="creation_unknown", evidence="当前版本已锁定，缺少任务号时禁止重试",
                    sku=prepared.sku, version=prepared.version,
                )
                log.set_paid_creation_status("creation_unknown")
                omni = self._load_omni()
                request_json_bytes = _serialized_request_json_bytes(client, body)
                post_started_ns = time.perf_counter_ns()
                try:
                    code, response = client.post_with_status(
                        omni.VIDEO_ENDPOINT, json=body, timeout=120
                    )
                except Exception as exc:
                    elapsed_ms = max(0, (time.perf_counter_ns() - post_started_ns) // 1_000_000)
                    http_status = _exception_http_status(exc, omni)
                    failure_kind = _creation_failure_kind(
                        client=client, omni=omni, status=http_status, exc=exc,
                    )
                    try:
                        _record_creation_diagnostic(
                            log, result="creation_unknown", failure_kind=failure_kind,
                            elapsed_ms=elapsed_ms, reference_count=len(prepared.images),
                            request_json_bytes=request_json_bytes, http_status=http_status,
                            sku=prepared.sku, version=prepared.version,
                        )
                    except Exception:
                        # The durable creation_unknown record remains the source of truth.
                        pass
                    raise OmniBridgeError("Omni创建结果未知；任务记录已锁定，禁止重试") from None
                elapsed_ms = max(0, (time.perf_counter_ns() - post_started_ns) // 1_000_000)
                http_status = _safe_http_status(code)
                try:
                    task_id = omni.task_id_of(response)
                except Exception:
                    failure_kind = "unknown"
                    try:
                        _record_creation_diagnostic(
                            log, result="creation_unknown", failure_kind=failure_kind,
                            elapsed_ms=elapsed_ms, reference_count=len(prepared.images),
                            request_json_bytes=request_json_bytes, http_status=http_status,
                            sku=prepared.sku, version=prepared.version,
                        )
                    except Exception:
                        pass
                    raise OmniBridgeError("Omni响应未能确认任务号；创建结果未知，禁止重试") from None
                if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
                    failure_kind = _creation_failure_kind(
                        client=client, omni=omni, status=http_status, has_task_id=False,
                    )
                    try:
                        _record_creation_diagnostic(
                            log, result="creation_unknown", failure_kind=failure_kind,
                            elapsed_ms=elapsed_ms, reference_count=len(prepared.images),
                            request_json_bytes=request_json_bytes, http_status=http_status,
                            sku=prepared.sku, version=prepared.version,
                        )
                    except Exception:
                        pass
                    raise OmniBridgeError("Omni响应未提供可用任务号；创建结果未知，禁止重试")
                record["state"] = "task_known"
                record["task_id"] = task_id
                record["http_status"] = http_status
                self._write_task_receipt(record_path, record)
                self._replace_record(record_path, record)
                log.set_paid_creation_status("task_known")
                failure_kind = _creation_failure_kind(
                    client=client, omni=omni, status=http_status, has_task_id=True,
                )
                _record_creation_diagnostic(
                    log, result="task_known", failure_kind=failure_kind,
                    elapsed_ms=elapsed_ms, reference_count=len(prepared.images),
                    request_json_bytes=request_json_bytes, http_status=http_status,
                    sku=prepared.sku, version=prepared.version,
                )
                log.record_step(
                    model="local", role="query", reason="已持久化Omni任务号",
                    result="task_known", evidence="后续只查询该本地任务记录",
                    sku=prepared.sku, version=prepared.version,
                )
            del body, prepared
            return self._query_and_save(client, record_path, record, log)

    def _query_and_save(
        self, client: Any, record_path: Path, record: dict[str, Any], log: ProcessingLog
    ) -> dict[str, Any]:
        task_id = record["task_id"]
        omni = self._load_omni()
        try:
            code, response = client.get_with_status(
                omni.status_endpoint(record["model"], task_id), timeout=30
            )
        except Exception as exc:
            category, http_status = _query_exception_failure_category(client, omni, exc)
            _record_query_diagnostic(
                log, result="query_unavailable", failure_category=category,
                http_status=http_status, task_state="unknown", task_id_echo="absent",
                video_field_present=False, sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError("Omni状态查询暂不可用；任务号已保留，请稍后继续查询") from None

        http_status = _safe_http_status(code)
        echo_state = _task_id_echo_state(omni, response, task_id)
        if http_status is None or not 200 <= http_status < 300:
            category = _query_http_failure_category(http_status) or _query_structured_failure_category(response) or "unknown"
            _record_query_diagnostic(
                log, result="query_unavailable", failure_category=category,
                http_status=http_status, task_state="unknown", task_id_echo=echo_state,
                video_field_present=False, sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError("Omni状态查询HTTP失败；任务号已保留，请稍后继续查询") from None
        if not isinstance(response, dict):
            _record_query_diagnostic(
                log, result="query_unavailable", failure_category="parse",
                http_status=http_status, task_state="unknown", task_id_echo=echo_state,
                video_field_present=False, sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError("Omni状态响应无法解析；任务号已保留") from None
        if echo_state in {"mismatch", "invalid"}:
            _record_query_diagnostic(
                log, result="task_id_mismatch", failure_category="unknown",
                http_status=http_status, task_state="unknown", task_id_echo=echo_state,
                video_field_present=False, sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError("Omni状态响应任务号与本地记录不匹配；本地任务号已保留") from None

        try:
            raw_state = omni.status_of(response)
            raw_url = omni.video_url_of(response)
        except Exception:
            _record_query_diagnostic(
                log, result="query_unavailable", failure_category="parse",
                http_status=http_status, task_state="unknown", task_id_echo=echo_state,
                video_field_present=False, sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError("Omni状态响应无法解析；任务号已保留") from None
        state = raw_state.strip().lower() if type(raw_state) is str else ""
        safe_state = state if state in _SAFE_REMOTE_STATES else "unknown"
        video_url = raw_url if type(raw_url) is str and raw_url else None
        video_field_present = video_url is not None
        structured_category = _query_structured_failure_category(response)
        data = response.get("data")
        success_false = response.get("success") is False or (
            isinstance(data, dict) and data.get("success") is False
        )
        done_states = getattr(omni, "DONE", _REMOTE_DONE_STATES)
        is_completed = state in done_states or (video_field_present and not state)

        if echo_state != "matched" and (state in _REMOTE_FAILED_STATES or is_completed):
            _record_query_diagnostic(
                log, result="task_id_unverified", failure_category=structured_category or "unknown",
                http_status=http_status, task_state=safe_state, task_id_echo=echo_state,
                video_field_present=video_field_present,
                sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError(
                "Omni终态或成片响应未回显匹配任务号；结果未接纳，任务号已保留"
            ) from None

        if state in _REMOTE_FAILED_STATES:
            record["state"] = "remote_failed"
            record["remote_state"] = state
            self._replace_record(record_path, record)
            _record_query_diagnostic(
                log, result="remote_failed", failure_category=structured_category or "unknown",
                http_status=http_status, task_state=safe_state, task_id_echo=echo_state,
                video_field_present=video_field_present,
                sku=record["sku"], version=record["version"],
            )
            return {
                "state": "remote_failed", "sku": record["sku"],
                "version": record["version"], "model": record["model"],
                "task_id": task_id, "remote_state": state,
                "review_status": "not_ready",
            }

        if success_false:
            _record_query_diagnostic(
                log, result="query_unavailable", failure_category=structured_category or "unknown",
                http_status=http_status, task_state=safe_state, task_id_echo=echo_state,
                video_field_present=video_field_present,
                sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError("Omni返回失败响应，但任务终态未确认；任务号已保留") from None

        if is_completed and not video_field_present:
            _record_query_diagnostic(
                log, result="completed_without_video", failure_category=structured_category or "unknown",
                http_status=http_status, task_state=safe_state, task_id_echo=echo_state,
                video_field_present=False, sku=record["sku"], version=record["version"],
            )
            raise OmniBridgeError("Omni任务已完成但没有提供成片地址；任务号已保留") from None

        if is_completed:
            _record_query_diagnostic(
                log, result="completed", failure_category=None,
                http_status=http_status, task_state=safe_state, task_id_echo=echo_state,
                video_field_present=True, sku=record["sku"], version=record["version"],
            )
            local_path = self._download_validate_and_publish(video_url, record)
            record["state"] = "pending_human_review"
            record["local_path"] = str(local_path.relative_to(self.root))
            self._replace_record(record_path, record)
            log.record_step(
                model="local", role="download", reason="技术校验后保存Omni候选视频",
                result="pending_human_review", evidence="10秒9比16技术校验通过，等待人工审核",
                sku=record["sku"], version=record["version"],
            )
            return {
                "state": "pending_human_review",
                "sku": record["sku"], "version": record["version"],
                "model": record["model"], "task_id": task_id,
                "local_path": str(local_path), "review_status": "pending_human_review",
            }
        _record_query_diagnostic(
            log, result="query_received", failure_category=None,
            http_status=http_status, task_state=safe_state, task_id_echo=echo_state,
            video_field_present=video_field_present,
            sku=record["sku"], version=record["version"],
        )
        return {
            "state": "task_known",
            "sku": record["sku"], "version": record["version"],
            "model": record["model"], "task_id": task_id,
            "remote_state": safe_state,
            "review_status": "not_ready",
        }

    def _download_validate_and_publish(self, url: object, record: dict[str, Any]) -> Path:
        if type(url) is not str:
            raise OmniBridgeError("完成任务未提供可用候选视频；任务号已保留")
        _public_https_url(url)
        review_dir = self.root / "02_待审核"
        _ensure_directory(review_dir, self.root)
        try:
            with tempfile.TemporaryDirectory(prefix="omni-candidate-") as temp_dir:
                staged = Path(temp_dir) / "candidate.mp4"
                downloaded = self.downloader(url, staged)
                report = self.video_validator(Path(downloaded))
                if hasattr(report, "to_dict"):
                    report = report.to_dict()
                if not isinstance(report, dict):
                    raise OmniBridgeError("候选视频本地校验未通过；任务号已保留")
                width, height, duration = report.get("width"), report.get("height"), report.get("duration")
                if (
                    report.get("ok") is not True
                    or type(width) is not int or type(height) is not int
                    or width * 16 != height * 9
                    or type(duration) not in (int, float) or isinstance(duration, bool)
                    or not math.isfinite(float(duration)) or round(float(duration), 3) != 10.0
                    or not Path(downloaded).is_file()
                ):
                    raise OmniBridgeError("候选视频未通过10秒9比16本地校验；任务号已保留")
                final = review_dir / f"Omni_{record['sku']}_{record['version']}_{uuid.uuid4().hex}.mp4"
                _reject_reparse_components(final)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".Omni_{record['sku']}_{record['version']}_",
                    suffix=".tmp",
                    dir=review_dir,
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as target, Path(downloaded).open("rb") as source:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                    if temporary.stat().st_size != Path(downloaded).stat().st_size:
                        raise OSError("candidate copy size mismatch")
                    os.link(temporary, final)
                finally:
                    temporary.unlink(missing_ok=True)
                return final
        except OmniBridgeError:
            raise
        except FileExistsError:
            raise OmniBridgeError("待审核候选文件名冲突；请按原任务号重新查询") from None
        except Exception:
            raise OmniBridgeError("候选视频未能安全下载或保存；任务号已保留") from None

    def resume(self, sku: str, version: str) -> dict[str, Any]:
        with self._processing_log(
            command="omni-resume", sku=sku, version=version,
            mode="read_only", paid=False,
        ) as log:
            record_path, record = self._read_record(sku, version)
            if record["state"] == "creation_unknown" or not record.get("task_id"):
                raise OmniBridgeError("本地记录没有任务号且状态未知；禁止查询外的重试")
            if record["state"] == "remote_failed":
                remote_state = record.get("remote_state")
                if remote_state not in _REMOTE_FAILED_STATES:
                    remote_state = "unknown"
                log.record_step(
                    model="local", role="query", reason="复用已确认的Omni远端失败状态",
                    result="remote_failed", evidence="本地已记录终态；没有发出网络请求或创建请求",
                    sku=sku, version=version,
                )
                return {
                    "state": "remote_failed", "sku": sku, "version": version,
                    "model": record["model"], "task_id": record["task_id"],
                    "remote_state": remote_state, "review_status": "not_ready",
                }
            if record["state"] == "pending_human_review":
                existing = self.root / record["local_path"]
                if existing.is_file() and not _is_reparse(existing):
                    log.record_step(
                        model="local", role="query", reason="续查时复用已保存的待审核候选",
                        result="pending_human_review", evidence="本地候选仍在审核区，未发出网络请求",
                        sku=sku, version=version,
                    )
                    return {
                        "state": "pending_human_review", "sku": sku, "version": version,
                        "model": record["model"], "task_id": record["task_id"],
                        "local_path": str(existing), "review_status": "pending_human_review",
                    }
            client = self._client()
            log.record_step(
                model="local", role="query", reason="按持久化任务号续查Omni状态",
                result="querying", evidence="未创建新任务",
                sku=sku, version=version,
            )
            return self._query_and_save(client, record_path, record, log)


def _ask_masked_key() -> str | None:
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        return simpledialog.askstring(
            "Aimaxhug凭据", "请输入 Aimaxhug API Key（输入内容将被遮蔽）",
            show="*", parent=root,
        )
    finally:
        root.destroy()


def setup_credential(
    credential_store: Any | None = None,
    *,
    ask_secret: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    store = credential_store or SeedanceCredentialStore()
    try:
        state = store.status().get("api-key")
    except Exception:
        raise OmniBridgeError("无法读取Aimaxhug凭据状态") from None
    if state == "PRESENT":
        return {"state": "credential_present", "updated": False}
    if state != "ABSENT":
        raise OmniBridgeError("Aimaxhug凭据状态无效")
    try:
        secret = (ask_secret or _ask_masked_key)()
    except Exception:
        raise OmniBridgeError("凭据输入窗口无法安全打开") from None
    if secret is None:
        return {"state": "cancelled", "updated": False}
    try:
        store.set_secret(secret)
        del secret
    except Exception:
        raise OmniBridgeError("Aimaxhug凭据保存失败；没有记录凭据内容") from None
    return {"state": "credential_present", "updated": True}


def _project_root() -> Path:
    value = os.environ.get("KLING_WORKFLOW_PROJECT_ROOT")
    return Path(value) if value else Path.cwd()


def _record_argument_parse_failure() -> None:
    try:
        root = OmniBridge(_project_root()).root
        entry = ProcessingLog.start(
            root, command="omni-parse-error", sku=None, version=None,
            network_mode="offline", paid_authorization_present=False,
        )
    except Exception:
        return
    try:
        entry.record_step(
            model="local", role="query", reason="Omni命令行参数解析失败",
            result="rejected", evidence="原始参数已隐藏；操作未执行",
        )
    except Exception:
        pass
    finally:
        try:
            entry.finish(status="failure", exit_code=2)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = _OmniArgumentParser(description="Omni项目直连入口；离线预检和任务续查独立于可灵/Seedance")
    sub = parser.add_subparsers(
        dest="command", required=True, parser_class=_OmniArgumentParser
    )
    preview = sub.add_parser("preview", help="离线校验manifest，不联网、不显示提示词或图片载荷")
    preview.add_argument("--manifest", required=True)
    readonly = sub.add_parser("read-only-check", help="只读检查模型端点；不提交生成")
    readonly.add_argument("--model", choices=_MODELS, default="omni-fast-no-water")
    submit = sub.add_parser("submit", help="明确付费提交；输出未知时禁止重试")
    submit.add_argument("--manifest", required=True)
    submit.add_argument("--confirm-paid-request", action="store_true")
    resume = sub.add_parser("resume", help="按本地task记录查询已有任务")
    resume.add_argument("--sku", required=True)
    resume.add_argument("--version", required=True)
    sub.add_parser("set-credential", help="仅缺少凭据时打开可取消的遮蔽输入窗口")
    try:
        args = parser.parse_args(argv)
    except _OmniArgumentError:
        _record_argument_parse_failure()
        print("Omni命令参数无效；原始参数已隐藏", file=sys.stderr)
        return 2
    try:
        bridge = OmniBridge(_project_root())
        if args.command == "set-credential":
            with bridge._processing_log(
                command="omni-set-credential", sku=None, version=None,
                mode="offline", paid=False,
            ) as log:
                result = setup_credential()
                state = result.get("state") if isinstance(result, dict) else None
                if type(state) is not str or state not in {"credential_present", "cancelled"}:
                    state = "unknown"
                log.record_step(
                    model="local", role="query", reason="仅登记Omni凭据状态结果",
                    result=state, evidence="不记录凭据内容",
                )
        else:
            if args.command == "preview":
                result = bridge.preview(args.manifest)
            elif args.command == "read-only-check":
                result = bridge.read_only_check(args.model)
            elif args.command == "submit":
                result = bridge.submit(args.manifest, paid_confirmation=args.confirm_paid_request)
            else:
                result = bridge.resume(args.sku, args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except OmniBridgeError as exc:
        print(f"Omni操作未完成：{exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Omni操作未完成：本地处理失败；敏感响应内容已隐藏", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
