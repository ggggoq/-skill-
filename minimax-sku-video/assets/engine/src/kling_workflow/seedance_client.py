"""Independent Aimaxhug Seedance HTTP adapter; no Kling API operations.

Only the create/query routes supplied by the user are treated as a contract.
Result aliases below are conservative, UNVERIFIED compatibility handling, not
claims about the vendor schema. Missing results or watermark proof deliberately
leave download_url unset for the workflow's existing delivery gate.

validate_payload is entirely local: public media hosts are checked syntactically;
it cannot prove a domain's DNS resolution or remote reachability. Actual downloads
reuse KlingClient's HTTPS, public-IP pinning and exclusive-file implementation.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import ipaddress
import logging
import re
from urllib.parse import urlsplit

import httpx

from .seedance_models import SEEDANCE_MODELS, supports_resolution
from .client import (
    KlingClient,
    KlingError,
    NetworkUnknownOutcome,
    RemoteTask,
    RemoteVideo,
    _DEFAULT_MAX_DOWNLOAD_BYTES,
    _HTTP_LOGGER_NAMES,
    _HTTP_LOG_FILTER_LOCK,
    _TIMEOUT,
)


BASE_URL = "https://apis.aimaxhug.cloud"
VIDEOS = "/v1/videos"
MODELS = frozenset(SEEDANCE_MODELS)
_CREATE_TIMEOUT = httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0)
_COMMON_FIELDS = frozenset(
    {"model", "prompt", "duration", "seconds", "aspect_ratio", "ratio", "resolution"}
)
_CHANNEL2_FIELDS = _COMMON_FIELDS | frozenset(
    {"images", "referenceVideos", "referenceAudios"}
)
_STANDARD_FIELDS = _COMMON_FIELDS | frozenset(
    {"image_url", "reference_image_urls", "reference_videos", "reference_audios"}
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}\Z")
_CHANNEL2_STATUS = {
    "submitted": "submitted",
    "processing": "processing",
    "completed": "succeed",
    "failed": "failed",
}
_STANDARD_STATUS = {
    "queued": "submitted",
    "processing": "processing",
    "SUCCESS": "succeed",
    "failed": "failed",
    "FAILED": "failed",
}
_QUIET_HTTP: ContextVar[bool] = ContextVar("seedance_quiet_http", default=False)


class SeedanceUnknownOutcome(NetworkUnknownOutcome):
    """Compatible exception type, without the old external-ID recovery promise."""

    def __init__(self) -> None:
        KlingError.__init__(
            self,
            "Aimaxhug Seedance 创建请求结果未知，禁止自动重试；请在平台人工核查，取得 task_id 后再查询",
            kind="creation_unknown",
        )


def _invalid_payload() -> KlingError:
    # Never interpolate user-supplied fields, URLs, prompts, or image bytes.
    return KlingError("Aimaxhug Seedance 请求参数无效或包含未支持字段", kind="invalid_payload")


def _invalid_response() -> KlingError:
    return KlingError("Aimaxhug Seedance 返回字段无效、冲突或缺少有效状态", kind="invalid_response")


def _public_url(value: object, *, https_only: bool = False) -> bool:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    if "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme not in ({"https"} if https_only else {"http", "https"})
                or not host or parsed.username is not None or parsed.password is not None
                or parsed.fragment or (port is not None and not 1 <= port <= 65535)
                or "%" in parsed.netloc):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.rstrip(".").lower()
            if host.endswith((".localhost", ".local", ".internal", ".lan", ".home",
                              ".invalid", ".test", ".example", ".onion")):
                return False
            labels = host.split(".")
            # Reject single-label and numeric/legacy IPv4 encodings without DNS.
            return (len(host) <= 253 and len(labels) >= 2
                    and bool(re.fullmatch(r"[a-z]{2,63}", labels[-1]))
                    and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels))
        return address.is_global and not address.is_multicast and not address.is_unspecified
    except (ValueError, UnicodeError):
        return False


def _image(value: object) -> bool:
    if _public_url(value):
        return True
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/]+={0,2})", value)
    if not match:
        return False
    try:
        return bool(base64.b64decode(match[1], validate=True))
    except (ValueError, binascii.Error):
        return False


def validate_payload(payload: Mapping[str, object]) -> None:
    """Validate the explicit HTTP input contract, without I/O or mutation.

    Accept duration/seconds and aspect_ratio/ratio aliases; simultaneous values
    must agree. Resolution is validated against the selected model's documented
    capability (including 4k only where supported). Unlike the raw API's optional
    fields/defaults, this adapter deliberately requires explicit
    duration (or seconds), ratio, and resolution so reviewed generation settings
    cannot silently change. Callers must materialize any approved defaults first.
    create_video normalizes seconds to duration without changing the input.
    No business policy (approval, reference duration, vertical-only) is replaced here.
