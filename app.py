"""Этап 2 — локальный веб-дашборд вакансий Salling Group.

Запуск:  python -m uvicorn app:app --reload
Открыть: http://127.0.0.1:8000
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from collections import Counter
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import func

import config
import labels
import geo
import settings_store
import translator
import translator_setup
import html_sanitize
import profile_store
import credentials_store
import subscription
import account as account_mod
import cloud_auth
import transit
from db import Job, init_db, get_session, select, utcnow
import scraper
import autopilot
import tg
import telegram_store
from apscheduler.schedulers.background import BackgroundScheduler

PROFILE_REQUIRED = [
    ("first_name", "Имя"),
    ("last_name", "Фамилия"),
    ("email", "Email"),
    ("phone", "Телефон"),
    ("address", "Адрес"),
    ("zip", "Индекс"),
    ("city", "Город"),
    ("country", "Страна"),
]

SAFE_JOB_STATUSES = {"new", "seen", "applied", "hidden", "interview", "offer", "rejected", "closed"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    else:
        host = host.split(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1"}


def _is_loopback_url(value: str) -> bool:
    try:
        return _is_loopback_host(urlsplit(value).hostname or "")
    except Exception:
        return False


def _allowed_local_write(request: Request) -> bool:
    """Block cross-site form/fetch writes against the local desktop server."""
    if request.method.upper() not in UNSAFE_METHODS:
        return True
    if not _is_loopback_host(request.headers.get("host", "")):
        return False
    origin = request.headers.get("origin", "")
    if origin:
        return _is_loopback_url(origin)
    referer = request.headers.get("referer", "")
    if referer:
        return _is_loopback_url(referer)
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    return True


def _url_with_system_response(url: str, notice: str = "", error: str = "") -> str:
    """Append one short UI response to a local redirect target."""
    target = url or "/"
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("notice", None)
    query.pop("error", None)
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def _redirect_back(
    request: Request,
    fallback: str = "/",
    notice: str = "",
    error: str = "",
) -> RedirectResponse:
    return RedirectResponse(
        _url_with_system_response(request.headers.get("referer") or fallback, notice, error),
        status_code=303,
    )

# --- автообновление вакансий: каждые 30 минут + при старте, если данные устарели ---
_sync_lock = threading.Lock()
_sync_state = {"running": False, "last_error": "", "last_scan": 0.0}
_scheduler = None  # BackgroundScheduler; нужен, чтобы знать время следующей проверки

# частота фонового скана вакансий: автопилот включён — проверяем часто (почти в
# реальном времени, чтобы ловить новые вакансии сразу), выключен — редко (только
# чтобы база не устаревала). Источник — лёгкий Algolia API, частый опрос допустим.
AUTOPILOT_SCAN_MIN = 3
IDLE_SCAN_MIN = 30


def _scan_interval_min() -> int:
    """Текущий интервал скана в минутах по состоянию автопилота."""
    try:
        return AUTOPILOT_SCAN_MIN if autopilot.get_rule().get("enabled") else IDLE_SCAN_MIN
    except Exception:  # noqa: BLE001
        return IDLE_SCAN_MIN


def _reschedule_autopilot_scan() -> None:
    """Подстроить частоту фонового скана под состояние автопилота. Вызывается
    при каждом включении/выключении автопилота или автоотправки."""
    if _scheduler is None:
        return
    try:
        _scheduler.reschedule_job("auto_sync", trigger="interval", minutes=_scan_interval_min())
    except Exception as e:  # noqa: BLE001
        print(f"автопилот: не удалось перенастроить интервал скана — {e}")


def _sync_jobs():
    """Обновляет базу вакансий. Не запускается параллельно сам с собой."""
    if not _sync_lock.acquire(blocking=False):
        return
    _sync_state["running"] = True
    try:
        scraper.sync()
        _sync_state["last_error"] = ""
        autopilot.scan_and_notify()  # автопилот: уведомить о новых совпадениях
        # автоотправка (фаза 3, по умолчанию ВЫКЛ): отправляет ТОЛЬКО при явно
        # включённом auto_submit, в пределах дневного лимита и только новые
        autopilot.auto_submit_tick(lambda ids: _launch_salling_apply(ids, submit=True))
        _tg_offer_tick()  # режим «по разрешению»: спросить в Telegram про новые подходящие
    except Exception as e:
        _sync_state["last_error"] = str(e)[:200]
        print(f"автообновление: ошибка — {e}")
    finally:
        _sync_state["last_scan"] = time.time()  # отметка «когда последний раз проверяли»
        _sync_state["running"] = False
        _sync_lock.release()


def _autopilot_status_payload() -> dict:
    """Полная сводка для живого монитора автопилота (главная опрашивает её)."""
    st = autopilot.status()
    st["running"] = _sync_state["running"]
    st["error"] = _sync_state.get("last_error") or ""
    # время последней проверки. После перезапуска процесса счётчик в памяти
    # сбрасывается — тогда берём момент последнего обновления базы (last_seen),
    # чтобы монитор не врал «ещё не проверял», когда данные на самом деле свежие.
    last = _sync_state.get("last_scan") or 0.0
    if not last:
        age = _data_age_minutes()
        if age is not None:
            last = time.time() - age * 60
    st["last_scan"] = last
    st["every_min"] = _scan_interval_min()
    nxt = 0.0
    try:
        if _scheduler is not None:
            job = _scheduler.get_job("auto_sync")
            if job and job.next_run_time:
                nxt = job.next_run_time.timestamp()
    except Exception:  # noqa: BLE001
        nxt = 0.0
    st["next_scan"] = nxt
    st["now"] = time.time()  # серверное «сейчас» — фронт считает дельты от него
    return st


def _data_age_minutes() -> int | None:
    """Сколько минут назад вакансии обновлялись (по last_seen в базе)."""
    with get_session() as s:
        last = s.exec(select(func.max(Job.last_seen))).one()
    if not last:
        return None
    return max(0, int((utcnow() - last).total_seconds() // 60))


# ── Telegram: фоновый слушатель (long polling) ──────────────────────────
# Один поток на всё приложение: ловит /start (привязка chat_id) и нажатия
# кнопок ✅/❌ под карточками вакансий. Вся работа с Telegram — в модуле tg;
# когда дойдём до облака, переносить в облако нужно будет только его.
_tg_thread = None
_tg_stop = threading.Event()


def _tg_handle_decision(cb: dict, data: str, token: str) -> None:
    """Нажата кнопка под карточкой вакансии (формат data: 'ap_yes:<id>' / 'ap_no:<id>').
    Реальная подача по ✅ подключается в режиме «по разрешению» (следующий шаг)."""
    msg = cb.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    if not (chat and mid and ":" in data):
        return
    action, job_id = data.split(":", 1)
    result = autopilot.tg_decide(job_id, approve=(action == "ap_yes"),
                                 launcher=lambda ids: _launch_salling_apply(ids, submit=True))
    tg.edit_message(token, chat, mid, result or "Готово.")


def _tg_handle_update(u: dict) -> None:
    try:
        token = telegram_store.get_token()
        if not token:
            return
        # 1) обычное сообщение → привязка: запоминаем chat_id отправителя
        msg = u.get("message")
        if msg and msg.get("chat"):
            chat = msg["chat"]
            if not telegram_store.get_chat():
                name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                telegram_store.set_chat(chat["id"], name or chat.get("username", ""))
                tg.send_message(token, chat["id"],
                                "✅ <b>Telegram привязан к WexFlow!</b>\n\n"
                                "Я буду присылать сюда подходящие вакансии, а ты подтверждаешь "
                                "подачу одной кнопкой:\n"
                                "✅ <b>Подать</b> — WexFlow подаст заявку за тебя\n"
                                "❌ <b>Пропустить</b> — пройдём мимо\n\n"
                                "🔔 Жди первую вакансию — пришлю, как только найдётся.")
            return
        # 2) нажатие кнопки ✅/❌ под карточкой вакансии
        cb = u.get("callback_query")
        if cb:
            data = cb.get("data") or ""
            if data == "ap_demo":  # кнопки в примере-«проверочном» ничего не делают
                tg.answer_callback(token, cb["id"], "Это пример — реальная заявка не отправляется.")
                return
            tg.answer_callback(token, cb["id"])
            _tg_handle_decision(cb, data, token)
    except Exception as e:  # noqa: BLE001 — слушатель не должен падать
        print(f"telegram: ошибка обработки апдейта — {e}")


def _tg_poller_loop() -> None:
    """Опрашивает облако: какие решения (✅/❌) принял пользователь под карточками,
    и выполняет их локально (подать/пропустить).

    Заменяет старый getUpdates: бот теперь общий и работает через webhook, поэтому
    нажатия кнопок собирает облако, а приложение забирает готовые решения."""
    while not _tg_stop.is_set():
        try:
            if account_mod.is_signed_in():
                for d in cloud_auth.fetch_decisions():
                    jid = d.get("jobId")
                    action = d.get("action")
                    if not jid or jid == "__demo__" or action not in ("submit", "skip"):
                        continue
                    autopilot.tg_decide(
                        jid, approve=(action == "submit"),
                        launcher=lambda ids: _launch_salling_apply(ids, submit=True),
                    )
        except Exception as e:  # noqa: BLE001 — слушатель не должен падать
            print(f"telegram(cloud): ошибка опроса решений — {e}")
        _tg_stop.wait(4)


def _ensure_tg_poller() -> None:
    """Запустить слушатель Telegram, если он ещё не работает."""
    global _tg_thread
    if _tg_thread and _tg_thread.is_alive():
        return
    _tg_stop.clear()
    _tg_thread = threading.Thread(target=_tg_poller_loop, daemon=True, name="tg-poller")
    _tg_thread.start()


TG_MAX_PER_SCAN = 3  # не больше карточек на подтверждение за один фоновый скан


_title_ru_cache: dict[str, str] = {}


def _title_ru(title: str) -> str:
    """Русский перевод названия вакансии для карточки (для тех, кто не знает датский).
    Кэшируется в памяти; при сбое перевода тихо возвращает пусто."""
    title = (title or "").strip()
    if not title:
        return ""
    if title in _title_ru_cache:
        return _title_ru_cache[title]
    ru = ""
    try:
        raw = translator.translate_to_ru(title) or ""
        ru = re.sub(r"<[^>]+>", "", raw).strip()  # убрать html-теги, оставить текст
    except Exception:  # noqa: BLE001 — перевод не должен ломать карточку
        ru = ""
    _title_ru_cache[title] = ru
    return ru


def _tg_card(job) -> str:
    """Красивая карточка вакансии для Telegram (HTML)."""
    def e(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = [f"🏷 <b>{e(job.title)}</b>"]
    ru_title = _title_ru(job.title)
    if ru_title and ru_title.lower() != (job.title or "").strip().lower():
        lines.append(f"   ↳ <i>{e(ru_title)}</i>")
    if job.brand:
        lines.append(f"🏪 {e(labels.brand(job.brand))}")
    # местоположение + расстояние от дома, если знаем
    loc = (job.city or "").strip()
    try:
        home = settings_store.get_home()
        if home and job.lat is not None and job.lon is not None:
            km = round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon))
            loc = f"{loc} · ~{km} км от дома" if loc else f"~{km} км от дома"
    except Exception:  # noqa: BLE001
        pass
    if loc:
        lines.append(f"📍 {e(loc)}")
    if job.hours:
        lines.append(f"🕒 {e(job.hours)} ч/нед")
    if job.application_link:
        lines.append(f'🔗 <a href="{e(job.application_link)}">Открыть вакансию на сайте</a>')
    return ("🆕 <b>Новая подходящая вакансия</b>\n"
            "──────────────\n"
            + "\n".join(lines)
            + "\n──────────────\n"
            "Подать заявку от твоего имени?")


def _tg_offer_jobs(jobs) -> dict:
    """Отправить список вакансий в облачного бота. Безопасно: сама заявка не
    уходит, пока пользователь не нажмёт ✅ в Telegram."""
    sent = 0
    last_error = ""
    for job in jobs:
        r = cloud_auth.offer(_tg_card(job), job.id)
        if r and r.get("ok"):
            autopilot.tg_pending_add(job.id, r.get("messageId"))
            autopilot.log_event("info", f"TG: спросил разрешение — {job.title}")
            sent += 1
        else:
            last_error = (r or {}).get("error") or "не удалось отправить карточку"
            autopilot.log_event("info", f"TG: не отправилось — {last_error}")
            break
    return {"sent": sent, "error": last_error}


def _tg_offer_tick(include_existing: bool = False, ignore_schedule: bool = False) -> dict:
    """После скана: если включён режим «по разрешению» и Telegram привязан —
    отправить карточки НОВЫХ подходящих вакансий с кнопками ✅/❌."""
    try:
        if not autopilot.get_rule().get("tg_approval"):
            return {"sent": 0, "error": "режим Telegram выключен"}
        if not ignore_schedule and not autopilot.within_schedule():
            return {"sent": 0, "error": "сейчас вне часов активности"}
        if not account_mod.is_signed_in():
            return {"sent": 0, "error": "сначала войди через Telegram в разделе Аккаунт"}
        jobs = autopilot.tg_eligible(limit=TG_MAX_PER_SCAN, include_existing=include_existing)
        if not jobs:
            return {"sent": 0, "error": ""}
        result = _tg_offer_jobs(jobs)
        result["remaining"] = len(autopilot.tg_eligible(10000, include_existing=include_existing))
        return result
    except Exception as e:  # noqa: BLE001 — не должно ронять фоновый скан
        print(f"telegram(cloud): ошибка отправки карточек — {e}")
        return {"sent": 0, "error": str(e)[:120]}


@asynccontextmanager
async def _lifespan(app):
    global _scheduler
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_sync_jobs, "interval", minutes=IDLE_SCAN_MIN, id="auto_sync")
    sched.start()
    _scheduler = sched
    _reschedule_autopilot_scan()  # подстроить интервал под текущее состояние автопилота
    _ensure_tg_poller()           # слушатель Telegram (привязка + кнопки ✅/❌)
    age = _data_age_minutes()
    if age is None or age >= 30:  # данные устарели — обновить сразу, в фоне
        threading.Thread(target=_sync_jobs, daemon=True).start()
    else:
        # Если база свежая, всё равно сразу проверим Telegram-очередь:
        # пользователь запустил приложение и ожидает уведомления без ожидания интервала.
        threading.Thread(target=_tg_offer_tick, daemon=True).start()
    yield
    _tg_stop.set()
    sched.shutdown(wait=False)


app = FastAPI(title="Salling Jobs", lifespan=_lifespan)


@app.middleware("http")
async def _no_cache(request, call_next):
    if not _allowed_local_write(request):
        return JSONResponse({"ok": False, "error": "blocked cross-site request"}, status_code=403)
    # WebView2 кэширует страницы/скрипты агрессивно — для desktop-приложения это
    # вредно (после обновления показывает старый интерфейс). Запрещаем кэш.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/api/version")
def api_version():
    """Версия и результат проверки обновлений — для баннера и диагностики."""
    try:
        import version
        import update_check
        return {
            "version": version.__version__,
            "repo": version.GITHUB_REPO,
            "update": update_check.check(),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"версия: не удалось проверить обновления — {exc}")
        return {"version": "dev", "repo": "", "update": None, "error": "version unavailable"}


app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
templates.env.globals["brand_label"] = labels.brand
templates.env.globals["L"] = labels
# Текущий тариф доступен во всех шаблонах (бейдж в боковом меню и т.п.).
templates.env.globals["current_plan"] = subscription.plan
templates.env.globals["plan_label"] = lambda p=None: subscription.PLANS.get(p or subscription.plan(), subscription.PLANS["free"])["name"]
# Флаг видимости витрины подписки/аккаунта (в публичном релизе скрыто).
templates.env.globals["show_billing"] = lambda: subscription.SHOW_BILLING
try:
    import changelog as _changelog
    import version as _version
    templates.env.globals["CHANGELOG"] = _changelog.ENTRIES
    templates.env.globals["APP_VERSION"] = _version.__version__
except Exception:  # noqa: BLE001 — без журнала изменений окно просто не показывается
    templates.env.globals["CHANGELOG"] = []
    templates.env.globals["APP_VERSION"] = ""
init_db()


def _job_address(job: Job) -> str:
    parts = [
        job.street or "",
        " ".join(p for p in [job.zip or "", job.city or ""] if p),
        job.country or "",
    ]
    return ", ".join(p for p in parts if p)


def _maps_url(job: Job, home: dict | None = None) -> str:
    destination = _job_address(job)
    if not destination and job.lat is not None and job.lon is not None:
        destination = f"{job.lat},{job.lon}"
    origin = ""
    if home:
        origin = home.get("lookup_address") or home.get("address") or ""
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(destination)}"
        "&travelmode=transit"
    )


def _profile_missing(profile: dict) -> list[str]:
    return [label for key, label in PROFILE_REQUIRED if not str(profile.get(key) or "").strip()]


def _profile_choices() -> tuple[list[str], list[str]]:
    city_set = {
        "København", "København K", "København N", "København S", "København V",
        "København Ø", "København NV", "København SV", "Frederiksberg",
        "Brønshøj", "Valby", "Vanløse", "Rødovre", "Hvidovre", "Herlev",
        "Glostrup", "Ballerup", "Taastrup", "Kastrup", "Aarhus", "Aarhus C",
        "Odense", "Aalborg", "Esbjerg", "Randers", "Kolding", "Vejle",
        "Roskilde", "Køge", "Greve", "Ishøj", "Kgs. Lyngby", "Hillerød",
        "Helsingør", "Næstved", "Slagelse", "Holbæk", "Svendborg",
        "Sønderborg", "Viborg", "Horsens", "Silkeborg", "Herning",
        "Fredericia", "Hjørring", "Skive", "Ringsted", "Haderslev",
        "Skanderborg", "Nyborg", "Aabenraa", "Kalundborg", "Nørresundby",
        "Farum", "Birkerød", "Værløse", "Allerød", "Solrød Strand",
        "Frederikssund", "Frederiksværk", "Hundested", "Tårnby", "Dragør",
        "Albertslund", "Brøndby", "Hedehusene", "Nivå", "Humlebæk",
        "Fredensborg", "Espergærde", "Rønne", "Nykøbing F", "Nakskov",
        "Vordingborg", "Haslev", "Sorø", "Ringkøbing", "Holstebro",
        "Struer", "Ikast", "Brande", "Billund", "Vejen", "Middelfart",
        "Assens", "Faaborg", "Middelfart", "Frederikshavn", "Thisted",
        "Hobro", "Grenaa", "Ebeltoft", "Skagen", "Ribe", "Tønder",
        "Varde", "Brønderslev", "Lemvig", "Odder", "Nykøbing Mors",
        "Lillerød", "Charlottenlund", "Hellerup", "Gentofte", "Virum",
        "Søborg", "Bagsværd", "Lyngby", "Måløv", "Smørum", "Ølstykke",
    }
    for values in labels.CITY_GROUPS.values():
        city_set.update(values)
    city_set.update(labels.CITY_ALIASES.values())
    try:
        with get_session() as s:
            city_set.update(c for c in s.exec(select(Job.city)).all() if c)
    except Exception:
        pass
    countries = ["Danmark", "Sverige", "Norge", "Tyskland", "Polen"]
    return sorted(city_set, key=str.casefold), countries


def _profile_file_info(profile: dict) -> dict:
    cv_status = profile_store.file_status(profile.get("cv_path", ""))
    cover_status = profile_store.file_status(profile.get("cover_letter_path", ""))
    return {
        "cv_label": profile_store.file_label(profile.get("cv_path", "")),
        "cv_status": cv_status,
        "cv_url": "/settings/file/cv" if cv_status == "ok" else "",
        "cover_label": profile_store.file_label(profile.get("cover_letter_path", "")),
        "cover_status": cover_status,
        "cover_url": "/settings/file/cover" if cover_status == "ok" else "",
    }


def _text(html: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip().lower()


def _job_facts(job: Job, distance: float | None) -> list[dict]:
    facts = []
    if job.brand:
        facts.append({"label": "Бренд", "value": labels.brand(job.brand), "kind": ""})
    address = _job_address(job)
    if address:
        facts.append({"label": "Адрес", "value": address, "kind": "place"})
    if job.region:
        facts.append({"label": "Регион", "value": labels.REGION.get(job.region, job.region), "kind": ""})
    if distance is not None:
        facts.append({"label": "От дома", "value": f"≈ {distance} км по прямой", "kind": "distance"})
    if job.hours:
        facts.append({"label": "Часы", "value": f"{job.hours} ч/нед", "kind": "time"})
    if job.employment_type:
        facts.append({
            "label": "Занятость",
            "value": labels.EMPLOYMENT.get(job.employment_type, job.employment_type),
            "kind": "work",
        })
    if job.job_level:
        facts.append({"label": "Уровень", "value": labels.LEVEL.get(job.job_level, job.job_level), "kind": "level"})
    if job.job_level == "employeeUnder18" or "under 18" in (job.title or "").lower():
        facts.append({"label": "Возраст", "value": "позиция для сотрудников до 18 лет", "kind": "important"})
    if job.start_date:
        facts.append({"label": "Старт", "value": job.start_date, "kind": "date"})
    if job.published:
        facts.append({"label": "Опубликовано", "value": job.published[:10], "kind": "muted"})
    if job.pay_rate:
        facts.append({"label": "Ставка", "value": job.pay_rate, "kind": "money"})
    else:
        facts.append({"label": "Ставка", "value": "не указана в объявлении", "kind": "muted"})
    if job.categories:
        cat_labels = [labels.CATEGORY.get(c, c) for c in job.categories.split(",") if c]
        if cat_labels:
            facts.append({"label": "Категория", "value": ", ".join(cat_labels[:3]), "kind": "category"})

    body = _text(job.description)
    signals = []
    if any(w in body for w in ["oplæring", "uddannelse", "kursus", "training"]):
        signals.append("есть обучение/ввод в работу")
    if any(w in body for w in ["rabat", "personalerabat", "medarbejderrabat", "discount"]):
        signals.append("упоминаются скидки/льготы")
    if any(w in body for w in ["weekend", "aften", "nat", "morgen"]):
        signals.append("в тексте есть смены/вечер/ночь/выходные")
    if any(w in body for w in ["ansvar", "selvstændig", "team", "service", "kunde"]):
        signals.append("важны сервис, команда и ответственность")
    if signals:
        facts.append({"label": "Из описания", "value": "; ".join(signals[:3]), "kind": "note"})
    return facts


def _distinct(session, column):
    rows = session.exec(select(column).distinct()).all()
    return sorted([r for r in rows if r])


def _active_counts(session):
    rows = session.exec(select(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))).all()
    counts = {
        "brand": Counter(),
        "region": Counter(),
        "employment": Counter(),
        "level": Counter(),
        "category": Counter(),
        "city": Counter(),
    }
    for job in rows:
        if job.brand:
            counts["brand"][job.brand] += 1
        if job.region:
            counts["region"][job.region] += 1
        if job.employment_type:
            counts["employment"][job.employment_type] += 1
        if job.job_level:
            counts["level"][job.job_level] += 1
        if job.city:
            counts["city"][job.city] += 1
        for cat in (job.categories or "").split(","):
            if cat:
                counts["category"][cat] += 1
    return counts


def _seven_eleven_state() -> dict:
    """Живые данные модуля 7-Eleven для карточки хаба (читаем его профиль)."""
    import pathlib
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or str(pathlib.Path.home() / "AppData" / "Roaming")
        seven_dir = pathlib.Path(base) / "WexFlow" / "seven11"
    else:
        seven_dir = pathlib.Path(r"C:\seven11-apply")
    try:
        d = json.loads((seven_dir / "profiles" / "me.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет профиля → пустое состояние
        return {"stores": 0, "profile_ready": False, "name": ""}
    loc = d.get("location", {}) or {}
    sel = loc.get("selected_addresses") or loc.get("preferred_addresses") or []
    p = d.get("personal", {}) or {}
    ans = d.get("answers", {}) or {}
    att = d.get("attachments", {}) or {}
    ready = bool(
        p.get("first_name") and p.get("last_name")
        and "@" in str(p.get("email") or "")
        and len(str(p.get("phone") or "")) >= 8
        and str(att.get("cv_path") or "")
        and len(str(ans.get("why_7eleven") or "").strip()) >= 10
    )
    return {"stores": len(sel), "profile_ready": ready, "name": str(p.get("first_name") or "")}


@app.get("/hub", response_class=HTMLResponse)
def hub(request: Request):
    with get_session() as s:
        total_jobs = s.exec(select(func.count(Job.id))).one() or 0
        active_jobs = (
            s.exec(
                select(func.count(Job.id)).where(
                    Job.status.not_in(["closed", "hidden", "applied"])
                )
            ).one()
            or 0
        )
        applied_jobs = s.exec(select(func.count(Job.id)).where(Job.status == "applied")).one() or 0
        last_applied = None
        try:
            last = s.exec(
                select(Job).where(Job.status == "applied").order_by(Job.modified.desc())
            ).first()
            if last:
                last_applied = {"title": last.title, "brand": last.brand}
        except Exception:  # noqa: BLE001
            last_applied = None

    seven = _seven_eleven_state()
    try:
        name = (profile_store.load_profile().get("first_name") or "").strip() or seven["name"]
    except Exception:  # noqa: BLE001
        name = seven["name"]

    ap_rule = autopilot.get_rule()
    ap = {
        "enabled": bool(ap_rule.get("enabled")),
        "count": autopilot.match_count() if ap_rule.get("enabled") else 0,
    }

    return templates.TemplateResponse(
        "hub.html",
        {
            "request": request,
            "total_jobs": total_jobs,
            "active_jobs": active_jobs,
            "applied_jobs": applied_jobs,
            "data_age_min": _data_age_minutes(),
            "sync_running": _sync_state["running"],
            "seven": seven,
            "last_applied": last_applied,
            "user_name": name,
            "autopilot": ap,
            "autopilot_status": _autopilot_status_payload(),
            "subscription": subscription.status(),
        },
    )


@app.get("/apply-by-link")
def apply_by_link():
    return RedirectResponse("http://127.0.0.1:8078/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    city: str = "",
    brand: str = "",
    region: str = "",
    employment_type: str = "",
    category: str = "",
    job_level: str = "",
    status: str = "active",
    sort: str = "",
    radius: str = "",
    group: str = "",
    show_applied: str = "",
    page: str = "1",
    geoerror: str = "",
    batch: str = "",
    mode: str = "",
    reset: str = "",
):
    # запоминаем фильтры в cookie и восстанавливаем при заходе на голую "/"
    _fkeys = ["q", "city", "brand", "region", "employment_type", "category", "job_level", "status", "sort", "show_applied"]
    if not request.query_params and not reset:
        raw = request.cookies.get("saling_filters")
        if raw:
            try:
                saved = json.loads(raw)
                q = saved.get("q", q); city = saved.get("city", city)
                brand = saved.get("brand", brand); region = saved.get("region", region)
                employment_type = saved.get("employment_type", employment_type)
                category = saved.get("category", category); job_level = saved.get("job_level", job_level)
                status = saved.get("status", status); sort = saved.get("sort", sort)
                show_applied = saved.get("show_applied", show_applied)
                radius = saved.get("radius", radius); group = saved.get("group", group)
            except Exception:
                pass

    home = settings_store.get_home()
    if not sort:
        sort = "distance" if home else "published"
    with get_session() as s:
        stmt = select(Job)
        if status == "active":
            excluded_statuses = ["closed", "hidden"]
            if not show_applied:
                excluded_statuses.append("applied")
            stmt = stmt.where(Job.status.not_in(excluded_statuses))
        elif status:
            stmt = stmt.where(Job.status == status)
        city_lookup = labels.city_query(city)
        city_terms = labels.city_terms(city)
        if city_terms:  # русский/англ/датский алиас, район или агломерация
            cond = None
            for term in city_terms:
                c = Job.city.ilike(f"%{term.strip()}%")
                cond = c if cond is None else (cond | c)
            stmt = stmt.where(cond)
        brand_code = labels.resolve(labels.BRANDS, brand)
        if brand_code:
            stmt = stmt.where(Job.brand == brand_code)
        region_code = labels.resolve(labels.REGION, region)
        if region_code:
            stmt = stmt.where(Job.region == region_code)
        employment_code = labels.resolve(labels.EMPLOYMENT, employment_type)
        if employment_code:
            stmt = stmt.where(Job.employment_type == employment_code)
        level_code = labels.resolve(labels.LEVEL, job_level)
        if level_code:
            stmt = stmt.where(Job.job_level == level_code)
        category_code = labels.resolve(labels.CATEGORY, category)
        if category_code:
            stmt = stmt.where(
                (Job.categories == category_code)
                | Job.categories.like(f"{category_code},%")
                | Job.categories.like(f"%,{category_code},%")
                | Job.categories.like(f"%,{category_code}")
            )
        if q:  # умный поиск: русский запрос расширяем датскими синонимами
            import ru_search
            terms = ru_search.expand(q)
            city_term = labels.city_query(q)
            if city_term and city_term != q.strip():
                terms.append(city_term)
            cond = None
            for t in terms:
                like = f"%{t}%"
                c = (
                    Job.title.ilike(like)
                    | Job.description.ilike(like)
                    | Job.city.ilike(like)
                    | Job.street.ilike(like)
                )
                cond = c if cond is None else (cond | c)
            if cond is not None:
                stmt = stmt.where(cond)

        if sort == "title":
            stmt = stmt.order_by(Job.title)
        elif sort == "city":
            stmt = stmt.order_by(Job.city)
        else:  # published (по умолчанию) и distance до пост-сортировки
            stmt = stmt.order_by(Job.published.desc())

        jobs = list(s.exec(stmt).all())

        cities = _distinct(s, Job.city)
        brands = _distinct(s, Job.brand)
        regions = _distinct(s, Job.region)
        etypes = _distinct(s, Job.employment_type)
        levels = _distinct(s, Job.job_level)
        cat_rows = _distinct(s, Job.categories)
        cats = sorted({c for row in cat_rows for c in row.split(",") if c})
        counts = _active_counts(s)
        total_active = s.exec(
            select(func.count()).select_from(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))
        ).one()
        applied_count = s.exec(
            select(func.count()).select_from(Job).where(Job.status == "applied")
        ).one()
        last = s.exec(select(func.max(Job.last_seen))).one()

    # расстояние от дома (если задан) + сортировка по близости
    distances = {}
    if home:
        for j in jobs:
            if j.lat is not None and j.lon is not None:
                distances[j.id] = round(
                    geo.haversine_km(home["lat"], home["lon"], j.lat, j.lon), 1
                )
        # фильтр по радиусу (только в пределах N км от дома)
        try:
            radius_km = float(radius) if radius else 0
        except ValueError:
            radius_km = 0
        if radius_km > 0:
            jobs = [j for j in jobs if j.id in distances and distances[j.id] <= radius_km]
        if sort == "distance":
            jobs.sort(key=lambda j: distances.get(j.id, float("inf")))

    # --- группировка по магазину (адрес) ---
    groups = []
    if group:
        bucket, order = {}, []
        for j in jobs:
            key = (j.brand, j.street, j.zip, j.city)
            if key not in bucket:
                bucket[key] = {
                    "brand": j.brand, "street": j.street, "zip": j.zip, "city": j.city,
                    "region": j.region, "country": j.country, "dist": distances.get(j.id),
                    "first": j, "jobs": [],
                }
                order.append(key)
            g = bucket[key]
            g["jobs"].append(j)
            d = distances.get(j.id)
            if d is not None and (g["dist"] is None or d < g["dist"]):
                g["dist"] = d
        groups = [bucket[k] for k in order]

    # --- пагинация ---
    PER_PAGE = 24 if group else 60
    items = groups if group else jobs
    total = len(items)
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    items = items[(page - 1) * PER_PAGE: page * PER_PAGE]
    if group:
        groups = items
        jobs = [j for g in groups for j in g["jobs"]]  # для maps_urls
    else:
        jobs = items

    maps_urls = {j.id: _maps_url(j, home) for j in jobs}

    # отсортировать опции по русской подписи
    def by_label(items, mapping):
        return sorted(items, key=lambda k: labels.bi(mapping, k).lower())

    resp = templates.TemplateResponse("index.html", {
        "request": request, "jobs": jobs, "count": len(jobs),
        "groups": groups, "group": group,
        "total_filtered": total, "page": page, "pages": pages, "per_page": PER_PAGE,
        "cities": cities,
        # опции отсортированы по популярности (частые сверху) — удобнее выбирать
        "brands": [
            (b, labels.with_count(labels.brand(b), counts["brand"][b]))
            for b in sorted(brands, key=lambda k: -counts["brand"][k])
        ],
        "regions": [
            (r, labels.with_count(labels.REGION.get(r, r), counts["region"][r]))
            for r in sorted(regions, key=lambda k: -counts["region"][k])
        ],
        "etypes": [
            (e, labels.with_count(labels.EMPLOYMENT.get(e, e), counts["employment"][e]))
            for e in sorted(etypes, key=lambda k: -counts["employment"][k])
        ],
        "levels": [
            (l, labels.with_count(labels.LEVEL.get(l, l), counts["level"][l]))
            for l in sorted(levels, key=lambda k: -counts["level"][k])
        ],
        "cats": [
            (c, labels.with_count(labels.CATEGORY.get(c, c), counts["category"][c]))
            for c in sorted(cats, key=lambda k: -counts["category"][k])
        ],
        "city_suggestions": [
            (city, labels.with_count(city, count))
            for city, count in counts["city"].most_common(120)
        ],
        "f": (_f := {"q": q,
              "city": city,
              "brand": brand_code,
              "region": region_code,
              "category": category_code,
              "employment_type": employment_code,
              "job_level": level_code,
              "status": status, "sort": sort, "radius": radius, "group": group,
              "show_applied": show_applied}),
        "total_active": total_active, "applied_count": applied_count, "last_update": last,
        "autopilot": autopilot.get_rule(),
        "autopilot_count": autopilot.match_count(),
        "data_age_min": (max(0, int((utcnow() - last).total_seconds() // 60)) if last else None),
        "sync_running": _sync_state["running"],
        "sync_error": _sync_state["last_error"],
        "home": home, "distances": distances, "geoerror": geoerror,
        "presets": settings_store.get_presets(),
        "batch": batch, "batch_mode": mode,
        "apply_files": {
            "cv": profile_store.file_label((_apply_profile := profile_store.load_profile()).get("cv_path", "")),
            "cover": profile_store.file_label(_apply_profile.get("cover_letter_path", "")),
        },
        "maps_urls": maps_urls,
        "resolved": {
            "city": (
                f"{city_lookup} + районы" if city and len(city_terms) > 1
                else (city_lookup if city and city_lookup != city.strip() else "")
            ),
            "brand": brand_code if brand and brand_code != brand.strip() else "",
            "region": labels.bi(labels.REGION, region_code) if region and region_code and region_code != region.strip() else "",
            "category": labels.bi(labels.CATEGORY, category_code) if category and category_code and category_code != category.strip() else "",
            "employment_type": labels.bi(labels.EMPLOYMENT, employment_code) if employment_type and employment_code and employment_code != employment_type.strip() else "",
            "job_level": labels.bi(labels.LEVEL, level_code) if job_level and level_code and level_code != job_level.strip() else "",
        },
    })
    # запоминаем выбранные фильтры на 30 дней (восстановятся при заходе на "/")
    if reset:
        resp.delete_cookie("saling_filters")
    else:
        resp.set_cookie("saling_filters", json.dumps(_f), max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/job/{job_id}/status")
def set_status(job_id: str, request: Request, status: str = Form(...)):
    if status not in SAFE_JOB_STATUSES:
        return _redirect_back(request, "/", error="Неизвестный статус вакансии.")
    status_labels = {
        "applied": "Статус обновлён: вакансия отмечена как поданная.",
        "hidden": "Вакансия скрыта. Её можно вернуть из статуса «Скрытые».",
        "seen": "Статус сброшен: вакансия снова в просмотренных.",
        "interview": "Статус обновлён: собеседование.",
        "offer": "Статус обновлён: оффер.",
        "rejected": "Статус обновлён: отказ.",
    }
    with get_session() as s:
        job = s.get(Job, job_id)
        if job:
            job.status = status
            if status == "applied":
                job.applied_at = utcnow()
            s.add(job)
            s.commit()
        else:
            return _redirect_back(request, "/", error="Вакансия не найдена. Возможно, список обновился.")
    return _redirect_back(request, "/", notice=status_labels.get(status, "Статус вакансии обновлён."))


@app.post("/refresh")
def refresh(request: Request):
    # обновление уходит в фон: страница не виснет, индикатор в шапке показывает
    # «обновляется…», список сам перезагрузится по окончании
    threading.Thread(target=_sync_jobs, daemon=True).start()
    return _redirect_back(request, "/", notice="Обновление вакансий запущено. Список сам перезагрузится, когда появятся свежие данные.")


@app.post("/presets/save")
def save_preset(request: Request, name: str = Form(...), query: str = Form("")):
    settings_store.add_preset(name, query)
    return _redirect_back(request, "/", notice=f"Фильтр «{name.strip() or 'без названия'}» сохранён.")


@app.post("/presets/delete")
def delete_preset(request: Request, name: str = Form(...)):
    settings_store.delete_preset(name)
    return _redirect_back(request, "/", notice=f"Фильтр «{name.strip() or 'без названия'}» удалён.")


@app.post("/set-home")
def set_home(request: Request, address: str = Form(...)):
    lookup = labels.localize_address(address)
    coords = geo.geocode_address(lookup)
    ref = request.headers.get("referer")
    if coords:
        settings_store.set_home(address, coords[0], coords[1], lookup)
        target = _url_with_system_response(ref or "/?sort=distance", notice="Домашний адрес сохранён. Теперь доступны сортировка и фильтр по расстоянию.")
        return RedirectResponse(target, status_code=303)
    sep = "&" if (ref and "?" in ref) else "?"
    target = (ref + sep + "geoerror=1") if ref else "/?geoerror=1"
    target = _url_with_system_response(target, error="Не удалось распознать адрес. Попробуй улицу с номером дома, город или индекс.")
    return RedirectResponse(target, status_code=303)


@app.post("/set-home-coords")
def set_home_coords(lat: float = Form(...), lon: float = Form(...)):
    """Сохранить дом по координатам (из кнопки «определить местоположение»).
    Координаты превращаем в читаемый датский адрес через reverse-геокод."""
    label = geo.reverse_geocode(lat, lon) or f"Моё местоположение ({lat:.4f}, {lon:.4f})"
    settings_store.set_home(label, lat, lon, label)
    return {"ok": True, "address": label}


@app.get("/api/address-suggestions")
def address_suggestions(q: str = ""):
    """Живые подсказки домашнего адреса из датской адресной базы."""
    return {"items": geo.suggest_addresses(q)}


@app.get("/api/apply-log")
def apply_log():
    """Хвост лога последней подачи — показывается прямо на странице «Подать»."""
    path = config.DATA_DIR / "apply_last.log"
    if not path.exists():
        return JSONResponse({"ok": False, "lines": []})
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"диагностика подачи: не удалось прочитать лог — {e}")
        return JSONResponse({"ok": False, "error": "diagnostics unavailable", "lines": []})
    return JSONResponse({"ok": True, "lines": lines[-120:]})


@app.get("/api/sync-status")
def api_sync_status():
    """Идёт ли сейчас фоновое обновление вакансий. Главная опрашивает это
    легко и перезагружается ОДИН раз, когда обновление закончилось — вместо
    того чтобы перезагружать страницу по таймеру снова и снова."""
    return JSONResponse({"running": _sync_state["running"]})


@app.get("/api/autopilot/status")
def api_autopilot_status():
    """Живой статус автопилота для монитора на главной: работает ли, когда
    проверял / следующая проверка, счётчики (нашёл/подготовил/подал) и лента событий."""
    return JSONResponse(_autopilot_status_payload())


@app.post("/api/autopilot/scan-now")
def api_autopilot_scan_now():
    """Запустить проверку вручную («Проверить сейчас») — тот же фоновый скан."""
    if _sync_state["running"]:
        return JSONResponse({"ok": False, "error": "уже идёт проверка"})
    threading.Thread(target=_sync_jobs, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/autopilot/toggle")
def api_autopilot_toggle():
    """Пауза/возобновление автопилота (кнопка на странице /autopilot)."""
    new_enabled = not bool(autopilot.get_rule().get("enabled"))
    autopilot.save_rule({"enabled": new_enabled})
    if new_enabled:
        # как при сохранении правила: текущие совпадения считаем «виденными»,
        # чтобы реагировать только на НОВЫЕ вакансии позже (без спама)
        autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
        autopilot.log_event("info", "Автопилот включён")
    else:
        autopilot.log_event("info", "Автопилот поставлен на паузу")
    _reschedule_autopilot_scan()  # подстроить частоту фонового скана
    return JSONResponse({"ok": True, "enabled": new_enabled})


@app.post("/api/autopilot/mode")
async def api_autopilot_mode(request: Request):
    """Единый режим работы: off | notify | telegram | auto."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    mode = body.get("mode")
    if mode == "telegram" and not account_mod.is_signed_in():
        return {"ok": False, "error": "Сначала войди через Telegram в разделе «Аккаунт»."}
    if mode not in autopilot.MODES:
        return {"ok": False, "error": "Неизвестный режим."}
    new_mode = autopilot.set_mode(mode)
    if new_mode != "off":
        # охват «только новые»: текущие совпадения — baseline, чтобы не завалить бэклогом
        if (autopilot.get_rule().get("submit_scope") or "new") != "all":
            autopilot.set_autosubmit_baseline()
        autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
    if new_mode == "telegram":
        stats = autopilot.tg_queue_stats()
        autopilot.log_event(
            "info",
            f"Telegram включён: подходит {stats['found']}, новых к отправке {stats['eligible_new']}. "
            "Текущие можно прислать кнопкой в настройках.",
        )
    _reschedule_autopilot_scan()
    return {"ok": True, "mode": new_mode}


