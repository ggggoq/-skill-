from __future__ import annotations

import base64
from contextlib import closing
from contextlib import contextmanager
from contextvars import ContextVar
import ipaddress
import httpx
import json
import logging
import os
import re
import socket
import stat
import tempfile
import threading
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from .assets import PreparedAssets


BASE_URL = "https://api-beijing.klingai.com"
ELEMENTS = "/v1/general/advanced-custom-elements"
VIDEOS = "/v1/videos/image2video"
_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
_VALID_STATUSES = frozenset({"submitted", "processing", "succeed", "failed"})
_JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_LONG_BASE64_PATTERN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/])")
_REPARSE_POINT_ATTRIBUTE = 0x400
_DEFAULT_MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
_HTTP_LOGGER_NAMES = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_HTTP_LOG_CONTEXT: ContextVar[frozenset[str] | None] = ContextVar(
    "kling_http_log_redaction", default=None
)
_HTTP_LOG_FILTER_LOCK = threading.RLock()


class KlingError(RuntimeError):
    """A safe, user-facing Kling client error."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        kind: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.kind = kind
        super().__init__(message)


class DownloadTargetExists(KlingError):
    """The approved local download target already exists; no remote retry is needed."""

    def __init__(self) -> None:
        super().__init__("下载目标文件已存在，已保留原文件")


class NetworkUnknownOutcome(KlingError):
    """A create POST may have reached Kling; callers must query before resuming."""

    def __init__(self, _message: str | None = None) -> None:
        super().__init__("可灵创建请求结果未知，请按 external_task_id 查询后再继续")


@dataclass(frozen=True)
class ConnectionResult:
    request_id: str | None
    message: str
    element_count: int


@dataclass(frozen=True)
class RemoteTask:
    task_id: str
    status: str
    external_task_id: str | None = None
    request_id: str | None = None
    message: str = ""
    final_unit_deduction: str | None = None
    final_balance_deduction: dict[str, str] | None = None


@dataclass(frozen=True)
class RemoteElement(RemoteTask):
    element_id: int | None = None


@dataclass(frozen=True)
class RemoteVideo(RemoteTask):
    video_id: str | None = None
    download_url: str | None = field(default=None, repr=False, compare=False)
    duration: str | None = None
    watermark_enabled: bool | None = None


@dataclass(frozen=True)
class _DirectorySnapshot:
    allowed_root: Path
    destination_parent: Path
    identities: tuple[tuple[Path, tuple[int, int]], ...]


class _RequestLogSanitizer(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        sensitive_values = _HTTP_LOG_CONTEXT.get()
        if not sensitive_values:
            return True
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        redacted = _redact_log_text(rendered, sensitive_values)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        if record.exc_info:
            try:
                exception_text = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = _redact_log_text(exception_text, sensitive_values)
                record.exc_info = None
            except Exception:
                record.exc_text = "[异常详情已隐藏]"
                record.exc_info = None
        if record.stack_info:
            record.stack_info = _redact_log_text(record.stack_info, sensitive_values)
        return True


def _redact_log_text(value: str, sensitive_values: Iterable[str]) -> str:
    redacted = value
    for sensitive in sorted((item for item in sensitive_values if item), key=len, reverse=True):
        redacted = redacted.replace(sensitive, "[已隐藏敏感信息]")
    return _URL_PATTERN.sub("[已隐藏下载链接]", redacted)


@contextmanager
def _redact_transport_logs(sensitive_values: Iterable[str]):
    context_values = frozenset(value for value in sensitive_values if value)
    context_token = _HTTP_LOG_CONTEXT.set(context_values)
    sanitizer = _RequestLogSanitizer()
    loggers = [logging.getLogger(name) for name in _HTTP_LOGGER_NAMES]
    with _HTTP_LOG_FILTER_LOCK:
        for logger in loggers:
            logger.addFilter(sanitizer)
    try:
        yield
    finally:
        with _HTTP_LOG_FILTER_LOCK:
            for logger in loggers:
                logger.removeFilter(sanitizer)
        _HTTP_LOG_CONTEXT.reset(context_token)


class KlingClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        api_key_provider: Callable[[], str],
        api_transport: httpx.BaseTransport | None = None,
        download_transport: httpx.BaseTransport | None = None,
        host_resolver: Callable[[str], Iterable[str]] | None = None,
        max_download_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES,
    ) -> None:
        if base_url.rstrip("/") != BASE_URL:
            raise KlingError("可灵 API 地址必须使用固定的北京域名")
        self.base_url = BASE_URL
        self._api_key_provider = api_key_provider
        for name, transport in (
            ("API", api_transport),
            ("下载", download_transport),
        ):
            if transport is not None and not isinstance(transport, httpx.BaseTransport):
                raise KlingError(f"可灵{name}依赖必须是低层 HTTP transport")
        self._api_client = httpx.Client(
            auth=None,
            cookies=None,
            headers=None,
            event_hooks=None,
            trust_env=False,
            proxy=None,
            mounts=None,
            follow_redirects=False,
            transport=api_transport,
        )
        self._download_client = httpx.Client(
            auth=None,
            cookies=None,
            headers=None,
            event_hooks=None,
            trust_env=False,
            proxy=None,
            mounts=None,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
            transport=download_transport,
        )
        if (
            not isinstance(max_download_bytes, int)
            or isinstance(max_download_bytes, bool)
            or max_download_bytes <= 0
        ):
            raise KlingError("可灵下载体积上限必须为正整数")
        self._max_download_bytes = max_download_bytes
        self._host_resolver = _system_host_resolver if host_resolver is None else host_resolver

    def close(self) -> None:
        self._api_client.close()
        self._download_client.close()

    def check_connection(self) -> ConnectionResult:
        payload = self._request(
            "GET", ELEMENTS, params={"pageNum": 1, "pageSize": 1}
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise KlingError("可灵连接检查返回的数据结构无效", kind="invalid_response")
        return ConnectionResult(
            request_id=_safe_identifier(payload.get("request_id")),
            message=_safe_text(payload.get("message")),
            element_count=len(data),
        )

    def create_element(
        self, prepared: PreparedAssets, sku: str, external_task_id: str
    ) -> RemoteTask:
        if not isinstance(sku, str) or not sku or len(sku) > 20:
            raise KlingError("可灵元素名称必须为 1–20 个字符")
        _validate_external_id(external_task_id)
        if prepared.sku != sku:
            raise KlingError("款号与已准备的服装参考图不一致")
        references = tuple(prepared.references)
        if not 1 <= len(references) <= 3:
            raise KlingError("可灵元素必须包含 1 张正面图和 1–3 张其他参考图")

        try:
            frontal_b64 = base64.b64encode(prepared.frontal.api_path.read_bytes()).decode("ascii")
            reference_b64 = [
                base64.b64encode(item.api_path.read_bytes()).decode("ascii")
                for item in references
            ]
        except OSError:
            raise KlingError("无法读取已准备的可灵元素参考图") from None

        body: dict[str, object] = {
            "element_name": sku,
            "element_description": "服装多角度参考元素",
            "reference_type": "image_refer",
            "element_image_list": {
                "frontal_image": frontal_b64,
                "refer_images": [{"image_url": image} for image in reference_b64],
            },
            "external_task_id": external_task_id,
            "tag_list": [{"tag_id": "o_105"}],
        }
        payload = self._request("POST", ELEMENTS, json_body=body)
        try:
            return _parse_remote_task(payload)
        except KlingError:
            raise NetworkUnknownOutcome() from None

    def query_element(
        self, task_id: str | None = None, external_task_id: str | None = None
    ) -> RemoteElement:
        identifier = _select_identifier(task_id, external_task_id)
        payload = self._request("GET", f"{ELEMENTS}/{quote(identifier, safe='')}")
        task = _parse_remote_task(payload)
        element_id: int | None = None
        result = _safe_mapping(payload.get("data")).get("task_result")
        result_map = _safe_mapping(result)
        elements = result_map.get("elements")
        if elements is not None:
            if not isinstance(elements, list) or (elements and not isinstance(elements[0], Mapping)):
                raise KlingError("可灵元素查询返回的数据结构无效", request_id=task.request_id)
            if elements:
                raw_element_id = elements[0].get("element_id")
                if isinstance(raw_element_id, int) and not isinstance(raw_element_id, bool) and raw_element_id > 0:
                    element_id = raw_element_id
                else:
                    raise KlingError("可灵元素查询缺少有效 element_id", request_id=task.request_id)
        if task.status == "succeed" and element_id is None:
            raise KlingError("可灵元素任务已成功，但响应缺少 element_id", request_id=task.request_id)
        return RemoteElement(**task.__dict__, element_id=element_id)

    def create_video(self, payload: Mapping[str, object]) -> RemoteTask:
        if not isinstance(payload, Mapping):
            raise KlingError("可灵视频请求内容必须是字段映射")
        body = dict(payload)
        # The API contract has no speed setting; local policy is fixed to standard.
        body.pop("local_generation_policy", None)
        external_task_id = body.get("external_task_id")
        _validate_external_id(external_task_id)
        response = self._request("POST", VIDEOS, json_body=body)
        try:
            return _parse_remote_task(response)
        except KlingError:
            raise NetworkUnknownOutcome() from None

    def query_video(
        self, task_id: str | None = None, external_task_id: str | None = None
    ) -> RemoteVideo:
        identifier = _select_identifier(task_id, external_task_id)
        payload = self._request("GET", f"{VIDEOS}/{quote(identifier, safe='')}")
        task = _parse_remote_task(payload)
        video_id: str | None = None
        download_url: str | None = None
        duration: str | None = None
        watermark_enabled: bool | None = None
        result_map = _safe_mapping(_safe_mapping(payload.get("data")).get("task_result"))
        videos = result_map.get("videos")
        if videos is not None:
            if not isinstance(videos, list) or (videos and not isinstance(videos[0], Mapping)):
                raise KlingError("可灵视频查询返回的数据结构无效", request_id=task.request_id)
            if videos:
                video = videos[0]
                video_id = _safe_identifier(video.get("id"))
                raw_url = video.get("url")
                duration = _safe_text(video.get("duration"), limit=32) or None
                watermark_info = _safe_mapping(video.get("watermark_info"))
                raw_watermark_enabled = watermark_info.get("enabled")
                if type(raw_watermark_enabled) is bool:
                    watermark_enabled = raw_watermark_enabled
                if (
                    not video_id
                    or watermark_enabled is not False
                    or not isinstance(raw_url, str)
                    or not _valid_https_url(raw_url)
                ):
                    raise KlingError("可灵视频查询缺少有效结果信息", request_id=task.request_id)
                download_url = raw_url
        if task.status == "succeed" and not download_url:
            raise KlingError("可灵视频任务已成功，但响应缺少下载结果", request_id=task.request_id)
        return RemoteVideo(
            **task.__dict__,
            video_id=video_id,
            download_url=download_url,
            duration=duration,
            watermark_enabled=watermark_enabled,
        )

    def query_video_by_external_id(self, external_task_id: str) -> RemoteVideo:
        return self.query_video(external_task_id=external_task_id)

    def download(
        self,
        url: str,
        destination: str | Path,
        *,
        allowed_root: str | Path,
    ) -> Path:
        target = Path(destination)
        root = Path(allowed_root)
        snapshot = _capture_directory_snapshot(root, target.parent)
        _reject_existing_download_target(target)
        approved_ip = _validate_public_download_url(url, self._host_resolver)
        original_url = httpx.URL(url)
        original_host = original_url.host
        connect_url = original_url.copy_with(host=approved_ip)
        host_header = f"[{original_host}]" if ":" in original_host else original_host
        if original_url.port is not None and original_url.port != 443:
            host_header = f"{host_header}:{original_url.port}"

        file_descriptor: int | None = None
        temporary_path: Path | None = None
        temporary_identity: tuple[int, int] | None = None
        try:
            _verify_directory_snapshot(snapshot)
            file_descriptor, raw_temporary = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".download", dir=target.parent
            )
            temporary_path = Path(raw_temporary)
            descriptor_stat = os.fstat(file_descriptor)
            temporary_identity = (int(descriptor_stat.st_dev), int(descriptor_stat.st_ino))
            if temporary_identity != _file_identity(temporary_path):
                raise KlingError("下载临时文件身份在创建期间发生变化")
            output = os.fdopen(file_descriptor, "wb")
            file_descriptor = None
            with output:
                request = httpx.Request(
                    "GET",
                    connect_url,
                    headers={"Accept": "video/*", "Host": host_header},
                    extensions={
                        "timeout": _TIMEOUT.as_dict(),
                        "sni_hostname": original_host,
                    },
                )
                with _redact_transport_logs({url}):
                    with closing(
                        self._download_client.send(
                            request,
                            stream=True,
                            auth=None,
                            follow_redirects=False,
                        )
                    ) as response:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError:
                            raise KlingError(
                                f"可灵视频下载失败（HTTP {response.status_code}）"
                            ) from None
                        content_type = response.headers.get("content-type", "").lower()
                        if "json" in content_type or "text/html" in content_type:
                            raise KlingError("可灵下载地址返回了错误响应")
                        declared_size = _content_length(response.headers.get("content-length"))
                        if declared_size is not None and declared_size > self._max_download_bytes:
                            raise KlingError("可灵视频超过本地下载体积上限")
                        received = 0
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            received += len(chunk)
                            if received > self._max_download_bytes:
                                raise KlingError("可灵视频超过本地下载体积上限")
                            output.write(chunk)
                        if received == 0:
                            raise KlingError("可灵下载内容为空，未保存文件")
                output.flush()
                os.fsync(output.fileno())

            _verify_directory_snapshot(snapshot)
            _verify_file_identity(temporary_path, temporary_identity)
            _reject_existing_download_target(target)
            _publish_temp_exclusive(temporary_path, target)
            if os.name == "nt":
                temporary_path = None
            _verify_directory_snapshot(snapshot)
            if _file_identity(target) != temporary_identity:
                raise KlingError("下载文件发布后的身份校验失败")
            if temporary_path is not None:
                _safe_unlink_owned_temp(temporary_path, temporary_identity, snapshot)
                temporary_path = None
            return target
        except KlingError:
            raise
        except httpx.HTTPError as exc:
            raise KlingError(f"可灵视频下载失败：{type(exc).__name__}") from None
        except (OSError, ValueError, TypeError) as exc:
            raise KlingError(f"可灵视频下载失败：{type(exc).__name__}") from None
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temporary_path is not None and temporary_identity is not None:
                _safe_unlink_owned_temp(temporary_path, temporary_identity, snapshot)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            api_key = self._api_key_provider()
        except Exception:
            raise KlingError("无法取得可灵 API Key", kind="credential") from None
        if not isinstance(api_key, str) or not api_key:
            raise KlingError("无法取得可灵 API Key", kind="credential") from None
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        sensitive_values = {api_key}
        sensitive_values.update(_image_values(json_body))
        try:
            with _redact_transport_logs(sensitive_values):
                response = self._api_client.request(
                    method,
                    f"{BASE_URL}{path}",
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=_TIMEOUT,
                    follow_redirects=False,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError:
                    status_code = response.status_code
                    if method == "POST" and (status_code >= 500 or status_code == 408):
                        raise NetworkUnknownOutcome() from None
                    if status_code in {401, 403}:
                        kind = "auth"
                    elif status_code in {404, 405}:
                        kind = "endpoint"
                    else:
                        kind = "http"
                    raise KlingError(
                        f"可灵请求失败（HTTP {status_code}）", kind=kind
                    ) from None
                response_payload = response.json()
        except NetworkUnknownOutcome:
            raise
        except KlingError:
            raise
        except httpx.HTTPError as exc:
            if method == "POST":
                raise NetworkUnknownOutcome() from None
            raise KlingError(
                f"可灵网络请求失败：{type(exc).__name__}", kind="network"
            ) from None
        except (ValueError, json.JSONDecodeError):
            if method == "POST":
                raise NetworkUnknownOutcome() from None
            raise KlingError("可灵接口返回了无效 JSON", kind="invalid_response") from None

        if not isinstance(response_payload, Mapping):
            if method == "POST":
                raise NetworkUnknownOutcome() from None
            raise KlingError("可灵接口返回的数据结构无效", kind="invalid_response")
        code = response_payload.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            if method == "POST":
                raise NetworkUnknownOutcome() from None
            raise KlingError("可灵接口返回的数据结构无效", kind="invalid_response")
        if code != 0:
            safe_request_id = _safe_identifier(
                response_payload.get("request_id"), sensitive_values=sensitive_values
            )
            safe_message = _safe_text(response_payload.get("message"), sensitive_values=sensitive_values)
            message = f"可灵接口失败：{safe_message or '未知错误'}"
            if safe_request_id:
                message += f"；request_id={safe_request_id}"
            raise KlingError(
                message, request_id=safe_request_id, kind="api_rejected"
            )
        return _safe_response(response_payload, sensitive_values)


def _parse_remote_task(payload: Mapping[str, object]) -> RemoteTask:
    data = _safe_mapping(payload.get("data"))
    task_id = _strict_response_identifier(data.get("task_id"), "task_id")
    status = _safe_text(data.get("task_status"), limit=32)
    if not task_id or status not in _VALID_STATUSES:
        raise KlingError("可灵任务响应缺少有效 task_id 或 task_status")
    task_info = _safe_mapping(data.get("task_info"))
    external_task_id = _strict_response_identifier(
        task_info.get("external_task_id"), "external_task_id", optional=True
    )
    request_id = _strict_response_identifier(
        payload.get("request_id"), "request_id", optional=True
    )
    message = _safe_text(data.get("task_status_msg")) or _safe_text(payload.get("message"))
    unit_deduction = _safe_text(data.get("final_unit_deduction"), limit=64) or None
    balance_deduction = _safe_deduction(data.get("final_balance_deduction"))
    return RemoteTask(
        task_id=task_id,
        status=status,
        external_task_id=external_task_id,
        request_id=request_id,
        message=message,
        final_unit_deduction=unit_deduction,
        final_balance_deduction=balance_deduction,
    )


def _safe_deduction(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    safe: dict[str, str] = {}
    for key in ("quota", "list_price"):
        text = _safe_text(value.get(key), limit=64)
        if text:
            safe[key] = text
    return safe or None


def _safe_response(
    payload: Mapping[str, object], sensitive_values: set[str]
) -> dict[str, object]:
    safe_payload: dict[str, object] = {
        "code": 0,
        "request_id": _safe_identifier(
            payload.get("request_id"), sensitive_values=sensitive_values
        ),
        "message": _safe_text(
            payload.get("message"), sensitive_values=sensitive_values
        ),
    }
    raw_data = payload.get("data")
    if isinstance(raw_data, list):
        safe_payload["data"] = [{} for _ in raw_data]
        return safe_payload
    if not isinstance(raw_data, Mapping):
        safe_payload["data"] = None
        return safe_payload

    raw_task_id = raw_data.get("task_id")
    safe_data: dict[str, object] = {
        "task_id": _safe_identifier(raw_task_id, sensitive_values=sensitive_values),
        "task_status": _safe_text(
            raw_data.get("task_status"), limit=32, sensitive_values=sensitive_values
        ),
        "task_status_msg": _safe_text(
            raw_data.get("task_status_msg"), sensitive_values=sensitive_values
        ),
    }
    task_info = raw_data.get("task_info")
    if isinstance(task_info, Mapping):
        raw_external_id = task_info.get("external_task_id")
        safe_data["task_info"] = {
            "external_task_id": (
                None
                if raw_external_id is None
                else (
                    _safe_identifier(raw_external_id, sensitive_values=sensitive_values)
                    if isinstance(raw_external_id, str)
                    and raw_external_id
                    and raw_external_id == raw_external_id.strip()
                    else ""
                )
            )
        }
    for key in ("final_unit_deduction",):
        safe_data[key] = _safe_text(
            raw_data.get(key), limit=64, sensitive_values=sensitive_values
        ) or None
    balance = raw_data.get("final_balance_deduction")
    if isinstance(balance, Mapping):
        safe_data["final_balance_deduction"] = {
            key: _safe_text(
                balance.get(key), limit=64, sensitive_values=sensitive_values
            )
            for key in ("quota", "list_price")
            if _safe_text(
                balance.get(key), limit=64, sensitive_values=sensitive_values
            )
        }

    task_result = raw_data.get("task_result")
    if isinstance(task_result, Mapping):
        safe_result: dict[str, object] = {}
        raw_elements = task_result.get("elements")
        if isinstance(raw_elements, list):
            safe_result["elements"] = [
                {"element_id": item.get("element_id")}
                for item in raw_elements
                if isinstance(item, Mapping)
            ]
        raw_videos = task_result.get("videos")
        if isinstance(raw_videos, list):
            safe_videos: list[dict[str, object]] = []
            for item in raw_videos:
                if not isinstance(item, Mapping):
                    continue
                safe_videos.append(
                    {
                        "id": _safe_identifier(item.get("id"), sensitive_values=sensitive_values),
                        # The signed URL is kept only for download and is hidden by RemoteVideo.__repr__.
                        "url": item.get("url"),
                        "duration": _safe_text(
                            item.get("duration"), limit=32, sensitive_values=sensitive_values
                        ),
                        "watermark_info": {
                            "enabled": _safe_mapping(item.get("watermark_info")).get("enabled")
                            if type(_safe_mapping(item.get("watermark_info")).get("enabled")) is bool
                            else None
                        },
                    }
                )
            safe_result["videos"] = safe_videos
        safe_data["task_result"] = safe_result
    safe_payload["data"] = safe_data
    return safe_payload


def _safe_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _safe_identifier(
    value: object, *, sensitive_values: set[str] | frozenset[str] = frozenset()
) -> str | None:
    if not isinstance(value, str):
        return None
    text = _safe_text(value, limit=256, sensitive_values=sensitive_values)
    return text or None


def _safe_text(
    value: object,
    *,
    limit: int = 512,
    sensitive_values: set[str] | frozenset[str] = frozenset(),
) -> str:
    if value is None or isinstance(value, (dict, list, tuple, bytes, bytearray)):
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = str(value).strip()
    for sensitive in sorted((item for item in sensitive_values if item), key=len, reverse=True):
        text = text.replace(sensitive, "[已隐藏]")
    text = _JWT_PATTERN.sub("[已隐藏令牌]", text)
    text = _BEARER_PATTERN.sub("Bearer [已隐藏]", text)
    text = _URL_PATTERN.sub("[已隐藏链接]", text)
    text = _LONG_BASE64_PATTERN.sub("[已隐藏图片数据]", text)
    return text[:limit]


def _image_values(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if "image" in str(key).lower():
                found.update(_string_values(item))
            else:
                found.update(_image_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_image_values(item))
    return {item for item in found if item}


def _string_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {item for nested in value.values() for item in _string_values(nested)}
    if isinstance(value, (list, tuple)):
        return {item for nested in value for item in _string_values(nested)}
    return set()


def _validate_external_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
    ):
        raise KlingError("external_task_id 必须是非空文本")


def _select_identifier(task_id: str | None, external_task_id: str | None) -> str:
    if (task_id is None) == (external_task_id is None):
        raise KlingError("查询时必须且只能提供 task_id 或 external_task_id")
    identifier = task_id if task_id is not None else external_task_id
    if (
        not isinstance(identifier, str)
        or not identifier
        or identifier != identifier.strip()
        or identifier in {".", ".."}
    ):
        raise KlingError("查询任务标识无效")
    return identifier


def _valid_https_url(value: str) -> bool:
    if not isinstance(value, str) or value != value.strip() or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    try:
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and (port is None or 1 <= port <= 65535)
    )


def _strict_response_identifier(
    value: object, field_name: str, *, optional: bool = False
) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
    ):
        raise KlingError(f"可灵响应中的 {field_name} 类型或内容无效")
    return value


def _validate_public_download_url(
    value: str, host_resolver: Callable[[str], Iterable[str]]
) -> str:
    if not _valid_https_url(value):
        raise KlingError("可灵下载地址无效")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port or 443
    except ValueError:
        raise KlingError("可灵下载地址端口无效") from None
    if port != 443 or not host or host == "localhost" or host.endswith(
        (".localhost", ".local", ".internal", ".example", ".test", ".invalid", ".onion")
    ):
        raise KlingError("可灵下载地址主机不受允许")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = tuple(host_resolver(host))
        except Exception:
            raise KlingError("可灵下载地址无法安全解析") from None
        if not resolved:
            raise KlingError("可灵下载地址无法安全解析")
    else:
        resolved = (literal,)

    for address in resolved:
        try:
            parsed_address = ipaddress.ip_address(str(address))
        except (TypeError, ValueError):
            raise KlingError("可灵下载地址解析结果无效") from None
        if not parsed_address.is_global:
            raise KlingError("可灵下载地址不能指向本机、内网或保留地址")
    try:
        return ipaddress.ip_address(str(resolved[0])).compressed
    except (TypeError, ValueError, IndexError):
        raise KlingError("可灵下载地址解析结果无效") from None


def _system_host_resolver(host: str) -> tuple[str, ...]:
    try:
        results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise KlingError("可灵下载地址无法安全解析") from None
    return tuple(result[4][0] for result in results)


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text or not text.isascii() or not text.isdecimal():
        raise KlingError("可灵下载响应的 Content-Length 无效")
    return int(text)


def _capture_directory_snapshot(
    allowed_root: str | Path, destination_parent: str | Path
) -> _DirectorySnapshot:
    raw_root = Path(allowed_root)
    raw_parent = Path(destination_parent)
    if ".." in raw_root.parts or ".." in raw_parent.parts:
        raise KlingError("下载路径不能包含父目录跳转")
    root = Path(os.path.abspath(raw_root))
    parent = Path(os.path.abspath(raw_parent))

    ordered_paths: list[Path] = []
    for candidate in (*reversed(root.parents), root, *reversed(parent.parents), parent):
        if candidate not in ordered_paths:
            ordered_paths.append(candidate)

    identities: list[tuple[Path, tuple[int, int]]] = []
    try:
        for directory in ordered_paths:
            metadata = directory.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or attributes & _REPARSE_POINT_ATTRIBUTE
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise KlingError("允许输出根或下载父链包含链接、重解析点或非目录")
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            if identity == (0, 0):
                raise KlingError("文件系统无法提供可靠的下载目录身份")
            identities.append((directory, identity))
    except FileNotFoundError:
        raise KlingError("下载目标文件夹不存在") from None
    except OSError:
        raise KlingError("无法安全检查下载目录") from None

    snapshot = _DirectorySnapshot(root, parent, tuple(identities))
    _verify_directory_snapshot(snapshot)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
        if resolved_parent != resolved_root:
            resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        raise KlingError("下载目标必须严格位于已存在的允许输出目录内") from None
    _verify_directory_snapshot(snapshot)
    return snapshot


def _verify_directory_snapshot(snapshot: _DirectorySnapshot) -> None:
    try:
        for directory, expected_identity in snapshot.identities:
            metadata = directory.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            actual_identity = (int(metadata.st_dev), int(metadata.st_ino))
            if (
                stat.S_ISLNK(metadata.st_mode)
                or attributes & _REPARSE_POINT_ATTRIBUTE
                or not stat.S_ISDIR(metadata.st_mode)
                or actual_identity != expected_identity
            ):
                raise KlingError("下载目录身份发生变化，已停止发布")
    except (FileNotFoundError, OSError):
        raise KlingError("下载目录身份发生变化，已停止发布") from None


def _reject_existing_download_target(target: Path) -> None:
    if target.name in {"", ".", ".."}:
        raise KlingError("下载目标文件名无效")
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise KlingError("无法安全检查下载目标") from None
    raise DownloadTargetExists()


def _publish_temp_exclusive(temporary_path: Path, target: Path) -> None:
    try:
        if os.name == "nt":
            # Windows rename is atomic and fails when the destination already exists.
            os.rename(temporary_path, target)
        else:
            # A hard-link creation is atomic and never replaces an existing destination.
            os.link(temporary_path, target)
    except FileExistsError:
        raise DownloadTargetExists() from None


def _file_identity(path: str | Path) -> tuple[int, int]:
    try:
        metadata = Path(path).lstat()
    except OSError:
        raise KlingError("下载临时文件身份无法验证") from None
    attributes = getattr(metadata, "st_file_attributes", 0)
    identity = (int(metadata.st_dev), int(metadata.st_ino))
    if (
        stat.S_ISLNK(metadata.st_mode)
        or attributes & _REPARSE_POINT_ATTRIBUTE
        or not stat.S_ISREG(metadata.st_mode)
        or identity == (0, 0)
    ):
        raise KlingError("下载临时文件类型或身份无效")
    return identity


def _verify_file_identity(path: str | Path, expected_identity: tuple[int, int]) -> None:
    if _file_identity(path) != expected_identity:
        raise KlingError("下载临时文件身份发生变化，已停止发布")


def _safe_unlink_owned_temp(
    path: str | Path,
    expected_identity: tuple[int, int],
    snapshot: _DirectorySnapshot | None = None,
) -> bool:
    try:
        if snapshot is not None:
            _verify_directory_snapshot(snapshot)
        _verify_file_identity(path, expected_identity)
        Path(path).unlink()
    except (KlingError, OSError):
        return False
    return True