"""
    if not isinstance(payload, Mapping):
        raise _invalid_payload()
    model = payload.get("model")
    if not isinstance(model, str) or model not in MODELS:
        raise _invalid_payload()
    fields = _STANDARD_FIELDS if model.startswith("Seedance-") else _CHANNEL2_FIELDS
    if set(payload) - fields:
        raise _invalid_payload()
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _invalid_payload()
    durations = [payload[key] for key in ("duration", "seconds") if key in payload]
    if not durations or any(type(value) is not int or not 4 <= value <= 15 for value in durations):
        raise _invalid_payload()
    if len(set(durations)) != 1:
        raise _invalid_payload()
    ratios = [payload[key] for key in ("aspect_ratio", "ratio") if key in payload]
    if not ratios or any(not isinstance(r, str) or r not in {"16:9", "9:16", "1:1"} for r in ratios):
        raise _invalid_payload()
    if len(set(ratios)) != 1:
        raise _invalid_payload()
    resolution = payload.get("resolution")
    if not supports_resolution(model, resolution):
        raise _invalid_payload()
    media_fields = (
        (
            ("reference_image_urls", 30, _image),
            ("reference_videos", 10, _public_url),
            ("reference_audios", 10, _public_url),
        )
        if model.startswith("Seedance-")
        else (
            ("images", 9, _image),
            ("referenceVideos", 3, _public_url),
            ("referenceAudios", 3, _public_url),
        )
    )
    if "image_url" in payload and not _image(payload["image_url"]):
        raise _invalid_payload()
    for field, limit, validator in media_fields:
        if field not in payload:
            continue
        items = payload[field]
        if not isinstance(items, list) or len(items) > limit:
            raise _invalid_payload()
        if any(not validator(value) for value in items):
            raise _invalid_payload()


class _QuietTransport(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _QUIET_HTTP.get()


@contextmanager
def _quiet_transport_logs():
    """Suppress transport details only in this request context, including traces."""
    token = _QUIET_HTTP.set(True)
    log_filter = _QuietTransport()
    loggers = [logging.getLogger(name) for name in (*_HTTP_LOGGER_NAMES, "httpcore")]
    with _HTTP_LOG_FILTER_LOCK:
        for logger in loggers:
            logger.addFilter(log_filter)
    try:
        yield
    finally:
        with _HTTP_LOG_FILTER_LOCK:
            for logger in loggers:
                logger.removeFilter(log_filter)
        _QUIET_HTTP.reset(token)


def _identifier(value: object, sensitive: Iterable[str] = ()) -> str:
    if (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
            or any(secret and secret in value for secret in sensitive)):
        raise _invalid_response()
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid_response()
        result[key] = value
    return result


def _layers(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Only top/result/data/data.result are provisional supported result scopes."""
    layers = [payload]
    for field in ("result", "data"):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, Mapping):
                raise _invalid_response()
            layers.append(value)
    data = payload.get("data")
    if isinstance(data, Mapping) and data.get("result") is not None:
        if not isinstance(data["result"], Mapping):
            raise _invalid_response()
        layers.append(data["result"])
    return layers