def _autopilot_rule_summary(rule: dict) -> list[dict]:
    """Человеко-читаемая сводка «что ищет автопилот» для страницы /autopilot."""
    def _csv(raw, mapping):
        parts = [p.strip() for p in str(raw or "").split(",") if p.strip()]
        return ", ".join(mapping.get(p, p) for p in parts)

    def _nums(raw):
        out_n: list[float] = []
        for p in str(raw or "").split(","):
            p = p.strip()
            try:
                f = float(p)
            except ValueError:
                continue
            if f > 0:
                out_n.append(int(f) if f == int(f) else f)
        return out_n

    out: list[dict] = []
    if rule.get("brand"):
        out.append({"label": "Бренд", "value": _csv(rule["brand"], labels.BRANDS)})
    if rule.get("category"):
        out.append({"label": "Категория", "value": _csv(rule["category"], labels.CATEGORY)})
    if rule.get("employment_type"):
        out.append({"label": "Занятость", "value": _csv(rule["employment_type"], labels.EMPLOYMENT)})
    if rule.get("age"):
        out.append({"label": "Возраст", "value": _csv(rule["age"], {"under18": "до 18 лет", "adult": "от 18 лет"})})
    if rule.get("keywords"):
        out.append({"label": "Ключевые слова", "value": rule["keywords"]})
    km = _nums(rule.get("max_km"))
    if km:
        out.append({"label": "Радиус от дома", "value": f"до {max(km)} км"})
    mh = _nums(rule.get("min_hours"))
    if mh:
        out.append({"label": "Часы в неделю", "value": f"от {min(mh)} ч"})
    ag = _nums(rule.get("max_age_days"))
    if ag:
        out.append({"label": "Свежесть", "value": f"не старше {max(ag)} дн."})
    if rule.get("max_hours"):
        mxh = _nums(rule.get("max_hours"))
        if mxh:
            out.append({"label": "Часов не больше", "value": f"до {max(mxh)} ч"})
    if rule.get("cities"):
        out.append({"label": "Город", "value": rule["cities"]})
    if rule.get("regions"):
        out.append({"label": "Регион", "value": rule["regions"]})
    return out


