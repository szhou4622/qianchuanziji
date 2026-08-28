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


def current_user_identity() -> str:
    """返回稳定的当前 Windows 用户身份。"""
    if os.name == "nt":
        try:
            import win32api  # type: ignore[import-not-found]
            import win32security  # type: ignore[import-not-found]

            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
            )
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            return win32security.ConvertSidToStringSid(sid)
        except Exception:
            pass
    return f"{os.getenv('USERDOMAIN', '')}\\{os.getenv('USERNAME', '')}"


def mutex_name(identity: str | None = None) -> str:
    """生成不包含用户名明文的用户级全局 Mutex 名。"""
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
            ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None
        self.already_running = False

    def __enter__(self) -> "GlobalUserMutex":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
