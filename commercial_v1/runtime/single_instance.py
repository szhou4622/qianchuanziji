"""Windows 用户范围单实例锁。

正式运行边界为 Windows。非 Windows 分支只为自动化测试提供兼容行为。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes
from dataclasses import dataclass

ERROR_ALREADY_EXISTS = 183
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


def _windows_user_sid() -> str:
    """仅使用 Windows 原生 API 获取当前进程用户 SID，不依赖 pywin32。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        required = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(required))
        if required.value <= 0:
            raise ctypes.WinError(ctypes.get_last_error())

        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_USER_CLASS,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.User.Sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = str(sid_text.value or "").strip()
            if not value:
                raise RuntimeError("Windows returned an empty user SID")
            return value
        finally:
            if sid_text:
                kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
    finally:
        kernel32.CloseHandle(token)


def current_user_identity() -> str:
    """返回稳定的当前用户身份；正式 Windows 环境严格使用 SID。"""
    if os.name == "nt":
        return _windows_user_sid()
    # 非 Windows 只用于开发和 CI，不是正式单实例安全边界。
    return f"{os.getenv('USERDOMAIN', '')}\\{os.getenv('USER') or os.getenv('USERNAME', '')}"


def mutex_name(identity: str | None = None) -> str:
    """生成不包含用户名/SID明文的用户级全局 Mutex 名。"""
    raw_identity = identity if identity is not None else current_user_identity()
    digest = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()[:24]
    return f"Global\\QCSCKP-commercial-v1-{digest}"


@dataclass
class GlobalUserMutex:
    """同一 Windows 用户只允许一个商业版 Runtime。"""

    name: str | None = None

    def __post_init__(self) -> None:
        self.name = self.name or mutex_name()
        self._handle: int | None = None
        self.already_running = False

    def acquire(self) -> bool:
        if self._handle is not None:
            return not self.already_running

        if os.name != "nt":
            self.already_running = False
            self._handle = -1
            return True

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        self._handle = int(handle)
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        return not self.already_running

    def close(self) -> None:
        if self._handle is None:
            return
        if os.name == "nt" and self._handle != -1:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle(wintypes.HANDLE(self._handle))
        self._handle = None
        self.already_running = False

    def __enter__(self) -> "GlobalUserMutex":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