def _autopilot_profiles_summary(profiles: list[dict]) -> list[dict]:
    """Сводка «что ищет автопилот» по профилям: строка на профиль."""
    rows: list[dict] = []
    for p in profiles:
        parts = [f"{r['label'].lower()}: {r['value']}" for r in _autopilot_rule_summary(p)]
        exclude = [p.get("exclude_brands"), p.get("exclude_cities"), p.get("exclude_keywords")]
        if any(exclude):
            parts.append("есть исключения")
        name = (p.get("name") or "Правило") + ("" if p.get("enabled", True) else " · выкл")
        rows.append({"label": name, "value": "; ".join(parts) or "без ограничений"})
    return rows


@app.get("/autopilot", response_class=HTMLResponse)
def autopilot_page(request: Request):
    rule = autopilot.get_rule()
    return templates.TemplateResponse(
        "autopilot.html",
        {
            "request": request,
            "status": _autopilot_status_payload(),
            "events": autopilot.event_log(),
            "rule_summary": _autopilot_profiles_summary(autopilot.get_profiles(rule)),
            "enabled": bool(rule.get("enabled")),
            "auto_submit": bool(rule.get("auto_submit")),
        },
    )


@app.get("/autopilot/mini", response_class=HTMLResponse)
def autopilot_mini(request: Request):
    """Компактный монитор автопилота для отдельного мини-окна (живая статистика)."""
    return templates.TemplateResponse("autopilot_mini.html", {"request": request})


