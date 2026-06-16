"""Аккаунт WexFlow — ЗАГОТОВКА (без реального входа).

Пока приложение локальное, «аккаунт» — это просто личность из профиля.
Настоящие вход/регистрация и синхронизация профиля и подписки между
устройствами появятся вместе с облачным сервером (та же фаза, что и оплата).
Здесь подготовлена единая точка: status() для шапки и is_signed_in() на будущее.
"""
import json

import config

ACCOUNT_PATH = config.SHARED_DIR / "account.json"

DEFAULT = {"signed_in": False, "email": None}


def load() -> dict:
    data = dict(DEFAULT)
    try:
        if ACCOUNT_PATH.exists():
            saved = json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: saved.get(k, data[k]) for k in DEFAULT})
    except (OSError, ValueError):
        pass
    return data


def is_signed_in() -> bool:
    return bool(load().get("signed_in"))


def _initial(name: str, email: str) -> str:
    name = (name or "").strip()
    if name:
        return name[0].upper()
    email = (email or "").strip()
    return email[0].upper() if email else "?"


def status(profile: dict) -> dict:
    """Данные для карточки «Аккаунт» на странице настроек."""
    acc = load()
    first = (profile.get("first_name") or "").strip()
    last = (profile.get("last_name") or "").strip()
    email = (profile.get("email") or "").strip()
    full = (first + " " + last).strip()
    return {
        "signed_in": bool(acc.get("signed_in")),
        "display_name": full or "Гость",
        "has_name": bool(full),
        "initial": _initial(first, email),
        "email": email,
    }
