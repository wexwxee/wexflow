"""Аккаунт WexFlow — вход через Telegram (облачный).

Вход выполняется на облачной странице (см. cloud_auth), а здесь хранится его
результат: кто вошёл (Telegram-личность) и какой у него тариф. Тариф приходит
из облака — это позволяет включать людям Pro/Max вручную через поддержку, не
трогая приложение: на следующем опросе приложение само подхватит новый тариф.

Локально состояние лежит в одном общем JSON (config.SHARED_DIR/account.json).
"""
import threading

import config
from json_store import atomic_write_json, read_json

ACCOUNT_PATH = config.SHARED_DIR / "account.json"

DEFAULT = {
    "signed_in": False,
    "tg_id": None,        # Telegram user id (строка)
    "tg_name": None,      # имя из Telegram
    "username": None,     # @username из Telegram
    "plan": "free",       # тариф из облака: free | pro | max
    "email": None,        # на будущее (email-вход), для обратной совместимости
    # После явного «Выйти» фоновый poller не должен тут же залогинить человека
    # обратно из всё ещё живой облачной сессии.
    "cloud_sync_paused": False,
}
_LOCK = threading.RLock()


def load() -> dict:
    with _LOCK:
        data = dict(DEFAULT)
        saved = read_json(ACCOUNT_PATH, {}, dict)
        data.update({k: saved.get(k, data[k]) for k in DEFAULT})
        return data


def save(data: dict) -> None:
    with _LOCK:
        merged = dict(DEFAULT)
        merged.update({k: data.get(k, merged[k]) for k in DEFAULT})
        try:
            atomic_write_json(ACCOUNT_PATH, merged, indent=2)
        except OSError:
            pass


def is_signed_in() -> bool:
    return bool(load().get("signed_in"))


def _sync_subscription(plan: str) -> None:
    """Источник тарифа — облако. Прокидываем его в подписку (для гейтинга/витрины)."""
    try:
        import subscription
        subscription.set_plan(plan, active=(plan != "free"), source="server")
    except Exception:  # noqa: BLE001 — подписка не должна ронять вход
        pass


def _apply_session(user: dict, *, respect_pause: bool) -> dict | None:
    plan = user.get("plan")
    if plan not in ("free", "pro", "max"):
        plan = "free"
    with _LOCK:
        data = load()
        # Проверка и запись находятся под тем же lock: выход не может вклиниться
        # между ними и быть затёрт старым ответом фонового cloud-poll.
        if respect_pause and data.get("cloud_sync_paused"):
            return None
        data.update({
            "signed_in": True,
            "tg_id": str(user.get("tgId") or user.get("tg_id") or "") or None,
            "tg_name": (user.get("name") or "").strip() or None,
            "username": (user.get("username") or "").strip() or None,
            "plan": plan,
            "cloud_sync_paused": False,
        })
        save(data)
        _sync_subscription(plan)
    return data


def apply_session(user: dict) -> dict:
    """Применить явный новый Telegram-вход пользователя."""
    return _apply_session(user, respect_pause=False) or dict(DEFAULT)


def apply_cloud_session(user: dict) -> bool:
    """Применить фоновый ответ облака, только если пользователь не вышел."""
    return _apply_session(user, respect_pause=True) is not None


def sign_out() -> None:
    with _LOCK:
        data = dict(DEFAULT)
        data["cloud_sync_paused"] = True
        save(data)
        _sync_subscription("free")


def cloud_sync_paused() -> bool:
    return bool(load().get("cloud_sync_paused"))


def _initial(name: str, email: str) -> str:
    name = (name or "").strip()
    if name:
        return name[0].upper()
    email = (email or "").strip()
    return email[0].upper() if email else "?"


def status(profile: dict) -> dict:
    """Данные для карточки «Аккаунт» на странице настроек."""
    acc = load()
    signed_in = bool(acc.get("signed_in"))
    tg_name = (acc.get("tg_name") or "").strip()
    username = (acc.get("username") or "").strip()
    # имя для показа: профиль кандидата → имя из Telegram → «Гость»
    first = (profile.get("first_name") or "").strip()
    last = (profile.get("last_name") or "").strip()
    email = (profile.get("email") or "").strip()
    full = (first + " " + last).strip()
    display_name = full or tg_name or "Гость"
    return {
        "signed_in": signed_in,
        "display_name": display_name,
        "has_name": bool(full or tg_name),
        "initial": _initial(full or tg_name, email),
        "email": email,
        "username": username,
        "tg_name": tg_name,
        "plan": acc.get("plan") or "free",
    }