@app.get("/api/transit/{job_id}")
def api_transit(job_id: str):
    home = settings_store.get_home()
    if not home:
        return JSONResponse({"ok": False, "error": "домашний адрес не задан в настройках"})
    with get_session() as s:
        job = s.get(Job, job_id)
    if not job or job.lat is None or job.lon is None:
        return JSONResponse({"ok": False, "error": "у вакансии нет координат для маршрута"})
    try:
        return JSONResponse(transit.summary(home["lat"], home["lon"], job.lat, job.lon))
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "маршрут сейчас не посчитался"})


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, saved: str = "", missing: str = ""):
    """Общие настройки приложения: единый профиль, документы и подписка."""
    # если уже вошли — освежим тариф/имя из облака (подхватит выданный Pro/Max)
    if account_mod.is_signed_in():
        try:
            u = cloud_auth.fetch_session()
            if u:
                account_mod.apply_session(u)
        except Exception:  # noqa: BLE001 — обновление не должно мешать открытию страницы
            pass
    profile = profile_store.load_profile()
    city_options, country_options = _profile_choices()
    missing_fields = [x for x in missing.split(",") if x]
    return templates.TemplateResponse("account.html", {
        "request": request, "profile": profile,
        "file_info": _profile_file_info(profile),
        "saved": saved, "missing_fields": missing_fields,
        "city_options": city_options, "country_options": country_options,
        "subscription": subscription.status(),
        "account": account_mod.status(profile),
        "account_tg_id": account_mod.load().get("tg_id") or "",
        "cloud_login_url": cloud_auth.login_url(),
    })


