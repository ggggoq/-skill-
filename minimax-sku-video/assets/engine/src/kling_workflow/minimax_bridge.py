"""Fail-closed Aimaxhug MiniMax H3 bridge with its own task namespace."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from PIL import Image

from .validation import MIN_OUTPUT_WIDTH


MODEL_KEYS = (
    "minimax-h3-768p",
    "minimax-h3-2k",
    "minimax-h3-pro-768p",
    "minimax-h3-pro-2k",
)
# 主管 2026-11-09 硬规则：本项目所有 MiniMax 生成固定使用 FIXED_MODEL，不允许选择其他模型。
# MODEL_KEYS 只是“能被解析的四档白名单”，不是换档授权；提交前必须过 tools/check_fixed_model.py。
FIXED_MODEL = "minimax-h3-pro-768p"
# Confirmed by this project's minimax-h3-2k provider error (2013) on 2026-11-05.
_CONFIRMED_IMAGE_DIMENSION_LIMITS = {"minimax-h3-2k": (256, 5760)}
IMAGE_ROLES = ("model_image", "reverse_flat", "action_storyboard", "reverse_worn")
CURRENT_FOUR_IMAGE_ROLES = ("model_image", "reverse_worn", "front_flat", "action_storyboard")
LEGACY_FIVE_IMAGE_ROLES = IMAGE_ROLES + ("front_flat",)
MAX_PROMPT_CHARS = 7000
_SKU = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_VERSION = re.compile(r"v\d{3,}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_BIND_FIELDS = (
    "schema_version", "provider", "sku", "version", "group_number", "model", "mode",
    "prompt_sha256", "request_body_sha256", "roles", "asset_fingerprints", "action_source",
    "reference_video", "duration", "ratio",
)
_APPROVAL_FIELDS = frozenset({
    "reviewer", "decision", "sku", "version", "group_number", "model",
    "prompt_sha256", "request_body_sha256", "roles", "asset_fingerprints", "action_source",
    "reference_video", "duration", "ratio",
})
# referenceVideos 仅 Pro 档支持（依据已安装 aimaxhug-generation 技能的 minimax_video.py 校验）。
_PRO_MODELS = frozenset({"minimax-h3-pro-768p", "minimax-h3-pro-2k"})
MAX_REFERENCE_VIDEO_BYTES = 50 * 1024 * 1024
_DONE_STATES = frozenset({"completed", "complete", "succeeded", "success", "done", "finished"})
_FAILED_STATES = frozenset({"failed", "failure", "error", "cancelled", "canceled", "rejected"})
_SAFE_POST_FAILURE_CATEGORIES = frozenset({
    "credential_unavailable", "invalid_request_body", "request_build_error",
    "connect_timeout", "connect_error", "read_timeout", "write_timeout",
    "protocol_error", "other_transport_error", "transport_unknown",
})
_SAFE_POST_FAILURE_STAGES = frozenset({
    "credential_lookup", "serialize", "request_build", "send", "client_call",
})


class MiniMaxBridgeError(RuntimeError):
    """Safe local failures; never include paths, prompts, media, URLs, or keys."""


@dataclass(frozen=True)
class MiniMaxAsset:
    role: str
    path: Path = field(repr=False)
    mime_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    data_uri: str = field(repr=False)


@dataclass(frozen=True)
class PreparedMiniMaxRequest:
    sku: str
    version: str
    group_number: int
    model: str
    duration: int
    ratio: str
    prompt: str = field(repr=False)
    prompt_sha256: str
    roles: tuple[str, ...]
    assets: tuple[MiniMaxAsset, ...] = field(repr=False)
    action_source: dict[str, object] = field(repr=False)
    body: Mapping[str, object] = field(repr=False)
    body_sha256: str
    request_json_bytes: int
    manifest_path: Path = field(repr=False)
    prompt_path: Path = field(repr=False)
    reference_video: dict[str, object] | None = field(default=None, repr=False)


def _render_mode(prepared: PreparedMiniMaxRequest) -> str:
    """记录本次请求用的参考模式；参考视频为 None 时保持历史值不变。"""
    return "reference_images_video" if prepared.reference_video is not None else "reference_images"


class MiniMaxTaskStore:
    """Atomic, provider-isolated previews, task records, receipts, and version locks."""

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve(strict=True)
        self.base = self.root / ".minimax_bridge"

    @staticmethod
    def _identity(value: str, *, version: bool = False) -> str:
        if not isinstance(value, str) or not (_VERSION if version else _SKU).fullmatch(value):
            raise MiniMaxBridgeError("款号或版本号格式无效")
        return value

    def _path(self, category: str, sku: str, version: str, suffix: str = ".json") -> Path:
        sku = self._identity(sku)
        version = self._identity(version, version=True)
        if category not in {"previews", "tasks", "receipts", "locks", "candidates", "approvals"}:
            raise MiniMaxBridgeError("MiniMax记录类别无效")
        return self.base / category / sku / f"{version}{suffix}"

    def _ensure_dir(self, target: Path) -> None:
        try:
            relative = target.absolute().relative_to(self.root)
        except ValueError:
            raise MiniMaxBridgeError("MiniMax记录路径超出项目目录") from None
        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                info = current.lstat()
            except OSError:
                raise MiniMaxBridgeError("MiniMax记录目录不可用") from None
            if (not stat.S_ISDIR(info.st_mode) or current.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise MiniMaxBridgeError("MiniMax记录目录不安全")

    def _read_json(self, path: Path) -> dict[str, object] | None:
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 1024 * 1024):
                raise ValueError
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except FileNotFoundError:
            return None
        except Exception:
            raise MiniMaxBridgeError("MiniMax记录文件无效") from None

    def _write_json(self, path: Path, value: Mapping[str, object], *, replace: bool) -> None:
        self._ensure_dir(path.parent)
        if path.exists() or path.is_symlink():
            if not replace:
                raise MiniMaxBridgeError("MiniMax版本记录已存在")
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError
            except Exception:
                raise MiniMaxBridgeError("MiniMax记录目标不安全") from None
        raw = _json_bytes(dict(value))
        temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            raise MiniMaxBridgeError("MiniMax版本记录已存在") from None
        except Exception:
            raise MiniMaxBridgeError("MiniMax记录保存失败") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_bytes_once(self, path: Path, raw: bytes) -> None:
        self._ensure_dir(path.parent)
        if path.exists() or path.is_symlink():
            try:
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode) or path.read_bytes() != raw:
                    raise MiniMaxBridgeError("该版本已有不同的提示词审核文件")
                return
            except MiniMaxBridgeError:
                raise
            except Exception:
                raise MiniMaxBridgeError("提示词审核文件不可读") from None
        temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
        except FileExistsError:
            if path.is_file() and path.read_bytes() == raw:
                return
            raise MiniMaxBridgeError("该版本已有不同的提示词审核文件") from None
        except Exception:
            raise MiniMaxBridgeError("提示词审核文件保存失败") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @contextmanager
    def lock(self, sku: str, version: str):
        path = self._path("locks", sku, version, ".lock")
        self._ensure_dir(path.parent)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            raise MiniMaxBridgeError("该MiniMax版本正在处理；禁止并行创建") from None
        except OSError:
            raise MiniMaxBridgeError("无法取得MiniMax版本锁") from None
        os.close(fd)
        try:
            yield
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def save_review(self, prepared: PreparedMiniMaxRequest) -> dict[str, object]:
        record_path = self._path("previews", prepared.sku, prepared.version)
        prompt_path = record_path.with_name(f"{prepared.version}.prompt.txt")
        fingerprints = [
            {"role": asset.role, "sha256": asset.sha256, "size_bytes": asset.size_bytes,
             "width": asset.width, "height": asset.height, "mime_type": asset.mime_type}
            for asset in prepared.assets
        ]
        record = {
            "schema_version": 1, "provider": "aimaxhug", "sku": prepared.sku,
            "version": prepared.version, "group_number": prepared.group_number,
            "model": prepared.model, "mode": _render_mode(prepared), "duration": prepared.duration,
            "ratio": prepared.ratio, "prompt_sha256": prepared.prompt_sha256,
            "request_body_sha256": prepared.body_sha256,
            "roles": list(prepared.roles), "asset_fingerprints": fingerprints,
            "action_source": dict(prepared.action_source), "status": "awaiting_supervisor",
            "reference_video": prepared.reference_video,
        }
        existing = self._read_json(record_path)
        prompt_bytes = prepared.prompt.encode("utf-8")
        if existing is not None:
            if existing != record or self._read_prompt(prompt_path) != prompt_bytes:
                raise MiniMaxBridgeError("该MiniMax版本已有不同的审核或素材绑定")
        else:
            self._write_bytes_once(prompt_path, prompt_bytes)
            self._write_json(record_path, record, replace=False)
        return {
            "state": "awaiting_supervisor", "prompt_sha256": prepared.prompt_sha256,
            "review_record_path": record_path.relative_to(self.root).as_posix(),
            "prompt_review_path": prompt_path.relative_to(self.root).as_posix(),
        }

    def save_approval(self, prepared: PreparedMiniMaxRequest,
                      approval: Mapping[str, object]) -> Path:
        approval_path = self._path("approvals", prepared.sku, prepared.version)
        expected = {
            "reviewer": approval.get("reviewer"), "decision": "approved",
            "sku": prepared.sku, "version": prepared.version,
            "group_number": prepared.group_number, "model": prepared.model,
            "prompt_sha256": prepared.prompt_sha256, "request_body_sha256": prepared.body_sha256,
            "roles": list(prepared.roles),
            "asset_fingerprints": [
                {"role": asset.role, "sha256": asset.sha256, "size_bytes": asset.size_bytes,
                 "width": asset.width, "height": asset.height, "mime_type": asset.mime_type}
                for asset in prepared.assets
            ],
            "action_source": dict(prepared.action_source),
            "reference_video": prepared.reference_video,
            "duration": prepared.duration, "ratio": prepared.ratio,
        }
        reviewer = expected["reviewer"]
        if (set(approval) != _APPROVAL_FIELDS or not isinstance(reviewer, str)
                or not reviewer.strip() or len(reviewer) > 128
                or any(ord(char) < 32 for char in reviewer)
                or approval.get("decision") != "approved"):
            raise MiniMaxBridgeError("主管审核记录格式无效或未明确批准")
        if dict(approval) != expected:
            raise MiniMaxBridgeError("主管审核与当前款号、版本、组别、模型、提示词或素材不匹配")
        existing = self._read_json(approval_path)
        if existing is not None and existing != expected:
            raise MiniMaxBridgeError("该MiniMax版本已存在不同审核记录")
        if existing is None:
            self._write_json(approval_path, expected, replace=False)
        return approval_path

    def _read_prompt(self, path: Path) -> bytes | None:
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 1024 * 1024):
                raise ValueError
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except Exception:
            raise MiniMaxBridgeError("MiniMax审核提示词文件不安全") from None

    def review_record(self, sku: str, version: str) -> dict[str, object] | None:
        return self._read_json(self._path("previews", sku, version))

    def task_record(self, sku: str, version: str) -> dict[str, object] | None:
        path = self._path("tasks", sku, version)
        receipt_path = self._path("receipts", sku, version)
        record = self._read_json(path)
        receipt = self._read_json(receipt_path)
        if record is None and receipt is None:
            return None
        review = self.review_record(sku, version)
        if not isinstance(review, dict):
            raise MiniMaxBridgeError("任务缺少审核绑定；禁止新建或查询")
        if record is not None and any(record.get(key) != review.get(key) for key in _BIND_FIELDS):
            raise MiniMaxBridgeError("MiniMax任务记录与审核绑定冲突")
        if receipt is not None:
            if (not isinstance(receipt.get("task_id"), str) or not _TASK_ID.fullmatch(receipt["task_id"])
                    or any(receipt.get(key) != review.get(key) for key in _BIND_FIELDS)):
                raise MiniMaxBridgeError("MiniMax任务回执与审核绑定冲突")
            if record is None:
                record = dict(receipt)
                record.update(state="task_known", task_id=receipt["task_id"], generation_attempts=1)
                self._write_json(path, record, replace=False)
            elif record.get("task_id") not in (None, receipt["task_id"]):
                raise MiniMaxBridgeError("MiniMax主记录与正式回执任务号冲突")
            elif not record.get("task_id"):
                recovered = dict(record)
                recovered.update(task_id=receipt["task_id"], state="task_known", generation_attempts=1)
                self._write_json(path, recovered, replace=True)
                record = recovered
        if record and record.get("task_id") and not _TASK_ID.fullmatch(str(record["task_id"])):
            raise MiniMaxBridgeError("MiniMax任务号格式无效")
        return record

    def create_task(self, record: Mapping[str, object]) -> None:
        sku, version = str(record.get("sku", "")), str(record.get("version", ""))
        if self.task_record(sku, version) is not None:
            raise MiniMaxBridgeError("该MiniMax版本已有任务记录；禁止重复提交")
        self._write_json(self._path("tasks", sku, version), record, replace=False)

    def update_task(self, sku: str, version: str, record: Mapping[str, object]) -> None:
        path = self._path("tasks", sku, version)
        if self._read_json(path) is None:
            raise MiniMaxBridgeError("MiniMax任务记录缺失")
        self._write_json(path, record, replace=True)

    def persist_task_id(self, record: Mapping[str, object], task_id: str) -> str:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise MiniMaxBridgeError("MiniMax任务号格式无效")
        sku, version = str(record["sku"]), str(record["version"])
        current = self._read_json(self._path("tasks", sku, version))
        if current is None or any(current.get(key) != record.get(key) for key in _BIND_FIELDS):
            raise MiniMaxBridgeError("创建意图与MiniMax任务绑定不一致")
        receipt = {key: record[key] for key in _BIND_FIELDS}
        receipt.update(task_id=task_id, state="task_known", generation_attempts=1)
        receipt_path = self._path("receipts", sku, version)
        main_path = self._path("tasks", sku, version)
        old_receipt = self._read_json(receipt_path)
        if old_receipt is not None and (
            old_receipt.get("task_id") != task_id
            or any(old_receipt.get(key) != record.get(key) for key in _BIND_FIELDS)
        ):
            raise MiniMaxBridgeError("MiniMax任务回执已有冲突任务号；保留原记录")
        if current.get("task_id") not in (None, task_id):
            raise MiniMaxBridgeError("MiniMax主记录已有不同任务号；保留原记录")
        receipt_ok = main_ok = False
        if old_receipt is not None:
            receipt_ok = True
        else:
            try:
                self._write_json(receipt_path, receipt, replace=False)
                receipt_ok = True
            except Exception:
                if self._read_json(receipt_path) == receipt:
                    receipt_ok = True
        try:
            if current.get("task_id") == task_id and current.get("state") == "task_known":
                main_ok = True
            else:
                main = dict(record)
                main.update(task_id=task_id, state="task_known")
                self._write_json(main_path, main, replace=True)
                main_ok = True
        except Exception:
            latest = self._read_json(main_path)
            if latest is not None and latest.get("task_id") == task_id:
                main_ok = True
        if not receipt_ok and not main_ok:
            raise MiniMaxBridgeError("任务号未能写入MiniMax主记录或正式回执")
        return "both" if receipt_ok and main_ok else ("receipt" if receipt_ok else "main")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _regular_file(path: Path) -> bytes:
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
                or getattr(before, "st_file_attributes", 0) & 0x400 or before.st_size <= 0):
            raise ValueError
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            content = handle.read()
            after = os.fstat(handle.fileno())
        latest = path.lstat()
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if (identity(before) != identity(opened) or identity(opened) != identity(after)
                or identity(before) != identity(latest) or len(content) != before.st_size):
            raise ValueError
        return content
    except Exception:
        raise MiniMaxBridgeError("素材必须是读取期间未变化的普通文件") from None


def _sha256_stable_file(path: Path) -> tuple[str, int]:
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
                or getattr(before, "st_file_attributes", 0) & 0x400 or before.st_size <= 0):
            raise ValueError
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            if identity(opened) != identity(before):
                raise ValueError
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        latest = path.lstat()
        if identity(before) != identity(after) or identity(after) != identity(latest):
            raise ValueError
        return digest.hexdigest(), int(before.st_size)
    except Exception:
        raise MiniMaxBridgeError("候选文件读取期间发生变化或不是普通文件") from None


def _walk_objects(value: object, *, limit: int = 8):
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop(0)
        if depth > limit:
            continue
        if isinstance(current, Mapping):
            yield current
            for child in current.values():
                if isinstance(child, (Mapping, list)):
                    pending.append((child, depth + 1))
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current if isinstance(child, (Mapping, list)))


def _task_echoes(payload: object) -> set[str]:
    values: set[str] = set()
    for node in _walk_objects(payload):
        for key in ("task_id", "taskId"):
            value = node.get(key)
            if isinstance(value, str) and _TASK_ID.fullmatch(value):
                values.add(value)
            elif value is not None:
                values.add("<invalid>")
    return values


def _http_error_detail(payload: object) -> str | None:
    """提取上游错误信封里的 message（仅用于本地诊断，截断 400 字符）。"""
    if not isinstance(payload, Mapping):
        return None
    err = payload.get("error")
    message: object = None
    if isinstance(err, Mapping):
        message = err.get("message") or err.get("msg")
    elif isinstance(err, str):
        message = err
    if not isinstance(message, str):
        return None
    text = " ".join(message.split())[:400]
    return text or None


def _query_task_echoes(payload: object) -> set[str]:
    # Query responses may contain a public root `id` and a distinct internal
    # `task_id`. Prefer the public id; only use the legacy task_id scan when the
    # root id field is absent.
    if isinstance(payload, Mapping) and "id" in payload:
        value = payload["id"]
        return {value} if isinstance(value, str) and _TASK_ID.fullmatch(value) else {"<invalid>"}
    return _task_echoes(payload)


def _remote_status(payload: object) -> str:
    nodes = list(_walk_objects(payload))
    for keys in (("task_state", "task_status"), ("state", "status")):
        candidates: set[str] = set()
        for node in nodes:
            for key in keys:
                value = node.get(key)
                if isinstance(value, str) and value.strip() and not value.strip().isdigit():
                    candidates.add(value.strip().casefold().replace(" ", "_"))
        if candidates:
            return next(iter(candidates)) if len(candidates) == 1 else "unknown"
    return "unknown"


def _media_url(payload: object) -> str | None:
    fields = {"video_url", "output_video_url", "download_url", "output_url"}
    candidates: set[str] = set()
    if isinstance(payload, Mapping):
        for node in _walk_objects(payload):
            for key in fields:
                value = node.get(key)
                if not isinstance(value, str) or any(ord(char) < 32 or char.isspace() for char in value):
                    continue
                try:
                    parsed = urlsplit(value)
                    if (parsed.scheme == "https" and parsed.hostname and parsed.username is None
                            and parsed.password is None and not parsed.fragment and "\\" not in value):
                        candidates.add(value)
                except ValueError:
                    continue
    return next(iter(candidates)) if len(candidates) == 1 else None


def _video_extension(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".mp4", ".webm", ".mov", ".m4v"} else ".mp4"


class MiniMaxBridge:
    """Prepare the current Omni-style 10s/9:16 H3 request using exact source bytes."""

    def __init__(self, project_root: str | Path, *, client: Any = None,
                 credential_store: Any = None, video_validator=None, downloader=None,
                 source_video_probe=None, audit_log=None) -> None:
        try:
            self.root = Path(project_root).resolve(strict=True)
            root_stat = self.root.lstat()
            if not self.root.is_dir() or self.root.is_symlink() or getattr(root_stat, "st_file_attributes", 0) & 0x400:
                raise OSError
        except OSError:
            raise MiniMaxBridgeError("项目目录无效") from None
        self.client = client
        self.credentials = credential_store
        self.video_validator = video_validator
        self.downloader = downloader
        self.source_video_probe = source_video_probe
        self.audit_log = audit_log
        self.store = MiniMaxTaskStore(self.root)

    def _project_file(self, value: object, label: str) -> Path:
        if type(value) is not str or not value or len(value) > 1024:
            raise MiniMaxBridgeError(f"{label}路径无效")
        candidate = Path(value)
        if candidate.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in candidate.parts:
            raise MiniMaxBridgeError(f"{label}必须位于项目目录内")
        path = self.root / candidate
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
            current = self.root
            for part in candidate.parts:
                current = current / part
                item = current.lstat()
                if current.is_symlink() or getattr(item, "st_file_attributes", 0) & 0x400:
                    raise ValueError
            if not resolved.is_file():
                raise ValueError
            return resolved
        except Exception:
            raise MiniMaxBridgeError(f"{label}必须是项目目录内的普通文件") from None

    _MANIFEST_REQUIRED = frozenset({
        "schema_version", "sku", "version", "group_number", "model", "duration",
        "ratio", "prompt_file", "images", "action_source",
    })
    _MANIFEST_OPTIONAL = frozenset({"reference_video"})

    def _manifest(self, manifest: str | Path) -> dict[str, object]:
        try:
            resolved = self._resolve_manifest(manifest)
            data = json.loads(_regular_file(resolved).decode("utf-8"))
            if (not isinstance(data, dict) or not self._MANIFEST_REQUIRED <= set(data)
                    or set(data) - self._MANIFEST_REQUIRED - self._MANIFEST_OPTIONAL):
                raise ValueError
            return data
        except MiniMaxBridgeError:
            raise
        except Exception:
            raise MiniMaxBridgeError("MiniMax manifest无效") from None

    def _resolve_manifest(self, manifest: str | Path) -> Path:
        try:
            raw_path = Path(manifest)
            if raw_path.is_absolute():
                relative = raw_path.absolute().relative_to(self.root)
                value = relative.as_posix()
            else:
                value = str(manifest)
            return self._project_file(value, "manifest")
        except MiniMaxBridgeError:
            raise
        except Exception:
            raise MiniMaxBridgeError("manifest必须位于项目目录内") from None

    def prepare(self, manifest: str | Path) -> PreparedMiniMaxRequest:
        manifest_path = self._resolve_manifest(manifest)
        data = self._manifest(manifest)
        sku, version, group, model = data["sku"], data["version"], data["group_number"], data["model"]
        if (data["schema_version"] != 1 or type(sku) is not str or not _SKU.fullmatch(sku)
                or type(version) is not str or not _VERSION.fullmatch(version)
                or type(group) is not int or group <= 0 or model not in MODEL_KEYS):
            raise MiniMaxBridgeError("MiniMax款号、版本、参考组或模型无效")
        if type(data["duration"]) is not int or data["duration"] != 10 or data["ratio"] != "9:16":
            raise MiniMaxBridgeError("本次Omni规则只允许10秒、9:16")

        prompt_path = self._project_file(data["prompt_file"], "提示词")
        prompt_bytes = _regular_file(prompt_path)
        try:
            prompt = prompt_bytes.decode("utf-8")
        except UnicodeError:
            raise MiniMaxBridgeError("提示词必须是UTF-8文本") from None
        if not prompt or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise MiniMaxBridgeError("完整提示词为空或超过7000字符；不会截断后提交")

        raw_images = data["images"]
        if not isinstance(raw_images, dict):
            raise MiniMaxBridgeError("MiniMax参考图角色集合无效")
        image_keys = set(raw_images)
        if image_keys == set(CURRENT_FOUR_IMAGE_ROLES):
            roles = CURRENT_FOUR_IMAGE_ROLES
        elif image_keys == set(IMAGE_ROLES):
            roles = IMAGE_ROLES
        elif image_keys == set(LEGACY_FIVE_IMAGE_ROLES):
            roles = LEGACY_FIVE_IMAGE_ROLES
        else:
            raise MiniMaxBridgeError("MiniMax参考图角色集合无效")
        assets: list[MiniMaxAsset] = []
        references: list[str] = []
        fingerprints: list[dict[str, object]] = []
        for role in roles:
            path = self._project_file(raw_images[role], "参考图片")
            blob = _regular_file(path)
            try:
                with Image.open(io.BytesIO(blob)) as image:
                    image_format = image.format
                    width, height = image.size
                    frames = getattr(image, "n_frames", 1)
                    image.verify()
                if image_format not in _IMAGE_MIME or width <= 0 or height <= 0 or frames != 1:
                    raise ValueError
            except Exception:
                raise MiniMaxBridgeError("参考图必须是有效的JPEG、PNG或WEBP静态图片") from None
            limits = _CONFIRMED_IMAGE_DIMENSION_LIMITS.get(model)
            if limits is not None and (width < limits[0] or height < limits[0]
                                       or width > limits[1] or height > limits[1]):
                raise MiniMaxBridgeError(
                    f"{model}参考图宽高均须在{limits[0]}至{limits[1]}像素内（依据本项目已确认的上游错误2013）"
                )
            mime = _IMAGE_MIME[image_format]
            # Base64 is transport encoding only: it preserves the original file bytes.
            data_uri = f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
            asset = MiniMaxAsset(role, path, mime, width, height, len(blob), hashlib.sha256(blob).hexdigest(), data_uri)
            assets.append(asset)
            references.append(data_uri)
            fingerprints.append({"role": role, "sha256": asset.sha256, "size_bytes": asset.size_bytes,
                                 "width": width, "height": height, "mime_type": mime})

        raw_action = data["action_source"]
        if not isinstance(raw_action, dict) or set(raw_action) != {
            "path", "sha256", "source_start_seconds", "source_end_seconds",
        }:
            raise MiniMaxBridgeError("动作源窗口绑定无效")
        action_path = self._project_file(raw_action["path"], "动作源视频")
        action_bytes = _regular_file(action_path)
        start, end = raw_action["source_start_seconds"], raw_action["source_end_seconds"]
        if self.source_video_probe is None:
            from .muse_bridge import probe_video_source
            source_probe = probe_video_source
        else:
            source_probe = self.source_video_probe
        try:
            source_duration = float(source_probe(action_path))
        except Exception:
            raise MiniMaxBridgeError("动作源视频无法完整解码或读取时长") from None
        if (not isinstance(raw_action["sha256"], str) or not _SHA256.fullmatch(raw_action["sha256"])
                or hashlib.sha256(action_bytes).hexdigest() != raw_action["sha256"]
                or type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(float(start)) or not math.isfinite(float(end))
                or start < 0 or end > source_duration + 0.001
                or round(float(end) - float(start), 3) != 10.0):
            raise MiniMaxBridgeError("动作源文件或连续10秒窗口与审核清单不匹配")
        action_source = {"path": action_path.relative_to(self.root).as_posix(), "sha256": raw_action["sha256"],
                         "source_start_seconds": float(start), "source_end_seconds": float(end),
                         "source_duration_seconds": source_duration}

        reference_video = None
        reference_video_uri = None
        raw_reference_video = data.get("reference_video")
        if raw_reference_video is not None:
            if model not in _PRO_MODELS:
                raise MiniMaxBridgeError("参考视频仅Pro档模型支持（minimax-h3-pro-768p / minimax-h3-pro-2k）")
            if (not isinstance(raw_reference_video, dict)
                    or set(raw_reference_video) != {"path", "sha256"}):
                raise MiniMaxBridgeError("参考视频绑定无效")
            video_path = self._project_file(raw_reference_video["path"], "参考视频")
            video_bytes = _regular_file(video_path)
            if len(video_bytes) > MAX_REFERENCE_VIDEO_BYTES:
                raise MiniMaxBridgeError("参考视频超过50MB上限")
            declared_sha = raw_reference_video["sha256"]
            if (not isinstance(declared_sha, str) or not _SHA256.fullmatch(declared_sha)
                    or hashlib.sha256(video_bytes).hexdigest() != declared_sha):
                raise MiniMaxBridgeError("参考视频文件与审核清单不匹配")
            video_suffix = video_path.suffix.lower()
            if video_suffix not in {".mp4", ".mov", ".m4v"}:
                raise MiniMaxBridgeError("参考视频必须是MP4、MOV或M4V")
            try:
                video_duration = float(source_probe(video_path))
            except Exception:
                raise MiniMaxBridgeError("参考视频无法完整解码或读取时长") from None
            if not 2.0 <= video_duration <= 5.0:
                raise MiniMaxBridgeError(
                    f"参考视频时长必须为2至5秒（容器实测），当前为{video_duration:.3f}秒"
                )
            video_mime = "video/quicktime" if video_suffix == ".mov" else "video/mp4"
            reference_video = {
                "path": video_path.relative_to(self.root).as_posix(),
                "sha256": declared_sha,
                "size_bytes": len(video_bytes),
                "duration_seconds": round(video_duration, 3),
                "mime_type": video_mime,
            }
            reference_video_uri = (f"data:{video_mime};base64,"
                                   f"{base64.b64encode(video_bytes).decode('ascii')}")

        body_plain = {"model": model, "prompt": prompt, "duration": 10, "ratio": "9:16",
                      "referenceImages": tuple(references)}
        if reference_video_uri is not None:
            body_plain["referenceVideos"] = (reference_video_uri,)
        body_bytes = _json_bytes(body_plain)
        body = MappingProxyType(body_plain)
        return PreparedMiniMaxRequest(
            sku=sku, version=version, group_number=group, model=model, duration=10, ratio="9:16",
            prompt=prompt, prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(), roles=roles,
            assets=tuple(assets), action_source=MappingProxyType(action_source), body=body,
            body_sha256=hashlib.sha256(body_bytes).hexdigest(), request_json_bytes=len(body_bytes),
            manifest_path=manifest_path, prompt_path=prompt_path, reference_video=reference_video,
        )

    def _assert_current(self, prepared: PreparedMiniMaxRequest) -> None:
        current = self.prepare(prepared.manifest_path)
        actual_request = _json_bytes(dict(prepared.body))
        if (hashlib.sha256(actual_request).hexdigest() != prepared.body_sha256
                or len(actual_request) != prepared.request_json_bytes
                or current.body_sha256 != prepared.body_sha256
                or current.prompt_sha256 != prepared.prompt_sha256
                or current.sku != prepared.sku or current.version != prepared.version
                or current.group_number != prepared.group_number or current.model != prepared.model
                or current.duration != prepared.duration or current.ratio != prepared.ratio
                or dict(current.action_source) != dict(prepared.action_source)
                or current.reference_video != prepared.reference_video
                or current.roles != prepared.roles
                or [(asset.role, asset.sha256, asset.size_bytes, asset.width, asset.height, asset.mime_type)
                    for asset in current.assets]
                   != [(asset.role, asset.sha256, asset.size_bytes, asset.width, asset.height, asset.mime_type)
                       for asset in prepared.assets]):
            raise MiniMaxBridgeError("MiniMax素材或完整提示词与预检时不同")

    def review(self, prepared: PreparedMiniMaxRequest) -> dict[str, object]:
        self._assert_current(prepared)
        return self.store.save_review(prepared)

    def _credential_ready(self) -> None:
        if self.credentials is None:
            raise MiniMaxBridgeError("Aimaxhug凭据读取器未配置")
        try:
            status = self.credentials.status()
        except Exception:
            raise MiniMaxBridgeError("无法读取Aimaxhug凭据状态") from None
        if not isinstance(status, Mapping) or status.get("api-key") != "PRESENT":
            raise MiniMaxBridgeError("Aimaxhug凭据未配置")
        if self.client is None:
            raise MiniMaxBridgeError("MiniMax客户端未配置")
        prepare_credentials = getattr(self.client, "prepare_credentials", None)
        if callable(prepare_credentials):
            try:
                prepare_credentials()
            except Exception:
                raise MiniMaxBridgeError("无法安全读取Aimaxhug凭据") from None

    def submit(self, prepared: PreparedMiniMaxRequest, *,
               supervisor_approval: Mapping[str, object] | None,
               paid_confirmation: bool) -> dict[str, object]:
        if paid_confirmation is not True:
            raise MiniMaxBridgeError("MiniMax付费提交必须显式确认")
        self._assert_current(prepared)
        review = self.store.review_record(prepared.sku, prepared.version)
        if (not isinstance(review, Mapping) or review.get("status") != "awaiting_supervisor"
                or any(review.get(key) != self._review_value(prepared, key) for key in _BIND_FIELDS)):
            raise MiniMaxBridgeError("没有与当前完整输入绑定的主管审核")
        if not isinstance(supervisor_approval, Mapping):
            raise MiniMaxBridgeError("提交前必须取得主管审核批准")
        self.store.save_approval(prepared, supervisor_approval)

        with self.store.lock(prepared.sku, prepared.version):
            if self.store.task_record(prepared.sku, prepared.version) is not None:
                raise MiniMaxBridgeError("该MiniMax版本已有任务记录；禁止重复提交")
            self._assert_current(prepared)
            self._credential_ready()
            record = {key: review[key] for key in _BIND_FIELDS}
            record.update(state="creation_unknown", generation_attempts=1,
                          failure_category=None, watermark_status="unproven")
            self.store.create_task(record)
            if self.audit_log is not None:
                try:
                    self.audit_log.set_paid_creation_status("creation_unknown")
                except Exception:
                    record["failure_category"] = "audit_state_unavailable"
                    self.store.update_task(prepared.sku, prepared.version, record)
                    return {"state": "creation_unknown", "failure_category": "audit_state_unavailable"}
            try:
                status, payload = self.client.post_video(prepared.body)
            except Exception as exc:
                category = getattr(exc, "kind", None)
                if type(category) is not str or category not in _SAFE_POST_FAILURE_CATEGORIES:
                    category = "transport_unknown"
                stage = getattr(exc, "stage", None)
                if type(stage) is not str or stage not in _SAFE_POST_FAILURE_STAGES:
                    stage = "client_call"
                record["failure_category"] = category
                record["failure_stage"] = stage
                self.store.update_task(prepared.sku, prepared.version, record)
                failure_log_status = None
                if self.audit_log is not None:
                    recorder = getattr(self.audit_log, "record_paid_creation_failure", None)
                    if callable(recorder):
                        try:
                            recorder(category=category, stage=stage)
                            failure_log_status = "recorded"
                        except Exception:
                            failure_log_status = "unavailable"
                            record["failure_log_status"] = failure_log_status
                            self.store.update_task(prepared.sku, prepared.version, record)
                result = {"state": "creation_unknown", "failure_category": category,
                          "failure_stage": stage}
                if failure_log_status is not None:
                    result["failure_log_status"] = failure_log_status
                return result

            ids = _task_echoes(payload)
            if len(ids) != 1 or "<invalid>" in ids:
                record["failure_category"] = "http_error" if type(status) is int and not 200 <= status < 300 else "task_id_missing_or_conflicting"
                record["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
                detail = _http_error_detail(payload)
                if detail is not None:
                    record["http_error_message"] = detail
                self.store.update_task(prepared.sku, prepared.version, record)
                result = {"state": "creation_unknown", "failure_category": record["failure_category"],
                          "http_status": record["http_status"]}
                if detail is not None:
                    result["http_error_message"] = detail
                return result

            task_id = next(iter(ids))
            known = dict(record)
            valid_http_status = type(status) is int and 100 <= status <= 599
            successful_http = valid_http_status and 200 <= status < 300
            known.update(state="task_known", task_id=task_id,
                         failure_category=None if successful_http else "task_id_with_http_error",
                         http_status=status if valid_http_status else None)
            try:
                persistence = self.store.persist_task_id(known, task_id)
            except Exception:
                return {"state": "task_id_persistence_failed", "task_id": task_id,
                        "preserved_task_id": None,
                        "failure_category": "manual_task_id_recovery_required"}
            if self.audit_log is not None:
                try:
                    self.audit_log.set_paid_creation_status("task_known")
                except Exception:
                    return {"state": "task_known_audit_state_pending", "task_id": task_id}
            state = "task_known" if persistence == "both" and successful_http else (
                "task_known_http_error" if not successful_http else f"task_known_{persistence}_saved")
            return {"state": state, "task_id": task_id,
                    "http_status": status if valid_http_status else None}

    @staticmethod
    def _review_value(prepared: PreparedMiniMaxRequest, key: str) -> object:
        if key == "roles":
            return list(prepared.roles)
        if key == "asset_fingerprints":
            return [
                {"role": asset.role, "sha256": asset.sha256, "size_bytes": asset.size_bytes,
                 "width": asset.width, "height": asset.height, "mime_type": asset.mime_type}
                for asset in prepared.assets
            ]
        if key == "action_source":
            return dict(prepared.action_source)
        if key == "reference_video":
            return prepared.reference_video
        if key == "mode":
            return _render_mode(prepared)
        if key == "provider":
            return "aimaxhug"
        if key == "schema_version":
            return 1
        if key == "prompt_sha256":
            return prepared.prompt_sha256
        if key == "request_body_sha256":
            return prepared.body_sha256
        values = {"sku": prepared.sku, "version": prepared.version, "group_number": prepared.group_number,
                  "model": prepared.model, "duration": prepared.duration, "ratio": prepared.ratio}
        return values.get(key)

    def resume(self, sku: str, version: str, *, group_number: int | None = None) -> dict[str, object]:
        if not isinstance(sku, str) or not _SKU.fullmatch(sku) or not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise MiniMaxBridgeError("MiniMax款号或版本号无效")
        if group_number is not None and (type(group_number) is not int or group_number <= 0):
            raise MiniMaxBridgeError("MiniMax参考组编号无效")
        with self.store.lock(sku, version):
            record = self.store.task_record(sku, version)
            if not isinstance(record, dict):
                raise MiniMaxBridgeError("MiniMax任务记录不存在")
            if group_number is not None and record.get("group_number") != group_number:
                raise MiniMaxBridgeError("MiniMax任务记录属于其他参考组；停止查询")
            task_id, model = record.get("task_id"), record.get("model")
            if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
                raise MiniMaxBridgeError("创建结果未知且没有可核验任务号；禁止重新提交")
            if not isinstance(model, str) or model not in MODEL_KEYS:
                raise MiniMaxBridgeError("MiniMax任务模型绑定无效；停止查询")
            if record.get("state") == "candidate_isolated_for_review":
                return {"state": "candidate_isolated_for_review", "candidate_path": record.get("candidate_path"),
                        "candidate_sha256": record.get("candidate_sha256"), "watermark_status": "unproven"}
            if record.get("state") == "technical_validation_failed":
                return {"state": "technical_validation_failed", "candidate_path": record.get("candidate_path"),
                        "candidate_sha256": record.get("candidate_sha256")}
            self._credential_ready()
            try:
                status, payload = self.client.get_task(model, task_id)
            except Exception:
                record.update(last_query_state="transport_error")
                self.store.update_task(sku, version, record)
                return {"state": "transport_error"}
            if type(status) is not int or not 200 <= status < 300:
                record.update(last_query_state="http_error", last_query_http_status=status if type(status) is int else None)
                self.store.update_task(sku, version, record)
                return {"state": "http_error", "http_status": status if type(status) is int else None}
            ids = _query_task_echoes(payload)
            if ids != {task_id}:
                record.update(last_query_state="identity_mismatch", last_query_http_status=status)
                self.store.update_task(sku, version, record)
                return {"state": "identity_mismatch", "http_status": status}
            remote = _remote_status(payload)
            record.update(remote_status=remote, last_query_state=remote, last_query_http_status=status)
            if remote in _FAILED_STATES:
                record["state"] = "remote_failed"
                self.store.update_task(sku, version, record)
                return {"state": "remote_failed", "remote_status": remote, "http_status": status}
            if remote not in _DONE_STATES:
                record["state"] = "processing"
                self.store.update_task(sku, version, record)
                return {"state": "processing", "remote_status": remote, "http_status": status}
            recovered = self._resume_pending_candidate(sku, version, record, status)
            if recovered is not None:
                return recovered
            media_url = _media_url(payload)
            if media_url is None:
                record["state"] = "remote_completed_no_media"
                self.store.update_task(sku, version, record)
                return {"state": "remote_completed_no_media", "http_status": status}
            return self._download_isolated(sku, version, record, media_url, status)

    def _download_isolated(self, sku: str, version: str, record: dict[str, object],
                           media_url: str, http_status: int) -> dict[str, object]:
        candidate_dir = self.store._path("candidates", sku, version).parent
        self.store._ensure_dir(candidate_dir)
        extension = _video_extension(media_url)
        task_id = str(record["task_id"])
        destination = candidate_dir / f"{version}-{task_id}-{os.urandom(8).hex()}{extension}"
        relative = destination.relative_to(self.root).as_posix()
        attempts = record.get("candidate_attempts")
        if not isinstance(attempts, list):
            attempts = []
        attempt: dict[str, object] = {
            "sku": sku, "version": version, "group_number": record.get("group_number"),
            "model": record.get("model"), "task_id": task_id,
            "candidate_path": relative, "state": "download_started",
        }
        attempts.append(attempt)
        record.update(candidate_attempts=attempts, state="candidate_download_started",
                      candidate_path=relative, failure_category=None, watermark_status="unproven")
        self.store.update_task(sku, version, record)
        try:
            downloaded = (self.downloader.download(media_url, destination, allowed_root=self.store.base)
                          if self.downloader is not None else
                          self.client.download_candidate(media_url, destination, allowed_root=self.store.base))
            downloaded = Path(downloaded)
            if (downloaded.absolute() != destination.absolute() or downloaded.is_symlink()
                    or not downloaded.is_file() or downloaded.stat().st_size <= 0):
                raise ValueError
            digest, size = _sha256_stable_file(destination)
            attempt.update(state="downloaded_unvalidated", sha256=digest, size_bytes=size)
            record.update(state="candidate_validation_pending", candidate_sha256=digest,
                          candidate_size_bytes=size, last_query_http_status=http_status)
            self.store.update_task(sku, version, record)
            return self._validate_candidate_attempt(sku, version, record, attempt, http_status)
        except Exception:
            attempt.update(state="download_failed")
            record.update(state="candidate_download_failed", failure_category="candidate_download",
                          last_query_http_status=http_status)
            self.store.update_task(sku, version, record)
            return {"state": "candidate_download_failed"}

    def _resume_pending_candidate(self, sku: str, version: str, record: dict[str, object],
                                  http_status: int) -> dict[str, object] | None:
        attempts = record.get("candidate_attempts")
        if not isinstance(attempts, list) or not attempts:
            return None
        pending = next((item for item in reversed(attempts)
                        if isinstance(item, dict) and item.get("state") == "downloaded_unvalidated"), None)
        if pending is None:
            return None
        try:
            bound = (pending.get("sku") == sku and pending.get("version") == version
                     and pending.get("group_number") == record.get("group_number")
                     and pending.get("model") == record.get("model")
                     and pending.get("task_id") == record.get("task_id")
                     and isinstance(pending.get("sha256"), str) and _SHA256.fullmatch(pending["sha256"]))
            path = self._candidate_file(sku, version, pending.get("candidate_path"))
            digest, size = _sha256_stable_file(path)
            if not bound or digest != pending["sha256"] or size != pending.get("size_bytes"):
                raise ValueError
        except Exception:
            pending["state"] = "candidate_identity_failed"
            record.update(state="candidate_identity_failed", failure_category="candidate_identity",
                          last_query_http_status=http_status)
            self.store.update_task(sku, version, record)
            return None
        record.update(candidate_path=pending["candidate_path"], candidate_sha256=digest,
                      candidate_size_bytes=size, state="candidate_validation_pending",
                      last_query_http_status=http_status)
        self.store.update_task(sku, version, record)
        return self._validate_candidate_attempt(sku, version, record, pending, http_status)

    def _candidate_file(self, sku: str, version: str, relative_value: object) -> Path:
        if type(relative_value) is not str or not relative_value or len(relative_value) > 1024:
            raise MiniMaxBridgeError("候选路径无效")
        relative = Path(relative_value)
        if relative.is_absolute() or PureWindowsPath(relative_value).is_absolute() or ".." in relative.parts:
            raise MiniMaxBridgeError("候选路径无效")
        expected_root = self.store._path("candidates", sku, version).parent
        self.store._ensure_dir(expected_root)
        resolved_root = expected_root.resolve(strict=True)
        path = self.root / relative
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        current = resolved_root
        for part in resolved.relative_to(resolved_root).parts:
            current = current / part
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise MiniMaxBridgeError("候选路径不安全")
        info = resolved.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise MiniMaxBridgeError("候选文件不是普通文件")
        return resolved

    def _validate_candidate_attempt(self, sku: str, version: str, record: dict[str, object],
                                    attempt: dict[str, object], http_status: int) -> dict[str, object]:
        try:
            path = self._candidate_file(sku, version, attempt.get("candidate_path"))
            digest, size = _sha256_stable_file(path)
            if digest != attempt.get("sha256") or size != attempt.get("size_bytes"):
                raise ValueError
            if self.video_validator is None:
                from .validation import validate_video
                metrics = validate_video(path, write_report=False, expected_aspect_ratio=9 / 16,
                                         decode_all_frames=True, expected_duration_seconds=10)
            else:
                metrics = self.video_validator(path)
        except Exception:
            record.update(state="candidate_validation_pending", failure_category="validation_interrupted",
                          last_query_http_status=http_status)
            self.store.update_task(sku, version, record)
            return {"state": "candidate_validation_pending", "candidate_path": attempt.get("candidate_path"),
                    "candidate_sha256": attempt.get("sha256")}
        get_value = metrics.get if isinstance(metrics, Mapping) else lambda name, default=None: getattr(metrics, name, default)
        duration = get_value("duration")
        width = get_value("width")
        height = get_value("height")
        decoded = get_value("decoded") is True or get_value("ok") is True
        if (not decoded or type(width) is not int or type(height) is not int
                or width < MIN_OUTPUT_WIDTH or height <= 0
                or not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or round(float(duration), 3) != 10.0):
            attempt.update(state="technical_validation_failed")
            record.update(state="technical_validation_failed", failure_category="candidate_validation",
                          candidate_path=attempt["candidate_path"], candidate_sha256=attempt["sha256"],
                          candidate_size_bytes=attempt["size_bytes"], last_query_http_status=http_status)
            self.store.update_task(sku, version, record)
            return {"state": "technical_validation_failed", "candidate_path": attempt["candidate_path"],
                    "candidate_sha256": attempt["sha256"]}
        attempt.update(state="candidate_isolated_for_review")
        record.update(state="candidate_isolated_for_review", candidate_path=attempt["candidate_path"],
                      candidate_sha256=attempt["sha256"], candidate_size_bytes=attempt["size_bytes"],
                      candidate_duration_seconds=float(duration), candidate_width=width,
                      candidate_height=height, watermark_status="unproven",
                      last_query_http_status=http_status)
        self.store.update_task(sku, version, record)
        return {"state": "candidate_isolated_for_review", "candidate_path": attempt["candidate_path"],
                "candidate_sha256": attempt["sha256"], "duration_seconds": float(duration),
                "width": width, "height": height, "watermark_status": "unproven"}

