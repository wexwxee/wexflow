"""Encrypted local credentials for the Lidl candidate portal.

The password is protected with Windows DPAPI.  Only the current Windows user
on this computer can decrypt it; plaintext is never written to JSON.
"""
from __future__ import annotations

import base64
import threading

import config
from json_store import atomic_write_json, read_json


PATH = config.DATA_DIR / "lidl_credentials.json"
_LOCK = threading.RLock()


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    source = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(
        len(data),
        ctypes.cast(source, ctypes.POINTER(ctypes.c_char)),
    )
    blob_out = DATA_BLOB()
    function = (
        ctypes.windll.crypt32.CryptProtectData
        if protect else ctypes.windll.crypt32.CryptUnprotectData
    )
    if not function(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("Windows не смог зашифровать данные Lidl через DPAPI")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _encrypt(password: str) -> str:
    encrypted = _dpapi(password.encode("utf-8"), protect=True)
    return base64.b64encode(encrypted).decode("ascii")


def _decrypt(token: str) -> str:
    encrypted = base64.b64decode(token)
    return _dpapi(encrypted, protect=False).decode("utf-8")


def _read() -> dict:
    data = read_json(PATH, {}, dict)
    # Defensive cleanup if a future/old build ever wrote an unsafe key.
    if "password" in data:
        plain = str(data.pop("password") or "")
        if plain and not data.get("password_enc"):
            data["password_enc"] = _encrypt(plain)
        atomic_write_json(PATH, data, indent=2)
    return data


def save(email: str, password: str) -> None:
    with _LOCK:
        data = _read()
        data["email"] = str(email or "").strip()
        # An empty password in the UI means "keep the encrypted password".
        if password:
            data["password_enc"] = _encrypt(str(password))
        atomic_write_json(PATH, data, indent=2)


def get() -> dict:
    with _LOCK:
        data = _read()
        password = ""
        if data.get("password_enc"):
            try:
                password = _decrypt(str(data["password_enc"]))
            except Exception:  # noqa: BLE001
                password = ""
        return {
            "email": str(data.get("email") or ""),
            "password": password,
        }


def status(default_email: str = "") -> dict:
    with _LOCK:
        data = _read()
        return {
            "email": str(data.get("email") or default_email or ""),
            "has_password": bool(data.get("password_enc")),
        }


def clear() -> None:
    with _LOCK:
        try:
            PATH.unlink()
        except FileNotFoundError:
            pass