def _autopilot_geo_options(rule: dict | None = None):
    """Списки городов и регионов из активных датских вакансий.

    Города подстраиваем под текущий профиль автопилота: если уже выбрана
    категория/бренд/регион/возраст, не показываем весь каталог городов подряд.
    """
    with get_session() as s:
        jobs = list(
            s.exec(
                select(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))
            ).all()
        )
    jobs = [j for j in jobs if (j.country or "").upper() == "DK"]

    def clean_city(value: str | None) -> str:
        value = re.sub(r"\s+", " ", (value or "").strip(" ,;"))
        if value.endswith("."):
            value = value[:-1].strip()
        return value

    all_cities = sorted({clean_city(j.city) for j in jobs if clean_city(j.city)})
    regions = sorted({(j.region or "").strip() for j in jobs if j.region and j.region.strip()})

    if not rule:
        return all_cities, regions

    city_rule = dict(rule)
    # Само поле города не должно сужать список подсказок; остальные фильтры
    # оставляем, чтобы убрать заведомо лишние города.
    city_rule["cities"] = ""
    home = settings_store.get_home()
    try:
        filtered = [j for j in jobs if autopilot._matches(j, city_rule, home)]
    except Exception:  # noqa: BLE001
        filtered = []
    cities = sorted({clean_city(j.city) for j in filtered if clean_city(j.city)})
    return (cities or all_cities), regions


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    profile = profile_store.load_profile()
    # профили подбора: выбранный профиль (?profile=id) редактируется формой ниже.
    ap_profiles = autopilot.ensure_profiles()
    sel_id = request.query_params.get("profile") or ""
    sel_profile = next((p for p in ap_profiles if p.get("id") == sel_id), ap_profiles[0])
    # «вид» правила для формы = глобальные поля + фильтры ВЫБРАННОГО профиля,
    # чтобы шаблон фильтров не переписывать (он читает autopilot.<поле>).
    ap_view = dict(autopilot.get_rule())
    for k in autopilot._FILTER_FIELDS:
        ap_view[k] = sel_profile.get(k, autopilot.DEFAULT_RULE.get(k))
    ap_cities, ap_regions = _autopilot_geo_options(ap_view)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "profile": profile, "file_info": _profile_file_info(profile),
        "creds": credentials_store.status(), "home": settings_store.get_home(),
        "saved": saved, "geoerror": geoerror,
        "subscription": subscription.status(),
        "autopilot": ap_view, "brands": labels.BRANDS,
        "categories": labels.CATEGORY, "employments": labels.EMPLOYMENT,
        "autopilot_cities": ap_cities, "autopilot_regions": ap_regions,
        "autopilot_region_labels": labels.REGION,
        "autopilot_mode": autopilot.get_mode(),
        "autopilot_profiles": ap_profiles, "autopilot_profile": sel_profile,
        "autopilot_profile_count": autopilot.profile_match_count(sel_profile),
        "autopilot_count": autopilot.match_count(),
        "autopilot_pending": autopilot.pending_count(),
        "autopilot_submitted_today": autopilot.submitted_today(),
        "autopilot_submit_log": autopilot.submit_log(),
        "autopilot_eligible": autopilot.eligible_count(),
        "autopilot_scope_pool": autopilot.scope_all_pool(),
        "autopilot_scope_guard": autopilot.SCOPE_ALL_GUARD,
        "autopilot_tg_stats": autopilot.tg_queue_stats(),
        "autopilot_max_per_scan": autopilot.MAX_PER_SCAN,
        "autopilot_scan_min": AUTOPILOT_SCAN_MIN,
    })


