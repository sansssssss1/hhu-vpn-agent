"""Windows DPAPI 凭据保护（纯 ctypes，不加依赖）。

动机：上游 config.ini 明文存密码。能让密码不必明文落盘就尽量别落盘——
DPAPI 用当前 Windows 用户的密钥加密，文件被拷到别的机器/别的用户下都解不开。

非 Windows 平台直接返回 None，调用方回落到 config.ini 明文（行为与上游一致）。
"""

from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

SECRET_FILE = "credentials.dpapi"

_IS_WINDOWS = sys.platform == "win32"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from_bytes(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _bytes_from_blob(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _cryptprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _blob_from_bytes(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "hhu-vpn-agent", None, None, None, 0,
                                  ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"CryptProtectData 失败: {ctypes.GetLastError()}")
    try:
        return _bytes_from_blob(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _cryptunprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _blob_from_bytes(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                    ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"CryptUnprotectData 失败: {ctypes.GetLastError()}")
    try:
        return _bytes_from_blob(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def available() -> bool:
    return _IS_WINDOWS


def secret_path(state_dir: Path) -> Path:
    return Path(state_dir) / SECRET_FILE


def save_secret(state_dir: Path, username: str, password: str) -> Path:
    """把凭据加密落盘。"""
    path = secret_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{username}\n{password}".encode("utf-8")
    if _IS_WINDOWS:
        blob = _cryptprotect(payload)
        path.write_bytes(base64.b64encode(blob))
    else:
        # 非 Windows：退化为 base64（不是加密！只是避免肉眼直读，README 里写明了）
        path.write_bytes(b"plain:" + base64.b64encode(payload))
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return path


def load_secret(state_dir: Path) -> tuple[str, str] | None:
    path = secret_path(state_dir)
    if not path.exists():
        return None
    raw = path.read_bytes()
    try:
        if raw.startswith(b"plain:"):
            payload = base64.b64decode(raw[6:])
        else:
            payload = _cryptunprotect(base64.b64decode(raw))
    except Exception:  # noqa: BLE001
        return None
    text = payload.decode("utf-8", "replace")
    user, _, pwd = text.partition("\n")
    return user, pwd


def clear_secret(state_dir: Path) -> bool:
    path = secret_path(state_dir)
    if path.exists():
        path.unlink()
        return True
    return False


__all__ = ["available", "save_secret", "load_secret", "clear_secret", "secret_path"]
