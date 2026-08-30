"""Windows 当前用户 DPAPI 封装。"""
from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class DPAPIUnavailableError(RuntimeError):
    pass


class DPAPIError(RuntimeError):
    pass


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _require_windows() -> None:
    if os.name != "nt":
        raise DPAPIUnavailableError("Windows DPAPI is only available on Windows")


def _blob_from_bytes(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    _require_windows()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, flags, ctypes.byref(out_blob))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, flags, ctypes.byref(out_blob))
    if not ok:
        raise DPAPIError(f"DPAPI operation failed: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect_bytes(data: bytes) -> bytes:
    return _crypt(data, decrypt=False)


def unprotect_bytes(data: bytes) -> bytes:
    return _crypt(data, decrypt=True)


def protect_text(value: str) -> str:
    encrypted = protect_bytes(value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def unprotect_text(value: str) -> str:
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    return unprotect_bytes(raw).decode("utf-8")