@app.post("/account/save")
@app.post("/settings/save")  # legacy-алиас: общий профиль теперь в «Общих настройках»
def account_save(
    first_name: str = Form(""), last_name: str = Form(""), email: str = Form(""),
    phone: str = Form(""), address: str = Form(""), zipcode: str = Form(""),
    city: str = Form(""), country: str = Form(""), linkedin: str = Form(""),
):
    """Общий профиль — только личные данные. Документы (CV/письмо) — в настройках фирмы."""
    profile = profile_store.load_profile()
    profile.update({
        "first_name": first_name.strip(), "last_name": last_name.strip(),
        "email": email.strip(), "phone": phone.strip(), "address": address.strip(),
        "zip": zipcode.strip(), "city": city.strip(), "country": country.strip(),
        "linkedin": linkedin.strip(),
    })
    missing = _profile_missing(profile)
    if missing:
        return RedirectResponse("/account?missing=" + quote_plus(",".join(missing)), status_code=303)
    profile_store.save_profile(profile)
    return RedirectResponse("/account?saved=1", status_code=303)


@app.post("/account/waitlist")
async def account_waitlist(request: Request):
    """Вейтлист интереса к платному тарифу (заготовка под будущую оплату)."""
    form = await request.form()
    email = str(form.get("email") or "").strip()
    plan = str(form.get("plan") or "pro").strip()
    if not email:
        email = str(profile_store.load_profile().get("email") or "").strip()
    ok = subscription.add_waitlist(email, plan)
    return JSONResponse({"ok": ok, "email": email})


def _sync_profile_with_cloud():
    """При первом входе переносим профиль между устройством и облаком — без потерь.

    - есть локальные данные → выгружаем в облако (перенос/резервная копия);
    - локально пусто, а в облаке профиль есть → скачиваем (данные следуют за
      человеком на новый ПК/после переустановки).
    Непустой локальный профиль НИКОГДА не затирается пустым облачным.
    """
    try:
        local = profile_store.load_profile()
        has_local = any(
            str(local.get(k) or "").strip()
            for k in ("first_name", "last_name", "email", "phone")
        )
        if has_local:
            cloud_auth.push_profile(local)
        else:
            cloud = cloud_auth.pull_profile()
            if cloud:
                merged = dict(local)
                merged.update(cloud)
                profile_store.save_profile(merged)
    except Exception:  # noqa: BLE001 — перенос не должен мешать входу
        pass


@app.get("/account/login/poll")
def account_login_poll():
    """Спрашивает облако, вошёл ли пользователь (страница входа открыта в браузере).

    Возвращает {"signed_in": bool, ...}. Страница аккаунта опрашивает это, пока
    человек подтверждает вход через Telegram; как только облако скажет «вошёл» —
    сохраняем личность и тариф локально и (один раз) переносим профиль.
    """
    user = cloud_auth.fetch_session()
    if user:
        was_signed_in = account_mod.is_signed_in()
        acc = account_mod.apply_session(user)
        if not was_signed_in:
            _sync_profile_with_cloud()
        return JSONResponse({
            "signed_in": True,
            "tg_id": acc.get("tg_id") or "",
            "name": acc.get("tg_name") or "",
            "username": acc.get("username") or "",
            "plan": acc.get("plan") or "free",
        })
    return JSONResponse({"signed_in": account_mod.is_signed_in()})


@app.post("/account/logout")
def account_logout():
    """Выйти из аккаунта (локально). Облачная сессия остаётся — можно войти снова."""
    account_mod.sign_out()
    return RedirectResponse("/account", status_code=303)


@app.post("/account/rebind/start")
def account_rebind_start():
    """Шаг 1 перепривязки: облако шлёт код подтверждения в текущий (старый) Telegram."""
    if not account_mod.is_signed_in():
        return JSONResponse({"ok": False, "error": "Сначала войди в аккаунт"}, status_code=401)
    return JSONResponse(cloud_auth.rebind_start())


@app.post("/account/rebind/confirm")
def account_rebind_confirm(code: str = Form("")):
    """Шаг 2: проверяем код. При успехе возвращаем ссылку входа НОВЫМ аккаунтом."""
    code = (code or "").strip()
    if not code:
        return JSONResponse({"ok": False, "error": "Введи код"}, status_code=400)
    return JSONResponse(cloud_auth.rebind_confirm(code))


@app.post("/settings/documents/save")
def settings_documents_save(
    cv_path: str = Form(""), cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None), cover_letter_file: UploadFile | None = File(None),
):
    """Документы для Salling (CV/письмо). WexFlow использует их в анкетах Salling."""
    profile = profile_store.load_profile()
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(_url_with_system_response("/settings", error=file_error), status_code=303)
    profile_store.save_profile(profile)
    return RedirectResponse("/settings?saved=1#documents", status_code=303)


@app.post("/autopilot/save")
def autopilot_save(
    enabled: str = Form(""),
    profile_id: str = Form(""),
    max_km: str = Form(""),
    min_hours: str = Form(""),
    max_hours: str = Form(""),
    max_age_days: str = Form(""),
    category: str = Form(""),
    employment_type: str = Form(""),
    age: str = Form(""),
    keywords: str = Form(""),
    brand: str = Form(""),
    cities: str = Form(""),
    regions: str = Form(""),
    exclude_brands: str = Form(""),
    exclude_cities: str = Form(""),
    exclude_keywords: str = Form(""),
    active_from: str = Form(""),
    active_to: str = Form(""),
):
    def _num_csv(v: str) -> str:
        """Несколько порогов через запятую → нормализованный CSV (дубли/пустые/≤0 убраны)."""
        out: list[str] = []
        for p in str(v or "").split(","):
            p = p.strip()
            if not p:
                continue
            try:
                f = max(0.0, float(p.replace(",", ".")))
            except ValueError:
                continue
            if f <= 0:
                continue
            s = str(int(f) if f == int(f) else f)
            if s not in out:
                out.append(s)
        return ",".join(out)

    def _hour(v: str, default: int) -> int:
        try:
            return max(0, min(24, int(float(v))))
        except (ValueError, TypeError):
            return default

    # ФИЛЬТРЫ пишем в выбранный профиль (режим/лимиты/расписание — глобальные).
    autopilot.save_profile_filters(profile_id, {
        "max_km": _num_csv(max_km),
        "min_hours": _num_csv(min_hours),
        "max_hours": _num_csv(max_hours),
        "max_age_days": _num_csv(max_age_days),
        "category": category.strip(),
        "employment_type": employment_type.strip(),
        "age": age.strip(),
        "keywords": keywords.strip(),
        "brand": brand.strip(),
        "cities": cities.strip(),
        "regions": regions.strip(),
        "exclude_brands": exclude_brands.strip(),
        "exclude_cities": exclude_cities.strip(),
        "exclude_keywords": exclude_keywords.strip(),
    })
    autopilot.save_rule({"active_from": _hour(active_from, 0), "active_to": _hour(active_to, 24)})
    # «с этого момента»: текущие совпадения считаем уже виденными (без спама о старых).
    autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
    _reschedule_autopilot_scan()
    dest = f"/settings?saved=1&profile={profile_id}#autopilot" if profile_id else "/settings?saved=1#autopilot"
    return RedirectResponse(dest, status_code=303)


