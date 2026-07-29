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
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

import config
from json_store import atomic_write_json

# Базовый адрес облачного бота/сервиса. Один на всех пользователей.
CLOUD_BASE = "https://wexflow-bot.vercel.app"

# Стабильный идентификатор этой установки (общий для всех модулей WexFlow).
DEVICE_PATH = config.SHARED_DIR / "device.json"

_device_cache: dict | None = None
_device_lock = threading.Lock()
_poll_error_lock = threading.Lock()
_last_poll_error: dict = {}


def _remember_poll_error(data: dict | None = None, *, http_status: int = 0) -> None:
    """Сохранить безопасную причину последнего сбоя poll для интерфейса."""
    global _last_poll_error
    friendly = _friendly_cloud_result(dict(data or {}), http_status=http_status)
    if not friendly.get("error"):
        friendly.update({
            "ok": False,
            "code": friendly.get("code") or "cloud_unavailable",
            "error": "Нет связи с облаком Telegram",
        })
    with _poll_error_lock:
        _last_poll_error = {
            "code": str(friendly.get("code") or ""),
            "error": str(friendly.get("error") or "")[:240],
            "http_status": int(http_status or 0),
        }


def _clear_poll_error() -> None:
    global _last_poll_error
    with _poll_error_lock:
        _last_poll_error = {}


def last_poll_error() -> dict:
    """Безопасная копия причины последнего сбоя облачного poll."""
    with _poll_error_lock:
        return dict(_last_poll_error)


