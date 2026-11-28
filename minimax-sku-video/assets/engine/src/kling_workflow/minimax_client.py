"""Fixed-route Aimaxhug MiniMax H3 transport with isolated, safe responses."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
import httpx
import re
from urllib.parse import quote

from .client import KlingClient
from .seedance_client import _quiet_transport_logs


# 2026-11-12 起按主管当前规则固定使用 .cloud。2026-11-11 的 .com 调用、响应与
# creation_unknown 记录均属于历史证据，不因本默认值切换而改写或恢复创建额度。
API_BASE = "https://apis.aimaxhug.cloud"
VIDEO_ROUTE = "/v1/videos"
# 2026-11-13 17:06 QF95451061 v001 在 read=180 s 上抛 read_timeout → creation_unknown
# （请求已发出、没拿到 task_id，无法续查）。同一时段网关连一个假 ID 查询都要 5–8 s，
# 说明是上游排队慢而不是请求本身有问题。read 放宽到 420 s：只改本地等待上限，
# 不改任何请求字节、不改模型/时长/比例，审批绑定的 request_body_sha256 仍然有效。
_TIMEOUT = httpx.Timeout(connect=30.0, read=420.0, write=60.0, pool=30.0)
_TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SAFE_FAILURE_STAGES = frozenset({"credential_lookup", "serialize", "request_build", "send"})


def _transport_failure_category(error: BaseException) -> str:
    categories = (
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ConnectError, "connect_error"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.ProtocolError, "protocol_error"),
    )
    for error_type, category in categories:
        if isinstance(error, error_type):
            return category
    return "other_transport_error"


class MiniMaxClientError(RuntimeError):
    """Fixed-category transport failures; never carries provider text or secrets."""

    def __init__(self, kind: str, http_status: int | None = None, *, stage: str | None = None) -> None:
        self.kind = kind
        self.http_status = http_status
        self.stage = stage if type(stage) is str and stage in _SAFE_FAILURE_STAGES else None
        super().__init__(kind)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class MiniMaxClient:
    """Only POST /v1/videos and GET /v1/videos/{full_model_key}/{task_id}."""

    def __init__(self, *, api_key_provider: Callable[[], str],
                 transport: httpx.BaseTransport | None = None,
                 downloader=None) -> None:
        if not callable(api_key_provider):
            raise MiniMaxClientError("credential_unavailable")
        if transport is not None and not isinstance(transport, httpx.BaseTransport):
            raise MiniMaxClientError("invalid_transport")
        self._api_key_provider = api_key_provider
        self._cached_api_key: str | None = None
        self._http = httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False,
            timeout=_TIMEOUT, auth=None, headers=None, cookies=None,
        )
        self._downloader = downloader

    def _key(self) -> str:
        if self._cached_api_key is not None:
            return self._cached_api_key
        try:
            value = self._api_key_provider()
        except Exception:
            raise MiniMaxClientError("credential_unavailable", stage="credential_lookup") from None
        if not isinstance(value, str) or not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise MiniMaxClientError("credential_unavailable", stage="credential_lookup")
        self._cached_api_key = value
        return value

    def prepare_credentials(self) -> None:
        self._key()

    @staticmethod
    def _payload(response: httpx.Response) -> object:
        try:
            value = response.json(object_pairs_hook=_unique_json)
        except Exception:
            return {}
        return value if isinstance(value, (dict, list)) else {}

    def post_video(self, body: Mapping[str, object]) -> tuple[int, object]:
        key = self._key()
        try:
            raw_body = json.dumps(dict(body), ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False).encode("utf-8")
        except Exception:
            raise MiniMaxClientError("invalid_request_body", stage="serialize") from None
        try:
            request = httpx.Request(
                "POST", API_BASE + VIDEO_ROUTE,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                content=raw_body, extensions={"timeout": _TIMEOUT.as_dict()},
            )
        except Exception:
            raise MiniMaxClientError("request_build_error", stage="request_build") from None
        try:
            with _quiet_transport_logs():
                response = self._http.send(request, auth=None, follow_redirects=False)
        except Exception as exc:
            raise MiniMaxClientError(_transport_failure_category(exc), stage="send") from None
        try:
            return response.status_code, self._payload(response)
        finally:
            response.close()
            self._http.cookies.clear()

    def get_task(self, model: str, task_id: str) -> tuple[int, object]:
        if (not isinstance(model, str) or not re.fullmatch(r"minimax-h3(?:-pro)?-(?:768p|2k)", model)
                or not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)):
            raise MiniMaxClientError("invalid_query")
        key = self._key()
        endpoint = f"{API_BASE}{VIDEO_ROUTE}/{model}/{quote(task_id, safe='')}"
        try:
            request = httpx.Request(
                "GET", endpoint, headers={"Authorization": f"Bearer {key}"},
                extensions={"timeout": _TIMEOUT.as_dict()},
            )
            with _quiet_transport_logs():
                response = self._http.send(request, auth=None, follow_redirects=False)
        except Exception:
            raise MiniMaxClientError("query_transport_error") from None
        try:
            return response.status_code, self._payload(response)
        finally:
            response.close()
            self._http.cookies.clear()

    def download_candidate(self, url: str, destination, *, allowed_root):
        downloader = self._downloader
        owned = False
        if downloader is None:
            try:
                downloader = KlingClient(api_key_provider=lambda: "unused-for-public-download")
                owned = True
            except Exception:
                raise MiniMaxClientError("download_unavailable") from None
        try:
            return downloader.download(url, destination, allowed_root=allowed_root)
        except Exception:
            raise MiniMaxClientError("download_error") from None
        finally:
            if owned:
                try:
                    downloader.close()
                except Exception:
                    pass

    def close(self) -> None:
        self._cached_api_key = None
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