@app.post("/autopilot/profile/add")
def autopilot_profile_add(name: str = Form("Новое правило")):
    pid = autopilot.add_profile((name or "").strip() or "Новое правило")
    return RedirectResponse(f"/settings?profile={pid}#autopilot", status_code=303)


@app.post("/autopilot/profile/delete")
def autopilot_profile_delete(profile_id: str = Form("")):
    autopilot.delete_profile(profile_id)
    return RedirectResponse("/settings#autopilot", status_code=303)


@app.post("/autopilot/profile/rename")
def autopilot_profile_rename(profile_id: str = Form(""), name: str = Form("")):
    autopilot.rename_profile(profile_id, name)
    return RedirectResponse(f"/settings?profile={profile_id}#autopilot", status_code=303)


@app.post("/autopilot/profile/toggle")
def autopilot_profile_toggle(profile_id: str = Form("")):
    autopilot.toggle_profile(profile_id)
    return RedirectResponse(f"/settings?profile={profile_id}#autopilot", status_code=303)


@app.post("/autopilot/prepare")
def autopilot_prepare(request: Request):
    """Автоподготовка (Фаза 2): открыть до 5 свежих подходящих вакансий,
    заполнить анкеты и ОСТАНОВИТЬСЯ перед отправкой. Реальную отправку (--submit)
    не запускаем НИКОГДА — её жмёт пользователь сам."""
    jobs = autopilot.pending_prepare(limit=5)
    if not jobs:
        return _redirect_back(request, "/settings",
                              notice="Новых подходящих для подготовки нет — свежие уже готовились.")
    ids = [j.id for j in jobs]
    _launch_salling_apply(ids, submit=False)
    autopilot.mark_prepared(ids)
    return _redirect_back(
        request, "/settings",
        notice=f"Готовлю {len(ids)} вакансий — WexFlow заполнит формы "
               "и остановится перед отправкой. Проверь и нажми «Отправить» сам.",
    )


@app.post("/autopilot/autosubmit")
def autopilot_autosubmit(
    request: Request,
    daily_limit: str = Form(""),
    submit_scope: str = Form("new"),
):
    """Настройки автоотправки (дневной лимит + охват). Включение/выключение режима
    делается селектором «Режим работы» (/api/autopilot/mode) — здесь режим
    (enabled/auto_submit) НЕ трогаем, чтобы сохранение настроек его не сбрасывало."""
    try:
        limit = max(1, min(50, int(float(daily_limit)))) if daily_limit.strip() else 3
    except ValueError:
        limit = 3
    scope = "all" if submit_scope == "all" else "new"
    was_scope = autopilot.get_rule().get("submit_scope") or "new"

    # ПРЕДОХРАНИТЕЛЬ: «все подходящие» + включение нельзя, если под правило
    # сейчас попадает слишком много — иначе автоотправка начнёт постепенно подавать на
    # сотни вакансий. Заставляем сузить фильтры или взять «только новые».
    if scope == "all":
        pool = autopilot.scope_all_pool()
        if pool > autopilot.SCOPE_ALL_GUARD:
            autopilot.save_rule({"daily_limit": limit})  # лимит сохраним, охват — нет
            return _redirect_back(
                request, "/settings",
                error=f"Охват «все подходящие» сейчас нельзя: под правило подходит {pool} "
                      f"(предел {autopilot.SCOPE_ALL_GUARD}). Сузь фильтры или оставь «только новые».",
            )

    autopilot.save_rule({"daily_limit": limit, "submit_scope": scope})
    # переключение на «только новые» — обновить baseline (слать лишь новые после этого)
    if scope == "new" and was_scope != "new":
        autopilot.set_autosubmit_baseline()
    return _redirect_back(request, "/settings", notice="Настройки автоотправки сохранены.")


@app.post("/autopilot/stop")
def autopilot_stop(request: Request):
    """Аварийная остановка автоотправки — мгновенно выключает."""
    autopilot.save_rule({"auto_submit": False})
    return _redirect_back(request, "/settings", notice="Автоотправка остановлена.")


@app.post("/autopilot/toggle")
def autopilot_toggle(request: Request):
    """Быстрый тумблер автопилота с главной: включает/выключает ТОЛЬКО поиск
    (enabled). Опасную автоотправку отсюда не трогаем — она остаётся под замком
    в настройках. При включении считаем текущие совпадения уже виденными."""
    r = autopilot.get_rule()
    now_on = not bool(r.get("enabled"))
    autopilot.save_rule({"enabled": now_on})
    if now_on:
        autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
        msg = f"Автопилот включён — слежу за новыми вакансиями (подходит сейчас: {autopilot.match_count()})."
    else:
        msg = "Автопилот выключен."
    _reschedule_autopilot_scan()  # подстроить частоту фонового скана
    if now_on:
        # маркер apwin=1 → base.html сам откроет мини-окно автопилота
        url = _url_with_system_response(request.headers.get("referer") or "/", msg, "")
        return RedirectResponse(url + ("&" if "?" in url else "?") + "apwin=1", status_code=303)
    return _redirect_back(request, "/", notice=msg)


# ── Telegram: привязка бота (режим «по разрешению») ─────────────────────
@app.get("/api/telegram/status")
def telegram_status():
    acc = account_mod.load()
    rule = autopilot.get_rule()
    return {
        "cloud": True,
        "signed_in": account_mod.is_signed_in(),
        "tg_id": acc.get("tg_id") or "",
        "username": acc.get("username") or "",
        "name": acc.get("tg_name") or "",
        "approval": bool(rule.get("tg_approval")),
        "mode": autopilot.get_mode(),
        "within_schedule": autopilot.within_schedule(rule),
        "max_per_send": TG_MAX_PER_SCAN,
        **autopilot.tg_queue_stats(),
    }


@app.post("/api/telegram/approval")
async def telegram_approval(request: Request):
    """Включить/выключить режим «спрашивать в Telegram перед подачей»."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    on = bool(body.get("on"))
    if on and not account_mod.is_signed_in():
        return {"ok": False, "error": "Сначала войди через Telegram в разделе «Аккаунт»."}
    patch = {"tg_approval": on}
    if on:
        patch["enabled"] = True  # режиму нужен работающий поиск
    autopilot.save_rule(patch)
    if on:
        # спрашивать только про НОВЫЕ вакансии (текущие фиксируем как baseline),
        # если охват «только новые» — чтобы не завалить бэклогом
        if (autopilot.get_rule().get("submit_scope") or "new") != "all":
            autopilot.set_autosubmit_baseline()
        autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
    _reschedule_autopilot_scan()
    return {"ok": True, "on": on}


@app.post("/api/telegram/save-token")
async def telegram_save_token(request: Request):
    """Проверить токен через getMe и сохранить (шифрованно). Запускает слушатель."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    token = (body.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "Вставь токен бота от @BotFather."}
    me = tg.get_me(token)
    if not me.get("ok"):
        return {"ok": False, "error": f"Токен не подошёл: {me.get('error')}"}
    telegram_store.save_token(token, me.get("username", ""))
    _ensure_tg_poller()
    return {"ok": True, "username": me.get("username", "")}


@app.post("/api/telegram/test")
def telegram_test():
    """Проверочное сообщение = РЕАЛЬНЫЙ вид карточки вакансии с кнопками
    (на примере подходящей вакансии). Кнопки в примере ничего не отправляют."""
    if not account_mod.is_signed_in():
        return {"ok": False, "error": "Сначала войди через Telegram (раздел «Аккаунт»)."}
    sample = None
    try:
        matches = autopilot.find_matches()
        sample = matches[0] if matches else None
    except Exception:  # noqa: BLE001
        sample = None
    if sample is None:
        with get_session() as s:
            sample = s.exec(
                select(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))
            ).first()
    if sample is None:
        return {"ok": False, "error": "Подходящих вакансий для примера сейчас нет."}
    text = ("🔔 <b>Пример сообщения автопилота</b>\n"
            "Вот так будет приходить вакансия на подтверждение:\n\n" + _tg_card(sample))
    r = cloud_auth.offer(text, "__demo__")
    return {"ok": bool(r and r.get("ok")), "error": (r or {}).get("error", "")}


@app.post("/api/telegram/send-current")
def telegram_send_current():
    """Ручная отправка текущих подходящих вакансий в Telegram.
    Нужна для понятного сценария: счётчик «подходит» уже есть, но безопасный
    режим автоматически шлёт только новые после включения."""
    if not account_mod.is_signed_in():
        return {"ok": False, "error": "Сначала войди через Telegram (раздел «Аккаунт»)."}
    autopilot.save_rule({"enabled": True, "tg_approval": True})
    _reschedule_autopilot_scan()
    result = _tg_offer_tick(include_existing=True, ignore_schedule=True)
    stats = autopilot.tg_queue_stats()
    if result.get("sent"):
        return {"ok": True, "sent": result["sent"], "remaining": stats["eligible_current"]}
    return {
        "ok": False,
        "sent": 0,
        "remaining": stats["eligible_current"],
        "error": result.get("error") or "Нечего отправлять: текущие уже предложены, пропущены или поданы.",
    }


@app.post("/api/telegram/unlink")
def telegram_unlink():
    """Отвязать: забыть токен и chat_id (бот при этом не удаляется)."""
    telegram_store.clear()
    return {"ok": True}