def _device_record() -> dict:
    """{id, secret, persisted}. Шаг 5: кроме id устройство хранит СЕКРЕТ —
    им подписываются все запросы к облаку (заголовок x-device-token), чтобы
    профиль и очереди не отдавались любому, кто подсмотрел device id.
    У старых установок секрета в файле нет — дописываем при первом запуске."""
    global _device_cache
    if _device_cache is not None:
        return _device_cache
    with _device_lock:
        if _device_cache is not None:
            return _device_cache
        rec: dict = {}
        try:
            if DEVICE_PATH.exists():
                d = json.loads(DEVICE_PATH.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    rec = d
        except (OSError, ValueError):
            rec = {}
        changed = False
        if not rec.get("id"):
            rec["id"] = uuid.uuid4().hex
            changed = True
        if not rec.get("secret"):
            rec["secret"] = secrets.token_hex(32)
            changed = True
        persisted = not changed
        if changed:
            try:
                atomic_write_json(
                    DEVICE_PATH, {"id": rec["id"], "secret": rec["secret"]}
                )
                persisted = True
            except OSError:
                # секрет не сохранился: регистрировать его НЕЛЬЗЯ, иначе после
                # перезапуска устройство навсегда останется без верного токена
                persisted = False
        _device_cache = {"id": str(rec["id"]), "secret": str(rec["secret"]),
                         "persisted": persisted}
        return _device_cache


def device_id() -> str:
    """Уникальный id устройства. Создаётся один раз и хранится локально."""
    return _device_record()["id"]


def device_secret() -> str:
    """Секрет устройства для заголовка x-device-token (шаг 5)."""
    return _device_record()["secret"]


def active_profile_id() -> str:
    """Candidate scope attached to every cloud write."""
    try:
        import candidate_profiles
        return candidate_profiles.active_profile_id()
    except Exception:  # noqa: BLE001 - primary is the compatibility fallback
        return "primary"


_registered = False
_register_lock = threading.Lock()


def _rotate_device_identity() -> bool:
    """После GDPR-удаления заменить id+секрет отозванного устройства."""
    global _device_cache, _registered
    rec = {"id": uuid.uuid4().hex, "secret": secrets.token_hex(32)}
    with _register_lock:
        try:
            atomic_write_json(DEVICE_PATH, rec)
        except OSError:
            _registered = False
            return False
        with _device_lock:
            _device_cache = {**rec, "persisted": True}
        _registered = False
    return True


def _ensure_registered(timeout: int = 6) -> None:
    """Один раз за процесс сообщить облаку секрет устройства (шаг 5).
    Кто первый зарегистрировал — того и устройство. Сбой сети не критичен:
    до успешной регистрации облако пускает устройство по-старому, а мы
    попробуем снова при следующем запросе."""
    global _registered
    if _registered or not _device_record()["persisted"]:
        return
    with _register_lock:
        if _registered:
            return
        payload = json.dumps({"device": device_id(),
                              "secret": device_secret()}).encode("utf-8")
        req = urllib.request.Request(
            f"{CLOUD_BASE}/api/session", data=payload,
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if json.loads(r.read().decode("utf-8")).get("ok"):
                    _registered = True
        except (urllib.error.URLError, OSError, ValueError):
            pass


def _open(url: str, payload: dict | None = None, timeout: int = 10):
    """Запрос к облаку с токеном устройства. GET — если payload is None."""
    _ensure_registered()
    headers = {"x-device-token": device_secret()}
    data = None
    if payload is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def login_url() -> str:
    """Ссылка, которую открывает кнопка «Войти через Telegram»."""
    return f"{CLOUD_BASE}/api/login?device={device_id()}"


def fetch_session_state(timeout: int = 10) -> dict | None:
    """Вернуть состояние облачной сессии или None именно при ошибке связи."""
    url = f"{CLOUD_BASE}/api/session?device={device_id()}"
    try:
        with _open(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fetch_session(timeout: int = 10) -> dict | None:
    """Спросить облако, кто вошёл по нашему device. Вернёт user-dict или None.

    user-dict: {"tgId", "name", "username", "plan", "ts"}.
    Сеть/таймаут не роняют вызывающего — при любой ошибке вернётся None.
    """
    data = fetch_session_state(timeout)
    if data and data.get("loggedIn") and isinstance(data.get("user"), dict):
        return data["user"]
    return None


def fetch_profile_binding(profile_id: str, timeout: int = 6) -> dict | None:
    """Return the Telegram identity linked to one family candidate.

    Unlike fetch_session(), this never returns the device owner's Telegram for
    a non-primary candidate.
    """
    profile_id = str(profile_id or "").strip()
    if not profile_id or profile_id == "primary":
        return None
    query = urllib.parse.urlencode({
        "device": device_id(),
        "profileId": profile_id,
    })
    try:
        with _open(f"{CLOUD_BASE}/api/session?{query}", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) and data.get("ok") else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def push_profile(profile: dict, timeout: int = 10) -> bool:
    """Выгрузить локальный профиль в облачный аккаунт (перенос/резервная копия).

    Облако хранит профиль, привязанный к Telegram-аккаунту, — чтобы данные не
    терялись и подтягивались на других устройствах. Ошибки сети не критичны.
    """
    url = f"{CLOUD_BASE}/api/profile"
    try:
        with _open(url, {"device": device_id(), "profile": profile}, timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pull_profile(timeout: int = 10) -> dict | None:
    """Загрузить профиль из облачного аккаунта (для нового/чистого устройства)."""
    url = f"{CLOUD_BASE}/api/profile?device={device_id()}"
    try:
        with _open(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        prof = data.get("profile")
        if isinstance(prof, dict) and prof:
            return prof
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def _friendly_cloud_result(data: dict, http_status: int = 0) -> dict:
    """Не показывать человеку сырые HTTP/Redis ошибки из облачного сервиса."""
    if not isinstance(data, dict):
        data = {}
    code = str(data.get("code") or "")
    if code == "store_quota":
        data["error"] = (
            "Облачное хранилище Telegram исчерпало лимит. Данные на ПК в "
            "безопасности; нужно восстановить или заменить облачную базу."
        )
    elif http_status >= 500 and not data.get("error"):
        data.update({
            "ok": False,
            "code": code or "cloud_unavailable",
            "error": "Облако Telegram временно недоступно. Повтори немного позже.",
        })
    return data


def _post_json(path: str, payload: dict, timeout: int = 10) -> dict:
    """POST JSON на облако и вернуть разобранный ответ (или {ok:False,error}).

    Сетевые ошибки не роняют вызывающего — превращаются в {"ok": False, ...}.
    """
    url = f"{CLOUD_BASE}{path}"
    try:
        with _open(url, payload, timeout) as r:
            return _friendly_cloud_result(json.loads(r.read().decode("utf-8")))
    except urllib.error.HTTPError as e:  # 4xx/5xx с телом-ошибкой
        try:
            return _friendly_cloud_result(
                json.loads(e.read().decode("utf-8")),
                http_status=e.code,
            )
        except (ValueError, OSError):
            return _friendly_cloud_result({"ok": False}, http_status=e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return {"ok": False, "error": "Нет связи с облаком"}


def delete_cloud_data(timeout: int = 15) -> dict:
    """Удалить облачный аккаунт/очереди и отозвать старую identity устройства."""
    old_device = device_id()
    result = _post_json(
        "/api/session", {"action": "delete_data", "device": old_device}, timeout
    )
    if result.get("ok"):
        result["identityRotated"] = _rotate_device_identity()
    return result


def unlink_device(timeout: int = 12) -> dict:
    """Отвязать только этот компьютер от Telegram-аккаунта в облаке.

    Профиль, тариф, сам Telegram-аккаунт и секрет устройства сохраняются:
    пользователь сможет позднее осознанно войти снова. Очереди и обратные
    привязки этого ПК удаляются, поэтому Mini App перестаёт им управлять.
    """
    return _post_json(
        "/api/session",
        {"action": "unlink_device", "device": device_id()},
        timeout,
    )


def link_new(timeout: int = 10) -> dict:
    """Получить одноразовый код привязки по ID. Пользователь отправляет код боту
    @wexflowbot со своего Telegram — облако логинит его аккаунт в это устройство
    (как «Войти через Telegram», только без браузера). Возвращает {ok, code, botUsername}."""
    return _post_json("/api/link/new", {"deviceId": device_id()}, timeout)


def create_profile_invite(profile_id: str, profile_name: str, timeout: int = 10) -> dict:
    """Create a one-use Telegram invite for an existing non-primary candidate."""
    return _post_json("/api/link/new", {
        "deviceId": device_id(),
        "profileId": str(profile_id or ""),
        "profileName": str(profile_name or "")[:40],
    }, timeout)


def rebind_start(timeout: int = 10) -> dict:
    """Шаг 1 перепривязки: попросить облако прислать код в СТАРЫЙ Telegram."""
    return _post_json("/api/rebind", {"action": "start", "device": device_id()}, timeout)


def rebind_confirm(code: str, timeout: int = 10) -> dict:
    """Шаг 2: проверить код. При успехе вернёт {ok, loginUrl} — ссылку входа новым аккаунтом."""
    return _post_json(
        "/api/rebind", {"action": "confirm", "device": device_id(), "code": code}, timeout
    )


def offer(text: str, job_id: str, timeout: int = 15, *,
          job: dict | None = None, panel: bool = False,
          demo: bool = False) -> dict | None:
    """Отправить карточку вакансии через облако — бот пришлёт её пользователю с
    кнопками ✅/❌. text — уже собранная карточка (с переводом).

    demo=True — проверочная карточка: всегда идёт в чат (даже в режиме «только
    в приложении») и не попадает в список панели, чтобы пример нельзя было подать."""
    url = f"{CLOUD_BASE}/api/offer"
    job_payload = dict(job or {})
    job_payload["id"] = job_id
    payload = {
        "deviceId": device_id(), "profileId": active_profile_id(),
        "job": job_payload, "text": text, "panel": bool(panel),
    }
    if demo:
        payload["demo"] = True
    try:
        with _open(url, payload, timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def send_digest(text: str, timeout: int = 10) -> bool:
    """Одно информационное сообщение в чат (без кнопок ✅/❌) — дневной дайджест.
    Бот добавит кнопку «Открыть панель»; решения принимаются в панели."""
    return bool(send_test_message(text, timeout).get("ok"))


def send_test_message(text: str, timeout: int = 10) -> dict:
    """Отправить простое сообщение для проверки привязки Telegram.

    В отличие от send_digest возвращает полный ответ облака, чтобы мастер
    подключения мог объяснить needsBotStart и другие причины, а не просто False.
    """
    payload = {
        "deviceId": device_id(), "profileId": active_profile_id(),
        "digest": True, "text": str(text or "")[:2000],
    }
    return _post_json("/api/offer", payload, timeout)


def clear_panel(timeout: int = 5) -> bool:
    """Очистить сохранённые вакансии в Telegram Mini App для этого устройства."""
    payload = {
        "deviceId": device_id(), "profileId": active_profile_id(), "clearPanel": True,
    }
    return bool(_post_json("/api/offer", payload, timeout).get("ok"))


def fetch_decisions(timeout: int = 15) -> list:
    """Забрать решения пользователя (✅/❌) из облака. Очередь очищается на стороне облака.
    Возвращает список [{jobId, action, ts}]."""
    url = f"{CLOUD_BASE}/api/decisions?deviceId={device_id()}"
    try:
        with _open(url, timeout=timeout) as r:
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
        with _open(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        cmds = data.get("commands")
        return cmds if isinstance(cmds, list) else []
    except (urllib.error.URLError, OSError, ValueError):
        return []


def fetch_poll(
    tg_id: str = "",
    timeout: int = 12,
    *,
    sync_binding: bool = False,
) -> dict | None:
    """Одним запросом получить решения + команды и обновить heartbeat.

    None означает именно сбой связи/серверную ошибку; пустые списки означают
    успешный опрос без работы. Это различие нужно для backoff и диагностики."""
    query = {"deviceId": device_id(), "kind": "poll2"}
    if tg_id and sync_binding:
        query["tgId"] = str(tg_id)
        query["bind"] = "1"
    url = f"{CLOUD_BASE}/api/decisions?{urllib.parse.urlencode(query)}"
    try:
        with _open(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not data.get("ok"):
            _remember_poll_error(data)
            return None
        # Backward compatibility during a staged rollout: an older cloud
        # endpoint treats unknown kind=poll as the decisions-only request and
        # omits "commands". Keep remote control working until the bot deploys.
        commands = data.get("commands")
        if not isinstance(commands, list):
            commands = fetch_commands(tg_id=tg_id, timeout=timeout)
        result = {
            "decisions": data.get("decisions") if isinstance(data.get("decisions"), list) else [],
            "commands": commands,
            "ack": bool(data.get("ack")),
            # облако говорит, открыта ли сейчас панель на телефоне: пока открыта,
            # ПК слушает часто, чтобы кнопки срабатывали за секунды, а не за 2 минуты
            "active": bool(data.get("active")),
        }
        _clear_poll_error()
        return result
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            data = {}
        _remember_poll_error(data, http_status=e.code)
        return None
    except (urllib.error.URLError, OSError, ValueError):
        _remember_poll_error()
        return None


def acknowledge_poll(decisions: list, commands: list, timeout: int = 10) -> bool:
    """Confirm only items the desktop has already handled.

    Old cloud versions never advertise ACK mode, so callers do not invoke this
    during a staged rollout.
    """
    decision_ids = [str(x.get("_deliveryId") or "") for x in decisions if isinstance(x, dict)]
    command_ids = [str(x.get("_deliveryId") or "") for x in commands if isinstance(x, dict)]
    payload = {
        "kind": "poll_ack",
        "deviceId": device_id(),
        "decisionIds": [x for x in decision_ids if x][:200],
        "commandIds": [x for x in command_ids if x][:200],
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def send_command_result(command: dict, text: str, timeout: int = 10) -> bool:
    """Отправить результат выполнения удалённой команды обратно в Telegram."""
    payload = {
        "kind": "command_result",
        "deviceId": device_id(),
        "profileId": str(command.get("profileId") or active_profile_id()),
        "commandId": command.get("id") or "",
        "chatId": command.get("chatId") or "",
        "text": text,
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_apply_result(job_id: str, state: str, msg: str = "", timeout: int = 8) -> bool:
    """Сообщить облаку статус подачи вакансии — для «живого эфира» в Mini App-панели.
    state: submitting | submitted | unconfirmed | failed.
    Панель опрашивает result:<device>:<job>."""
    payload = {
        "kind": "apply_result",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "jobId": str(job_id),
        "state": str(state),
        "msg": str(msg or ""),
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_apply_proof(job_id: str, photo_b64: str, caption: str = "",
                       timeout: int = 20, ask_send: bool = False) -> bool:
    """Отправить в чат скрин-доказательство подачи (через облако — токена бота у
    ПК нет). Картинка нигде не хранится: облако сразу пересылает её в Telegram."""
    if not photo_b64:
        return False
    payload = {
        "kind": "apply_proof",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "jobId": str(job_id),
        "caption": str(caption or "")[:900],
        "photo": photo_b64,
        # под скрином подготовленной анкеты бот покажет «Отправить»/«Отмена»
        "askSend": bool(ask_send),
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_applied(items, timeout: int = 8) -> bool:
    """Отправить в облако список поданных вакансий — для раздела «Поданные» в Mini App
    (одно облако: подал на ПК → видно в телефоне). Облако заодно убирает эти вакансии
    из «ждут решения», чтобы не висели стейлом. items: [{id,title,brand,city,hours,url,ts}]."""
    payload = {
        "kind": "applied_sync",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "applied": list(items or [])[:60],
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_jobs(items, timeout: int = 12) -> bool:
    """Отправить в облако текущий список вакансий для главной вкладки Mini App
    (подходящие + ближайшие активные, как на главном экране приложения). Это не
    запускает подачу: кнопка в панели всё равно идёт через очередь decisions и
    локальный F27-гейт."""
    payload = {
        "kind": "jobs_sync",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "jobs": list(items or [])[:500],
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_job_texts(items, timeout: int = 8) -> bool:
    """Отправить в облако полные тексты вакансий (перевод + оригинал + статус)
    для экрана детали в Mini App. items: [{id, ru, orig, st}]. Неуспех не фатален
    (старое облако не знает kind jobtext_sync — просто вернёт ok:false)."""
    payload = {
        "kind": "jobtext_sync",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "texts": list(items or [])[:40],
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_ai_reply(req_id: str, reply: str, done: bool, fields, error: str = "", timeout: int = 8) -> bool:
    """Ответ ИИ-помощника фильтров для диалога в панели: ПК посчитал через Gemini,
    кладём в облако (ai_reply:<device>), панель забирает по reqId."""
    payload = {
        "kind": "ai_reply",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "reqId": str(req_id or ""),
        "reply": str(reply or ""),
        "done": bool(done),
        "fields": fields if isinstance(fields, dict) else None,
        "error": str(error or ""),
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))


def report_filters(payload: dict, timeout: int = 8) -> bool:
    """Отправить в облако текущие фильтры первого набора + варианты выбора —
    панель Mini App показывает их и может прислать команду set_filters.
    Автоотправка/лимиты сюда не входят и с телефона недоступны."""
    body = {
        "kind": "filters_sync",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "filters": dict(payload or {}),
    }
    return bool(_post_json("/api/decisions", body, timeout).get("ok"))


def report_apply_progress(progress: dict, timeout: int = 6) -> bool:
    """Сообщить облаку сводку прогресса пакетной подачи — для панели прогресса в
    Mini App («Подаю X из N», что сейчас, сколько подано/не удалось). Облако хранит
    progress:<device> с коротким TTL, поэтому по завершении баннер сам исчезает."""
    payload = {
        "kind": "apply_progress",
        "deviceId": device_id(),
        "profileId": active_profile_id(),
        "progress": progress or {},
    }
    return bool(_post_json("/api/decisions", payload, timeout).get("ok"))