def _parse_response(
    payload: object,
    *,
    creating: bool,
    task_id: str | None,
    sensitive: set[str],
    model: str,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise _invalid_response()
    layers = _layers(payload)
    ids = [_identifier(layer[field], sensitive) for layer in layers for field in ("task_id", "id") if field in layer]
    if len(set(ids)) > 1 or (task_id is not None and ids and ids[0] != task_id):
        raise _invalid_response()
    if task_id is not None:
        _identifier(task_id, sensitive)
    identifier = ids[0] if ids else task_id
    if identifier is None:
        raise _invalid_response()
    status_map = _STANDARD_STATUS if model.startswith("Seedance-") else _CHANNEL2_STATUS
    statuses = [layer["status"] for layer in layers if "status" in layer]
    if any(not isinstance(s, str) or s not in status_map for s in statuses):
        raise _invalid_response()
    normalized_statuses = [status_map[status] for status in statuses]
    if len(set(normalized_statuses)) > 1:
        raise _invalid_response()
    if creating:
        # A synchronous completion still has a usable ID. The coordinator must
        # persist it, then GET results separately; it is not an unknown creation.
        return {
            "task_id": identifier,
            "status": normalized_statuses[0] if normalized_statuses else "submitted",
        }
    if not statuses:
        raise _invalid_response()
    status = normalized_statuses[0]
    if status != "succeed":
        return {"task_id": identifier, "status": status}

    urls: list[str] = []
    watermarks: list[bool] = []
    for layer in layers:
        for field in ("urlWithoutWatermark", "url_without_watermark", "video_url", "url"):
            if field in layer:
                value = layer[field]
                if not _public_url(value, https_only=True):
                    raise _invalid_response()
                urls.append(value)
                if field in {"urlWithoutWatermark", "url_without_watermark"}:
                    watermarks.append(False)
        if "watermark_enabled" in layer:
            watermarks.append(layer["watermark_enabled"])
        if "watermark_info" in layer:
            info = layer["watermark_info"]
            if not isinstance(info, Mapping):
                raise _invalid_response()
            if "enabled" in info:
                watermarks.append(info["enabled"])
    if (any(type(flag) is not bool for flag in watermarks)
            or len(set(watermarks)) > 1 or len(set(urls)) > 1):
        raise _invalid_response()
    watermark = watermarks[0] if watermarks else None
    return {"task_id": identifier, "status": status,
            "download_url": urls[0] if urls and watermark is False else None,
            "watermark_enabled": watermark}


class SeedanceClient(KlingClient):
    def __init__(
        self, *, model: str = "Seedance-2.0-pro", api_key_provider: Callable[[], str],
        api_transport: httpx.BaseTransport | None = None,
        download_transport: httpx.BaseTransport | None = None,
        host_resolver: Callable[[str], Iterable[str]] | None = None,
        max_download_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES,
    ) -> None:
        if not isinstance(model, str) or model not in MODELS:
            raise _invalid_payload()
        # Construct the existing isolated, trust_env=False clients without I/O.
        # All inherited API entry points are overridden below; only download/close
        # are reused. Never pass the new origin to the Kling constructor.
        super().__init__(api_key_provider=api_key_provider, api_transport=api_transport,
                         download_transport=download_transport, host_resolver=host_resolver,
                         max_download_bytes=max_download_bytes)
        self.base_url = BASE_URL
        self.model = model

    def create_video(self, payload: Mapping[str, object]) -> RemoteTask:
        validate_payload(payload)
        if payload["model"] != self.model:
            raise _invalid_payload()
        body = {key: list(value) if isinstance(value, list) else value for key, value in payload.items()}
        if "seconds" in body:
            body["duration"] = body.pop("seconds")
        return RemoteTask(**self._request("POST", VIDEOS, json_body=body))

    def query_video(self, task_id: str | None = None, external_task_id: str | None = None) -> RemoteVideo:
        if external_task_id is not None:
            raise KlingError("Aimaxhug Seedance 只支持 task_id 查询", kind="unsupported")
        try:
            identifier = _identifier(task_id)
        except KlingError:
            raise KlingError("Aimaxhug Seedance 查询需要有效 task_id", kind="invalid_payload") from None
        return RemoteVideo(**self._request("GET", f"{VIDEOS}/{self.model}/{identifier}"))

    def check_connection(self):
        raise KlingError("Aimaxhug Seedance 尚未提供连接检查合同", kind="unsupported")

    def create_element(self, prepared, sku, external_task_id):
        raise KlingError("Aimaxhug Seedance 不支持元素创建", kind="unsupported")

    def query_element(self, task_id=None, external_task_id=None):
        raise KlingError("Aimaxhug Seedance 不支持元素查询", kind="unsupported")

    def query_video_by_external_id(self, external_task_id):
        raise KlingError("Aimaxhug Seedance 只支持 task_id 查询", kind="unsupported")

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> dict[str, object]:
        creating = method == "POST" and path == VIDEOS
        prefix = f"{VIDEOS}/{self.model}/"
        task_id = None
        if not isinstance(self.model, str) or self.model not in MODELS or params is not None:
            raise _invalid_payload()
        if creating:
            validate_payload(json_body)
            if json_body["model"] != self.model:
                raise _invalid_payload()
        elif method == "GET" and isinstance(path, str) and path.startswith(prefix) and json_body is None:
            task_id = _identifier(path[len(prefix):])
        else:
            raise KlingError("Aimaxhug Seedance 未支持此 API 操作", kind="unsupported")
        try:
            api_key = self._api_key_provider()
        except Exception:
            raise KlingError("无法取得 Aimaxhug API Key", kind="credential") from None
        if not isinstance(api_key, str) or not api_key or any(ord(c) <= 32 or ord(c) >= 127 for c in api_key):
            raise KlingError("Aimaxhug API Key 无效", kind="credential")
        sensitive = {api_key}
        if json_body:
            sensitive.add(json_body["prompt"])
            if isinstance(json_body.get("image_url"), str):
                sensitive.add(json_body["image_url"])
            for field in (
                "images",
                "referenceVideos",
                "referenceAudios",
                "reference_image_urls",
                "reference_videos",
                "reference_audios",
            ):
                for value in json_body.get(field, []):
                    sensitive.add(value)
                    if value.startswith("data:"):
                        sensitive.add(value.partition(",")[2])
        try:
            # A fresh Request bypasses client cookie/header/auth state. The origin
            # is a constant, not self.base_url, even if a caller mutates it.
            request_timeout = _CREATE_TIMEOUT if creating else _TIMEOUT
            request = httpx.Request(method, BASE_URL + path,
                                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                    json=json_body, extensions={"timeout": request_timeout.as_dict()})
            with _quiet_transport_logs():
                try:
                    response = self._api_client.send(request, auth=None, follow_redirects=False)
                except Exception:
                    # A low-level transport can raise non-httpx exceptions too;
                    # none of their text or request objects may escape.
                    if creating:
                        raise SeedanceUnknownOutcome() from None
                    raise KlingError("Aimaxhug Seedance 网络请求失败", kind="network") from None
                try:
                    code = response.status_code
                    if not 200 <= code < 300:
                        if creating and (code == 408 or code >= 500):
                            raise SeedanceUnknownOutcome()
                        kind = "auth" if code in {401, 403} else "endpoint" if code in {404, 405} else "http"
                        raise KlingError(f"Aimaxhug Seedance 请求失败（HTTP {code}）", kind=kind)
                    raw = response.json(object_pairs_hook=_unique_object)
                    return _parse_response(
                        raw,
                        creating=creating,
                        task_id=task_id,
                        sensitive=sensitive,
                        model=self.model,
                    )
                finally:
                    response.close()
        except NetworkUnknownOutcome:
            raise
        except KlingError as exc:
            if creating and exc.kind == "invalid_response":
                raise SeedanceUnknownOutcome() from None
            raise
        except httpx.HTTPError:
            if creating:
                raise SeedanceUnknownOutcome() from None
            raise KlingError("Aimaxhug Seedance 网络请求失败", kind="network") from None
        except (ValueError, TypeError, RecursionError):
            if creating:
                raise SeedanceUnknownOutcome() from None
            raise _invalid_response() from None