@app.post("/settings/profile/autosave")
async def settings_profile_autosave(request: Request):
    form = await request.form()
    profile = profile_store.load_profile()
    for form_key, profile_key in [
        ("first_name", "first_name"), ("last_name", "last_name"), ("email", "email"),
        ("phone", "phone"), ("address", "address"), ("zipcode", "zip"),
        ("city", "city"), ("country", "country"), ("linkedin", "linkedin"),
        ("cv_path", "cv_path"), ("cover_letter_path", "cover_letter_path"),
    ]:
        if form_key in form:
            profile[profile_key] = str(form.get(form_key) or "").strip()
    profile = profile_store.clean_profile(profile)
    profile_store.save_profile(profile)
    return JSONResponse({"ok": True, "missing": _profile_missing(profile), "profile": profile})


@app.get("/settings/file/{kind}")
def settings_file(kind: str):
    profile = profile_store.load_profile()
    path = {
        "cv": profile.get("cv_path", ""),
        "cover": profile.get("cover_letter_path", ""),
    }.get(kind)
    if not path or profile_store.file_status(path) != "ok":
        raise HTTPException(status_code=404, detail="Файл не найден")
    clean_path = profile_store.validate_document_path(path)
    filename = profile_store.file_label(clean_path)
    is_pdf = filename.lower().endswith(".pdf")
    media_type = "application/pdf" if is_pdf else "application/octet-stream"
    disposition = "inline" if is_pdf else "attachment"
    return FileResponse(clean_path, media_type=media_type, filename=filename, content_disposition_type=disposition)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def detail(request: Request, job_id: str, trerror: str = ""):
    with get_session() as s:
        job = s.get(Job, job_id)
        if job and job.status == "new":
            job.status = "seen"
            s.add(job)
            s.commit()
            s.refresh(job)
    distance = None
    home = settings_store.get_home()
    if job and home and job.lat is not None and job.lon is not None:
        distance = round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon), 1)
    maps_url = _maps_url(job, home) if job else ""
    facts = _job_facts(job, distance) if job else []
    return templates.TemplateResponse(
        "detail.html", {
            "request": request,
            "job": job,
            "distance": distance,
            "has_home": bool(home),
            "maps_url": maps_url,
            "facts": facts,
            "description_html": html_sanitize.sanitize_html(job.description if job else ""),
            "description_ru_html": html_sanitize.sanitize_html(job.description_ru if job else ""),
            "translator_name": translator.provider_name(),
            "translator_install": translator_setup.status(),
            "trerror": trerror,
        }
    )


@app.get("/job/{job_id}/apply", response_class=HTMLResponse)
def apply_prepare(request: Request, job_id: str, started: str = "", saved: str = "", reset: str = ""):
    with get_session() as s:
        job = s.get(Job, job_id)
    profile = profile_store.load_profile()
    file_info = {
        "cv_label": profile_store.file_label(profile.get("cv_path", "")),
        "cv_status": profile_store.file_status(profile.get("cv_path", "")),
        "cover_label": profile_store.file_label(profile.get("cover_letter_path", "")),
        "cover_status": profile_store.file_status(profile.get("cover_letter_path", "")),
    }
    home = settings_store.get_home()
    distance = None
    if job and home and job.lat is not None and job.lon is not None:
        distance = round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon), 1)
    return templates.TemplateResponse(
        "apply.html",
        {
            "request": request,
            "job": job,
            "profile": profile,
            "file_info": file_info,
            "distance": distance,
            "maps_url": _maps_url(job, home) if job else "",
            "started": started,
            "saved": saved,
            "reset": reset,
            "creds": credentials_store.status(),
        },
    )


@app.post("/credentials/save")
def save_credentials(job_id: str = Form(""), sf_email: str = Form(""), sf_password: str = Form("")):
    credentials_store.save(sf_email, sf_password)
    target = f"/job/{job_id}/apply?saved=login" if job_id else _url_with_system_response("/", notice="Логин Salling сохранён.")
    return RedirectResponse(target, status_code=303)


@app.post("/apply/reset-browser")
def reset_browser(request: Request, job_id: str = Form("")):
    """Удаляет сохранённую сессию подачи — чтобы выйти из чужого/старого
    аккаунта Salling и войти заново под сохранёнными email/паролем."""
    import shutil
    shutil.rmtree(config.BROWSER_PROFILE_DIR, ignore_errors=True)
    ref = request.headers.get("referer")
    if ref and "/settings" in ref:
        return RedirectResponse("/settings?saved=1", status_code=303)
    target = f"/job/{job_id}/apply?reset=1" if job_id else "/"
    return RedirectResponse(target, status_code=303)


@app.post("/credentials/clear")
def clear_credentials(request: Request):
    credentials_store.clear()
    return _redirect_back(request, "/settings", notice="Логин Salling очищен.")


def _update_profile_files(profile: dict, cv_path: str, cover_letter_path: str, cv_file, cover_letter_file) -> dict:
    # приоритет: загруженный файл > указанный путь > СОХРАНИТЬ прежний (не стираем случайно)
    if cv_file and cv_file.filename:
        profile["cv_path"] = profile_store.save_upload(cv_file, "cv")
    elif cv_path.strip():
        profile["cv_path"] = profile_store.validate_document_path(cv_path)

    if cover_letter_file and cover_letter_file.filename:
        profile["cover_letter_path"] = profile_store.save_upload(cover_letter_file, "cover_letter")
    elif cover_letter_path.strip():
        profile["cover_letter_path"] = profile_store.validate_document_path(cover_letter_path)
    return profile


def _profile_files_result(profile: dict, cv_path: str, cover_letter_path: str, cv_file, cover_letter_file) -> tuple[dict, str]:
    try:
        return _update_profile_files(profile, cv_path, cover_letter_path, cv_file, cover_letter_file), ""
    except ValueError as exc:
        return profile, str(exc)


@app.post("/job/{job_id}/apply/save")
def save_apply_files(
    job_id: str,
    cv_path: str = Form(""),
    cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
):
    profile = profile_store.load_profile()
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(
            _url_with_system_response(f"/job/{job_id}/apply", error=file_error),
            status_code=303,
        )
    profile_store.save_profile(profile)
    return RedirectResponse(f"/job/{job_id}/apply?saved=files", status_code=303)


def _salling_apply_cmd(extra: list[str]) -> list[str]:
    """Команда запуска подачи Salling.

    dev: [python, -u, apply.py, ...]. Собранное приложение: exe запускает сам
    себя как воркер [WexFlow.exe, --worker-salling-apply, ...] — Python/.venv
    на чужом ПК нет.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker-salling-apply", *extra]
    return [sys.executable, "-u", str(config.BASE_DIR / "apply.py"), *extra]


def _launch_salling_apply(ids: list[str], submit: bool = False) -> None:
    """Запустить подачу Salling по списку id с диагностикой фонового процесса.
    submit=False — режим подготовки: WexFlow заполняет и останавливается перед отправкой."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = _salling_apply_cmd(list(ids) + ["--web"])
    if submit:
        cmd.append("--submit")
    log = open(config.DATA_DIR / "apply_last.log", "w", encoding="utf-8")
    try:
        subprocess.Popen(
            cmd, cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    finally:
        log.close()


@app.post("/job/{job_id}/apply/start")
def start_apply(
    job_id: str,
    mode: str = Form("submit"),
    cv_path: str = Form(""),
    cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
):
    with get_session() as s:
        job = s.get(Job, job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    profile = profile_store.load_profile()
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(
            _url_with_system_response(f"/job/{job_id}/apply", error=file_error),
            status_code=303,
        )
    profile_store.save_profile(profile)
    # вывод apply.py пишем в лог, чтобы сбои не были «молчаливыми»
    log = open(config.DATA_DIR / "apply_last.log", "w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # иначе print датских/русских символов падает (cp1251)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"        # чтобы диагностика писалась сразу
    cmd = _salling_apply_cmd([job_id, "--web"])
    if mode == "submit":
        cmd.append("--submit")
    try:
        subprocess.Popen(
            cmd,
            cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    finally:
        log.close()  # потомок унаследовал свой хэндл; родительский больше не нужен
    return RedirectResponse(f"/job/{job_id}/apply?started=1", status_code=303)


@app.post("/apply/batch")
def apply_batch(request: Request, job_ids: list[str] = Form(default=[]), mode: str = Form("dry")):
    ids = [j for j in job_ids if j]
    if not ids:
        return _redirect_back(request, "/", error="Сначала выбери хотя бы одну вакансию для пакетной подачи.")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = _salling_apply_cmd(ids + ["--web"])
    if mode == "submit":
        cmd.append("--submit")
    log = open(config.DATA_DIR / "apply_last.log", "w", encoding="utf-8")
    try:
        subprocess.Popen(
            cmd, cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    finally:
        log.close()
    return RedirectResponse(f"/?batch={len(ids)}&mode={mode}", status_code=303)


@app.post("/job/{job_id}/translate")
def translate_job(job_id: str):
    error = ""
    with get_session() as s:
        job = s.get(Job, job_id)
        if job and job.description:
            try:
                job.description_ru = translator.translate_to_ru(job.description, title=job.title)
                s.add(job)
                s.commit()
            except translator.TranslationError as e:
                print(f"перевод: ошибка — {e}")
                error = "Переводчик сейчас недоступен"
    target = f"/job/{job_id}?trerror={quote_plus(error)}" if error else _url_with_system_response(f"/job/{job_id}", notice="Перевод обновлён.")
    return RedirectResponse(target, status_code=303)


@app.post("/translator/install")
def install_translator(job_id: str = Form("")):
    translator_setup.start_install()
    target = _url_with_system_response(
        f"/job/{job_id}" if job_id else "/",
        notice="Установка переводчика запущена в фоне.",
    )
    return RedirectResponse(target, status_code=303)
