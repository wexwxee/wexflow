"""Хранилище привязки Telegram: токен бота + chat_id пользователя + @username.

Токен — это секрет (как пароль), поэтому шифруется Windows DPAPI тем же
способом, что и пароль Salling: расшифровать его сможет только текущий
пользователь Windows на этой машине. В файле telegram.json токен лежит
нечитаемой base64-строкой.
"""
import json

import config
from credentials_store import _decrypt, _encrypt  # переиспользуем DPAPI

PATH = config.DATA_DIR / "telegram.json"


def _read() -> dict:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _write(data: dict):
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_token(token: str, username: str = "") -> None:
    d = _read()
    if token:
        d["token_enc"] = _encrypt(token.strip())
    if username:
        d["username"] = username
    _write(d)


def set_chat(chat_id, name: str = "") -> None:
    d = _read()
    d["chat_id"] = chat_id
    if name:
        d["name"] = name
    _write(d)


def get_token() -> str:
    d = _read()
    if d.get("token_enc"):
        try:
            return _decrypt(d["token_enc"])
        except Exception:  # noqa: BLE001
            return ""
    return ""


def get_chat():
    return _read().get("chat_id")


def status() -> dict:
    d = _read()
    return {
        "has_token": bool(d.get("token_enc")),
        "username": d.get("username", ""),
        "linked": bool(d.get("chat_id")),
        "name": d.get("name", ""),
    }


def is_ready() -> bool:
    """Готов слать сообщения: есть и токен, и привязанный chat_id."""
    d = _read()
    return bool(d.get("token_enc") and d.get("chat_id"))


def clear() -> None:
    try:
        PATH.unlink()
    except FileNotFoundError:
        pass
