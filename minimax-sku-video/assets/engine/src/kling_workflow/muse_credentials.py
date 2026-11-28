"""Muse-first credential access with a read-only Aimaxhug fallback."""
from __future__ import annotations

import asyncio
import warnings
from typing import Protocol

import keyring


MUSE_SERVICE = "Aimaxhug Muse视频生成"
API_KEY_USER = "api-key"
STATE_USER = "credential-state"
_INCOMPLETE_MARKER = "\u241eMUSE_API_KEY_INCOMPLETE\u241e"


class _Keyring(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class MuseCredentialError(RuntimeError):
    """Credential errors contain no secret values or backend exception text."""


def _valid_key(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(32 < ord(char) < 127 for char in value)


def _masked_dialog() -> str | None:
    """Open a local masked input window; never fall back to console echo."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        return simpledialog.askstring(
            "设置 Muse 凭据",
            "请输入 Aimaxhug API Key：",
            parent=root,
            show="*",
        )
    finally:
        root.destroy()


class MuseCredentialStore:
    """Use a Muse-only key first and never mutate the existing Aimaxhug key."""

    def __init__(self, backend: _Keyring | None = None, fallback=None) -> None:
        self._backend = keyring if backend is None else backend
        if fallback is None:
            from .seedance_credentials import SeedanceCredentialStore

            fallback = SeedanceCredentialStore()
        self._fallback = fallback

    def _own_value(self) -> str | None:
        try:
            marker = self._backend.get_password(MUSE_SERVICE, STATE_USER)
            value = None if marker else self._backend.get_password(MUSE_SERVICE, API_KEY_USER)
        except Exception:
            raise MuseCredentialError("无法读取 Muse 凭据状态") from None
        if marker:
            raise MuseCredentialError("Muse 凭据写入状态不完整，请重新设置")
        return value if _valid_key(value) else None

    def status(self) -> dict[str, str]:
        own = self._own_value()
        if own:
            return {API_KEY_USER: "PRESENT", "source": "muse"}
        try:
            state = self._fallback.status().get(API_KEY_USER)
        except Exception:
            raise MuseCredentialError("无法读取 Aimaxhug 凭据状态") from None
        return {API_KEY_USER: "PRESENT" if state == "PRESENT" else "ABSENT",
                "source": "aimaxhug_fallback" if state == "PRESENT" else "none"}

    def read(self) -> str:
        own = self._own_value()
        if own:
            return own
        try:
            value = self._fallback.read()
        except Exception:
            raise MuseCredentialError("Muse 或现有 Aimaxhug 凭据未配置") from None
        if not _valid_key(value):
            raise MuseCredentialError("Muse 或现有 Aimaxhug 凭据未配置")
        return value

    def set_interactive(self, prompt_dialog=None) -> None:
        dialog = _masked_dialog if prompt_dialog is None else prompt_dialog
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                value = dialog()
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            raise MuseCredentialError("无法打开 Muse 凭据输入窗口") from None
        if value is None or value == "":
            raise MuseCredentialError("Muse 凭据输入已取消")
        self.set_secret(value)

    def set_secret(self, value: str) -> None:
        """Write only the Muse namespace using a fail-closed transaction marker."""
        if not _valid_key(value):
            raise MuseCredentialError("Muse API Key 不能为空或包含无效字符")
        try:
            self._backend.set_password(MUSE_SERVICE, STATE_USER, _INCOMPLETE_MARKER)
            if self._backend.get_password(MUSE_SERVICE, STATE_USER) != _INCOMPLETE_MARKER:
                raise MuseCredentialError("无法确认 Muse 凭据写入状态")
        except Exception:
            raise MuseCredentialError("无法确认 Muse 凭据写入状态，未保存") from None
        try:
            self._backend.set_password(MUSE_SERVICE, API_KEY_USER, value)
            if self._backend.get_password(MUSE_SERVICE, API_KEY_USER) != value:
                raise MuseCredentialError("无法确认 Muse API Key 保存结果")
        except Exception:
            raise MuseCredentialError("Muse 凭据保存失败；状态已锁定，请重新设置") from None
        try:
            if self._backend.get_password(MUSE_SERVICE, API_KEY_USER) != value:
                raise MuseCredentialError("无法确认 Muse API Key")
            self._backend.delete_password(MUSE_SERVICE, STATE_USER)
            if self._backend.get_password(MUSE_SERVICE, STATE_USER) is not None:
                raise MuseCredentialError("Muse 凭据状态仍已锁定")
        except Exception:
            raise MuseCredentialError("无法确认 Muse 凭据状态提交；请重新设置") from None
