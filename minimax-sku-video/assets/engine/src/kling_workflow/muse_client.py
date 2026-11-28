"""Fixed-route Muse Video Pro transport with safe, non-compressing media upload."""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx


API_BASE = "https://apis.aimaxhug.cloud"
VIDEO_ROUTE = "/v1/videos"
MODEL_ROUTE = "/v1/videos/models"
UPLOAD_URL = "https://base-api.aimaxhug.com/api/v2/upload/file"
MODEL = "muse-video-pro"
_TIMEOUT = httpx.Timeout(connect=20.0, read=180.0, write=60.0, pool=20.0)


class MuseClientError(RuntimeError):
    """Transport errors expose only a fixed category and optional HTTP status."""

    def __init__(self, kind: str, http_status: int | None = None) -> None:
        self.kind = kind
        self.http_status = http_status
        super().__init__(kind)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_https_url(value: object) -> bool:
    if not isinstance(value, str) or not value or any(ord(char) < 32 or char.isspace() for char in value):
        return False
    if "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "%" in parsed.netloc):
            return False
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global and not address.is_multicast and not address.is_unspecified
        except ValueError:
            host = host.rstrip(".").lower()
            if host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".invalid", ".test", ".example")):
                return False
            labels = host.split(".")
            return (len(host) <= 253 and len(labels) >= 2
                    and bool(re.fullmatch(r"[a-z]{2,63}", labels[-1]))
                    and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels))
    except (ValueError, UnicodeError):
        return False


class MuseClient:
    """A no-cookie, no-environment-proxy HTTP client for fixed Muse routes."""

    def __init__(self, api_key_provider: Callable[[], str], *, transport: httpx.BaseTransport | None = None,
                 downloader=None) -> None:
        if not callable(api_key_provider):
            raise MuseClientError("credential_unavailable")
        if transport is not None and not isinstance(transport, httpx.BaseTransport):
            raise MuseClientError("invalid_transport")
        self._api_key_provider = api_key_provider
        self._cached_api_key: str | None = None
        self._http = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=_TIMEOUT,
            auth=None,
            headers=None,
            cookies=None,
        )
        self._downloader = downloader

    def _key(self) -> str:
        if self._cached_api_key is not None:
            return self._cached_api_key
        try:
            value = self._api_key_provider()
        except Exception:
            raise MuseClientError("credential_unavailable") from None
        if not isinstance(value, str) or not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise MuseClientError("credential_unavailable")
        self._cached_api_key = value
        return value

    def prepare_credentials(self) -> None:
        """Read the key into process memory before durable create intent is written."""
        self._key()

    @staticmethod
    def _payload(response: httpx.Response) -> object:
        try:
            value = response.json(object_pairs_hook=_unique_json)
        except Exception:
            return {}
        return value if isinstance(value, (dict, list)) else {}

    def get_model_catalog(self) -> tuple[int, object]:
        try:
            response = self._http.get(API_BASE + MODEL_ROUTE, follow_redirects=False)
        except Exception:
            raise MuseClientError("catalog_transport_error") from None
        try:
            return response.status_code, self._payload(response)
        finally:
            response.close()

    def post_video(self, body: Mapping[str, object]) -> tuple[int, object]:
        key = self._key()
        try:
            response = self._http.post(
                API_BASE + VIDEO_ROUTE,
                headers={"Authorization": f"Bearer {key}"},
                json=dict(body),
                follow_redirects=False,
            )
        except Exception:
            raise MuseClientError("transport_unknown") from None
        try:
            return response.status_code, self._payload(response)
        finally:
            response.close()

    def get_task(self, task_id: str) -> tuple[int, object]:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
            raise MuseClientError("invalid_task_id")
        key = self._key()
        endpoint = f"{API_BASE}{VIDEO_ROUTE}/{MODEL}/{quote(task_id, safe='')}"
        try:
            response = self._http.get(
                endpoint,
                headers={"Authorization": f"Bearer {key}"},
                follow_redirects=False,
            )
        except Exception:
            raise MuseClientError("transport_error") from None
        try:
            return response.status_code, self._payload(response)
        finally:
            response.close()

    def upload_video(self, content: bytes, filename: str, mime_type: str) -> str:
        if (not isinstance(content, bytes) or not content or not isinstance(filename, str)
                or not filename or Path(filename).name != filename or not isinstance(mime_type, str)
                or not mime_type.startswith("video/")):
            raise MuseClientError("invalid_upload")
        key = self._key()
        try:
            response = self._http.post(
                UPLOAD_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, content, mime_type)},
                follow_redirects=False,
            )
        except Exception:
            # The remote upload may have completed; never label this as a timeout
            # or authorize an automatic second upload.
            raise MuseClientError("upload_unknown") from None
        try:
            if not 200 <= response.status_code < 300:
                kind = "upload_unknown" if response.status_code == 408 or response.status_code >= 500 else "upload_http_error"
                raise MuseClientError(kind, response.status_code)
            payload = self._payload(response)
            if not isinstance(payload, dict):
                raise MuseClientError("upload_unknown", response.status_code)
            scopes = [payload]
            if isinstance(payload.get("data"), dict):
                scopes.append(payload["data"])
            urls = [scope.get("tmp_url") for scope in scopes if "tmp_url" in scope]
            if (not urls or any(not _safe_https_url(url) for url in urls)
                    or len(set(urls)) != 1):
                raise MuseClientError("upload_unknown", response.status_code)
            return urls[0]
        finally:
            response.close()

    def download_candidate(self, url: str, destination: str | Path, *, allowed_root: str | Path) -> Path:
        if not _safe_https_url(url):
            raise MuseClientError("invalid_media_url")
        downloader = self._downloader
        owned = False
        if downloader is None:
            from .client import KlingClient

            # Reuse the project's existing public-IP-pinned, exclusive download
            # implementation. Its API-key callback is not invoked for downloads.
            downloader = KlingClient(api_key_provider=self._api_key_provider)
            owned = True
        try:
            return downloader.download(url, destination, allowed_root=allowed_root)
        except Exception:
            raise MuseClientError("download_error") from None
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
