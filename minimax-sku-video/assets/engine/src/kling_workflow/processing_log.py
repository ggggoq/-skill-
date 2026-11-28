from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SAFE_COMMAND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFE_SKU = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_VERSION = re.compile(r"^(?:v[0-9]{3,6}|[0-9]{8}-[0-9]{6})$")
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SAFE_STEP_MODELS = frozenset({"Luna", "Sol", "Web", "local"})
_SAFE_STEP_ROLES = frozenset({"initial_draft", "review", "source_lookup", "query", "download"})
_SAFE_CREATION_FAILURE_CATEGORIES = frozenset({
    "credential_unavailable", "invalid_request_body", "request_build_error",
    "connect_timeout", "connect_error", "read_timeout", "write_timeout",
    "protocol_error", "other_transport_error", "transport_unknown",
})
_SAFE_CREATION_FAILURE_STAGES = frozenset({
    "credential_lookup", "serialize", "request_build", "send", "client_call",
})
_LOG_SECRET_PATTERN = re.compile(
    r"(?i)(?:https?://|bearer\s|authorization|api[_ -]?key|secret|token|signed[_ -]?url)"
)


class ProcessingLogError(RuntimeError):
    """Raised when an append-only processing audit log cannot be written safely."""


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def _safe_optional(value: object, pattern: re.Pattern[str]) -> str | None:
    if type(value) is not str or not pattern.fullmatch(value):
        return None
    return value


