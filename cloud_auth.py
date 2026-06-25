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
import urllib.parse
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


def _post_json(path: str, payload: dict, timeout: int = 10) -> dict:
    """POST JSON на облако и вернуть разобранный ответ (или {ok:False,error}).

    Сетевые ошибки не роняют вызывающего — превращаются в {"ok": False, ...}.
    """
    url = f"{CLOUD_BASE}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 4xx/5xx с телом-ошибкой
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError):
        return {"ok": False, "error": "Нет связи с облаком"}


def link_new(timeout: int = 10) -> dict:
    """Получить одноразовый код привязки по ID. Пользователь отправляет код боту
    @wexflowbot со своего Telegram — облако логинит его аккаунт в это устройство
    (как «Войти через Telegram», только без браузера). Возвращает {ok, code, botUsername}."""
    return _post_json("/api/link/new", {"deviceId": device_id()}, timeout)


def rebind_start(timeout: int = 10) -> dict:
    """Шаг 1 перепривязки: попросить облако прислать код в СТАРЫЙ Telegram."""
    return _post_json("/api/rebind", {"action": "start", "device": device_id()}, timeout)


def rebind_confirm(code: str, timeout: int = 10) -> dict:
    """Шаг 2: проверить код. При успехе вернёт {ok, loginUrl} — ссылку входа новым аккаунтом."""
    return _post_json(
        "/api/rebind", {"action": "confirm", "device": device_id(), "code": code}, timeout
    )


def offer(text: str, job_id: str, timeout: int = 15, *,
          job: dict | None = None, panel: bool = False) -> dict | None:
    """Отправить карточку вакансии через облако — бот пришлёт её пользователю с
    кнопками ✅/❌. text — уже собранная карточка (с переводом)."""
    url = f"{CLOUD_BASE}/api/offer"
    job_payload = dict(job or {})
    job_payload["id"] = job_id
    payload = json.dumps({
        "deviceId": device_id(), "job": job_payload, "text": text, "panel": bool(panel),
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def clear_panel(timeout: int = 5) -> bool:
    """Очистить сохранённые вакансии в Telegram Mini App для этого устройства."""
    payload = {"deviceId": device_id(), "clearPanel": True}
    return bool(_post_json("/api/offer", payload, timeout).get("ok"))


def fetch_decisions(timeout: int = 15) -> list:
    """Забрать решения пользователя (✅/❌) из облака. Очередь очищается на стороне облака.
    Возвращает список [{jobId, action, ts}]."""
    url = f"{CLOUD_BASE}/api/decisions?deviceId={device_id()}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        d = data.get("decisions")
        return d if isinstance(d, list) else []
    except (urllib.error.URLError, OSError, ValueError):
        return []


def fetch_commands(tg_id: str = "", timeout: int = 15) -> list:
    """Забрать удалённые команды из Telegram-пульта. Очередь очищается в облаке.
    Возвращает список [{id, action, chatId, ts}]. Сам GET также служит heartbeat:
    облако видит, что ПК онлайн и может честно показывать это в боте."""
    query = {"deviceId": device_id(), "kind": "commands"}
    if tg_id:
        query["tgId"] = str(tg_id)
    url = f"{CLOUD_BASE}/api/decisions?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        cmds = data.get("commands")
        return cmds if isinstance(cmds, list) else []
    except (urllib.error.URLError, OSError, ValueError):
        return []


def send_command_result(command: dict, text: str, timeout: int = 10) -> bool:
    """Отправить результат выполнения удалённой команды обратно в Telegram."""
    payload = {
        "kind": "command_result",
        "deviceId": device_id(),
        "commandId": command.get("id") or "",
        "chatId": command.get("chatId") or "",
        "text": text,
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_apply_result(job_id: str, state: str, msg: str = "", timeout: int = 8) -> bool:
    """Сообщить облаку статус подачи вакансии — для «живого эфира» в Mini App-панели.
    state: submitting | submitted | failed. Панель опрашивает result:<device>:<job>."""
    payload = {
        "kind": "apply_result",
        "deviceId": device_id(),
        "jobId": str(job_id),
        "state": str(state),
        "msg": str(msg or ""),
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_applied(items, timeout: int = 8) -> bool:
    """Отправить в облако список поданных вакансий — для раздела «Поданные» в Mini App
    (одно облако: подал на ПК → видно в телефоне). Облако заодно убирает эти вакансии
    из «ждут решения», чтобы не висели стейлом. items: [{id,title,brand,city,hours,url,ts}]."""
    payload = {
        "kind": "applied_sync",
        "deviceId": device_id(),
        "applied": list(items or [])[:60],
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_apply_progress(progress: dict, timeout: int = 6) -> bool:
    """Сообщить облаку сводку прогресса пакетной подачи — для панели прогресса в
    Mini App («Подаю X из N», что сейчас, сколько подано/не удалось). Облако хранит
    progress:<device> с коротким TTL, поэтому по завершении баннер сам исчезает."""
    payload = {
        "kind": "apply_progress",
        "deviceId": device_id(),
        "progress": progress or {},
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))
