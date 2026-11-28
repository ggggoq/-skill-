from __future__ import annotations

import asyncio
import getpass
import warnings
from typing import Protocol

import keyring


SERVICE = "可灵图生视频"
API_KEY_USER = "api-key"
STATE_USER = "credential-state"
_INCOMPLETE_MARKER = "\u241eKLING_WORKFLOW_API_KEY_INCOMPLETE\u241e"


class _Keyring(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class CredentialError(RuntimeError):
    """A credential setup or read error that never contains a credential value."""


class CredentialStore:
    def __init__(self, backend: _Keyring | None = None) -> None:
        self._backend = keyring if backend is None else backend

    def status(self) -> dict[str, str]:
        try:
            marker = self._backend.get_password(SERVICE, STATE_USER)
            if marker:
                return {API_KEY_USER: "ABSENT"}
            api_key = self._backend.get_password(SERVICE, API_KEY_USER)
        except Exception:
            raise CredentialError("无法读取 Windows 凭据管理器状态") from None
        if api_key == _INCOMPLETE_MARKER:
            return {API_KEY_USER: "ABSENT"}
        return {API_KEY_USER: "PRESENT" if api_key else "ABSENT"}

    def read(self) -> str:
        try:
            marker = self._backend.get_password(SERVICE, STATE_USER)
            if marker:
                raise CredentialError("可灵凭据状态不完整，请重新运行“设置可灵凭据”")
            api_key = self._backend.get_password(SERVICE, API_KEY_USER)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("无法读取 Windows 凭据管理器") from None
        if api_key == _INCOMPLETE_MARKER:
            raise CredentialError("可灵凭据状态不完整，请重新运行“设置可灵凭据”")
        if not api_key:
            raise CredentialError("可灵 API Key 未配置，请先运行“设置可灵凭据”")
        return api_key

    def set_interactive(self) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                api_key = getpass.getpass("请输入可灵 API Key：")
        except (
            getpass.GetPassWarning,
            EOFError,
            KeyboardInterrupt,
            asyncio.CancelledError,
        ):
            raise CredentialError("无法安全读取凭据输入；请在支持隐藏输入的终端中重试") from None
        if not api_key:
            raise CredentialError("API Key 不能为空")

        try:
            self._backend.set_password(SERVICE, STATE_USER, _INCOMPLETE_MARKER)
            if self._backend.get_password(SERVICE, STATE_USER) != _INCOMPLETE_MARKER:
                raise CredentialError("无法确认凭据写入状态，未进行保存")
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise CredentialError("无法确认凭据写入状态，未进行保存") from None

        try:
            self._backend.set_password(SERVICE, API_KEY_USER, api_key)
            if self._backend.get_password(SERVICE, API_KEY_USER) != api_key:
                raise CredentialError("无法确认可灵 API Key 保存结果")
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise CredentialError("可灵凭据保存失败；状态已锁定，请重新设置") from None

        try:
            if self._backend.get_password(SERVICE, API_KEY_USER) != api_key:
                raise CredentialError("无法确认可灵 API Key，状态已锁定")
            self._backend.delete_password(SERVICE, STATE_USER)
            if self._backend.get_password(SERVICE, STATE_USER) is not None:
                raise CredentialError("无法确认凭据状态提交，状态仍已锁定")
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise CredentialError("无法确认凭据状态提交；请重新设置") from None