def _encode_entry(entry: dict[str, object]) -> bytes:
    return (
        json.dumps(
            entry,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_all(handle: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(handle, payload[offset:])
        if written <= 0:
            raise OSError("processing log write did not advance")
        offset += written


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


@dataclass
class ProcessingLog:
    path: Path
    _handle: int
    _paid_creation_status: str
    _finished: bool = False
    _sol_review_recorded: bool = False

    @classmethod
    def start(
        cls,
        root: Path,
        *,
        command: object,
        sku: object = None,
        version: object = None,
        network_mode: str,
        paid_authorization_present: bool,
    ) -> "ProcessingLog":
        if network_mode not in {"offline", "read_only", "paid"}:
            raise ProcessingLogError("invalid processing log mode")
        root = Path(root)
        log_dir = root / "06_处理日志"
        try:
            if log_dir.exists() or log_dir.is_symlink():
                metadata = log_dir.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
                    raise ProcessingLogError("unsafe processing log directory")
            else:
                log_dir.mkdir()
            if log_dir.resolve(strict=True).parent != root.resolve(strict=True):
                raise ProcessingLogError("processing log directory escaped project root")
        except ProcessingLogError:
            raise
        except OSError as exc:
            raise ProcessingLogError("cannot create processing log directory") from exc

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_command = _safe_optional(command, _SAFE_COMMAND) or "unknown"
        filename = f"{timestamp}-{safe_command}-{uuid.uuid4().hex[:12]}.jsonl"
        path = log_dir / filename
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        handle: int | None = None
        try:
            handle = os.open(path, flags, 0o600)
            paid_creation_status = (
                "not_observed" if network_mode == "paid" else "not_applicable"
            )
            started = {
                "schema_version": 1,
                "event": "started",
                "started_at_utc": _utc_timestamp(),
                "command": safe_command,
                "sku": _safe_optional(sku, _SAFE_SKU),
                "version": _safe_optional(version, _SAFE_VERSION),
                "network_mode": network_mode,
                "paid_authorization_present": paid_authorization_present is True,
                "paid_creation_status": paid_creation_status,
                "model": "local",
                "role": "command",
                "reason": "执行本地可灵工作流命令",
            }
            _write_all(handle, _encode_entry(started))
            os.fsync(handle)
        except OSError as exc:
            try:
                if handle is not None:
                    os.close(handle)
            except OSError:
                pass
            raise ProcessingLogError("cannot start processing log") from exc
        assert handle is not None
        return cls(
            path=path,
            _handle=handle,
            _paid_creation_status=paid_creation_status,
        )

    def finish(self, *, status: str, exit_code: int) -> None:
        if self._finished:
            raise ProcessingLogError("processing log already finished")
        if status not in {"success", "failure", "refused", "cancelled"}:
            raise ProcessingLogError("invalid processing status")
        if type(exit_code) is not int or not 0 <= exit_code <= 255:
            raise ProcessingLogError("invalid processing exit code")
        entry = {
            "schema_version": 1,
            "event": "finished",
            "finished_at_utc": _utc_timestamp(),
            "status": status,
            "exit_code": exit_code,
            "paid_creation_status": self._paid_creation_status,
            "model": "local",
            "role": "command_result",
            "reason": "记录本地工作流命令结果",
        }
        try:
            _write_all(self._handle, _encode_entry(entry))
            os.fsync(self._handle)
            os.close(self._handle)
        except OSError as exc:
            try:
                os.close(self._handle)
            except OSError:
                pass
            raise ProcessingLogError("cannot finish processing log") from exc
        finally:
            self._finished = True

    def set_paid_creation_status(self, status: str) -> None:
        """Update the final audit state as a paid creation request is observed."""
        transitions = {
            "not_observed": {"not_observed", "creation_unknown", "task_known"},
            "creation_unknown": {"creation_unknown", "task_known"},
            # A batch may complete one serial create before recording the next
            # create intent; each SKU/version still has its own durable task record.
            "task_known": {"task_known", "creation_unknown"},
        }
        if self._finished:
            raise ProcessingLogError("processing log already finished")
        if type(status) is not str or status not in transitions.get(self._paid_creation_status, set()):
            raise ProcessingLogError("invalid paid creation status transition")
        self._paid_creation_status = status

    def record_paid_creation_failure(self, *, category: str, stage: str) -> None:
        """Append only fixed transport categories and stages; never raw exception data."""
        if self._finished:
            raise ProcessingLogError("processing log already finished")
        if self._paid_creation_status != "creation_unknown":
            raise ProcessingLogError("paid creation failure requires an unknown creation state")
        if (type(category) is not str or category not in _SAFE_CREATION_FAILURE_CATEGORIES
                or type(stage) is not str or stage not in _SAFE_CREATION_FAILURE_STAGES):
            raise ProcessingLogError("invalid paid creation failure category or stage")
        entry = {
            "schema_version": 1,
            "event": "paid_creation_failure",
            "recorded_at_utc": _utc_timestamp(),
            "paid_creation_status": "creation_unknown",
            "failure_category": category,
            "failure_stage": stage,
            "model": "local",
            "role": "command_result",
            "reason": "记录创建请求的安全失败阶段",
        }
        try:
            _write_all(self._handle, _encode_entry(entry))
            os.fsync(self._handle)
        except OSError as exc:
            raise ProcessingLogError("cannot append paid creation failure") from exc

    def record_step(
        self,
        *,
        model: str,
        role: str,
        reason: str,
        result: str,
        evidence: str = "",
        exception: str = "",
        sku: str | None = None,
        version: str | None = None,
    ) -> None:
        """Append a bounded outcome record without request content or hidden reasoning."""
        if self._finished:
            raise ProcessingLogError("processing log already finished")
        if (
            type(model) is not str
            or type(role) is not str
            or model not in _SAFE_STEP_MODELS
            or role not in _SAFE_STEP_ROLES
        ):
            raise ProcessingLogError("invalid processing log model or role")
        if not all(
            _safe_log_value(value, limit=240, required=required)
            for value, required in (
                (reason, True), (result, True), (evidence, False), (exception, False)
            )
        ):
            raise ProcessingLogError("processing log content is unsafe")
        if model == "Sol" and role == "review":
            if self._sol_review_recorded:
                raise ProcessingLogError("Sol 审核只允许一次审核")
            self._sol_review_recorded = True
        entry = {
            "schema_version": 1,
            "event": "step",
            "recorded_at_utc": _utc_timestamp(),
            "model": model,
            "role": role,
            "reason": reason.strip(),
            "result": result.strip(),
            "evidence": evidence.strip(),
            "exception": exception.strip(),
        }
        if sku is not None:
            safe_sku = _safe_optional(sku, _SAFE_SKU)
            if safe_sku is None:
                raise ProcessingLogError("invalid processing log SKU context")
            entry["sku"] = safe_sku
        if version is not None:
            safe_version = _safe_optional(version, _SAFE_VERSION)
            if safe_version is None:
                raise ProcessingLogError("invalid processing log version context")
            entry["version"] = safe_version
        try:
            _write_all(self._handle, _encode_entry(entry))
            os.fsync(self._handle)
        except OSError as exc:
            raise ProcessingLogError("cannot append processing log step") from exc

    def record_rejection_archive(
        self,
        *,
        sku: str,
        reference_folder: str,
        source_path: str,
        archive_path: str,
        rejection_reason: str,
        version: str | None = None,
    ) -> None:
        """Append the required evidence for one rejected-video archive copy."""
        if self._finished:
            raise ProcessingLogError("processing log already finished")
        if _safe_optional(sku, _SAFE_SKU) is None:
            raise ProcessingLogError("invalid processing log SKU context")
        if version is not None and _safe_optional(version, _SAFE_VERSION) is None:
            raise ProcessingLogError("invalid processing log version context")
        if not all(
            _safe_log_value(value, limit=2048, required=True)
            for value in (reference_folder, source_path, archive_path)
        ) or not _safe_log_value(rejection_reason, limit=512, required=True):
            raise ProcessingLogError("processing log rejection archive fields are unsafe")
        entry = {
            "schema_version": 1,
            "event": "review_rejection_archived",
            "recorded_at_utc": _utc_timestamp(),
            "sku": sku,
            "reference_folder": reference_folder,
            "source_video_path": source_path,
            "archive_video_path": archive_path,
            "rejection_reason": rejection_reason.strip(),
        }
        if version is not None:
            entry["version"] = version
        try:
            _write_all(self._handle, _encode_entry(entry))
            os.fsync(self._handle)
        except OSError as exc:
            raise ProcessingLogError("cannot append rejection archive log entry") from exc


def _safe_log_value(value: object, *, limit: int, required: bool) -> bool:
    return (
        isinstance(value, str)
        and (bool(value.strip()) if required else True)
        and len(value) <= limit
        and not _LOG_SECRET_PATTERN.search(value)
        and "\x00" not in value
    )
