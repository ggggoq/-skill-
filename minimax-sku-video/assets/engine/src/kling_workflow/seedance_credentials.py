"""Isolated Aimaxhug credentials; never read or migrate the Kling namespace."""
from __future__ import annotations

import asyncio
import getpass
import warnings

import keyring

from .credentials import CredentialError, _Keyring


SERVICE = "Aimaxhug视频生成"
API_KEY_USER = "api-key"
STATE_USER = "credential-state"
_INCOMPLETE_MARKER = "\u241eAIMAXHUG_API_KEY_INCOMPLETE\u241e"


def _valid_key(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(32 < ord(c) < 127 for c in value)


class SeedanceCredentialStore:
    """Same status/read/set_interactive API, with independent transaction state."""

    def __init__(self, backend: _Keyring | None = None) -> None:
        self._backend = keyring if backend is None else backend

    def status(self) -> dict[str, str]:
        try:
            if self._backend.get_password(SERVICE, STATE_USER):
                return {API_KEY_USER: "ABSENT"}
            value = self._backend.get_password(SERVICE, API_KEY_USER)
        except Exception:
            raise CredentialError("无法读取 Aimaxhug 凭据状态") from None
        return {API_KEY_USER: "PRESENT" if _valid_key(value) else "ABSENT"}

    def read(self) -> str:
        try:
            marker = self._backend.get_password(SERVICE, STATE_USER)
            value = None if marker else self._backend.get_password(SERVICE, API_KEY_USER)
        except Exception:
            raise CredentialError("无法读取 Aimaxhug 凭据") from None
        if marker:
            raise CredentialError("Aimaxhug 凭据状态不完整，请重新设置 Aimaxhug 凭据")
        if not _valid_key(value):
            raise CredentialError("Aimaxhug API Key 未配置或无效，请设置 Aimaxhug 凭据")
        return value

    def set_interactive(self) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("请输入 Aimaxhug API Key：")
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise CredentialError("无法安全读取 Aimaxhug 凭据输入；请使用支持隐藏输入的终端") from None
        self.set_secret(value)

    def set_secret(self, value: str) -> None:
        """Persist a previously masked in-memory value using the same safe transaction."""
        if not _valid_key(value):
            raise CredentialError("Aimaxhug API Key 不能为空或包含无效字符")
        try:
            self._backend.set_password(SERVICE, STATE_USER, _INCOMPLETE_MARKER)
            if self._backend.get_password(SERVICE, STATE_USER) != _INCOMPLETE_MARKER:
                raise CredentialError("无法确认 Aimaxhug 凭据写入状态")
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise CredentialError("无法确认 Aimaxhug 凭据写入状态，未保存 API Key") from None
        try:
            self._backend.set_password(SERVICE, API_KEY_USER, value)
            if self._backend.get_password(SERVICE, API_KEY_USER) != value:
                raise CredentialError("无法确认 Aimaxhug API Key 保存结果")
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise CredentialError("Aimaxhug 凭据保存失败；状态已锁定，请重新设置") from None
        try:
            if self._backend.get_password(SERVICE, API_KEY_USER) != value:
                raise CredentialError("无法确认 Aimaxhug API Key")
            self._backend.delete_password(SERVICE, STATE_USER)
            if self._backend.get_password(SERVICE, STATE_USER) is not None:
                raise CredentialError("Aimaxhug 凭据状态仍已锁定")
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise CredentialError("无法确认 Aimaxhug 凭据状态提交；请重新设置") from None
