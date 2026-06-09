"""Локальное хранилище логина Salling Group для авто-входа.

Хранится в sf_credentials.json РЯДОМ с приложением, на твоём компьютере.
Пароль НЕ отправляется никуда, кроме формы входа Salling в браузере.
Это не шифрование — просто локальный файл; не клади папку в общий доступ/git.
"""
import json

import config

PATH = config.BASE_DIR / "sf_credentials.json"


def load() -> dict:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save(email: str, password: str):
    data = load()
    if email is not None:
        data["email"] = email.strip()
    # пустой пароль из формы означает «не менять сохранённый»
    if password:
        data["password"] = password
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get() -> dict:
    return load()


def status() -> dict:
    d = load()
    return {"email": d.get("email", ""), "has_password": bool(d.get("password"))}


def clear():
    try:
        PATH.unlink()
    except FileNotFoundError:
        pass
