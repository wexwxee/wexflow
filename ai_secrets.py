"""Безопасное локальное хранилище ИИ-ключей WexFlow с изоляцией по аккаунтам.

Зачем отдельный модуль (а не secrets.json):
  * Ключ каждого провайдера (Gemini, Groq, …) шифруется Windows DPAPI
    (``CryptProtectData``) — расшифровать его может только текущий Windows-
    пользователь на этой машине. В файле лежит только нечитаемая base64-строка.
  * Ключи привязаны к КОНКРЕТНОМУ аккаунту WexFlow: ``(account_id, provider)``.
    Чужой локальный аккаунт не видит и не может использовать ключ владельца.
  * Общего «встроенного» ключа WexFlow не существует. ``get_api_key`` всегда
    требует ``account_id`` и никогда не берёт ключ другого аккаунта как fallback.

Совместимость: старый ``secrets.json`` (``gemini_api_key``) и переменные
окружения ``GEMINI_API_KEY``/``GROQ_API_KEY`` остаются рабочими ТОЛЬКО для
разработки/CI и ТОЛЬКО для текущего аккаунта (см. ``env_key`` и
``ai_filters.api_key``). В собранном приложении (``paths.is_frozen()``) env-ключ
не используется как глобальный ключ всех аккаунтов.

Секреты никогда не пишутся в логи, traceback, JSON API, HTML, git или сборку —
наружу отдаётся только маска вида ``••••ABCD`` и короткий отпечаток (fingerprint)
для раздельного учёта лимитов.
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
import uuid

import config
from json_store import atomic_write_json, read_json

# Файл лежит в SHARED_DIR (общий для модулей WexFlow), рядом с профилем/подпиской.
PATH = config.SHARED_DIR / "ai_credentials.json"
_LOCAL_ID_PATH = config.SHARED_DIR / "ai_account.json"
_LOCK = threading.RLock()

PROVIDERS = ("gemini", "groq")

# In-memory кэш расшифрованных ключей per account — сбрасывается на logout/смену
# аккаунта, чтобы провайдер прежнего пользователя не «пережил» выход.
_KEY_CACHE: dict[tuple[str, str], str] = {}
_CACHE_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
#  Windows DPAPI (без внешних зависимостей). Монкипатчится в тестах.
# --------------------------------------------------------------------------- #
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob_in = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if protect
          else ctypes.windll.crypt32.CryptUnprotectData)
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _protect(raw: str) -> str:
    """Зашифровать строку -> base64. Тесты монкипатчат ``_dpapi``."""
    return base64.b64encode(_dpapi(raw.encode("utf-8"), protect=True)).decode("ascii")


def _unprotect(token: str) -> str:
    return _dpapi(base64.b64decode(token), protect=False).decode("utf-8")


# --------------------------------------------------------------------------- #
#  Утилиты: отпечаток и маска (наружу секрет не отдаём)
# --------------------------------------------------------------------------- #
def fingerprint_of(key: str) -> str:
    """Стабильный отпечаток ключа для РАЗДЕЛЬНОГО учёта лимитов.

    Это НЕ ключ и не его часть: односторонний хэш. Разные ключи -> разные
    отпечатки -> разная статистика. Один и тот же ключ -> один отпечаток.
    """
    key = (key or "").strip()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mask_of(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    tail = key[-4:] if len(key) >= 4 else key
    return "••••" + tail


# --------------------------------------------------------------------------- #
#  Идентификатор аккаунта WexFlow
# --------------------------------------------------------------------------- #
def _local_account_id() -> str:
    """Стабильный анонимный id для не-вошедшего локального аккаунта."""
    with _LOCK:
        data = read_json(_LOCAL_ID_PATH, {}, dict) or {}
        local = str(data.get("local_id") or "").strip()
        if not local:
            local = "local:" + uuid.uuid4().hex[:16]
            try:
                atomic_write_json(_LOCAL_ID_PATH, {"local_id": local}, indent=2)
            except OSError:
                pass
        return local


def current_account_id() -> str:
    """Текущий аккаунт WexFlow.

    Вошедший через Telegram -> ``tg:<id>`` (стабильно на пользователя). Иначе —
    локальный анонимный id. Возможности ИИ определяются ключами ЭТОГО аккаунта.
    """
    try:
        import account
        acc = account.load()
        if acc.get("signed_in") and acc.get("tg_id"):
            return "tg:" + str(acc["tg_id"]).strip()
    except Exception:  # noqa: BLE001 — аккаунт не должен ронять ИИ-слой
        pass
    return _local_account_id()


def _resolve(account_id: str | None) -> str:
    return (account_id or "").strip() or current_account_id()


# --------------------------------------------------------------------------- #
#  Хранилище
# --------------------------------------------------------------------------- #
def _load() -> dict:
    data = read_json(PATH, {}, dict) or {}
    if not isinstance(data.get("accounts"), dict):
        data["accounts"] = {}
    return data


def _save(data: dict) -> None:
    with _LOCK:
        atomic_write_json(PATH, data, indent=2)


def _entry(data: dict, account_id: str, provider: str) -> dict | None:
    acc = data.get("accounts", {}).get(account_id)
    if not isinstance(acc, dict):
        return None
    row = acc.get(provider)
    return row if isinstance(row, dict) else None


def _cache_drop(account_id: str | None = None) -> None:
    with _CACHE_LOCK:
        if account_id is None:
            _KEY_CACHE.clear()
        else:
            for k in [k for k in _KEY_CACHE if k[0] == account_id]:
                _KEY_CACHE.pop(k, None)


# --------------------------------------------------------------------------- #
#  Публичный API
# --------------------------------------------------------------------------- #
def env_key(provider: str) -> str:
    """Ключ из окружения — ТОЛЬКО dev/CI (не в собранном приложении)."""
    if paths_is_frozen():
        return ""
    name = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}.get(provider, "")
    return (os.getenv(name) or "").strip() if name else ""


def paths_is_frozen() -> bool:
    try:
        import paths
        return paths.is_frozen()
    except Exception:  # noqa: BLE001
        return False


def has_key(provider: str, account_id: str | None = None) -> bool:
    account_id = _resolve(account_id)
    with _LOCK:
        if _entry(_load(), account_id, provider) is not None:
            return True
    # dev/CI: env считается «подключением» только для текущего аккаунта
    if account_id == current_account_id() and env_key(provider):
        return True
    return False


def get_api_key(provider: str, account_id: str | None = None) -> str:
    """Расшифрованный ключ провайдера для КОНКРЕТНОГО аккаунта.

    Никогда не берёт ключ другого аккаунта. Порядок:
      1) зашифрованный ключ, сохранённый для (account_id, provider);
      2) dev/CI: переменная окружения — только если это текущий аккаунт и не frozen.
    Отсутствует -> "" (вызывающий покажет мастер подключения).
    """
    account_id = _resolve(account_id)
    cache_key = (account_id, provider)
    with _CACHE_LOCK:
        if cache_key in _KEY_CACHE:
            return _KEY_CACHE[cache_key]
    with _LOCK:
        row = _entry(_load(), account_id, provider)
    key = ""
    if row and row.get("key_enc"):
        try:
            key = _unprotect(str(row["key_enc"]))
        except Exception:  # noqa: BLE001 — битый шифртекст не должен ронять приложение
            key = ""
    if not key and account_id == current_account_id():
        key = env_key(provider)
    if key:
        with _CACHE_LOCK:
            _KEY_CACHE[cache_key] = key
    return key


def set_api_key(
    provider: str,
    key: str,
    account_id: str | None = None,
    *,
    consent: bool | None = None,
    model: str | None = None,
    primary: bool | None = None,
) -> dict:
    """Сохранить (зашифровать) ключ провайдера для аккаунта. Возвращает info().

    Замена ключа создаёт НОВЫЙ fingerprint -> новая статистика (старая не
    смешивается). Открытый ключ в файл не пишется никогда.
    """
    account_id = _resolve(account_id)
    key = (key or "").strip()
    if not key:
        raise ValueError("empty key")
    fp = fingerprint_of(key)
    enc = _protect(key)
    with _LOCK:
        data = _load()
        accounts = data.setdefault("accounts", {})
        acc = accounts.setdefault(account_id, {})
        prev = acc.get(provider) if isinstance(acc.get(provider), dict) else {}
        row = {
            "key_enc": enc,
            "fingerprint": fp,
            "mask": mask_of(key),
            "added_at": prev.get("added_at") or time.time(),
            "updated_at": time.time(),
            "last_checked_at": prev.get("last_checked_at") or 0.0,
            "last_check_ok": prev.get("last_check_ok"),
            "consent": bool(prev.get("consent")) if consent is None else bool(consent),
            "model": (model if model is not None else prev.get("model")),
            "primary": (bool(primary) if primary is not None else prev.get("primary")),
        }
        acc[provider] = row
        _save(data)
    _cache_drop(account_id)
    return info(provider, account_id)


def set_consent(provider: str, account_id: str | None = None, value: bool = True) -> None:
    account_id = _resolve(account_id)
    with _LOCK:
        data = _load()
        row = _entry(data, account_id, provider)
        if row is not None:
            row["consent"] = bool(value)
            _save(data)


def set_last_check(provider: str, ok: bool | None, account_id: str | None = None) -> None:
    account_id = _resolve(account_id)
    with _LOCK:
        data = _load()
        row = _entry(data, account_id, provider)
        if row is not None:
            row["last_checked_at"] = time.time()
            row["last_check_ok"] = None if ok is None else bool(ok)
            _save(data)


def set_model(provider: str, model: str | None, account_id: str | None = None) -> None:
    account_id = _resolve(account_id)
    with _LOCK:
        data = _load()
        row = _entry(data, account_id, provider)
        if row is not None:
            row["model"] = (model or None)
            _save(data)


def delete_api_key(provider: str, account_id: str | None = None) -> bool:
    """Удалить локальный ключ провайдера у аккаунта. Ключи ДРУГИХ аккаунтов и
    других провайдеров не трогаются."""
    account_id = _resolve(account_id)
    removed = False
    with _LOCK:
        data = _load()
        acc = data.get("accounts", {}).get(account_id)
        if isinstance(acc, dict) and provider in acc:
            acc.pop(provider, None)
            if not acc:
                data["accounts"].pop(account_id, None)
            _save(data)
            removed = True
    _cache_drop(account_id)
    return removed


def fingerprint(provider: str, account_id: str | None = None) -> str:
    account_id = _resolve(account_id)
    with _LOCK:
        row = _entry(_load(), account_id, provider)
    if row and row.get("fingerprint"):
        return str(row["fingerprint"])
    # env-ключ текущего аккаунта тоже имеет отпечаток для раздельного учёта
    if account_id == current_account_id():
        env = env_key(provider)
        if env:
            return fingerprint_of(env)
    return ""


def info(provider: str, account_id: str | None = None) -> dict:
    """Безопасная карточка провайдера (без ключа) для UI и API."""
    account_id = _resolve(account_id)
    with _LOCK:
        row = _entry(_load(), account_id, provider)
    if row is not None:
        return {
            "provider": provider,
            "connected": True,
            "source": "stored",
            "mask": str(row.get("mask") or ""),
            "fingerprint": str(row.get("fingerprint") or ""),
            "added_at": float(row.get("added_at") or 0),
            "last_checked_at": float(row.get("last_checked_at") or 0),
            "last_check_ok": row.get("last_check_ok"),
            "consent": bool(row.get("consent")),
            "model": row.get("model") or None,
            "primary": row.get("primary"),
        }
    if account_id == current_account_id():
        env = env_key(provider)
        if env:
            return {
                "provider": provider,
                "connected": True,
                "source": "env",
                "mask": mask_of(env),
                "fingerprint": fingerprint_of(env),
                "added_at": 0.0,
                "last_checked_at": 0.0,
                "last_check_ok": None,
                "consent": True,  # env — явное действие разработчика
                "model": None,
                "primary": None,
            }
    return {
        "provider": provider,
        "connected": False,
        "source": "",
        "mask": "",
        "fingerprint": "",
        "added_at": 0.0,
        "last_checked_at": 0.0,
        "last_check_ok": None,
        "consent": False,
        "model": None,
        "primary": None,
    }


def list_info(account_id: str | None = None) -> dict:
    account_id = _resolve(account_id)
    return {p: info(p, account_id) for p in PROVIDERS}


# --------------------------------------------------------------------------- #
#  Миграция legacy Gemini-ключа (secrets.json) в namespace аккаунта
# --------------------------------------------------------------------------- #
def legacy_gemini_key() -> str:
    """Старый ключ из secrets.json (для показа предложения о миграции)."""
    try:
        import json
        if config.SECRETS_PATH.exists():
            raw = json.loads(config.SECRETS_PATH.read_text(encoding="utf-8"))
            return (raw.get("gemini_api_key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def migrate_legacy_gemini(account_id: str | None = None, *, confirm: bool) -> bool:
    """Привязать существующий Gemini-ключ из secrets.json к ПОДТВЕРЖДЁННОМУ
    текущему аккаунту. Только по явному согласию (confirm=True).

    Безопасно: старый ключ в secrets.json НЕ удаляется, пока шифрование не
    подтверждено обратной расшифровкой. При любой ошибке ничего не ломаем.
    """
    if not confirm:
        return False
    account_id = _resolve(account_id)
    key = legacy_gemini_key()
    if not key:
        return False
    # не перетираем уже сохранённый ключ этого аккаунта
    with _LOCK:
        if _entry(_load(), account_id, "gemini") is not None:
            return False
    try:
        enc = _protect(key)
        if _unprotect(enc) != key:  # атомарная проверка успешного шифрования
            return False
    except Exception:  # noqa: BLE001
        return False
    set_api_key("gemini", key, account_id, consent=True, primary=True)
    return True


# --------------------------------------------------------------------------- #
#  Смена/выход аккаунта: чистим ключи из памяти
# --------------------------------------------------------------------------- #
def on_account_switch(account_id: str | None = None) -> None:
    """Вызывается при logout/смене аккаунта: сбросить кэш ключей в памяти,
    чтобы провайдер прежнего аккаунта не продолжал работать."""
    _cache_drop(account_id)
