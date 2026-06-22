"""Облачный вход WexFlow — «Continue with Telegram».

Само приложение не знает ни пароля, ни токена Telegram. Вход происходит на
облачной странице (wexflow-bot на Vercel), а приложение лишь:
  1) даёт ссылку на вход со своим уникальным device id,
  2) спрашивает облако «кто вошёл по этому устройству» (poll),
  3) сохраняет личность и тариф локально (через account.py).

Когда появится своя облачная платформа — менять нужно будет только CLOUD_BASE.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import config

# Базовый адрес облачного бота/сервиса. Один на всех пользователей.
CLOUD_BASE = "https://wexflow-bot.vercel.app"

# Стабильный идентификатор этой установки (общий для всех модулей WexFlow).
DEVICE_PATH = config.SHARED_DIR / "device.json"


def device_id() -> str:
    """Уникальный id устройства. Создаётся один раз и хранится локально."""
    try:
        if DEVICE_PATH.exists():
            d = json.loads(DEVICE_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("id"):
                return str(d["id"])
    except (OSError, ValueError):
        pass
    did = uuid.uuid4().hex
    try:
        DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEVICE_PATH.write_text(json.dumps({"id": did}), encoding="utf-8")
    except OSError:
        pass
    return did


def login_url() -> str:
    """Ссылка, которую открывает кнопка «Войти через Telegram»."""
    return f"{CLOUD_BASE}/api/login?device={device_id()}"


def fetch_session(timeout: int = 10) -> dict | None:
    """Спросить облако, кто вошёл по нашему device. Вернёт user-dict или None.

    user-dict: {"tgId", "name", "username", "plan", "ts"}.
    Сеть/таймаут не роняют вызывающего — при любой ошибке вернётся None.
    """
    url = f"{CLOUD_BASE}/api/session?device={device_id()}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("loggedIn") and isinstance(data.get("user"), dict):
            return data["user"]
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def push_profile(profile: dict, timeout: int = 10) -> bool:
    """Выгрузить локальный профиль в облачный аккаунт (перенос/резервная копия).

    Облако хранит профиль, привязанный к Telegram-аккаунту, — чтобы данные не
    терялись и подтягивались на других устройствах. Ошибки сети не критичны.
    """
    url = f"{CLOUD_BASE}/api/profile"
    payload = json.dumps({"device": device_id(), "profile": profile}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pull_profile(timeout: int = 10) -> dict | None:
    """Загрузить профиль из облачного аккаунта (для нового/чистого устройства)."""
    url = f"{CLOUD_BASE}/api/profile?device={device_id()}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        prof = data.get("profile")
        if isinstance(prof, dict) and prof:
            return prof
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None
