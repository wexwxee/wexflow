"""Этап 2 — локальный веб-дашборд вакансий Salling Group.

Запуск:  python -m uvicorn app:app --reload
Открыть: http://127.0.0.1:8000
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from collections import Counter
from urllib.parse import parse_qsl, quote_plus, unquote, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlmodel import func

import config
import candidate_profiles
import local_guard
import labels
import geo
import settings_store
import translator
import translator_setup
import html_sanitize
import profile_store
import form_questions
import document_rules
import document_import
import credentials_store
import subscription
import account as account_mod
import cloud_auth
import transit
from db import Application, Job, init_db, get_session, select, utcnow
import scraper
import connector_sync
import applications
import autopilot
import autostart
import ai_filters
import ai_gateway
import ai_secrets
import ai_usage
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
JOB_SOURCE_LABELS = {
    "salling": "Salling Group",
    "teamtailor": "Другие компании · Teamtailor",
    "greenhouse": "Другие компании · Greenhouse",
    "ashby": "Другие компании · Ashby",
    "lidl": "Lidl Danmark",
    "manual_link": "Добавлено по ссылке",
}
JOB_FILTER_KEYS = (
    "q", "source", "city", "brand", "region", "category",
    "employment_type", "job_level", "status", "sort", "radius",
    "group", "show_applied", "period",
)


def _clean_filter_query(raw_query: str) -> str:
    """Оставить в профиле поиска только известные безопасные параметры."""
    values: dict[str, str] = {}
    for key, value in parse_qsl(str(raw_query or ""), keep_blank_values=False):
        if key not in JOB_FILTER_KEYS:
            continue
        value = str(value).strip()[:240]
        if not value:
            continue
        if key in {"group", "show_applied"}:
            value = "1" if value in {"1", "true", "on"} else ""
        elif key == "status" and value not in SAFE_JOB_STATUSES | {"active"}:
            value = ""
        elif key == "sort" and value not in {"published", "distance", "title", "city"}:
            value = ""
        elif key == "period" and value not in {"today", "3d", "all"}:
            value = ""
        if value:
            values[key] = value
    return urlencode([(key, values[key]) for key in JOB_FILTER_KEYS if key in values])


def _filter_query(filters: dict, drop: str = "") -> str:
    """Каноническая строка текущего поиска для профилей, вкладок и chips."""
    pairs = []
    for key in JOB_FILTER_KEYS:
        if key == drop:
            continue
        value = filters.get(key)
        if value:
            pairs.append((key, str(value)))
    return _clean_filter_query(urlencode(pairs))


def _allowed_local_write(request: Request) -> bool:
    """Block cross-site form/fetch writes against the local desktop server.
    Единый барьер в local_guard (тот же, что в hub.py и connectors/webapp.py)."""
    return local_guard.allowed_write(
        request.method,
        request.headers.get("host", ""),
        request.headers.get("origin", ""),
        request.headers.get("referer", ""),
        request.headers.get("sec-fetch-site", ""),
    )


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
_sync_state = {"running": False, "last_error": "", "last_scan": 0.0,
               # сторожа деградации (шаг 7): сколько вакансий отдал источник в
               # последний раз (None — ещё не проверяли) и упал ли сам синк
               "last_hits": None, "sync_failed": False, "connector_errors": []}
_connector_sync_last = 0.0
_connector_sync_attempt_last = 0.0
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


def _sync_jobs(force_connectors: bool = False):
    """Обновляет базу вакансий. Не запускается параллельно сам с собой."""
    if not _sync_lock.acquire(blocking=False):
        return
    global _connector_sync_last, _connector_sync_attempt_last
    _sync_state["running"] = True
    try:
        try:
            info = scraper.sync() or {}
            _sync_state["last_hits"] = int(info.get("hits") or 0)
            _sync_state["sync_failed"] = False
        except Exception:
            _sync_state["sync_failed"] = True  # источник не ответил — сторож заметит
            raise
        # ATS-каталоги тяжелее одного Algolia-запроса, поэтому обновляем их не
        # чаще раза в 30 минут. Ошибка Teamtailor не ломает рабочий Salling.
        connector_due = time.time() - _connector_sync_last >= 30 * 60
        retry_due = time.time() - _connector_sync_attempt_last >= 10 * 60
        if force_connectors or (connector_due and retry_due):
            _connector_sync_attempt_last = time.time()
            try:
                report = connector_sync.sync()
                _sync_state["connector_errors"] = report.get("errors") or []
                if not report.get("errors"):
                    _connector_sync_last = time.time()
            except Exception as exc:  # connector infrastructure stays isolated
                _sync_state["connector_errors"] = [f"connector sync: {str(exc)[:180]}"]
                print(f"дополнительные источники: ошибка — {exc}")
        _sync_state["last_error"] = ""
        autopilot.scan_and_notify()  # автопилот: уведомить о новых совпадениях
        # автоотправка (фаза 3, по умолчанию ВЫКЛ): отправляет ТОЛЬКО при явно
        # включённом auto_submit, в пределах дневного лимита и только новые
        autopilot.auto_submit_tick(lambda ids: _launch_salling_apply(ids, submit=True, track_autopilot=True))
        _tg_offer_tick()  # режим «по разрешению»: спросить в Telegram про новые подходящие
    except Exception as e:
        _sync_state["last_error"] = str(e)[:200]
        print(f"автообновление: ошибка — {e}")
    finally:
        _sync_state["last_scan"] = time.time()  # отметка «когда последний раз проверяли»
        _sync_state["running"] = False
        _sync_lock.release()


def _ai_usage_payload() -> dict:
    # Легаси-блок (индикатор Gemini в хабе 1.3.21) — семантика прежняя: Gemini.
    payload = ai_usage.status()
    payload["connected"] = ai_filters.gemini_available()
    payload["model"] = ai_filters.model_name() if payload["connected"] else ""
    # Новый мультипровайдерный блок (sidebar-индикатор, раздел «ИИ и лимиты»).
    try:
        payload["ai"] = ai_gateway.usage_payload()
    except Exception:  # noqa: BLE001 — статус ИИ не должен ронять страницу
        payload["ai"] = {"connected": False, "primary": "", "compact": None,
                         "providers": {}, "active": None}
    return payload


def _autopilot_status_payload() -> dict:
    """Полная сводка для живого монитора автопилота (главная опрашивает её)."""
    st = autopilot.status()
    st["running"] = _sync_state["running"]
    st["error"] = _sync_state.get("last_error") or ""
    # время последней проверки. После перезапуска процесса счётчик в памяти
    # сбрасывается — тогда берём момент последнего обновления базы (last_seen),
    # чтобы монитор не врал «ещё не проверял», когда данные на самом деле свежие.
    last = max(
        float(_sync_state.get("last_scan") or 0.0),
        float(st.get("last_search_at") or 0.0),
    )
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
    st["ai_usage"] = _ai_usage_payload()
    return st


def _data_age_minutes() -> int | None:
    """Сколько минут назад вакансии обновлялись (по last_seen в базе)."""
    with get_session() as s:
        last = s.exec(select(func.max(Job.last_seen))).one()
    if not last:
        return None
    return max(0, int((utcnow() - last).total_seconds() // 60))


# ── Telegram: cloud decision poller ─────────────────────────────────────
# Общий бот @wexflowbot принимает нажатия в Telegram, а локальное приложение
# забирает готовые решения из облака и выполняет их на этом компьютере.
_tg_thread = None
_tg_stop = threading.Event()
_tg_session_sync_last = 0.0
_tg_poll_state = {"fail_streak": 0, "last_ok": 0.0, "last_error": ""}


def _sync_account_from_cloud() -> None:
    global _tg_session_sync_last
    now = time.time()
    if account_mod.cloud_sync_paused():
        return
    if now - _tg_session_sync_last < 60:
        return
    _tg_session_sync_last = now

    user = cloud_auth.fetch_session(timeout=5)
    if not user:
        return
    # Пользователь мог нажать «Выйти», пока сетевой запрос был в полёте.
    # Старый ответ не должен тут же авторизовать его снова.
    if account_mod.cloud_sync_paused():
        return

    acc = account_mod.load()
    remote_tg = str(user.get("tgId") or user.get("tg_id") or "")
    if not remote_tg:
        return

    changed = (
        remote_tg != str(acc.get("tg_id") or "")
        or str(user.get("plan") or "free") != str(acc.get("plan") or "free")
        or str(user.get("username") or "") != str(acc.get("username") or "")
    )
    if changed:
        # Внутри account проверка logout-флага и запись выполняются атомарно.
        account_mod.apply_cloud_session(user)


def _report_apply_result_safe(job_id: str, state: str, msg: str = "") -> bool:
    try:
        return bool(cloud_auth.report_apply_result(job_id, state, msg))
    except Exception:  # noqa: BLE001
        return False


# ── Сериализатор автоматической подачи ──────────────────────────────────
# Все автоматические подачи (из Mini App и автопилота) идут через ОДНУ очередь.
# Пока на ПК открыт браузер и заполняется одна пачка, новые решения НЕ запускают
# второй процесс apply.py — иначе два процесса дрались бы за один профиль
# браузера (browser_profile) и подавалась бы только часть заявок. Новые id
# встают в очередь и подаются сразу следом, как только освободится браузер.
_apply_queue: list[list[str]] = []
_apply_queue_lock = threading.Lock()
_apply_runner_busy = False

# Защита от гонки ручной подачи: нельзя запускать вторую подачу, пока идёт первая —
# иначе два браузера дерутся за один профиль (browser_profile) и часть заявок может
# уйти повторно или потеряться. Двойной клик «Подать пачкой» ловится коротким окном.
_manual_apply_lock = threading.Lock()
_last_manual_apply_ts = 0.0
_connector_launch_lock = threading.Lock()
_connector_launches: dict[str, float] = {}
_connector_processes: dict[str, subprocess.Popen] = {}


# Последний запущенный НАМИ воркер подачи (шаг 4): пока держим живой хэндл,
# «идёт ли подача» решает сам процесс, а не возраст файла прогресса.
_last_apply_proc = None
_last_apply_spawn_ts = 0.0


def _progress_started_ts(data: dict) -> float:
    """Момент старта пачки из файла прогресса (или -1, если не разобрать)."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(data.get("started_at") or "")).timestamp()
    except Exception:  # noqa: BLE001
        return -1.0


def _apply_progress_active() -> bool:
    """Идёт ли прямо сейчас воркер apply.py (по его живому прогрессу).
    Шаг 4: если воркера запускали мы и держим хэндл — спрашиваем сам процесс
    (жив/завершился), а не гадаем по возрасту файла. Правило «4 минуты тишины»
    остаётся только страховкой для воркера-сироты: приложение перезапустили,
    а воркер прошлого запуска ещё дописывает пачку."""
    try:
        p = config.DATA_DIR / "apply_progress.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("active"):
                proc = _last_apply_proc
                if proc is not None and _progress_started_ts(data) >= _last_apply_spawn_ts - 10:
                    return proc.poll() is None
                from datetime import datetime
                age = (datetime.now() - datetime.fromisoformat(data.get("updated_at") or "")).total_seconds()
                return age <= 240
    except Exception:  # noqa: BLE001 — нет/битый файл прогресса → считаем, что не идёт
        pass
    return False


def _submit_in_progress() -> bool:
    """Идёт ли прямо сейчас какая-либо подача (ручная пачка или очередь автопилота)."""
    return _apply_runner_busy or _apply_progress_active()


def _desktop_busy_for_profile_switch() -> bool:
    with _connector_launch_lock:
        connector_busy = bool(_connector_launches)
    return _submit_in_progress() or bool(_sync_state.get("running")) or connector_busy


def _cloud_profile_enabled() -> bool:
    """Family candidates use the device's cloud binding, not the owner's login."""
    return account_mod.is_signed_in() or not candidate_profiles.is_primary()


def _tg_item_profile(item: dict) -> str:
    return str((item or {}).get("profileId") or candidate_profiles.PRIMARY_ID)


def _partition_tg_items(items: list, active_profile_id: str) -> tuple[list, list, list]:
    """Split a device queue into current, another valid candidate, and invalid."""
    current, other, invalid = [], [], []
    for item in items or []:
        if not isinstance(item, dict):
            invalid.append(item)
            continue
        profile_id = _tg_item_profile(item)
        if profile_id == active_profile_id:
            current.append(item)
        elif candidate_profiles.get_profile(profile_id):
            other.append(item)
        else:
            invalid.append(item)
    return current, other, invalid


def _claim_apply_slot() -> bool:
    """Занять «слот» ручной подачи. False — если подача уже идёт или была запущена
    только что (двойной клик). True — слот занят, можно запускать."""
    global _last_manual_apply_ts
    with _manual_apply_lock:
        if _submit_in_progress() or (time.time() - _last_manual_apply_ts) < 12:
            return False
        _last_manual_apply_ts = time.time()
        return True


def _claim_connector_launch(job_id: str, cooldown: float = 10.0) -> bool:
    """Prevent a double click from opening two assisted browser windows."""
    now = time.monotonic()
    key = str(job_id or "")
    with _connector_launch_lock:
        stale = [
            item for item, ts in _connector_launches.items()
            if now - ts > 300 and (
                _connector_processes.get(item) is None
                or _connector_processes[item].poll() is not None
            )
        ]
        for item in stale:
            _connector_launches.pop(item, None)
            _connector_processes.pop(item, None)
        active_proc = _connector_processes.get(key)
        if key in _connector_launches and active_proc is not None and active_proc.poll() is None:
            return False
        if now - _connector_launches.get(key, -cooldown) < cooldown:
            return False
        _connector_launches[key] = now
        return True


def _release_connector_launch(job_id: str) -> None:
    with _connector_launch_lock:
        key = str(job_id or "")
        _connector_launches.pop(key, None)
        _connector_processes.pop(key, None)


def _enqueue_auto_submit(ids) -> None:
    """Поставить пачку id в очередь автоматической подачи и при необходимости
    поднять воркер очереди. Дубликаты (id, который уже ждёт в очереди) отсеиваются."""
    clean, seen = [], set()
    for raw in ids or []:
        jid = str(raw or "").strip()
        if jid and jid not in seen:
            seen.add(jid)
            clean.append(jid)
    if not clean:
        return
    global _apply_runner_busy
    with _apply_queue_lock:
        queued = {jid for batch in _apply_queue for jid in batch}
        batch = [jid for jid in clean if jid not in queued]
        if batch:
            _apply_queue.append(batch)
        if not _apply_runner_busy and _apply_queue:
            _apply_runner_busy = True
            threading.Thread(target=_apply_runner_loop, daemon=True,
                             name="apply-runner").start()


def _wait_for_manual_apply_to_finish(timeout: float = 300.0) -> None:
    """Симметрия гонки (F24): если в момент старта очереди ещё идёт РУЧНАЯ подача
    (она могла начаться, пока очередь была пуста), подождём её завершения — иначе на
    одном профиле браузера откроются два процесса. После старта очереди ручная подача
    уже невозможна: она сверяется с _apply_runner_busy. Поэтому страхуем только старт.
    По дедлайну выходим (страховка от вечного ожидания; дальше держит профиль-lock)."""
    deadline = time.time() + timeout
    while time.time() < deadline and not _tg_stop.is_set():
        with _manual_apply_lock:
            just_claimed = (time.time() - _last_manual_apply_ts) < 15
        if not just_claimed and not _apply_progress_active():
            return
        _tg_stop.wait(2)


def _apply_runner_loop() -> None:
    """Воркер очереди: берёт пачку, запускает подачу, ждёт её завершения
    (следит и сообщает статусы в Mini App), затем берёт следующую пачку.
    Так в любой момент времени работает только один браузер."""
    global _apply_runner_busy
    # Дождаться завершения ручной подачи, если она шла, когда очередь была пуста.
    _wait_for_manual_apply_to_finish()
    while True:
        with _apply_queue_lock:
            if _tg_stop.is_set() or not _apply_queue:
                _apply_runner_busy = False
                return
            batch = _apply_queue.pop(0)
        spawn_ts = time.time()  # чтобы отличить НАШ файл прогресса от файла прошлой пачки
        proc = _spawn_salling_apply(batch, submit=True, auto_close=True)
        # следим за пачкой и шлём статусы, пока процесс жив
        _watch_and_report_apply_batch(batch, proc, spawn_ts=spawn_ts)
        # гарантированно освобождаем браузер перед следующей пачкой:
        # если процесс завис, снимаем его — иначе следующая пачка снова
        # упрётся в занятый профиль браузера
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        _sync_applied_to_cloud(force=True)  # сразу обновим «Поданные» в Mini App


def _worker_progress_for(spawn_ts: float) -> dict | None:
    """Прогресс воркера из apply_progress.json, если файл написан пачкой,
    запущенной не раньше spawn_ts. Иначе None — это ещё файл ПРОШЛОЙ пачки,
    его итогам про наши заявки верить нельзя."""
    try:
        p = config.DATA_DIR / "apply_progress.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        if _progress_started_ts(data) >= spawn_ts - 10:
            return data
    except Exception:  # noqa: BLE001 — нет/битый файл → итогов ещё нет
        pass
    return None


def _watch_and_report_apply_batch(job_ids: list[str], proc=None,
                                  spawn_ts: float | None = None) -> None:
    """Следит за пачкой подачи и разносит итоги (реестр заявок + Mini App).

    Шаг 4 (Блок 1): итог каждой заявки читаем из apply_progress.json — его пишет
    сам воркер apply.py по факту отправки («ok»/«failed»), а не угадываем по базе
    с дедлайном «180 секунд на заявку». Ждём столько, сколько живёт процесс
    воркера; страховка от зависшего — не тикающий дедлайн, а «прогресс не менялся
    10 минут». Заявки, которых воркер не касался (отсеяны страховкой перед
    запуском / воркер оборвался), в конце решаются по базе, как раньше."""
    ids = []
    seen = set()
    for raw in job_ids or []:
        jid = str(raw or "").strip()
        if jid and jid not in seen:
            seen.add(jid)
            ids.append(jid)
    if not ids:
        return
    if spawn_ts is None:
        spawn_ts = time.time()

    for jid in ids:
        _report_apply_result_safe(jid, "submitting", "WexFlow заполняет форму")

    pending = set(ids)

    def _settle(states: dict) -> None:
        """Разнести подтверждённые, неподтверждённые и ошибочные итоги."""
        ok_ids = [jid for jid in list(pending) if states.get(jid) == "ok"]
        unconfirmed_ids = [
            jid for jid in list(pending) if states.get(jid) == "unconfirmed"
        ]
        failed_ids = [jid for jid in list(pending) if states.get(jid) == "failed"]
        recorded_ids = ok_ids + unconfirmed_ids
        if recorded_ids:
            pending.difference_update(recorded_ids)
            try:
                with get_session() as s:
                    jobs = [s.get(Job, jid) for jid in recorded_ids]
                autopilot.record_submitted([j for j in jobs if j is not None])
            except Exception:  # noqa: BLE001 — реестр не должен ронять разбор итогов
                pass
            for jid in ok_ids:
                _report_apply_result_safe(
                    jid, "submitted",
                    "Сайт показал квитанцию и подтвердил получение заявки.",
                )
            for jid in unconfirmed_ids:
                _report_apply_result_safe(
                    jid, "unconfirmed",
                    "Форма исчезла, но сайт не показал квитанцию. Проверь письмо или кабинет Salling.",
                )
        if failed_ids:
            pending.difference_update(failed_ids)
            autopilot.clear_submitting(failed_ids)
            for jid in failed_ids:
                _report_apply_result_safe(jid, "failed", "Подача не подтверждена — проверь вручную")

    last_mark = None
    last_change = time.time()
    while pending and not _tg_stop.is_set():
        prog = _worker_progress_for(spawn_ts)
        items = (prog or {}).get("items") or []
        _settle({str(it.get("id")): str(it.get("state") or "") for it in items})
        if not pending:
            return
        if proc is None or proc.poll() is not None:
            break  # воркер завершился (или вовсе не запускался) — добор ниже
        mark = (prog or {}).get("updated_at")
        if mark != last_mark:
            last_mark = mark
            last_change = time.time()
        elif time.time() - last_change > 600:
            break  # воркер жив, но 10 минут не пишет прогресс — считаем зависшим
        _tg_stop.wait(3)

    if not pending:
        return
    # Добор: воркер мог дописать итог в самый последний момент — перечитываем
    # файл ещё раз. Для заявок, которых в итогах воркера нет вовсе, решаем по
    # базе: applied — значит подано, иначе честно «не подтверждено».
    prog = _worker_progress_for(spawn_ts)
    items = (prog or {}).get("items") or []
    _settle({str(it.get("id")): str(it.get("state") or "") for it in items})
    really_failed = []
    try:
        with get_session() as s:
            for jid in list(pending):
                job = s.get(Job, jid)
                if job is not None and job.status == "applied":
                    autopilot.record_submitted([job])
                    if str(job.applied_confidence or "").lower() == "receipt":
                        _report_apply_result_safe(
                            jid, "submitted",
                            "Сайт показал квитанцию и подтвердил получение заявки.",
                        )
                    else:
                        _report_apply_result_safe(
                            jid, "unconfirmed",
                            "Заявка сохранена без квитанции сайта. Проверь письмо или кабинет.",
                        )
                else:
                    really_failed.append(jid)
    except Exception:  # noqa: BLE001
        really_failed = list(pending)
    if really_failed:
        autopilot.clear_submitting(really_failed)
        for jid in really_failed:
            _report_apply_result_safe(jid, "failed", "Подача не подтверждена — проверь вручную")


def _apply_result_msg(state: str, reason: str) -> str:
    if state == "submitted":
        return "Уже подано"
    if state == "unconfirmed":
        return "Сохранено без квитанции — проверь письмо или кабинет"
    if state == "submitting":
        return "Подача уже запущена"
    return {
        "missing": "Вакансия больше не доступна",
        "inactive": "Вакансия уже неактуальна — закрыта или заявка подана",
        "stale": "Карточка больше не подходит под текущие фильтры",
        "launch_error": "Не удалось запустить подачу на ПК",
        "not_offered": "Заявка под эту вакансию не предлагалась — подача отклонена",
    }.get(reason or "", "Подача не запущена")


def _hydrate_tg_job_snapshot(decision: dict):
    """Restore a public vacancy into the requesting candidate's local DB.

    Telegram can show the device-wide public catalogue while another candidate
    is active. After the native shell restarts into the requester, that
    candidate's isolated database may not have scanned the selected job yet.
    The attached snapshot was sanitized by the cloud and originally came from
    this device; no candidate documents, credentials, or history are shared.
    """
    if not isinstance(decision, dict):
        return None
    job_id = str(decision.get("jobId") or "").strip()
    snapshot = decision.get("job")
    if not job_id or not isinstance(snapshot, dict):
        return None
    if str(snapshot.get("id") or snapshot.get("jobId") or "").strip() != job_id:
        return None
    allowed_sources = {
        "salling", "lidl", "teamtailor", "greenhouse", "ashby", "manual_link",
    }
    source = str(snapshot.get("source") or "salling").strip().lower()
    if source not in allowed_sources:
        source = "salling"
    with get_session() as session:
        existing = session.get(Job, job_id)
        if existing is not None:
            job = existing
        else:
            job = Job(
                id=job_id,
                source=source,
                title=str(snapshot.get("titleBase") or snapshot.get("title") or "Vacancy")[:300],
                brand=str(snapshot.get("brandCode") or "")[:120] or None,
                categories=str(snapshot.get("categoriesCode") or "")[:500] or None,
                region=str(snapshot.get("regionCode") or "")[:120] or None,
                city=str(snapshot.get("city") or "")[:160] or None,
                hours=str(snapshot.get("hoursRaw") or snapshot.get("hours") or "")[:80] or None,
                employment_type=str(snapshot.get("employmentType") or "")[:120] or None,
                job_level=str(snapshot.get("jobLevel") or "")[:120] or None,
                pay_rate=str(snapshot.get("payRate") or "")[:120] or None,
                start_date=str(snapshot.get("startDate") or "")[:80] or None,
                published=str(snapshot.get("publishedRaw") or snapshot.get("published") or "")[:80] or None,
                description=str(snapshot.get("snippet") or snapshot.get("descriptionSnippet") or "")[:1000] or None,
                application_link=str(snapshot.get("url") or "")[:1000] or None,
                lat=snapshot.get("lat") if isinstance(snapshot.get("lat"), (int, float)) else None,
                lon=snapshot.get("lon") if isinstance(snapshot.get("lon"), (int, float)) else None,
                status="new",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
    applications.mark_listed([job_id])
    return job


def _watch_connector_prepare_for_phone(job_id: str) -> None:
    """Довести пробный прогон коннектора до телефона.

    Телефон — пульт: нажал «Подготовить» — и должен видеть, что происходит на
    ПК. Воркер пишет свой статус в файл; отдаём его в облако как prepare-состояние
    (никакого «подано»: prepared значит «анкета заполнена, отправка НЕ нажата»).
    """
    wanted = str(job_id or "")
    path = _connector_status_path(wanted)
    deadline = time.monotonic() + 900       # 15 мин — дольше прогон не ждём
    last_state = ""                         # шлём в облако ТОЛЬКО смену этапа:
    while not _tg_stop.is_set():            # частые записи выедают лимит Redis
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        same = str(payload.get("job_id") or "") == wanted
        state = str(payload.get("state") or "") if same else ""
        message = str(payload.get("message") or "").strip()
        if state in ("ready", "submit_ready", "submitted"):
            _report_apply_result_safe(
                wanted, "prepared",
                message or "Анкета заполнена и ждёт тебя — отправка не нажата.",
            )
            _release_connector_launch(wanted)
            return
        if state in ("error", "site_changed"):
            _report_apply_result_safe(
                wanted, "prepare_failed",
                message or ("Сайт изменил анкету — подготовка остановлена."
                            if state == "site_changed"
                            else "Окно подготовки завершилось с ошибкой."),
            )
            _release_connector_launch(wanted)
            return
        if state and state != last_state:
            last_state = state
            _report_apply_result_safe(
                wanted, "preparing",
                message or "WexFlow открыл браузер и заполняет анкету.",
            )
        with _connector_launch_lock:
            proc = _connector_processes.get(wanted)
        if proc is not None and proc.poll() is not None:
            _report_apply_result_safe(
                wanted, "prepare_failed",
                "Окно закрылось раньше, чем анкета была заполнена.",
            )
            _release_connector_launch(wanted)
            return
        if time.monotonic() > deadline:
            _release_connector_launch(wanted)
            return
        _tg_stop.wait(2.0)


def _app_version() -> str:
    """Версия WexFlow — уезжает в панель телефона вместе с фильтрами."""
    try:
        import version as _v
        return str(_v.__version__)
    except Exception:  # noqa: BLE001
        return ""


TG_PREPARE_TTL_MS = 30 * 60 * 1000   # пробный прогон живёт полчаса


def _tg_prepare_expired(item: dict, now_ms: float | None = None) -> bool:
    """Устарел ли пробный прогон из очереди. Без ts (старое облако) — не трогаем."""
    try:
        ts = float((item or {}).get("ts") or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    now = now_ms if now_ms is not None else time.time() * 1000
    return (now - ts) > TG_PREPARE_TTL_MS


def _write_prepare_signal(job_id: str, action: str) -> None:
    """Положить решение из чата рядом с воркером, который держит анкету открытой."""
    try:
        path = config.prepare_signal_path(job_id)
        from json_store import atomic_write_json
        atomic_write_json(path, {"action": action, "ts": time.time()})
        print(f"prepare-signal: {job_id} → {action}")
    except Exception as e:  # noqa: BLE001
        print(f"prepare-signal: не записался — {e}")


def _run_tg_prepare(salling_ids: list, connector_jobs: list, allowed: set) -> None:
    """Пробный прогон с телефона: заполнить анкеты на ПК и НЕ отправлять.

    Ровно то же, что кнопка «Подготовить · без отправки» в приложении: окно
    открывается, поля заполняются, отправка не жмётся. Ничего не помечается
    поданным, вакансия остаётся неразобранной — это проверка связи телефон→ПК.
    Каждый шаг уходит в облако (preparing → prepared/prepare_failed), чтобы в
    панели телефона было видно, что именно делает компьютер.
    """
    blocked = 0
    blocked_ids = []
    ready_ids = []
    for jid in salling_ids:
        if jid in allowed:
            ready_ids.append(jid)
        else:
            blocked += 1
            blocked_ids.append(jid)
    opened = 0
    errors = []
    for job in connector_jobs:
        # F27: открываем только то, что WexFlow сам показывал
        if job.id not in allowed:
            blocked += 1
            blocked_ids.append(job.id)
            continue
        if not _claim_connector_launch(job.id):
            _report_apply_result_safe(
                job.id, "preparing", "Форма этой вакансии уже открывается на ПК.")
            continue
        _report_apply_result_safe(
            job.id, "preparing", "Открываю анкету на компьютере — без отправки.")
        try:
            _launch_connector_filler(job.application_link or "", job.id, submit=False)
            opened += 1
            threading.Thread(
                target=_watch_connector_prepare_for_phone,
                args=(job.id,),
                daemon=True,
                name=f"connector-prepare-{str(job.id)[:24]}",
            ).start()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:100])
            _release_connector_launch(job.id)
            _report_apply_result_safe(
                job.id, "prepare_failed",
                f"Не удалось открыть форму на ПК: {str(exc)[:100]}")
    if ready_ids:
        for jid in ready_ids:
            _report_apply_result_safe(
                jid, "preparing", "Открываю анкету на компьютере — без отправки.")
        try:
            _launch_salling_apply(ready_ids, submit=False, phone_confirm=True)
            opened += len(ready_ids)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:100])
            for jid in ready_ids:
                _report_apply_result_safe(
                    jid, "prepare_failed",
                    f"Не удалось запустить прогон на ПК: {str(exc)[:100]}")
    for jid in blocked_ids:
        _report_apply_result_safe(
            jid, "prepare_failed",
            "Этой вакансии нет в списке WexFlow — форму не открываю.")

    if opened:
        text = (
            "🧪 <b>Пробный прогон без отправки</b>\n"
            f"Открываю на компьютере анкет: {opened}. WexFlow заполнит поля и "
            "остановится перед отправкой — заявка не уйдёт.\n"
            "Посмотри окно на ПК: так же выглядит и настоящая подача."
        )
    else:
        text = ("⚠️ <b>Пробный прогон не начался</b>\n"
                "Не нашёл, что открыть без отправки.")
    if blocked:
        text += f"\nПропустил вакансий, которых не было в списке WexFlow: {blocked}."
    if errors:
        text += "\nОшибка на ПК: " + errors[0]
    cloud_auth.send_digest(text)


def _handle_tg_decisions(decisions: list) -> None:
    submit_ids = []
    connector_jobs = []
    prepare_ids = []          # пробный прогон Salling: заполнить и не отправлять
    prepare_connectors = []   # то же для коннекторов (Lidl и др.)
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        jid = d.get("jobId")
        action = d.get("action")
        if not jid or jid == "__demo__" or action not in (
                "submit", "skip", "prepare", "prepare_submit", "prepare_cancel"):
            continue
        with get_session() as session:
            job = session.get(Job, jid)
        if job is None:
            job = _hydrate_tg_job_snapshot(d)
        if action == "skip":
            if job is None or getattr(job, "source", "salling") == "salling":
                autopilot.tg_decide(jid, approve=False, launcher=lambda ids: None)
            continue
        if action in ("prepare_submit", "prepare_cancel"):
            # кнопка под скрином подготовленной анкеты: воркер держит её открытой
            # и ждёт этого сигнала. Отправку подтвердил ЧЕЛОВЕК, а не автопилот.
            _write_prepare_signal(jid, "submit" if action == "prepare_submit" else "cancel")
            continue
        if action == "prepare":
            # «Подготовить без отправки» с телефона — то же, что кнопка
            # «Подготовить» в приложении. Ничего не помечаем поданным.
            # Прогон — действие «здесь и сейчас»: если ПК спал полчаса и дольше,
            # он НЕ должен вдруг сам открыть браузер (человек уже не у экрана).
            if _tg_prepare_expired(d):
                _report_apply_result_safe(
                    jid, "prepare_failed",
                    "Прогон устарел — ПК был офлайн. Нажми «Подготовить» ещё раз.")
                continue
            if job is not None and getattr(job, "source", "salling") != "salling":
                prepare_connectors.append(job)
            else:
                prepare_ids.append(jid)
            continue
        if job is not None and getattr(job, "source", "salling") != "salling":
            connector_jobs.append(job)
        else:
            submit_ids.append(jid)

    allowed = applications.offered_ids() | applications.listed_ids()
    if prepare_ids or prepare_connectors:
        _run_tg_prepare(prepare_ids, prepare_connectors, allowed)
    for job in connector_jobs:
        if job.id not in allowed:
            _report_apply_result_safe(
                job.id, "failed",
                "Вакансия не была показана в WexFlow — открытие формы отклонено",
            )
            continue
        source = getattr(job, "source", "") or "manual_link"
        state = applications.state_of(job.id, source=source)
        if state in {"submitting", "submitted"}:
            _report_apply_result_safe(
                job.id, state,
                "Форма уже открыта на ПК" if state == "submitting" else "Уже подано",
            )
            continue
        if not _claim_connector_launch(job.id):
            _report_apply_result_safe(job.id, "submitting", "Форма уже открывается на ПК")
            continue
        applications.mark_submitting([job.id], origin="telegram", source=source)
        try:
            # Режим подачи выбирает человек в настройках: «отправлять самому»
            # или «только заполнять». Даже в auto отправки не будет, пока есть
            # неотвеченный вопрос анкеты — это проверяет коннектор.
            auto_send = (getattr(job, "source", "") == "lidl"
                         and settings_store.get_apply_mode() == "auto")
            _launch_connector_filler(
                job.application_link or "",
                job.id,
                submit=auto_send,
            )
            _report_apply_result_safe(
                job.id,
                "submitting",
                (
                    "Форма Lidl открыта на ПК. Отвечаю сохранёнными ответами и подаю; "
                    "если на какой-то вопрос ответа нет — остановлюсь и скажу."
                    if auto_send else
                    "Форма открыта на ПК и заполнена. Проверь ответы и отправь сам."
                ),
            )
            threading.Thread(
                target=_watch_connector_result_for_phone,
                args=(job.id, source),
                daemon=True,
                name=f"connector-result-{str(job.id)[:24]}",
            ).start()
        except Exception as exc:  # noqa: BLE001
            _release_connector_launch(job.id)
            applications.mark_failed([job.id], source=source)
            _report_apply_result_safe(
                job.id, "failed", f"Не удалось открыть форму на ПК: {str(exc)[:100]}"
            )

    if not submit_ids:
        return

    result = autopilot.tg_submit_batch(
        submit_ids,
        launcher=lambda ids: _launch_salling_apply(ids, submit=True, track_autopilot=True),
    )
    for item in result.get("skipped") or []:
        jid = item.get("job_id")
        state = item.get("state")
        if jid and state in ("submitting", "submitted", "unconfirmed", "failed"):
            _report_apply_result_safe(jid, state, _apply_result_msg(state, item.get("reason", "")))


_applied_sync_last = 0.0
_jobs_sync_last = 0.0
_jobtexts_sync_last = 0.0
_jobtexts_sent = {}   # jobId -> отпечаток последнего отправленного текста (не гонять то же)
_cloud_sync_attempt_last = {"applied": 0.0, "jobs": 0.0, "filters": 0.0, "jobtexts": 0.0}
_cloud_sync_fail_streak = {"applied": 0, "jobs": 0, "filters": 0, "jobtexts": 0}
_cloud_sync_sent_hash = {}   # kind -> отпечаток последнего УСПЕШНО отправленного тела


def _sync_digest(payload) -> str:
    """Отпечаток тела синка. Если он не изменился с прошлой отправки — не гоним то
    же самое в облако повторно (пустая трата команд бесплатного Redis)."""
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return ""
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _begin_cloud_sync(kind: str, last_success: float, interval: int,
                      force: bool = False) -> float | None:
    """Start a due sync; repeated cloud failures get exponential backoff."""
    now = time.time()
    if not force and now - last_success < interval:
        return None
    failures = int(_cloud_sync_fail_streak.get(kind, 0))
    retry_after = min(1800, 30 * (2 ** min(failures, 6))) if failures else 30
    if not force and now - _cloud_sync_attempt_last.get(kind, 0.0) < retry_after:
        return None
    _cloud_sync_attempt_last[kind] = now
    return now


def _finish_cloud_sync(kind: str, success: bool) -> None:
    if success:
        _cloud_sync_fail_streak[kind] = 0
    else:
        _cloud_sync_fail_streak[kind] = min(
            10, int(_cloud_sync_fail_streak.get(kind, 0)) + 1
        )


def _sync_applied_to_cloud(force: bool = False) -> bool:
    """Одно облако: периодически шлём в облако список недавно поданных вакансий —
    чтобы раздел «Поданные» в Mini App был виден (подал на ПК → видно в телефоне),
    а поданные карточки ушли из «ждут решения»."""
    global _applied_sync_last
    if not _cloud_profile_enabled():
        return False
    attempt = _begin_cloud_sync("applied", _applied_sync_last, 300, force)
    if attempt is None:
        return False
    try:
        with get_session() as s:
            jobs = s.exec(
                select(Job).where(Job.status == "applied")
                .order_by(Job.applied_at.desc()).limit(60)
            ).all()
        items = []
        for job in jobs:
            try:
                ts = job.applied_at.isoformat() if job.applied_at else ""
            except Exception:  # noqa: BLE001
                ts = ""
            brand_bg, brand_fg = labels.BRAND_COLORS.get(document_rules.brand_key(job), ("", ""))
            items.append({
                "id": job.id,
                # title остаётся «как в чате» (его читают старые панели), но
                # телефон рисует карточку по отдельным полям — иначе название,
                # ID, часы и адрес слипались в одну обрезанную строку.
                "title": _tg_display_title(job),
                "titleBase": job.title or "",
                "brand": labels.brand(job.brand) if job.brand else "",
                "brandColor": brand_bg,
                "brandFg": brand_fg,
                "city": job.city or "",
                "address": _job_address(job),
                "hours": f"{job.hours} ч/нед" if job.hours else "",
                "employment": labels.EMPLOYMENT.get(job.employment_type or "", job.employment_type or ""),
                "source": job.source or "salling",
                "sourceLabel": "" if (job.source or "salling") == "salling"
                               else _SHORT_SOURCES.get(job.source or "", job.source or ""),
                "url": job.application_link or "",
                "ts": ts,
                "confidence": str(job.applied_confidence or "").strip().lower(),
            })
        digest = _sync_digest(items)
        if not force and digest and _cloud_sync_sent_hash.get("applied") == digest:
            _applied_sync_last = attempt   # список «Поданных» не изменился — не шлём
            _finish_cloud_sync("applied", True)
            return False
        if cloud_auth.report_applied(items):
            _applied_sync_last = attempt
            if digest:
                _cloud_sync_sent_hash["applied"] = digest
            _finish_cloud_sync("applied", True)
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"applied-sync: ошибка — {e}")
    _finish_cloud_sync("applied", False)
    return False


CLOUD_JOBS_LIMIT = 500   # сколько вакансий держим в телефоне (как в приложении, но с потолком)


def _all_active_jobs() -> list:
    """Активные вакансии — ровно тот набор, что показывает главный экран
    приложения (без закрытых, скрытых и поданных)."""
    with get_session() as s:
        return list(s.exec(select(Job).where(
            Job.status.not_in(["closed", "hidden", "applied"]),
            Job.applied_at.is_(None),
        )).all())


def _cloud_job_list(limit: int = CLOUD_JOBS_LIMIT) -> list[tuple]:
    """Вакансии для телефона: сначала подходящие под фильтры, затем остальные
    активные — тем же порядком, что на главном экране приложения (с домашним
    адресом ближние сверху, без него — свежие). Возвращает [(job, is_match)].

    Полностью весь список (тысячи вакансий) в облако не отправляем: телефон
    качал бы мегабайты на каждом открытии панели, а лимит Upstash сгорал бы за
    дни. Потолок CLOUD_JOBS_LIMIT покрывает всё, до чего реально можно доехать.
    """
    limit = max(0, int(limit))
    skip = applications.submitted_ids() | applications.skipped_ids() | applications.submitting_ids()
    home = settings_store.get_home()

    matches = [j for j in autopilot.find_matches() if j.id not in skip]
    matches.sort(key=lambda j: getattr(j, "first_seen", None) or utcnow(), reverse=True)
    matches = matches[:limit]
    out: list[tuple] = [(j, True) for j in matches]
    if len(out) >= limit:
        return out

    taken = {j.id for j in matches}
    rest = [j for j in _all_active_jobs() if j.id not in taken and j.id not in skip]

    def _km(job) -> float:
        if not home or job.lat is None or job.lon is None:
            return 10 ** 9
        try:
            return geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon)
        except Exception:  # noqa: BLE001
            return 10 ** 9

    def _seen(job):
        t = getattr(job, "first_seen", None) or utcnow()
        return t.replace(tzinfo=None) if getattr(t, "tzinfo", None) else t

    if home:
        rest.sort(key=lambda j: (_km(j), -_seen(j).timestamp()))
    else:
        rest.sort(key=_seen, reverse=True)
    out.extend((j, False) for j in rest[: limit - len(out)])
    return out


def _sync_jobs_to_cloud(force: bool = False) -> bool:
    """Фаза 2b: телефон видит не только офферы автопилота, а тот же список
    вакансий, что и приложение (подходящие + ближайшие активные).
    Синк троттлим, чтобы не жечь Upstash.
    """
    global _jobs_sync_last
    if not _cloud_profile_enabled():
        return False
    attempt = _begin_cloud_sync("jobs", _jobs_sync_last, 900, force)
    if attempt is None:
        return False
    try:
        pairs = _cloud_job_list()
        jobs = [j for j, _ in pairs]
        home = settings_store.get_home()
        # Название переводим онлайн только для подходящих: у остальных берём
        # уже готовый перевод из кэша, иначе один синк = сотни запросов к
        # переводчику. Роль по-русски (roleRu) считается локально и есть у всех.
        tcache = transit.snapshot()   # один раз на весь список, а не 500 чтений
        try:                          # первые карточки телефона — в начало очереди
            import transit_worker
            transit_worker.request([
                j for j, _ in pairs[:60]
                if j.lat is not None and j.lon is not None
                and transit.from_snapshot(tcache, home["lat"], home["lon"], j.lat, j.lon) is None
            ] if home else [])
        except Exception:  # noqa: BLE001
            pass
        payload = [_tg_job_payload(j, is_match=m, home=home, translate_title=m, lean=True,
                                   transit_cache=tcache)
                   for j, m in pairs]
        digest = _sync_digest(payload)
        if not force and digest and _cloud_sync_sent_hash.get("jobs") == digest:
            _jobs_sync_last = attempt
            _finish_cloud_sync("jobs", True)
            return False
        if cloud_auth.report_jobs(payload):
            applications.mark_listed([j.id for j in jobs])
            _jobs_sync_last = attempt
            if digest:
                _cloud_sync_sent_hash["jobs"] = digest
            _finish_cloud_sync("jobs", True)
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"jobs-sync: ошибка — {e}")
    _finish_cloud_sync("jobs", False)
    return False


def _translate_job_now(job_id: str) -> bool:
    """Перевести описание ОДНОЙ вакансии по запросу из панели и сразу отправить
    её текст в облако. Нужно для вакансий вне фильтров: фоновый воркер переводит
    только подходящие, а в телефоне теперь виден список шире."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return False
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None or not (job.description or "").strip():
            return False
        title, description, ru = job.title, job.description, job.description_ru
    if not translator._plain_text(ru or ""):
        try:
            ru_html = translator.translate_to_ru(description, title=title or "")
        except translator.TranslationError as e:
            print(f"translate-on-demand: переводчик недоступен — {e}")
            ru_html = ""
        if translator._plain_text(ru_html or ""):
            with get_session() as s:
                fresh = s.get(Job, job_id)
                if fresh is not None:
                    fresh.description_ru = ru_html
                    s.add(fresh)
                    s.commit()
            ru = ru_html
    ru_plain = translator._plain_text(ru or "")
    # Перевод либо получился (done), либо переводчик не смог — тогда честное
    # «unavailable»: панель покажет оригинал и не будет обещать перевод.
    item = {
        "id": job_id,
        "ru": ru_plain[:6000],
        "orig": translator._plain_text(description or "")[:6000],
        "st": "done" if ru_plain else "unavailable",
    }
    if cloud_auth.report_job_texts([item]):
        _jobtexts_sent[job_id] = f"{item['st']}:{len(item['ru'])}:{len(item['orig'])}"
        return True
    return False


def _sync_job_texts_to_cloud(force: bool = False) -> bool:
    """Полные тексты вакансий (перевод + оригинал) в облако — для экрана детали
    в Mini App. Переведённые уходят как st=done, ещё непереведённые — как
    pending (панель покажет датский + «готовится»), а если переводчик совсем
    недоступен — unavailable. Отпечатки отправленного не гоняем повторно."""
    global _jobtexts_sync_last
    if not _cloud_profile_enabled():
        return False
    attempt = _begin_cloud_sync("jobtexts", _jobtexts_sync_last, 600, force)
    if attempt is None:
        return False
    try:
        import translate_worker
        # Заранее шлём тексты только подходящих: их переводит фоновый воркер.
        # Остальные вакансии (в телефоне список теперь шире, как в приложении)
        # переводятся по запросу — панель просит командой translate, когда
        # человек реально открыл карточку. Иначе 500 описаний жгли бы и
        # переводчик, и лимит облака впустую.
        skip = applications.submitted_ids() | applications.skipped_ids() | applications.submitting_ids()
        jobs = [j for j in autopilot.find_matches() if j.id not in skip]
        jobs.sort(key=lambda j: getattr(j, "first_seen", None) or utcnow(), reverse=True)
        down = translate_worker.is_translator_down()
        items, batch = [], 0
        for job in jobs:
            if batch >= 12:
                break
            if not (job.description or "").strip():
                continue
            ru = translator._plain_text(job.description_ru or "")
            if ru:
                st, orig = "done", translator._plain_text(job.description)
            else:
                st, orig = ("unavailable" if down else "pending"), translator._plain_text(job.description)
            item = {"id": job.id, "ru": ru[:6000], "orig": orig[:6000], "st": st}
            fp = f"{st}:{len(item['ru'])}:{len(item['orig'])}"
            if _jobtexts_sent.get(job.id) == fp:
                continue   # уже отправляли ровно это — не гоняем
            items.append(item)
            batch += 1
        if not items:
            _jobtexts_sync_last = attempt
            _finish_cloud_sync("jobtexts", True)
            return True
        if cloud_auth.report_job_texts(items):
            for it in items:
                _jobtexts_sent[it["id"]] = f"{it['st']}:{len(it['ru'])}:{len(it['orig'])}"
            _jobtexts_sync_last = attempt
            _finish_cloud_sync("jobtexts", True)
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"jobtexts-sync: ошибка — {e}")
    _finish_cloud_sync("jobtexts", False)
    return False


# Фильтры, которые можно менять с телефона. Автоотправка/лимиты сюда сознательно
# НЕ входят: включать необратимое с телефона нельзя (правило из плана автопилота).
_REMOTE_FILTER_AGES = ("", "under18", "adult")


def _sanitize_remote_filters(fields: dict) -> dict:
    """Отфильтровать команду set_filters из облака до безопасного словаря.

    Берём только известные ключи и только осмысленные значения; всё прочее
    молча отбрасываем (облако — недоверенный вход, см. F27/шаг 5). Возвращаем
    ТОЛЬКО присланные ключи: save_profile_filters дольёт остальные из профиля."""
    if not isinstance(fields, dict):
        return {}
    out: dict = {}

    def _num(key):
        if key not in fields:
            return
        raw = str(fields.get(key) or "").strip().replace(",", ".")
        if raw == "":
            out[key] = ""
            return
        try:
            n = float(raw)
        except (ValueError, OverflowError):
            return
        if 0 < n <= 10000:
            out[key] = str(int(n) if n == int(n) else n)

    def _codes(key, known):
        if key not in fields:
            return
        vals = [v.strip() for v in str(fields.get(key) or "").split(",") if v.strip()]
        out[key] = ",".join(v for v in vals if v in known)

    def _text_csv(key, max_items=10, max_len=40):
        """Свободный текст CSV (города/слова): режем длину и число элементов,
        выкидываем управляющие символы — дальше это только ДАННЫЕ для сравнения."""
        if key not in fields:
            return
        items, seen = [], set()
        for part in str(fields.get(key) or "").split(","):
            item = re.sub(r"[\x00-\x1f<>]", "", part).strip()[:max_len]
            low = item.casefold()
            if item and low not in seen:
                seen.add(low)
                items.append(item)
            if len(items) >= max_items:
                break
        out[key] = ", ".join(items)

    def _hour(key):
        if key not in fields:
            return
        try:
            h = int(float(str(fields.get(key) or "0").strip()))
        except (ValueError, OverflowError):
            return
        if 0 <= h <= 24:
            out[key] = h

    _num("max_km")
    _num("min_hours")
    _num("max_hours")
    if "age" in fields:
        age = str(fields.get("age") or "").strip()
        if age in _REMOTE_FILTER_AGES:
            out["age"] = age
    _codes("category", set(labels.CATEGORY))
    _codes("brand", set(labels.BRANDS))
    _text_csv("cities")
    _text_csv("keywords")
    _text_csv("exclude_keywords")
    _hour("active_from")   # rule-уровень: обработчик команды вынет их отдельно
    _hour("active_to")
    return out


_filters_sync_last = 0.0


def _autopilot_reason() -> str:
    """Короткое объяснение для панели: почему сейчас нечего показывать.
    Пусто — значит объяснять нечего (работает и есть что предлагать)."""
    try:
        rule = autopilot.get_rule()
        if autopilot.get_mode() == "off":
            return "Автопилот на паузе — новые вакансии не приходят."
        if not autopilot.within_schedule():
            return (f"Сейчас вне часов активности "
                    f"({int(rule.get('active_from') or 0)}:00–{int(rule.get('active_to') or 24)}:00) — "
                    "жду подходящего времени.")
        stats = autopilot.tg_queue_stats()
        if not stats.get("found"):
            return "Под твои фильтры сейчас ничего не подходит — измени их ниже."
        if not stats.get("eligible_current"):
            return ("Всё подходящее уже предлагал или разобрано — новые появятся, "
                    "когда выйдут свежие вакансии.")
        if autopilot.tg_daily_remaining() <= 0:
            return "Дневной потолок карточек исчерпан — продолжу завтра."
        return ""
    except Exception:  # noqa: BLE001 — объяснение не должно ронять синк
        return ""


_questions_sync_hash = ""


def _sync_questions_to_cloud(force: bool = False) -> bool:
    """Банк вопросов анкет — в облако, чтобы отвечать и с телефона.

    Шлём только при изменении: вопросы появляются редко, а лимит Upstash общий.
    """
    global _questions_sync_hash
    if not _cloud_profile_enabled():
        return False
    try:
        items = form_questions.cloud_payload(
            profile_answers=profile_store.answers(profile_store.load_profile()))
        digest = _sync_digest(items)
        if not force and digest == _questions_sync_hash:
            return False
        if cloud_auth.report_questions(items):
            _questions_sync_hash = digest
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"questions-sync: ошибка — {e}")
    return False


def _sync_filters_to_cloud(force: bool = False) -> bool:
    """Панель Mini App показывает и меняет фильтры первого набора. Шлём текущие
    значения + варианты (категории/сети со счётчиками), чтобы панель ничего не
    выдумывала сама. Троттлинг — как у jobs_sync."""
    global _filters_sync_last
    if not _cloud_profile_enabled():
        return False
    attempt = _begin_cloud_sync("filters", _filters_sync_last, 900, force)
    if attempt is None:
        return False
    try:
        profs = autopilot.ensure_profiles()
        prof = profs[0]

        def _soft_num(key, pick):
            vals = []
            for v in str(prof.get(key) or "").split(","):
                try:
                    vals.append(float(v.strip()))
                except ValueError:
                    continue
            if not vals:
                return ""
            n = pick(vals)
            return str(int(n) if n == int(n) else n)

        with get_session() as s:
            fc = _active_counts(s)
        cat_options = sorted(
            ((code, lbl, int(fc["category"].get(code, 0))) for code, lbl in labels.CATEGORY.items()),
            key=lambda t: -t[2],
        )
        brand_options = sorted(
            ((code, lbl, int(fc["brand"].get(code, 0))) for code, lbl in labels.BRANDS.items()),
            key=lambda t: -t[2],
        )
        sel_cats = {c.strip() for c in str(prof.get("category") or "").split(",") if c.strip()}
        payload = {
            "values": {
                "max_km": _soft_num("max_km", max),
                "min_hours": _soft_num("min_hours", min),
                "max_hours": _soft_num("max_hours", max),
                "age": str(prof.get("age") or "").strip(),
                "category": str(prof.get("category") or "").strip(),
                "brand": str(prof.get("brand") or "").strip(),
                "cities": str(prof.get("cities") or "").strip(),
                "keywords": str(prof.get("keywords") or "").strip(),
                "exclude_keywords": str(prof.get("exclude_keywords") or "").strip(),
                "active_from": str(int(autopilot.get_rule().get("active_from") or 0)),
                "active_to": str(int(autopilot.get_rule().get("active_to") or 24)),
            },
            "options": {
                # топ-12 категорий + все выбранные (даже редкие) — панели хватает
                "categories": [
                    [c, l, n] for i, (c, l, n) in enumerate(cat_options)
                    if (i < 12 and n) or c in sel_cats
                ],
                "brands": [[c, l, n] for c, l, n in brand_options if n],
            },
            "profileName": str(prof.get("name") or "Набор 1"),
            "profilesTotal": len(profs),
            # версия WexFlow на ПК: в панели сразу видно, что компьютер старый —
            # иначе «кнопка не работает» выглядит как поломка облака
            "appVersion": _app_version(),
            "matchCount": autopilot.profile_match_count(prof),
            # живое состояние автопилота — для карточки в панели (управление с телефона)
            "autopilot": {
                "mode": autopilot.get_mode(),
                # почему в «ждут решения» пусто: раньше телефон об этом молчал
                "reason": _autopilot_reason(),
                "found": autopilot.match_count(),
                "submittedToday": autopilot.submitted_today(),
                "submittedTotal": autopilot.submitted_total(),
                "dailyLimit": int(autopilot.get_rule().get("daily_limit") or 0),
                "submitScope": str(autopilot.get_rule().get("submit_scope") or "new"),
            },
        }
        digest = _sync_digest(payload)
        if not force and digest and _cloud_sync_sent_hash.get("filters") == digest:
            _filters_sync_last = attempt
            _finish_cloud_sync("filters", True)
            return False
        if cloud_auth.report_filters(payload):
            _filters_sync_last = attempt
            if digest:
                _cloud_sync_sent_hash["filters"] = digest
            _finish_cloud_sync("filters", True)
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"filters-sync: ошибка — {e}")
    _finish_cloud_sync("filters", False)
    return False


# Холостой «пульс» на бесплатном облачном Redis: у него жёсткий лимит команд за
# период, и частый пульс (6 c) выедал его за ~3 дня → «команды не доходят до ПК».
# 120 c снижают постоянный расход примерно втрое. После найденной работы интервал
# временно падает до 2 c, поэтому серия команд остаётся отзывчивой.
TG_IDLE_POLL_SEC = 120
# Пока панель открыта в телефоне, облако помечает устройство «живым», и ПК
# слушает часто: телефон — пульт, ждать реакции 2 минуты нельзя. Флаг в облаке
# живёт 90 c, поэтому закрытая панель возвращает экономный пульс сама.
TG_LIVE_POLL_SEC = 4


def _tg_poll_delay(fail_streak: int, signed_in: bool, had_work: bool = False,
                   panel_active: bool = False) -> int:
    """Адаптивный интервал: быстрый ответ после работы, быстрый пульс пока
    человек держит панель открытой, редкий холостой heartbeat (бережём лимит
    облачного Redis) и экспоненциальный backoff при сбоях сети."""
    if fail_streak > 0:
        return min(900, 15 * (2 ** min(fail_streak - 1, 6)))
    if had_work:
        return 2
    if panel_active and signed_in:
        return TG_LIVE_POLL_SEC
    return TG_IDLE_POLL_SEC if signed_in else 20


def _tg_poller_loop() -> None:
    """Опрашивает облако: какие решения (✅/❌) принял пользователь под карточками,
    и выполняет их локально (подать/пропустить).

    Заменяет старый getUpdates: бот теперь общий и работает через webhook, поэтому
    нажатия кнопок собирает облако, а приложение забирает готовые решения."""
    binding_sync_last = 0.0
    panel_active = False
    while not _tg_stop.is_set():
        signed_in = False
        had_work = False
        try:
            if candidate_profiles.is_primary():
                _sync_account_from_cloud()
            signed_in = _cloud_profile_enabled()
            # Явный локальный выход означает «не слушать старый Telegram».
            # Войти снова можно только осознанно со страницы аккаунта.
            if not signed_in:
                _tg_poll_state.update({"fail_streak": 0, "last_error": ""})
                _tg_stop.wait(_tg_poll_delay(0, False))
                continue

            tg_id = account_mod.load().get("tg_id") or ""
            sync_binding = time.time() - binding_sync_last >= 600
            cycle = cloud_auth.fetch_poll(
                tg_id=str(tg_id),
                sync_binding=sync_binding,
            )
            if cycle is None:
                panel_active = False
                _tg_poll_state["fail_streak"] = int(_tg_poll_state.get("fail_streak") or 0) + 1
                poll_error = cloud_auth.last_poll_error()
                _tg_poll_state["last_error"] = (
                    poll_error.get("error") or "Нет связи с облаком Telegram"
                )
                _tg_poll_state["error_code"] = poll_error.get("code") or ""
            else:
                if sync_binding:
                    binding_sync_last = time.time()
                _tg_poll_state.update({
                    "fail_streak": 0,
                    "last_ok": time.time(),
                    "last_error": "",
                    "error_code": "",
                })
                decisions = cycle.get("decisions") or []
                commands = cycle.get("commands") or []
                had_work = bool(decisions or commands)
                panel_active = bool(cycle.get("active"))
                _tg_poll_state["panel_active"] = panel_active
                active_profile_id = candidate_profiles.active_profile_id()
                current_decisions, other_decisions, invalid_decisions = _partition_tg_items(
                    decisions, active_profile_id)
                current_commands, other_commands, invalid_commands = _partition_tg_items(
                    commands, active_profile_id)
                _handle_tg_decisions(current_decisions)
                _sync_applied_to_cloud()  # одно облако: держим «Поданные» свежими (троттлинг 30с)
                _sync_jobs_to_cloud()     # фаза 2b: список подходящих вакансий в Mini App
                _sync_job_texts_to_cloud()  # полные тексты вакансий для экрана детали в панели
                _sync_filters_to_cloud()  # текущие фильтры + варианты для настройки с телефона
                _sync_questions_to_cloud()  # вопросы анкет: на них отвечают и с телефона
                for cmd in current_commands:
                    if _tg_remote_command_expired(cmd):
                        continue
                    result_text = _handle_tg_remote_command(cmd)
                    if result_text:  # пустой ответ (напр. ИИ-диалог) в чат не шлём
                        cloud_auth.send_command_result(cmd, result_text)
                for cmd in invalid_commands:
                    if isinstance(cmd, dict):
                        cloud_auth.send_command_result(
                            cmd, "⚠️ Профиль этой команды не найден на компьютере.")
                # Подтверждаем (и тем «сливаем» очередь) только когда реально что-то
                # пришло. Пустой ack на каждом холостом пульсе = лишний HTTP-запрос и
                # лишние команды Redis — именно это доедало бесплатный лимит облака.
                handled_decisions = current_decisions + invalid_decisions
                handled_commands = current_commands + invalid_commands
                if cycle.get("ack") and (handled_decisions or handled_commands):
                    cloud_auth.acknowledge_poll(handled_decisions, handled_commands)

                # A command for another family member stays unacknowledged. Once
                # the current candidate is idle, ask the native shell for the same
                # clean restart used by manual profile switching. The new worker
                # will poll and execute this exact queue item in the right profile.
                waiting = other_decisions + other_commands
                if waiting and not _desktop_busy_for_profile_switch():
                    target_id = _tg_item_profile(waiting[0])
                    candidate_profiles.request_remote_switch(target_id)
                    had_work = True
        except Exception as e:  # noqa: BLE001 — слушатель не должен падать
            _tg_poll_state["fail_streak"] = int(_tg_poll_state.get("fail_streak") or 0) + 1
            _tg_poll_state["last_error"] = str(e)[:180]
            print(f"telegram(cloud): ошибка опроса решений — {e}")
        _tg_stop.wait(_tg_poll_delay(
            int(_tg_poll_state.get("fail_streak") or 0), signed_in, had_work, panel_active))


def _ensure_tg_poller() -> None:
    """Запустить слушатель Telegram, если он ещё не работает."""
    global _tg_thread
    if _tg_thread and _tg_thread.is_alive():
        return
    _tg_stop.clear()
    _tg_thread = threading.Thread(target=_tg_poller_loop, daemon=True, name="tg-poller")
    _tg_thread.start()


TG_MAX_PER_SCAN = 3  # не больше карточек на подтверждение за один фоновый скан
TG_REMOTE_COMMAND_TTL_MS = 10 * 60 * 1000


_title_ru_cache: dict[str, str] = {}


def _title_ru(title: str, cached_only: bool = False) -> str:
    """Русский перевод названия вакансии для карточки (для тех, кто не знает датский).
    Кэшируется в памяти; при сбое перевода тихо возвращает пусто.

    cached_only=True — не ходить в переводчик: так собирается длинный список для
    телефона (сотни вакансий), иначе один синк устроил бы сотни запросов подряд.
    Понятность не страдает: в карточке есть роль по-русски (role_summary)."""
    title = (title or "").strip()
    if not title:
        return ""
    if title in _title_ru_cache:
        return _title_ru_cache[title]
    if cached_only:
        return ""
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
    public_id = _job_public_id(job)
    if public_id:
        lines.append(f"ID: <code>{e(public_id)}</code>")
    ru_title = _title_ru(job.title)
    if ru_title and ru_title.lower() != (job.title or "").strip().lower():
        lines.append(f"   ↳ <i>{e(ru_title)}</i>")
    if job.brand:
        lines.append(f"🏪 {e(labels.brand(job.brand))}")
    # местоположение + расстояние от дома, если знаем
    loc = (job.city or "").strip()
    home = None
    try:
        home = settings_store.get_home()
        if home and job.lat is not None and job.lon is not None:
            km = round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon))
            loc = f"{loc} · ~{km} км от дома" if loc else f"~{km} км от дома"
    except Exception:  # noqa: BLE001
        pass
    if loc:
        lines.append(f"📍 {e(loc)}")
    address = _job_address(job)
    if address:
        lines.append(f"📌 {e(address)}")
        lines.append(f'🗺 <a href="{e(_maps_url(job, home))}">Открыть адрес в картах</a>')
    elif job.lat is not None and job.lon is not None:
        lines.append(f'🗺 <a href="{e(_maps_url(job, home))}">Открыть точку в картах</a>')
    if job.hours:
        lines.append(f"🕒 {e(job.hours)} ч/нед")
    if job.application_link:
        lines.append(f'🔗 <a href="{e(job.application_link)}">Открыть вакансию на сайте</a>')
    return ("🆕 <b>Новая подходящая вакансия</b>\n"
            "──────────────\n"
            + "\n".join(lines)
            + "\n──────────────\n"
            "Подать заявку от твоего имени?")


def _job_public_id(job) -> str:
    return str(job.requisition_id or job.id or "").strip()


def _job_short_id(job) -> str:
    raw = _job_public_id(job)
    if not raw:
        return ""
    return raw if len(raw) <= 12 else raw[-8:]


def _job_detail_bits(job) -> list[str]:
    bits = []
    if job.hours:
        bits.append(f"{job.hours} ч/нед")
    address = _job_address(job)
    if address:
        short_address = address.replace(", DK", "").replace(", Denmark", "")
        bits.append(short_address)
    elif job.city:
        bits.append(job.city)
    return bits


def _job_summary_line(job, loc: str = "", address: str = "") -> str:
    bits = []
    if job.brand:
        bits.append(labels.brand(job.brand))
    if loc:
        bits.append(loc)
    if job.hours:
        bits.append(f"{job.hours} ч/нед")
    if address:
        bits.append(address)
    return " · ".join(b for b in bits if b)


def _plain_snippet(value: str | None, limit: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _tg_display_title(job) -> str:
    title = (job.title or "Вакансия").strip()
    sid = _job_short_id(job)
    parts = [title]
    if sid and sid.lower() not in title.lower():
        parts.append(f"ID {sid}")
    parts.extend(_job_detail_bits(job)[:2])
    return " · ".join(p for p in parts if p)


_SHORT_SOURCES = {
    "salling": "Salling", "teamtailor": "Teamtailor", "greenhouse": "Greenhouse",
    "ashby": "Ashby", "lidl": "Lidl", "manual_link": "По ссылке",
}


def _transit_fields(job, home: dict | None, cache: dict | None = None) -> dict:
    """Готовое время в пути из кэша (без сети). Пусто, если ещё не считали.
    cache — снимок кэша (transit.snapshot()); без него читаем сами."""
    if not home or getattr(job, "lat", None) is None or getattr(job, "lon", None) is None:
        return {}
    try:
        res = (transit.from_snapshot(cache, home["lat"], home["lon"], job.lat, job.lon)
               if cache is not None else
               transit.cached(home["lat"], home["lon"], job.lat, job.lon))
    except Exception:  # noqa: BLE001
        return {}
    if not res or not res.get("ok"):
        return {}
    out = {"transitMin": int(res.get("minutes") or 0),
           "transitTransfers": int(res.get("transfers") or 0)}
    modes = [str(m) for m in (res.get("modes") or []) if m][:3]
    if modes:
        out["transitModes"] = ", ".join(modes)
        # вид транспорта каждой линии (bus/train/metro/tram/ferry) — телефон
        # рисует по нему иконку «чем добраться» вместо эмодзи
        out["transitKinds"] = ", ".join(transit.kinds_of(res)[:3])
    return out


def _tg_job_payload(job, is_match: bool | None = None, home: dict | None = None,
                    translate_title: bool = True, lean: bool = False,
                    transit_cache: dict | None = None) -> dict:
    """Структурные поля для Mini App-панели: фильтры не должны парсить только текст.

    Набор полей намеренно повторяет карточку главного экрана приложения (бренд с
    фирменным цветом, расстояние, роль по-русски, часы/занятость/старт, оплата,
    дата публикации, статус) — телефон должен показывать то же и так же.
    """
    distance = None
    try:
        if home is None:
            home = settings_store.get_home()
        if home and job.lat is not None and job.lon is not None:
            distance = round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon), 1)
    except Exception:  # noqa: BLE001
        home = None
    address = _job_address(job)
    loc = (job.city or "").strip()
    if distance is not None:
        loc = f"{loc} · ~{round(distance)} км от дома" if loc else f"~{round(distance)} км от дома"
    public_id = _job_public_id(job)
    short_id = _job_short_id(job)
    title = job.title or ""
    display_title = _tg_display_title(job)
    summary = _job_summary_line(job, loc=loc, address=address)
    # В списке сниппет короткий (две строки карточки): полный текст открывается
    # в детали отдельным запросом. На 500 вакансий каждая лишняя сотня символов —
    # это лишние сотни килобайт трафика телефона.
    description = _plain_snippet(job.description_ru or job.description, limit=160 if lean else 220)
    brand_bg, brand_fg = labels.BRAND_COLORS.get(document_rules.brand_key(job), ("", ""))
    categories_ru = ", ".join(
        labels.label_or_pretty(labels.CATEGORY, c.strip())
        for c in str(job.categories or "").split(",") if c.strip()
    )
    payload = {
        "id": job.id,
        "jobId": job.id,
        "titleBase": title,
        "titleRu": _title_ru(title, cached_only=not translate_title),
        "source": job.source or "salling",
        "descriptionSnippet": description,
        "brand": labels.brand(job.brand) if job.brand else "",
        "brandCode": job.brand or "",
        "brandColor": brand_bg,
        "brandFg": brand_fg,
        "city": job.city or "",
        "location": loc,
        "address": address,
        "region": labels.label_or_pretty(labels.REGION, job.region) if job.region else "",
        "regionCode": job.region or "",
        "hoursLabel": f"{job.hours} ч/нед" if job.hours else "",
        "hoursRaw": job.hours or "",
        "employment": labels.EMPLOYMENT.get(job.employment_type or "", job.employment_type or ""),
        "employmentType": job.employment_type or "",
        "level": labels.LEVEL.get(job.job_level or "", ""),
        "jobLevel": job.job_level or "",
        # Та же классификация, которой пользуется автопилот на ПК.
        "ageGroup": "under18" if autopilot.job_is_under18(job) else "adult",
        "categories": categories_ru,
        "categoriesCode": job.categories or "",
        # Русская расшифровка должности — та же строка, что в карточке приложения.
        "roleRu": labels.role_summary(job.title, job.categories or "", job.job_level or ""),
        "isLead": labels.is_leadership(job.title or ""),
        "payRate": job.pay_rate or "",
        "startDate": job.start_date or "",
        "publishedShort": labels.date_short(job.published) if job.published else "",
        "publishedRaw": job.published or "",
        "distanceKm": distance,
        # Реальное время в пути (Transitous, считается в фоне и кэшируется).
        # Расстояние по прямой врёт там, где дорога идёт в обход — озеро, ж/д,
        # залив: «≈1 км» превращалось в 20 минут пути.
        **_transit_fields(job, home, transit_cache),
        "lat": job.lat,
        "lon": job.lon,
        "url": job.application_link or "",
        "mapsUrl": _maps_url(job, home) if (address or job.lat is not None) else "",
        "status": job.status or "",
        "sourceLabel": "" if (job.source or "salling") == "salling"
                       else _SHORT_SOURCES.get(job.source or "", job.source or ""),
        # Подходит ли под фильтры подбора: в телефоне список шире (как в
        # приложении), и подходящие надо отличать от «просто активных».
        "isMatch": True if is_match is None else bool(is_match),
        # Панель показывает один и тот же полный список подходящих — без этой
        # пометки не видно, что появилось недавно, а что висит давно.
        "isNew": bool(job.first_seen and (utcnow() - job.first_seen).days < 3),
    }
    if lean:
        # Длинный список (сотни вакансий) — только то, что реально рисует панель.
        return payload
    # Полная карточка (оффер в Telegram): прежние поля на месте, их читают и
    # старые версии панели, и текст карточки в чате.
    payload.update({
        "publicId": public_id,
        "shortId": short_id,
        "requisitionId": job.requisition_id or "",
        "title": display_title,
        "displayTitle": display_title,
        "summary": summary,
        "subtitle": summary,
        "description": description,
        "brandCode": job.brand or "",
        "street": job.street or "",
        "zip": job.zip or "",
        "country": job.country or "",
        "hours": job.hours or "",
        "published": job.published or "",
        "publishedDate": (job.published or "")[:10],
        "lat": job.lat,
        "lon": job.lon,
        "source": job.source or "",
    })
    return payload


def _tg_offer_jobs(jobs, panel: bool = False) -> dict:
    """Отправить список вакансий в облачного бота. Безопасно: сама заявка не
    уходит, пока пользователь не нажмёт ✅ в Telegram."""
    sent = 0
    last_error = ""
    for job in jobs:
        r = cloud_auth.offer(_tg_card(job), job.id, job=_tg_job_payload(job), panel=panel)
        if r and r.get("ok"):
            autopilot.tg_pending_add(job.id, r.get("messageId"))
            if panel:
                autopilot.log_event("info", f"TG-панель: добавил вакансию — {job.title}")
            else:
                autopilot.log_event("info", f"TG: спросил разрешение — {job.title}")
            sent += 1
        else:
            last_error = (r or {}).get("error") or "не удалось отправить карточку"
            autopilot.log_event("info", f"TG: не отправилось — {last_error}")
            break
    return {"sent": sent, "error": last_error}


def _tg_offer_tick(include_existing: bool = False, ignore_schedule: bool = False,
                   limit: int | None = None, panel: bool = False) -> dict:
    """После скана: если включён режим «по разрешению» и Telegram привязан —
    отправить карточки НОВЫХ подходящих вакансий с кнопками ✅/❌.
    limit=None — штатный потолок (TG_MAX_PER_SCAN, чтобы не заливать чат);
    больше передаёт ручной показ из Mini App-панели."""
    try:
        if not autopilot.get_rule().get("tg_approval"):
            return {"sent": 0, "error": "режим Telegram выключен"}
        if not ignore_schedule and not autopilot.within_schedule():
            return {"sent": 0, "error": "сейчас вне часов активности"}
        if not account_mod.is_signed_in():
            return {"sent": 0, "error": "сначала войди через Telegram в разделе Аккаунт"}
        autopilot.tg_pending_expire()  # карточки без ответа не висят вечно
        cap = limit or TG_MAX_PER_SCAN
        automatic = not include_existing
        if automatic and autopilot.tg_digest_enabled():
            # режим дайджеста: вместо потока карточек — одно сообщение в день;
            # ручная кнопка «прислать текущие» карточками работает как раньше
            return _tg_digest_tick()
        if automatic:
            # дневной потолок — только для автоматического потока; ручную кнопку
            # «прислать текущие» пользователь жмёт сам и потолком не ограничен
            remaining_today = autopilot.tg_daily_remaining()
            if remaining_today <= 0:
                waiting = len(autopilot.tg_eligible(10000, include_existing=False))
                if waiting:
                    autopilot.tg_log_cap_once(waiting)
                return {"sent": 0, "error": "дневной потолок карточек достигнут"}
            cap = min(cap, remaining_today)
        jobs = autopilot.tg_eligible(limit=cap, include_existing=include_existing)
        if not jobs:
            return {"sent": 0, "error": ""}
        result = _tg_offer_jobs(jobs, panel=panel)
        if automatic:
            autopilot.tg_note_sent(int(result.get("sent") or 0))
        result["remaining"] = len(autopilot.tg_eligible(10000, include_existing=include_existing))
        return result
    except Exception as e:  # noqa: BLE001 — не должно ронять фоновый скан
        print(f"telegram(cloud): ошибка отправки карточек — {e}")
        return {"sent": 0, "error": str(e)[:120]}


def _tg_digest_tick() -> dict:
    """Дневной дайджест: одно сообщение со сводкой НОВЫХ подходящих вакансий.
    Вакансии из дайджеста помечаются «предложенными» (в реестре), чтобы завтра
    в сводку попали только действительно новые; подача — из панели (список
    вакансий там синхронизируется отдельно, jobs_sync)."""
    if not autopilot.tg_digest_due():
        return {"sent": 0, "error": "дайджест за сегодня уже отправлен"}
    jobs = autopilot.tg_eligible(limit=50, include_existing=False)
    if not jobs:
        return {"sent": 0, "error": ""}
    home = settings_store.get_home()
    ok = cloud_auth.send_digest(autopilot.build_digest_text(jobs, home))
    if not ok:
        return {"sent": 0, "error": "не удалось отправить дайджест"}
    autopilot.tg_digest_mark_sent()
    for job in jobs:
        applications.mark_offered(job.id)
    autopilot.log_event("info", f"TG: дайджест — новых подходящих {len(jobs)}")
    return {"sent": 1, "digest": True, "jobs": len(jobs)}


def _ago_text(ts: float | int | None) -> str:
    if not ts:
        return "ещё не было"
    sec = max(0, int(time.time() - float(ts)))
    if sec < 60:
        return "только что"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


def _remote_status_text(prefix: str = "") -> str:
    st = _autopilot_status_payload()
    mode_label = {
        "off": "пауза",
        "notify": "только уведомления",
        "telegram": "спрашивать в Telegram",
        "auto": "автоотправка",
    }.get(st.get("mode"), st.get("mode") or "неизвестно")
    lines = []
    if prefix:
        lines.append(prefix)
        lines.append("")
    lines.extend([
        "📊 WexFlow на ПК онлайн",
        f"Режим: {mode_label}",
        f"Подходит по фильтрам: {st.get('found', 0)}",
        f"Можно прислать текущих: {st.get('tg_eligible_current', 0)}",
        f"Новых к Telegram-вопросу: {st.get('tg_eligible_new', 0)}",
        f"Ждут ответа в Telegram: {st.get('tg_pending', 0)}",
        f"Подано сегодня: {st.get('submitted_today', 0)} из {st.get('daily_limit', 0)}",
        f"Последняя проверка: {_ago_text(st.get('last_scan'))}",
    ])
    if st.get("running"):
        lines.append("Сейчас идёт проверка вакансий.")
    if st.get("error"):
        lines.append(f"Последняя ошибка: {st.get('error')}")
    return "\n".join(lines)


def _tg_remote_command_expired(command: dict) -> bool:
    """Не выполняем старые команды из облака: пользователь мог нажать днём,
    а ПК проснулся ночью. Новые версии облака уже фильтруют это, но ПК тоже
    держит страховку для старого прод-сервера."""
    try:
        now_ms = int(time.time() * 1000)
        exp = int(float(command.get("exp") or 0))
        if exp:
            return exp < now_ms
        ts = int(float(command.get("ts") or 0))
        return bool(ts and now_ms - ts > TG_REMOTE_COMMAND_TTL_MS)
    except Exception:  # noqa: BLE001
        return False


def _handle_tg_remote_command(command: dict) -> str:
    """Выполнить команду, пришедшую из Telegram-пульта, и вернуть текст ответа."""
    action = str(command.get("action") or "").strip().lower()
    try:
        if action == "status":
            return _remote_status_text()

        if action == "pause":
            autopilot.set_mode("off")
            autopilot.log_event("info", "Telegram: автопилот поставлен на паузу")
            _reschedule_autopilot_scan()
            _sync_filters_to_cloud(force=True)   # карточка автопилота в панели — сразу свежая
            return _remote_status_text("⏸ Автопилот поставлен на паузу.")

        if action == "ai_chat":
            # ИИ-диалог настройки фильтров: панель шлёт реплики, ПК спрашивает
            # Gemini своим ключом и кладёт ответ в облако (панель заберёт по reqId).
            # В чат ничего не шлём (return "") — общение идёт в панели.
            req_id = str(command.get("reqId") or "")
            messages = command.get("messages") or []
            try:
                res = ai_filters.chat(messages, labels.CATEGORY, labels.BRANDS,
                                      labels.EMPLOYMENT, labels.REGION)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)[:200]}
            if res.get("ok"):
                cloud_auth.report_ai_reply(req_id, res.get("reply", ""),
                                           bool(res.get("done")), res.get("fields"))
            else:
                cloud_auth.report_ai_reply(req_id, "", False, None,
                                           error=res.get("error", "ИИ недоступен"))
            return ""

        if not account_mod.is_signed_in():
            return (
                "ПК онлайн, но в WexFlow не выполнен вход через Telegram.\n"
                "Открой приложение на ПК → Аккаунт → войти через Telegram."
            )

        if action == "start":
            autopilot.set_mode("telegram")
            if (autopilot.get_rule().get("submit_scope") or "new") != "all":
                autopilot.set_autosubmit_baseline()
            autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
            autopilot.log_event("info", "Telegram: включён режим подтверждения")
            _reschedule_autopilot_scan()
            threading.Thread(target=_tg_offer_tick, daemon=True).start()
            _sync_filters_to_cloud(force=True)   # карточка автопилота в панели — сразу свежая
            return _remote_status_text(
                "▶️ Telegram-режим включён. Новые подходящие вакансии будут приходить сюда."
            )

        if action == "send_current":
            autopilot.set_mode("telegram")
            _reschedule_autopilot_scan()
            # из Mini App-панели присылаем МНОГО (до 30 за раз), в чат — скромно,
            # чтобы не залить переписку. panel=True ставит панель при постановке команды.
            is_panel = bool(command.get("panel"))
            # Раньше панельная кнопка сбрасывала «предложено» (reset_tg_queue_for_filters)
            # и слала одни и те же вакансии заново при каждом нажатии. Теперь гейт
            # «предложено — навсегда» держится, а полный список панель получает
            # через jobs_sync ниже.
            limit = 30 if is_panel else None
            result = _tg_offer_tick(include_existing=True, ignore_schedule=True, limit=limit, panel=is_panel)
            if is_panel:
                _sync_jobs_to_cloud(force=True)
                _sync_job_texts_to_cloud(force=True)
            stats = autopilot.tg_queue_stats()
            if result.get("sent"):
                label = "Добавил в панель" if is_panel else "Отправил карточек"
                return (
                    f"📨 {label}: {result['sent']}.\n"
                    f"Осталось доступных текущих: {stats.get('eligible_current', 0)}."
                )
            if is_panel:
                return (
                    "📨 Новых предложений нет — всё текущее уже показывал.\n"
                    "Полный список подходящих обновил во вкладке «Вакансии»."
                )
            return (
                "📨 Сейчас нечего прислать.\n"
                f"{result.get('error') or 'Текущие вакансии уже предложены, пропущены или поданы.'}\n"
                f"Доступных текущих: {stats.get('eligible_current', 0)}."
            )

        if action == "scan":
            if _sync_state["running"]:
                return "🔄 Проверка уже идёт. Скоро пришлю новые подходящие вакансии, если они появятся."
            threading.Thread(target=_sync_jobs, daemon=True).start()
            return "🔄 Запустил проверку вакансий на ПК. Если появятся новые подходящие, пришлю сюда."

        if action == "test":
            # «Отправить проверочное» с телефона: тот же путь, что и кнопка в
            # приложении. Карточка-пример уходит в чат сама, поэтому при успехе
            # в чат ничего не дописываем (return "") — иначе будет два сообщения.
            res = _send_telegram_demo_card()
            if res.get("ok"):
                return ""
            return ("⚠️ Проверочное сообщение не ушло: "
                    f"{res.get('error') or 'нет связи с облаком'}.")

        if action == "transit":
            # «Сколько ехать» из панели: считаем маршрут для одной вакансии и
            # сразу досылаем список (в карточке появится время в пути).
            job_id = str(command.get("jobId") or "").strip()
            if job_id:
                try:
                    home = settings_store.get_home()
                    with get_session() as s:
                        job = s.get(Job, job_id)
                    if home and job is not None and job.lat is not None and job.lon is not None:
                        transit.summary(home["lat"], home["lon"], job.lat, job.lon)
                        _sync_jobs_to_cloud(force=True)
                except Exception as e:  # noqa: BLE001 — маршрут не критичен
                    print(f"transit-on-demand: ошибка — {e}")
            return ""

        if action == "translate":
            # Панель открыла вакансию, которая НЕ подходит под фильтры — фоновый
            # переводчик такие не берёт. Переводим одну по запросу и сразу
            # досылаем её текст в облако. В чат ничего не пишем (return "").
            job_id = str(command.get("jobId") or "").strip()
            if job_id:
                try:
                    _translate_job_now(job_id)
                except Exception as e:  # noqa: BLE001 — перевод не должен ронять опрос
                    print(f"translate-on-demand: ошибка — {e}")
            return ""

        if action == "answer_question":
            # Ответ на вопрос анкеты, данный с телефона. Ничего не подаёт —
            # просто кладёт ответ в банк, как кнопка «Да/Нет» в приложении.
            key = str(command.get("questionKey") or "").strip()
            value = str(command.get("answer") or "").strip().lower()
            if value not in {"yes", "no", ""}:
                return "Ответ бывает только «да» или «нет»."
            if not form_questions.set_answer(key, value):
                return "Такого вопроса у меня нет — обнови список в панели."
            _sync_questions_to_cloud(force=True)
            left = form_questions.pending_count(
                profile_store.answers(profile_store.load_profile()))
            return ("✅ Ответ сохранён. "
                    + (f"Осталось вопросов без ответа: {left}." if left
                       else "Все вопросы закрыты — подача пойдёт до конца сама."))

        if action == "set_filters":
            # Настройка с телефона меняет только ЧТО ИЩЕМ (первый набор фильтров).
            # Подача по-прежнему требует «Подать» (F27), автоотправку с телефона не трогаем.
            fields = _sanitize_remote_filters(command.get("filters") or {})
            # расписание — не поле профиля, а правило целиком (как /autopilot/save)
            schedule = {k: fields.pop(k) for k in ("active_from", "active_to") if k in fields}
            if not fields and not schedule:
                return "⚙️ Не получил ни одного корректного фильтра — ничего не менял."
            if schedule:
                autopilot.save_rule(schedule)
                _reschedule_autopilot_scan()
            prof = autopilot.ensure_profiles()[0]
            if fields:
                autopilot.save_profile_filters(prof["id"], fields)
            # как при сохранении на ПК: текущие совпадения не считаем «новыми»
            autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
            autopilot.log_event("info", "Telegram: фильтры обновлены с телефона")
            _sync_filters_to_cloud(force=True)
            _sync_jobs_to_cloud(force=True)
            n = autopilot.profile_match_count(autopilot.ensure_profiles()[0])
            return f"⚙️ Фильтры обновлены с телефона. Подходит сейчас: {n}."

        return "Неизвестная команда Telegram-пульта."
    except Exception as e:  # noqa: BLE001
        return f"Команда не выполнена: {str(e)[:180]}"


@asynccontextmanager
async def _lifespan(app):
    global _scheduler
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_sync_jobs, "interval", minutes=IDLE_SCAN_MIN, id="auto_sync")
    sched.start()
    _scheduler = sched
    _reschedule_autopilot_scan()  # подстроить интервал под текущее состояние автопилота
    _ensure_tg_poller()           # слушатель Telegram (привязка + кнопки ✅/❌)
    try:                          # фоновый перевод описаний для детали в Mini App
        import translate_worker
        translate_worker.start(sync_fn=_sync_job_texts_to_cloud, busy_fn=_submit_in_progress)
    except Exception as _exc:  # noqa: BLE001 — перевод не критичен для запуска
        print(f"translate-worker: не запустился — {_exc}")
    try:                          # фоновое время в пути (дом → магазин) для карточки
        import transit_worker
        transit_worker.start(sync_fn=_sync_jobs_to_cloud, busy_fn=_submit_in_progress)
    except Exception as _exc:  # noqa: BLE001 — маршруты не критичны для запуска
        print(f"transit-worker: не запустился — {_exc}")
    age = _data_age_minutes()
    if age is None or age >= 30:  # данные устарели — обновить сразу, в фоне
        threading.Thread(target=_sync_jobs, daemon=True).start()
    else:
        # Если база свежая, всё равно сразу проверим Telegram-очередь:
        # пользователь запустил приложение и ожидает уведомления без ожидания интервала.
        threading.Thread(target=_tg_offer_tick, daemon=True).start()
    yield
    _tg_stop.set()
    try:
        import translate_worker
        translate_worker.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        import transit_worker
        transit_worker.stop()
    except Exception:  # noqa: BLE001
        pass
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


@app.get("/api/apply/progress")
def api_apply_progress():
    """Живой прогресс пакетной подачи для панели в дашборде (и Mini App).
    Воркер apply.py пишет apply_progress.json; если он завис/умер и давно не
    обновлялся — считаем подачу неактивной, чтобы панель не висела вечно."""
    try:
        p = config.DATA_DIR / "apply_progress.json"
        if not p.exists():
            return {"active": False}
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("active"):
            proc = _last_apply_proc
            if proc is not None and _progress_started_ts(data) >= _last_apply_spawn_ts - 10:
                # Шаг 4: это файл НАШЕГО воркера — спрашиваем сам процесс.
                # Жив → прогресс честно активен (даже если долго ждёт логина);
                # умер, не закрыв файл → оборвался, показываем сразу, без 4 минут.
                if proc.poll() is not None:
                    data["active"] = False
                    data["stalled"] = True
            else:
                ts = data.get("updated_at") or ""
                try:
                    from datetime import datetime
                    age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
                    if age > 240:  # 4 мин тишины — воркер-сирота, видимо, оборвался
                        data["active"] = False
                        data["stalled"] = True
                except Exception:  # noqa: BLE001
                    pass
        return data
    except Exception:  # noqa: BLE001
        return {"active": False}


def _health_warnings(last_hits, sync_failed: bool, fail_streak: int,
                     cloud_fail_streak: int = 0, connector_errors=None,
                     cloud_error: str = "") -> list:
    """Сторожа деградации (шаг 7): приложение стоит на чужих недокументированных
    опорах (лента вакансий Salling, их форма подачи) — падение опоры надо хотя бы
    ЗАМЕЧАТЬ и говорить о нём пользователю, а не молча показывать пустой список.
    Чистая функция над снимком состояния — легко покрыть тестом."""
    warns = []
    if sync_failed or last_hits == 0:
        warns.append({
            "id": "source-down",
            "text": "Источник вакансий не отвечает: последняя проверка не принесла "
                    "ни одной вакансии. Обычно это временный сбой на стороне Salling. "
                    "Если баннер висит несколько часов — напишите в поддержку @wexwxeee.",
        })
    if fail_streak >= 3:
        warns.append({
            "id": "apply-unconfirmed",
            "text": f"Подача {fail_streak} раз подряд не подтвердилась. Возможно, "
                    "Salling изменил сайт и WexFlow больше не видит квитанцию. "
                "Проверь почту, подались ли заявки, и напиши в поддержку @wexwxeee.",
        })
    if cloud_fail_streak >= 3:
        quota_exhausted = "исчерпало лимит" in str(cloud_error).lower()
        warns.append({
            "id": "telegram-cloud-down",
            "text": (
                "Облачная база Telegram исчерпала квоту: команды с телефона не доходят "
                "до ПК. Локальный поиск и ручная подача работают; нужно заменить или "
                "расширить облачную базу."
                if quota_exhausted else
                "Нет устойчивой связи с Telegram: команды с телефона временно "
                "не доходят до ПК. WexFlow продолжит попытки автоматически. "
                "Проверь интернет; локальный поиск и ручная подача работают."
            ),
        })
    if connector_errors:
        warns.append({
            "id": "connectors-degraded",
            "text": "Дополнительные компании временно не обновились. Вакансии Salling "
                    "продолжают работать, а старые вакансии других компаний сохранены. "
                    "WexFlow повторит попытку автоматически.",
        })
    return warns


@app.get("/api/health")
def api_health():
    """Сторожа деградации для баннера в шапке (опрашивается из base.html)."""
    try:
        import applications
        streak = applications.failure_streak()
    except Exception:  # noqa: BLE001 — сторож не должен ронять страницу
        streak = 0
    return {"service": "wexflow-salling", "warnings": _health_warnings(
        _sync_state.get("last_hits"), bool(_sync_state.get("sync_failed")), streak,
        int(_tg_poll_state.get("fail_streak") or 0),
        _sync_state.get("connector_errors") or [],
        str(_tg_poll_state.get("last_error") or ""))}


app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
templates.env.globals["brand_label"] = labels.brand
templates.env.globals["L"] = labels
templates.env.globals["candidate_profiles_state"] = candidate_profiles.ui_state
def _questions_pending_badge() -> int:
    """Сколько вопросов анкет ждут ответа (бейдж в боковом меню).

    Банк вопросов — обычный файл; его недоступность не должна ронять страницы.
    """
    try:
        return form_questions.pending_count(
            profile_store.answers(profile_store.load_profile()))
    except Exception:  # noqa: BLE001
        return 0


templates.env.globals["questions_pending"] = _questions_pending_badge
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
# Реестр заявок (шаг 3): один раз переносим старые списки id из settings.json
# в таблицу application. Сбой миграции не должен мешать запуску приложения.
try:
    import applications as _applications
    _applications.ensure_migrated()
except Exception as _exc:  # noqa: BLE001
    print(f"реестр заявок: миграция не удалась — {_exc}")


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
        facts.append({"label": "Регион", "value": labels.label_or_pretty(labels.REGION, job.region), "kind": ""})
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
        facts.append({"label": "Старт", "value": labels.date_short(job.start_date), "kind": "date"})
    if job.published:
        facts.append({"label": "Опубликовано", "value": labels.date_short(job.published), "kind": "muted"})
    if job.modified:
        facts.append({"label": "Обновлено", "value": labels.date_short(job.modified), "kind": "muted"})
    if job.requisition_id:
        facts.append({"label": "ID вакансии", "value": job.requisition_id, "kind": "muted"})
    if job.first_seen:
        facts.append({"label": "Найдено WexFlow", "value": labels.date_short(job.first_seen.strftime("%Y-%m-%d")), "kind": "muted"})
    if job.pay_rate:
        facts.append({"label": "Ставка", "value": job.pay_rate, "kind": "money"})
    else:
        facts.append({"label": "Ставка", "value": "не указана в объявлении", "kind": "muted"})
    if job.categories:
        cat_labels = [labels.label_or_pretty(labels.CATEGORY, c) for c in job.categories.split(",") if c]
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
        "source": Counter(),
        "brand": Counter(),
        "region": Counter(),
        "employment": Counter(),
        "level": Counter(),
        "category": Counter(),
        "city": Counter(),
    }
    for job in rows:
        counts["source"][getattr(job, "source", "salling") or "salling"] += 1
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
    connector_sources = connector_sync.DEFAULT_SOURCES
    with get_session() as s:
        total_jobs = (
            s.exec(select(func.count(Job.id)).where(Job.source == "salling")).one() or 0
        )
        active_jobs = (
            s.exec(
                select(func.count(Job.id)).where(
                    Job.source == "salling",
                    Job.status.not_in(["closed", "hidden", "applied"]),
                )
            ).one()
            or 0
        )
        applied_jobs = (
            s.exec(
                select(func.count(Job.id)).where(
                    Job.source == "salling",
                    Job.status == "applied",
                )
            ).one()
            or 0
        )
        connector_jobs = {
            "total": s.exec(
                select(func.count(Job.id)).where(Job.source.in_(connector_sources))
            ).one()
            or 0,
            "active": s.exec(
                select(func.count(Job.id)).where(
                    Job.source.in_(connector_sources),
                    Job.status.not_in(["closed", "hidden", "applied"]),
                )
            ).one()
            or 0,
            "sources": s.exec(
                select(func.count(func.distinct(Job.source))).where(
                    Job.source.in_(connector_sources)
                )
            ).one()
            or 0,
        }
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
            "connector_jobs": connector_jobs,
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


def _age_label(minutes: int | None) -> str:
    """«N мин / N ч / N дней назад» — одно правило для всех страниц."""
    if minutes is None:
        return "нет данных"
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 1440:
        return f"{minutes // 60} ч назад"
    days = minutes // 1440
    return f"{days} {labels.plural(days, 'день', 'дня', 'дней')} назад"


@app.get("/status", response_class=HTMLResponse)
def system_status(request: Request):
    """Страница «Состояние системы»: всё ли работает, одним экраном.

    Собирает уже существующие сторожа и статусы (ничего нового не меряет):
    свежесть базы, ошибки синка, серию неподтверждённых подач, связь с
    Telegram-облаком, готовность профиля/документов, автопилот и 7-Eleven."""
    with get_session() as s:
        active_jobs = s.exec(
            select(func.count()).select_from(Job).where(
                Job.status.not_in(["closed", "hidden", "applied"]))
        ).one()

    try:
        streak = applications.failure_streak()
    except Exception:  # noqa: BLE001
        streak = 0

    age_min = _data_age_minutes()
    _profile = profile_store.load_profile()
    _creds = credentials_store.status()
    setup = {
        "profile": all(str(_profile.get(k) or "").strip() for k, _ in PROFILE_REQUIRED),
        "login": bool(_creds.get("email")),
        "docs": bool(str(_profile.get("cv_path") or "").strip()),
        "home": bool(settings_store.get_home()),
    }

    tg_linked = bool(account_mod.load().get("tg_id"))
    cloud_fail = int(_tg_poll_state.get("fail_streak") or 0)
    cloud_last_ok = float(_tg_poll_state.get("last_ok") or 0.0)
    cloud = {
        "linked": tg_linked,
        "ok": tg_linked and cloud_fail == 0,
        "fail_streak": cloud_fail,
        "last_ok_label": _age_label(int((time.time() - cloud_last_ok) // 60)) if cloud_last_ok else "",
        "error": str(_tg_poll_state.get("last_error") or ""),
    }

    ap = _autopilot_status_payload()
    ap_last_min = None
    if ap.get("last_scan"):
        ap_last_min = max(0, int((time.time() - float(ap["last_scan"])) // 60))
    ap_stale = (ap.get("enabled") and ap_last_min is not None
                and ap_last_min > max(3 * int(ap.get("every_min") or 30), 30))

    return templates.TemplateResponse("system_status.html", {
        "request": request,
        "active_jobs": active_jobs,
        "age_min": age_min,
        "age_label": _age_label(age_min),
        "sync_running": _sync_state["running"],
        "sync_failed": bool(_sync_state.get("sync_failed")),
        "sync_error": str(_sync_state.get("last_error") or ""),
        "last_hits": _sync_state.get("last_hits"),
        "connector_errors": _sync_state.get("connector_errors") or [],
        "streak": streak,
        "setup": setup,
        "cloud": cloud,
        "autopilot": {
            "enabled": bool(ap.get("enabled")),
            "auto_submit": bool(ap.get("auto_submit")),
            "found": int(ap.get("found") or 0),
            "last_label": _age_label(ap_last_min),
            "stale": bool(ap_stale),
            "every_min": int(ap.get("every_min") or 30),
        },
        "seven": _seven_eleven_state(),
    })


@app.get("/apply-by-link")
def apply_by_link(request: Request, pending: str = ""):
    with get_session() as session:
        rows = session.exec(select(Job).where(
            Job.source != "salling",
            Job.status.not_in(["closed", "hidden"]),
        )).all()
        pending_job = session.get(Job, pending) if pending else None
        pending_application = None
        if pending_job and pending_job.source != "salling":
            pending_application = session.exec(select(Application).where(
                Application.source == pending_job.source,
                Application.job_id == pending_job.id,
            )).first()
    try:
        profile = profile_store.load_profile()
        profile_missing = _profile_missing(profile)
        cv_ready = profile_store.file_status(profile.get("cv_path", "")) == "ok"
    except Exception:  # noqa: BLE001 — повреждённый профиль не должен ломать страницу
        profile_missing = [label for _key, label in PROFILE_REQUIRED]
        cv_ready = False
    counts = Counter(job.source for job in rows)
    sources = [
        {"key": key, "label": JOB_SOURCE_LABELS[key],
         "count": counts.get(key, 0), "href": f"/?source={key}"}
        for key in connector_sync.DEFAULT_SOURCES
    ]
    return templates.TemplateResponse("apply_by_link.html", {
        "request": request, "sources": sources,
        "total": sum(item["count"] for item in sources),
        "pending_job": pending_job,
        "pending_state": pending_application.state if pending_application else "",
        "profile_missing": profile_missing,
        "cv_ready": cv_ready,
    })


def _normalized_apply_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("У вакансии нет безопасной ссылки на форму")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
                       parsed.query, ""))


def _manual_link_job(url: str) -> Job:
    """Reuse a known job or create one stable journal entry for an arbitrary link."""
    value = _normalized_apply_url(url)
    parsed = urlsplit(value)
    with get_session() as session:
        existing = session.exec(select(Job).where(Job.application_link == value)).first()
        if existing:
            return existing
        slug = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
        slug = re.sub(r"^\d+[-_]?", "", slug)
        title = re.sub(r"[-_]+", " ", slug).strip().title()
        if not title or title.lower() in {"jobs", "job", "careers", "career"}:
            title = "Вакансия по ссылке"
        job_id = "link:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
        stored = session.get(Job, job_id)
        if stored:
            return stored
        job = Job(
            id=job_id,
            source="manual_link",
            title=title[:160],
            brand=parsed.hostname or parsed.netloc,
            application_link=value,
            status="new",
        )
        session.add(job)
        try:
            session.commit()
        except IntegrityError:
            # Two rapid clicks can race between the initial lookup and INSERT.
            # The deterministic id makes the already-created row the winner.
            session.rollback()
            stored = session.get(Job, job_id)
            if stored:
                return stored
            raise
        session.refresh(job)
        return job


def _connector_status_path(job_id: str):
    token = hashlib.sha256(str(job_id or "default").encode("utf-8")).hexdigest()[:16]
    return config.DATA_DIR / f"connector_apply_status_{token}.json"


def _launch_connector_filler(
    url: str,
    job_id: str = "",
    submit: bool = False,
) -> str:
    """Запустить помощника и дождаться подтверждения реального окна браузера.

    Раньше успешный ``Popen`` ошибочно считался успешным открытием формы: воркер
    мог сразу завершиться (например, без профиля), а интерфейс всё равно показывал
    зелёное сообщение. Теперь воркер подтверждает запуск через отдельный status-файл.
    """
    url = _normalized_apply_url(url)
    job_id = str(job_id or "")
    status_path = _connector_status_path(job_id)
    try:
        status_path.unlink(missing_ok=True)
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--worker-connector-apply", url, job_id]
    else:
        cmd = [sys.executable, "-m", "connectors.apply_dispatch", url, job_id, "--keep-open"]
    if submit:
        cmd.append("--submit")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    proc = subprocess.Popen(cmd, **kwargs)
    with _connector_launch_lock:
        if job_id in _connector_launches:
            _connector_processes[job_id] = proc

    deadline = time.monotonic() + 15.0
    last_state = ""
    while time.monotonic() < deadline:
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        if str(payload.get("job_id", "")) == job_id:
            last_state = str(payload.get("state", ""))
            if last_state in {"browser_opened", "ready", "submit_ready", "submitted",
                              # защита сработала / не хватило ответов — окно всё
                              # равно открыто, это нормальный конец запуска
                              "site_changed", "needs_answers", "no_receipt"}:
                return last_state
            if last_state == "error":
                message = str(payload.get("message", "")).strip()
                if not message or message == "SystemExit":
                    message = "профиль кандидата не найден или не заполнен"
                raise RuntimeError(message)
        if proc.poll() is not None:
            # Даём status-файлу короткий шанс дойти после завершения процесса.
            time.sleep(0.1)
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                message = str(payload.get("message", "")).strip()
            except (OSError, ValueError, TypeError):
                message = ""
            raise RuntimeError(message or "помощник завершился до открытия браузера")
        time.sleep(0.15)
    raise RuntimeError(
        "браузер не подтвердил запуск за 15 секунд"
        + (f" (этап: {last_state})" if last_state else "")
    )


def _watch_connector_result_for_phone(job_id: str, source: str) -> None:
    """Send a phone result only from the connector worker's real status.

    For Lidl, ``submitted`` is written only after the receipt is visible. If
    the browser closes without that receipt, the phone must say
    "not confirmed", never "submitted".
    """
    wanted = str(job_id or "")
    path = _connector_status_path(wanted)
    while not _tg_stop.is_set():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        state = str(payload.get("state") or "")
        message = str(payload.get("message") or "").strip()
        if str(payload.get("job_id") or "") == wanted and state == "submitted":
            _report_apply_result_safe(
                wanted, "submitted",
                message or "Сайт подтвердил получение заявки.",
            )
            _release_connector_launch(wanted)
            _sync_applied_to_cloud(force=True)
            return
        if str(payload.get("job_id") or "") == wanted and state == "error":
            applications.mark_failed([wanted], source=source)
            _report_apply_result_safe(
                wanted, "failed",
                message or "Окно подачи завершилось с ошибкой.",
            )
            _release_connector_launch(wanted)
            return
        # Защита от изменений сайта, нехватка ответов и «нажали, но квитанции
        # нет» — это НЕ подача. Человеку уходит причина, а не молчание.
        if (str(payload.get("job_id") or "") == wanted
                and state in {"site_changed", "needs_answers", "no_receipt"}):
            applications.mark_failed([wanted], source=source)
            _report_apply_result_safe(
                wanted, "failed",
                message or "Подача остановлена — заявка не отправлена.",
            )
            _release_connector_launch(wanted)
            return
        with _connector_launch_lock:
            proc = _connector_processes.get(wanted)
        if proc is not None and proc.poll() is not None:
            applications.mark_failed([wanted], source=source)
            _report_apply_result_safe(
                wanted, "failed",
                "Окно закрыто, но сайт не подтвердил получение заявки.",
            )
            _release_connector_launch(wanted)
            return
        _tg_stop.wait(1.0)


@app.post("/apply-by-link/start")
def start_apply_by_link(request: Request, url: str = Form(...)):
    try:
        job = _manual_link_job(url)
    except Exception as exc:  # noqa: BLE001
        return _redirect_back(
            request, "/apply-by-link",
            error=f"Не удалось открыть форму: {str(exc)[:160]}",
        )
    value = job.application_link or ""
    if job.status == "applied" or job.applied_at is not None:
        return _redirect_back(
            request, "/apply-by-link",
            notice="Эта ссылка уже отмечена как поданная. Повторно форму не открываю.",
        )
    if not _claim_connector_launch(job.id):
        return _redirect_back(
            request, "/apply-by-link",
            notice="Форма уже открывается — второе окно не запускаю.",
        )
    applications.mark_submitting([job.id], origin="assisted", source=job.source)
    try:
        _launch_connector_filler(value, job.id)
    except Exception as exc:  # noqa: BLE001
        _release_connector_launch(job.id)
        applications.mark_failed([job.id], source=job.source)
        return _redirect_back(
            request, "/apply-by-link",
            error=f"Не удалось открыть форму: {str(exc)[:160]}",
        )
    try:
        from connectors.apply_dispatch import detect, platform_name
        key = detect(value)
        platform = platform_name(key) if key else "универсальная форма"
    except Exception:
        platform = "форма вакансии"
    return RedirectResponse(
        _url_with_system_response(
            f"/apply-by-link?pending={quote_plus(job.id)}",
            notice=f"Открываю {platform}. После проверки отметь результат ниже.",
        ),
        status_code=303,
    )


@app.post("/job/{job_id}/connector/apply")
def start_connector_apply(
    job_id: str,
    request: Request,
    mode: str = Form("prepare"),
    submit_ack: str = Form(""),
):
    with get_session() as session:
        job = session.get(Job, job_id)
    if not job:
        return _redirect_back(request, "/", error="Вакансия больше не найдена.")
    if getattr(job, "source", "salling") == "salling":
        return RedirectResponse(f"/job/{job_id}/apply", status_code=303)
    if job.status == "applied" or job.applied_at is not None:
        return _redirect_back(
            request, f"/job/{job_id}",
            notice="Эта вакансия уже отмечена как поданная. Повторно форму не открываю.",
        )
    if not _claim_connector_launch(job.id):
        return _redirect_back(
            request, f"/job/{job_id}",
            notice="Форма уже открывается — второе окно не запускаю.",
        )
    mode = str(mode or "prepare").strip().lower()
    real_submit = mode == "submit"
    if real_submit and submit_ack != "1":
        return _redirect_back(
            request,
            f"/job/{job_id}",
            error="Реальная отправка не подтверждена. Заявка не отправлялась.",
        )
    if real_submit and getattr(job, "source", "") != "lidl":
        return _redirect_back(
            request,
            f"/job/{job_id}",
            error="Контролируемая отправка пока доступна только для Lidl.",
        )
    applications.mark_submitting([job.id], origin="assisted", source=job.source)
    try:
        _launch_connector_filler(
            job.application_link or "",
            job.id,
            submit=real_submit,
        )
    except Exception as exc:  # noqa: BLE001
        _release_connector_launch(job.id)
        applications.mark_failed([job.id], source=job.source)
        return _redirect_back(
            request, f"/job/{job_id}",
            error=f"Не удалось открыть форму: {str(exc)[:140]}",
        )
    return _redirect_back(
        request, f"/job/{job_id}",
        notice=(
            "Полная подача запущена: WexFlow заполнит анкету сохранёнными ответами "
            "и нажмёт Ansøg сам. Если на какой-то вопрос ответа нет — остановится "
            "и оставит зелёную кнопку тебе."
            if real_submit else
            "Проверка открыта: WexFlow заполнит форму до Ansøg и гарантированно не нажмёт её."
        ),
    )


@app.post("/job/{job_id}/connector/result")
def connector_apply_result(
    job_id: str,
    request: Request,
    outcome: str = Form(...),
    return_to: str = Form(""),
):
    target = "/apply-by-link" if return_to == "/apply-by-link" else f"/job/{job_id}"
    if outcome not in {"submitted", "incomplete"}:
        return _redirect_back(request, target, error="Неизвестный результат анкеты.")
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return _redirect_back(request, "/", error="Вакансия больше не найдена.")
        if job.source == "salling":
            return _redirect_back(request, f"/job/{job_id}", error="Этот результат относится только к внешним анкетам.")
        source = job.source
        if outcome == "submitted":
            job.status = "applied"
            job.applied_at = job.applied_at or utcnow()
            job.applied_confidence = "manual"
            session.add(job)
            session.commit()
            session.refresh(job)
    _release_connector_launch(job_id)
    if outcome == "submitted":
        applications.record_submitted([job])
        return RedirectResponse(_url_with_system_response(
            target,
            notice="Отмечено как поданное вручную. Запись добавлена в журнал.",
        ), status_code=303)
    applications.mark_failed([job_id], source=source)
    return RedirectResponse(_url_with_system_response(
        target,
        notice="Сохранил как незавершённую анкету — к ней можно вернуться позже.",
    ), status_code=303)


def _today_summary(home: dict | None) -> dict:
    """Числа для временных вкладок и компактного дневного статуса.

    «Сегодня» считается по first_seen — когда WexFlow впервые обнаружил
    вакансию в любом подключённом интернет-источнике. Это честнее published:
    сайты нередко отдают старую дату публикации у заново появившейся позиции.
    """
    import datetime as _dtm
    week_ago = utcnow() - _dtm.timedelta(days=7)
    fresh_cutoff = utcnow() - _dtm.timedelta(days=3)
    day_start, day_end = applications._local_day_utc_bounds()
    with get_session() as s:
        fresh_jobs = list(s.exec(select(Job).where(
            Job.status.not_in(["closed", "hidden", "applied"]),
            Job.first_seen >= fresh_cutoff,
        )).all())
        today_new = s.exec(
            select(func.count()).select_from(Job).where(
                Job.status.not_in(["closed", "hidden", "applied"]),
                Job.first_seen >= day_start,
                Job.first_seen < day_end,
            )
        ).one()
        submitted_week = s.exec(
            select(func.count()).select_from(Job).where(Job.applied_at >= week_ago)
        ).one()
    radius = autopilot.DEFAULT_HOME_RADIUS_KM
    if home:
        new_nearby = sum(
            1 for j in fresh_jobs
            if j.lat is not None and j.lon is not None
            and geo.haversine_km(home["lat"], home["lon"], j.lat, j.lon) <= radius
        )
    else:
        new_nearby = len(fresh_jobs)
    rule = autopilot.get_rule()
    awaiting = len(rule.get("tg_pending") or []) if rule.get("tg_approval") else 0
    return {
        "new_today": int(today_new or 0),
        "new_3d": len(fresh_jobs),
        "new_nearby": new_nearby,
        "awaiting": awaiting,
        "submitted_week": int(submitted_week or 0),
        "radius": radius,
        "has_home": bool(home),
        "tg_mode": bool(rule.get("tg_approval")),
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    source: str = "",
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
    period: str = "all",
    profile: str = "",
    page: str = "1",
    geoerror: str = "",
    batch: str = "",
    mode: str = "",
    skipped: str = "",
    dup: str = "",
    reset: str = "",
):
    # запоминаем фильтры в cookie и восстанавливаем при заходе на голую "/"
    if not request.query_params and not reset:
        raw = request.cookies.get("saling_filters")
        if raw:
            try:
                saved = json.loads(raw)
                q = saved.get("q", q); source = saved.get("source", source); city = saved.get("city", city)
                brand = saved.get("brand", brand); region = saved.get("region", region)
                employment_type = saved.get("employment_type", employment_type)
                category = saved.get("category", category); job_level = saved.get("job_level", job_level)
                status = saved.get("status", status); sort = saved.get("sort", sort)
                show_applied = saved.get("show_applied", show_applied)
                radius = saved.get("radius", radius); group = saved.get("group", group)
                period = saved.get("period", period)
            except Exception:
                pass

    period = period if period in {"today", "3d", "all"} else "all"
    profile = str(profile or "").strip()[:80]
    home = settings_store.get_home()
    if not sort:
        sort = "distance" if home else "published"
    with get_session() as s:
        stmt = select(Job)
        source_key = source if source in JOB_SOURCE_LABELS else ""
        if source_key:
            stmt = stmt.where(Job.source == source_key)
        if status == "active":
            excluded_statuses = ["closed", "hidden"]
            if not show_applied:
                excluded_statuses.append("applied")
            stmt = stmt.where(Job.status.not_in(excluded_statuses))
        elif status:
            stmt = stmt.where(Job.status == status)
        if period == "today":
            day_start, day_end = applications._local_day_utc_bounds()
            stmt = stmt.where(Job.first_seen >= day_start, Job.first_seen < day_end)
        elif period == "3d":
            import datetime as _dtm
            stmt = stmt.where(Job.first_seen >= utcnow() - _dtm.timedelta(days=3))
        city_lookup = labels.city_query(city)
        city_terms = labels.city_terms(city)
        if city_terms:  # русский/англ/датский алиас, район или агломерация
            cond = None
            for term in city_terms:
                c = Job.city.ilike(f"%{term.strip()}%")
                cond = c if cond is None else (cond | c)
            stmt = stmt.where(cond)
        # Known Salling brands accept aliases; connector company names are
        # already human-readable and filter by exact stored value.
        brand_code = labels.resolve(labels.BRANDS, brand) or str(brand or "").strip()
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

        # job_level из данных Salling недостоверен: руководящие должности
        # (Souschef, Serviceleder, Teamkoordinator …) часто приходят с
        # job_level="employee" и протекали в выборку «Сотрудник». Если выбран
        # НЕ-руководящий уровень — дополнительно отсекаем их по названию.
        if level_code in ("employee", "employeeUnder18", "apprentice"):
            jobs = [j for j in jobs if not labels.is_leadership(j.title)]

        cities = _distinct(s, Job.city)
        sources = _distinct(s, Job.source)
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
    trips = {}          # id -> {"minutes","transfers","modes"} из кэша маршрутов
    if home:
        tcache = transit.snapshot()
        need_route = []
        for j in jobs:
            if j.lat is None or j.lon is None:
                continue
            res = transit.from_snapshot(tcache, home["lat"], home["lon"], j.lat, j.lon)
            if res and res.get("ok"):
                trips[j.id] = {
                    "minutes": int(res.get("minutes") or 0),
                    "transfers": int(res.get("transfers") or 0),
                    "modes": ", ".join(str(m) for m in (res.get("modes") or [])[:3] if m),
                    # виды транспорта (bus/train/metro/…) — иконки в бейдже
                    "kinds": transit.kinds_of(res)[:3],
                }
            elif res is None:
                need_route.append(j)
        # то, что человек открыл, считаем первым — иначе время в пути появлялось
        # бы у случайных вакансий, а не у тех, на которые он смотрит
        if need_route:
            try:
                import transit_worker
                transit_worker.request(need_route[:60])
            except Exception:  # noqa: BLE001 — очередь маршрутов не критична
                pass
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

    # Чек-лист настройки (F15): что уже готово для подачи. Профиль + вход Salling +
    # CV — необходимый минимум (required_done); дом и Telegram — по желанию.
    _profile = profile_store.load_profile()
    _creds = credentials_store.status()
    _setup = {
        "profile": all(str(_profile.get(k) or "").strip() for k, _ in PROFILE_REQUIRED),
        "login": bool(_creds.get("email")),
        "docs": bool(str(_profile.get("cv_path") or "").strip()),
        "home": bool(home),
        "telegram": bool(account_mod.load().get("tg_id")),
    }
    _setup["required_done"] = _setup["profile"] and _setup["login"] and _setup["docs"]
    _setup["done"] = sum(1 for v in (_setup["profile"], _setup["login"],
                                     _setup["docs"], _setup["home"], _setup["telegram"]) if v)
    _setup["total"] = 5

    # F36: полный проход по базе (match_count) нужен ТОЛЬКО когда автопилот включён —
    # на главной счётчик показывается лишь внутри {% if autopilot.enabled %}. Когда
    # выключен, не гоняем find_matches зря на каждый рендер.
    _ap_rule = autopilot.get_rule()
    _ap_count = autopilot.match_count() if _ap_rule.get("enabled") else 0

    _f = {
        "q": q,
        "source": source_key,
        "city": city,
        "brand": brand_code,
        "region": region_code,
        "category": category_code,
        "employment_type": employment_code,
        "job_level": level_code,
        "status": status,
        "sort": sort,
        "radius": radius,
        "group": group,
        "show_applied": show_applied,
        "period": period,
    }
    _current_filter_query = _filter_query(_f)
    _preset_views = []
    for saved_preset in settings_store.get_presets():
        saved_query = _clean_filter_query(saved_preset.get("query", ""))
        preset_view = {
            **saved_preset,
            "query": saved_query,
            "url": "/?" + (
                saved_query + "&" if saved_query else ""
            ) + "profile=" + quote_plus(saved_preset["id"]),
            "active": saved_preset["id"] == profile,
        }
        _preset_views.append(preset_view)
    _active_preset = next((p for p in _preset_views if p["active"]), None)
    _active_profile_modified = bool(
        _active_preset and _active_preset["query"] != _current_filter_query
    )

    _scope_urls = {}
    for scope in ("today", "3d", "all"):
        scoped = {**_f, "status": "active", "period": scope}
        _scope_urls[scope] = "/?" + _filter_query(scoped)
    _scope_urls["applied"] = "/?" + _filter_query(
        {**_f, "status": "applied", "period": "all"}
    )

    _status_labels = {
        "new": "Новые (не просмотрены)",
        "seen": "Просмотренные",
        "applied": "Поданные",
        "interview": "Собеседование",
        "offer": "Оффер",
        "rejected": "Отказ",
        "hidden": "Скрытые",
        "closed": "Закрытые",
    }
    _chip_values = {
        "q": ("Поиск", q),
        "city": ("Город", city),
        "source": ("Источник", JOB_SOURCE_LABELS.get(source_key, source_key)),
        "brand": ("Бренд", labels.brand(brand_code) if brand_code else ""),
        "region": ("Регион", labels.bi(labels.REGION, region_code) if region_code else ""),
        "category": ("Категория", labels.bi(labels.CATEGORY, category_code) if category_code else ""),
        "employment_type": (
            "Занятость",
            labels.bi(labels.EMPLOYMENT, employment_code) if employment_code else "",
        ),
        "job_level": ("Уровень", labels.bi(labels.LEVEL, level_code) if level_code else ""),
        "radius": ("Радиус", f"{radius} км" if radius else ""),
        "group": ("Вид", "По магазинам" if group else ""),
        "show_applied": ("Поданные", "Показывать в активных" if show_applied else ""),
        "status": (
            "Статус",
            _status_labels.get(status, "") if status not in {"", "active"} else "",
        ),
    }
    _filter_chips = [
        {
            "key": key,
            "label": label,
            "value": value,
            "url": "/?" + _filter_query(_f, drop=key),
        }
        for key, (label, value) in _chip_values.items()
        if value
    ]

    _today = _today_summary(home)
    resp = templates.TemplateResponse("index.html", {
        "request": request, "jobs": jobs, "count": len(jobs),
        "application_states": applications.states_for_jobs(jobs),
        "source_labels": JOB_SOURCE_LABELS,
        "groups": groups, "group": group,
        "total_filtered": total, "page": page, "pages": pages, "per_page": PER_PAGE,
        "cities": cities,
        "sources": [
            (key, labels.with_count(JOB_SOURCE_LABELS.get(key, key), counts["source"][key]))
            for key in sources if key
        ],
        # Опции отсортированы по популярности (частые сверху). Варианты без
        # активных вакансий скрываем — кроме выбранного сейчас, иначе его
        # нельзя было бы снять.
        "brands": [
            (b, labels.with_count(labels.brand(b), counts["brand"][b]))
            for b in sorted(brands, key=lambda k: -counts["brand"][k])
            if counts["brand"][b] > 0 or b == brand_code
        ],
        # Регионы: сначала датские, заграничные (Германия/Польша) — после них.
        "regions": [
            (r, labels.with_count(labels.label_or_pretty(labels.REGION, r), counts["region"][r]))
            for r in sorted(regions, key=lambda k: (k not in labels.DANISH_REGIONS, -counts["region"][k]))
            if counts["region"][r] > 0 or r == region_code
        ],
        "etypes": [
            (e, labels.with_count(labels.EMPLOYMENT.get(e, e), counts["employment"][e]))
            for e in sorted(etypes, key=lambda k: -counts["employment"][k])
            if counts["employment"][e] > 0 or e == employment_code
        ],
        "levels": [
            (l, labels.with_count(labels.LEVEL.get(l, l), counts["level"][l]))
            for l in sorted(levels, key=lambda k: -counts["level"][k])
            if counts["level"][l] > 0 or l == level_code
        ],
        "cats": [
            (c, labels.with_count(labels.label_or_pretty(labels.CATEGORY, c), counts["category"][c]))
            for c in sorted(cats, key=lambda k: -counts["category"][k])
            if counts["category"][c] > 0 or c == category_code
        ],
        "city_suggestions": [
            (city, labels.with_count(city, count))
            for city, count in counts["city"].most_common(120)
        ],
        "f": _f,
        "total_active": total_active, "applied_count": applied_count, "last_update": last,
        "autopilot": _ap_rule,
        "autopilot_count": _ap_count,
        "data_age_min": (max(0, int((utcnow() - last).total_seconds() // 60)) if last else None),
        "sync_running": _sync_state["running"],
        "sync_error": _sync_state["last_error"],
        "home": home, "distances": distances, "trips": trips, "geoerror": geoerror,
        "presets": _preset_views,
        "active_preset": _active_preset,
        "active_profile_modified": _active_profile_modified,
        "filter_chips": _filter_chips,
        "active_filter_count": len(_filter_chips),
        "scope_urls": _scope_urls,
        "batch": batch, "batch_mode": mode, "skipped": skipped, "dup": dup,
        "apply_files": _profile_file_info(_profile),
        "document_rule_count": len(document_rules.get_rules()),
        "batch_ai_available": ai_gateway.available(),
        "batch_ai_on": settings_store.get_ai_fill(),
        "batch_ai_motivation_on": settings_store.get_ai_fill_motivation(),
        "setup": _setup,
        "today": _today,
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
                # ручная пометка — не отправка: в журнале доверия она должна
                # отличаться от заявок, которые WexFlow реально отправил
                job.applied_confidence = "manual"
            s.add(job)
            s.commit()
            s.refresh(job)
        else:
            return _redirect_back(request, "/", error="Вакансия не найдена. Возможно, список обновился.")
    if status == "applied":
        applications.record_submitted([job])
    return _redirect_back(request, "/", notice=status_labels.get(status, "Статус вакансии обновлён."))


@app.post("/refresh")
def refresh(request: Request):
    # обновление уходит в фон: страница не виснет, индикатор в шапке показывает
    # «обновляется…», список сам перезагрузится по окончании
    threading.Thread(target=_sync_jobs, kwargs={"force_connectors": True}, daemon=True).start()
    return _redirect_back(request, "/", notice="Обновление вакансий запущено. Список сам перезагрузится, когда появятся свежие данные.")


@app.post("/presets/save")
def save_preset(
    request: Request,
    name: str = Form(...),
    query: str = Form(""),
    profile_id: str = Form(""),
):
    name = str(name or "").strip()[:50]
    if not name:
        return _redirect_back(request, "/", error="Напиши название профиля поиска.")
    clean_query = _clean_filter_query(query)
    saved = settings_store.add_preset(name, clean_query, profile_id)
    if not saved:
        return _redirect_back(request, "/", error="Не удалось сохранить профиль поиска.")
    target = "/?" + (
        clean_query + "&" if clean_query else ""
    ) + "profile=" + quote_plus(saved["id"])
    return RedirectResponse(
        _url_with_system_response(target, notice=f"Профиль «{saved['name']}» сохранён."),
        status_code=303,
    )


@app.post("/presets/delete")
def delete_preset(
    request: Request,
    name: str = Form(""),
    profile_id: str = Form(""),
):
    shown_name = str(name or "").strip() or "без названия"
    settings_store.delete_preset(name=name, preset_id=profile_id)
    ref = request.headers.get("referer") or "/"
    parts = urlsplit(ref)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("profile", None)
    target = urlunsplit(("", "", parts.path or "/", urlencode(query), ""))
    return RedirectResponse(
        _url_with_system_response(
            target,
            notice=f"Профиль «{shown_name}» удалён.",
        ),
        status_code=303,
    )


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


@app.get("/api/ai/usage")
def api_ai_usage():
    return JSONResponse(_ai_usage_payload())


def _ai_public(res) -> dict:
    """Безопасный ответ мастера/проверки: без ключа и сырого HTTP JSON."""
    return {
        "ok": bool(res.ok),
        "provider": res.provider,
        "model": res.model,
        "error_code": res.error_code,
        "error": res.error_message,
        "retry_after": res.retry_after,
    }


@app.get("/api/ai/providers")
def api_ai_providers():
    """Карточки провайдеров текущего аккаунта (только текущего; без ключей)."""
    return JSONResponse(ai_gateway.usage_payload())


@app.post("/api/ai/connect")
async def api_ai_connect(request: Request):
    """Мастер подключения: проверить переданный ключ и сохранить ТОЛЬКО при успехе.
    Ключ не отражается обратно, не логируется и не уходит в облако."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    provider = str(body.get("provider") or "").strip()
    key = str(body.get("key") or "").strip()
    consent = bool(body.get("consent")) if body.get("consent") is not None else None
    use_generation = bool(body.get("use_generation"))
    res = ai_gateway.connect(provider, key, consent=consent,
                             use_generation=use_generation)
    out = _ai_public(res)
    if res.ok:
        out["usage"] = ai_gateway.usage_payload()
        out["info"] = ai_secrets.info(provider)  # маска, не ключ
    status = 200 if res.ok else (401 if res.error_code == "invalid_key" else 200)
    return JSONResponse(out, status_code=status)


@app.post("/api/ai/validate")
async def api_ai_validate(request: Request):
    """Проверить уже сохранённое подключение (кнопка «Проверить»)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    provider = str(body.get("provider") or "").strip()
    if provider not in ("gemini", "groq"):
        return JSONResponse({"ok": False, "error": "Неизвестный провайдер."}, status_code=400)
    res = ai_gateway.validate_key(provider, use_generation=bool(body.get("use_generation")))
    out = _ai_public(res)
    out["usage"] = ai_gateway.usage_payload()
    return JSONResponse(out)


@app.post("/api/ai/disconnect")
async def api_ai_disconnect(request: Request):
    """Отключить/удалить локальный ключ провайдера (с подтверждением на фронте)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    provider = str(body.get("provider") or "").strip()
    if provider not in ("gemini", "groq"):
        return JSONResponse({"ok": False, "error": "Неизвестный провайдер."}, status_code=400)
    removed = ai_gateway.disconnect(provider)
    return JSONResponse({"ok": True, "removed": removed, "usage": ai_gateway.usage_payload()})


@app.post("/api/ai/consent")
async def api_ai_consent(request: Request):
    """Явное согласие на передачу необходимых полей выбранному ИИ."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    provider = str(body.get("provider") or "").strip()
    if provider not in ("gemini", "groq"):
        return JSONResponse({"ok": False, "error": "Неизвестный провайдер."}, status_code=400)
    ai_secrets.set_consent(provider, value=bool(body.get("value", True)))
    return JSONResponse({"ok": True, "usage": ai_gateway.usage_payload()})


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
        return JSONResponse(
            {
                "ok": False,
                "code": "login_required",
                "error": "Сначала войди через Telegram в разделе «Аккаунт».",
                "setupUrl": "/account#telegram-setup",
            },
            status_code=401,
        )
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
        threading.Thread(target=_tg_offer_tick, daemon=True).start()
    _reschedule_autopilot_scan()
    return {"ok": True, "mode": new_mode}


@app.post("/api/autopilot/preview")
async def api_autopilot_preview(request: Request):
    """Сколько активных вакансий подходит под НЕСОХРАНЁННЫЙ набор фильтров —
    для светофора «узко/нормально/широко». Ничего не сохраняет."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    p = {k: (body.get(k) or "") for k in autopilot._FILTER_FIELDS}
    p["enabled"] = True
    try:
        count = autopilot.profile_match_count(p)
    except Exception:  # noqa: BLE001
        count = 0
    return {"ok": True, "count": count}


@app.post("/api/autopilot/ai_suggest")
async def api_autopilot_ai_suggest(request: Request):
    """«Опиши словами / резюме» -> черновик фильтров через Gemini.
    Возвращает только черновик; применяет его пользователь вручную."""
    if not ai_filters.available():
        return {
            "ok": False,
            "error_code": "not_connected",
            "error": "ИИ ещё не подключён. Открой «Настройки → ИИ и лимиты» и подключи "
                     "бесплатный ключ (Groq — за пару минут, без карты).",
            "setupUrl": "/settings/ai",
        }
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return ai_filters.suggest_filters(
        body.get("text") or "",
        labels.CATEGORY, labels.BRANDS, labels.EMPLOYMENT, labels.REGION,
    )


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
    elif str(rule.get("max_km") or "").strip().lower() == "all":
        out.append({"label": "Радиус от дома", "value": "вся Дания (выбрано явно)"})
    elif autopilot.default_radius_applies(rule, settings_store.get_home()):
        out.append({"label": "Радиус от дома",
                    "value": f"до {autopilot.DEFAULT_HOME_RADIUS_KM} км (по умолчанию)"})
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
        name = (p.get("name") or "Набор") + ("" if p.get("enabled", True) else " · выкл")
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
            "events": autopilot.grouped_events(autopilot.event_log()),
            "rule_summary": _autopilot_profiles_summary(autopilot.get_profiles(rule)),
            "enabled": bool(rule.get("enabled")),
            "auto_submit": bool(rule.get("auto_submit")),
        },
    )


@app.get("/autopilot/mini", response_class=HTMLResponse)
def autopilot_mini(request: Request):
    """Компактный монитор автопилота для отдельного мини-окна (живая статистика)."""
    return templates.TemplateResponse("autopilot_mini.html", {"request": request})


# ── Журнал аудита подач (read-only): что и когда отправлено под именем ──────
_RU_MONTHS_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _audit_day_label(day, today) -> str:
    """Подпись дня: «Сегодня» / «Вчера» / «15 июня» (+год, если не текущий)."""
    delta = (today - day).days
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Вчера"
    label = f"{day.day} {_RU_MONTHS_GEN[day.month]}"
    return label if day.year == today.year else f"{label} {day.year}"


def _audit_groups(entries, now):
    """Группирует записи (уже по убыванию applied_at) по дню. entries — список
    dict с ключом applied_at (datetime). → [(подпись_дня, [запись, …]), …]."""
    today = now.date()
    groups = []
    for e in entries:
        ts = e.get("applied_at")
        label = _audit_day_label(ts.date(), today) if ts else "Без даты"
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(e)
    return groups


def _applied_proofs() -> dict:
    """Карта «id вакансии/requisition → имя файла-скриншота» из logs/applied.

    Воркер подачи сохраняет скрин результата как YYYYmmdd_HHMMSS_<rid>.png
    (apply._save_proof), где rid = requisition_id или job.id. Файлы отсортированы
    по имени = по времени, поэтому последний в списке — самый свежий скрин."""
    proof_dir = config.DATA_DIR / "logs" / "applied"
    proofs: dict[str, str] = {}
    try:
        for f in sorted(proof_dir.glob("*.png")):
            m = re.match(r"\d{8}_\d{6}_(.+)\.png$", f.name)
            if m:
                proofs[m.group(1)] = f.name
    except OSError:
        pass
    return proofs


@app.get("/applied-proof/{name}")
def applied_proof(name: str):
    """Отдаёт скрин-доказательство подачи из logs/applied. Только просмотр;
    имя строго проверяется, чтобы нельзя было выбраться из папки скринов."""
    if not re.fullmatch(r"[\w.\-]+\.png", name) or ".." in name:
        raise HTTPException(status_code=404)
    p = config.DATA_DIR / "logs" / "applied" / name
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type="image/png")


@app.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request):
    """Submitted jobs plus unfinished assisted connector forms. Read-only."""
    from db import utcnow
    proofs = _applied_proofs()
    with get_session() as s:
        rows = s.exec(
            select(Job).where(Job.applied_at.is_not(None)).order_by(Job.applied_at.desc())
        ).all()
        entries = [{
            "id": j.id, "title": j.title, "city": j.city, "brand": j.brand,
            "status": j.status, "applied_at": j.applied_at,
            "confidence": j.applied_confidence or "",
            "source": j.source, "activity": "submitted",
            "proof": proofs.get(str(j.requisition_id or "")) or proofs.get(str(j.id)) or "",
        } for j in rows]
        pending = s.exec(select(Application).where(
            Application.source != "salling",
            Application.state.in_(("submitting", "failed")),
        ).order_by(Application.updated_at.desc())).all()
        applied_keys = {(row["source"], row["id"]) for row in entries}
        for application in pending:
            if (application.source, application.job_id) in applied_keys:
                continue
            job = s.get(Job, application.job_id)
            entries.append({
                "id": application.job_id,
                "title": job.title if job else application.job_id,
                "city": job.city if job else "",
                "brand": job.brand if job else "",
                "status": job.status if job else "",
                "applied_at": application.updated_at,
                "confidence": "",
                "source": application.source,
                "activity": "preparing" if application.state == "submitting" else "incomplete",
                "proof": "",
            })
        entries.sort(key=lambda row: row.get("applied_at") or utcnow(), reverse=True)
    return templates.TemplateResponse("audit.html", {
        "request": request,
        "groups": _audit_groups(entries, utcnow()),
        "total": len(entries),
        "source_labels": JOB_SOURCE_LABELS,
    })


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    """Справка/FAQ — частые вопросы простыми словами. Статичная, только чтение."""
    return templates.TemplateResponse("help.html", {"request": request})


@app.get("/questions")
def questions_page(request: Request):
    """Вопросы, которые задают анкеты магазинов, и ответы человека.

    Смысл раздела: WexFlow не имеет права отвечать за человека, поэтому
    неотвеченный вопрос останавливает автоматическую подачу. Ответил здесь
    один раз — дальше подставляется само.
    """
    answers = profile_store.answers(profile_store.load_profile())
    return templates.TemplateResponse("questions.html", {
        "request": request,
        "stores": form_questions.by_store(answers),
    })


@app.post("/questions/answer")
async def questions_answer(request: Request):
    form = await request.form()
    key = str(form.get("key") or "").strip()
    value = str(form.get("value") or "").strip().lower()
    if value not in {"yes", "no", ""}:
        return JSONResponse({"ok": False, "error": "ответ бывает только да/нет"}, status_code=400)
    ok = form_questions.set_answer(key, value)
    if ok:
        threading.Thread(target=_sync_questions_to_cloud, kwargs={"force": True},
                         daemon=True, name="questions-sync").start()
    answers = profile_store.answers(profile_store.load_profile())
    return JSONResponse({"ok": ok, "pending": form_questions.pending_count(answers)})


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
def account_page(request: Request, saved: str = "", missing: str = "",
                 deleted: str = "", delete_error: str = "",
                 unlinked: str = "", unlink_warning: str = ""):
    """Общие настройки приложения: единый профиль, документы и подписка."""
    candidate = candidate_profiles.active_profile()
    family_candidate = candidate["id"] != candidate_profiles.PRIMARY_ID
    # Основной профиль читает владельца общей сессии устройства. Семейный
    # профиль читает только свою отдельную profilemember-привязку.
    locally_signed_in = account_mod.is_signed_in()
    cloud_account_linked = None
    family_identity = None
    if family_candidate:
        try:
            binding = cloud_auth.fetch_profile_binding(candidate["id"])
            if binding is not None:
                cloud_account_linked = bool(binding.get("linked"))
                telegram = binding.get("telegram")
                family_identity = dict(telegram) if isinstance(telegram, dict) else {}
                family_identity["linked"] = cloud_account_linked
        except Exception:  # noqa: BLE001
            pass
    elif locally_signed_in:
        try:
            session_state = cloud_auth.fetch_session_state()
            if session_state is not None:
                cloud_account_linked = bool(session_state.get("loggedIn"))
            u = session_state.get("user") if (
                session_state and session_state.get("loggedIn")
                and isinstance(session_state.get("user"), dict)
            ) else None
            if isinstance(u, dict):
                account_mod.apply_session(u)
        except Exception:  # noqa: BLE001 — обновление не должно мешать открытию страницы
            pass
    if family_candidate:
        telegram_relink = False
        telegram_ready = bool(cloud_account_linked)
    else:
        telegram_relink = bool(locally_signed_in and cloud_account_linked is False)
        telegram_ready = bool(locally_signed_in and not telegram_relink)
    profile = profile_store.load_profile()
    account_view = (
        account_mod.status(profile, family_identity or {"linked": False})
        if family_candidate else account_mod.status(profile)
    )
    account_tg_id = (
        str((family_identity or {}).get("tgId") or "")
        if family_candidate else str(account_mod.load().get("tg_id") or "")
    )
    city_options, country_options = _profile_choices()
    missing_fields = [x for x in missing.split(",") if x]
    return templates.TemplateResponse("account.html", {
        "request": request, "profile": profile,
        "file_info": _profile_file_info(profile),
        "saved": saved, "missing_fields": missing_fields,
        "deleted": deleted, "delete_error": delete_error,
        "unlinked": unlinked, "unlink_warning": unlink_warning,
        "city_options": city_options, "country_options": country_options,
        "subscription": subscription.status(),
        "account": account_view,
        "account_tg_id": account_tg_id,
        "cloud_login_url": cloud_auth.login_url(),
        "telegram_ready": telegram_ready,
        "telegram_relink": telegram_relink,
        "family_candidate": family_candidate,
        "candidate_profile": candidate,
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


def _home_city(home: dict | None) -> str:
    """Город из домашнего адреса (для кнопки «мой город»). Датский адрес
    оканчивается на «<4 цифры индекс> <город>»; берём город и убираем
    хвостовую букву района (København V -> København), чтобы совпало шире."""
    if not home:
        return ""
    text = (home.get("address") or home.get("lookup_address") or "").strip()
    m = re.search(r"\b\d{4}\s+([^,]+)$", text)
    city = (m.group(1) if m else "").strip()
    city = re.sub(r"\s+[A-ZÆØÅ]{1,2}$", "", city).strip()  # срез района: V, K, SV, NV…
    return city


def _document_settings_options() -> tuple[list[dict], list[dict]]:
    """Brands and physical stores available for document rules."""
    with get_session() as s:
        jobs = list(
            s.exec(
                select(Job).where(
                    Job.status.not_in(["closed", "hidden", "applied"]),
                )
            ).all()
        )

    brand_counts: Counter = Counter()
    discovered_brands: dict[str, str] = {}
    stores: dict[str, dict] = {}
    for job in jobs:
        brand = document_rules.brand_key(job)
        if not brand:
            continue
        brand_counts[brand] += 1
        discovered_brands.setdefault(
            brand,
            labels.BRANDS.get(brand, (job.brand or brand).strip()),
        )
        key = document_rules.store_key(job)
        if not key:
            continue
        brand_label = labels.BRANDS.get(brand, (job.brand or brand).title())
        address = ", ".join(
            value for value in [
                (job.street or "").strip(),
                " ".join(value for value in [(job.zip or "").strip(), (job.city or "").strip()] if value),
            ]
            if value
        )
        if key not in stores:
            stores[key] = {
                "key": key,
                "brand": brand,
                "brand_label": brand_label,
                "label": f"{brand_label} · {address}" if address else brand_label,
                "count": 0,
            }
        stores[key]["count"] += 1

    known_brands = {
        document_rules.brand_key(code): label
        for code, label in labels.BRANDS.items()
    }
    known_brands.update(discovered_brands)
    for rule in document_rules.get_rules():
        known_brands.setdefault(rule["brand"], rule["brand_label"])
    brand_options = [
        {
            "key": key,
            "label": label,
            "count": int(brand_counts.get(key, 0)),
        }
        for key, label in known_brands.items()
    ]
    brand_options.sort(key=lambda item: (-item["count"], item["label"].casefold()))
    store_options = sorted(
        stores.values(),
        key=lambda item: (item["brand_label"].casefold(), item["label"].casefold()),
    )
    return brand_options, store_options


def _document_rule_view(rule: dict) -> dict:
    view = dict(rule)
    cv_status = profile_store.file_status(rule.get("cv_path", ""))
    cover_status = profile_store.file_status(rule.get("cover_letter_path", ""))
    view.update({
        "cv_label": profile_store.file_label(rule.get("cv_path", "")),
        "cv_status": cv_status,
        "cv_url": (
            f"/settings/document-rule/{rule['id']}/cv"
            if cv_status == "ok" else ""
        ),
        "cover_label": profile_store.file_label(rule.get("cover_letter_path", "")),
        "cover_status": cover_status,
        "cover_url": (
            f"/settings/document-rule/{rule['id']}/cover"
            if cover_status == "ok" else ""
        ),
    })
    return view


def _document_import_view(preview: dict | None) -> dict | None:
    if not preview:
        return None
    preview_id = str(preview.get("id") or "")
    files = []
    for raw in preview.get("files", []):
        item = dict(raw)
        item["url"] = (
            f"/settings/document-import/preview/{preview_id}/{item['id']}"
            if preview_id and item.get("id") else ""
        )
        files.append(item)
    file_map = {item["id"]: item for item in files}
    groups = []
    for raw in preview.get("groups", []):
        group = dict(raw)
        group["cv_file"] = file_map.get(group.get("cv_id"))
        group["cover_file"] = file_map.get(group.get("cover_id"))
        group["confidence_percent"] = round(float(group.get("confidence") or 0) * 100)
        groups.append(group)
    result = dict(preview)
    result["files"] = files
    result["groups"] = groups
    result["unassigned_files"] = [
        file_map[file_id]
        for file_id in preview.get("unassigned", [])
        if file_id in file_map
    ]
    return result


def _settings_context(
    request: Request,
    saved: str = "",
    geoerror: str = "",
    missing: str = "",
    section: str = "salling",
) -> dict:
    profile = profile_store.load_profile()
    ap_profiles = autopilot.ensure_profiles()
    sel_id = request.query_params.get("profile") or ""
    sel_profile = next((p for p in ap_profiles if p.get("id") == sel_id), ap_profiles[0])
    ap_view = dict(autopilot.get_rule())
    for k in autopilot._FILTER_FIELDS:
        ap_view[k] = sel_profile.get(k, autopilot.DEFAULT_RULE.get(k))
    ap_cities, ap_regions = _autopilot_geo_options(ap_view)
    # опции для чипов «Кем работать» и «Сети» со счётчиками активных вакансий,
    # частые сверху — чтобы выбор был осмысленным, а не вслепую
    ap_cat_options: list = []
    ap_brand_options: list = []
    if section == "autopilot":
        with get_session() as s:
            _fc = _active_counts(s)
        ap_cat_options = sorted(
            ((code, lbl, int(_fc["category"].get(code, 0))) for code, lbl in labels.CATEGORY.items()),
            key=lambda t: (-t[2], t[1]),
        )
        ap_brand_options = sorted(
            ((code, lbl, int(_fc["brand"].get(code, 0))) for code, lbl in labels.BRANDS.items()),
            key=lambda t: (-t[2], t[1]),
        )
    document_brand_options: list[dict] = []
    document_store_options: list[dict] = []
    if section == "documents":
        document_brand_options, document_store_options = _document_settings_options()
    document_target_options = [
        {
            "value": "global",
            "label": "Общий комплект · для всего остального",
        },
        *[
        {
            "value": f"brand:{item['key']}",
            "label": f"Бренд · {item['label']}",
        }
        for item in document_brand_options
        ],
    ]
    document_target_options.extend(
        {
            "value": f"store:{item['key']}",
            "label": f"Магазин · {item['label']}",
        }
        for item in document_store_options
    )
    saved_document_rules = document_rules.get_rules()
    titles = {
        "salling": ("Salling", "Логин, домашний адрес и управление сохранённой сессией"),
        "documents": ("Документы", "CV и мотивационные письма для брендов и отдельных магазинов"),
        "autopilot": ("Автопилот", "Наборы фильтров, режим работы и автоотправка"),
        "telegram": ("Telegram", "Статус @wexflowbot, проверка и ручная отправка текущих"),
        "forms": ("Анкеты и ИИ", "Умное дозаполнение внешних форм и безопасные черновики"),
        "ai": ("ИИ и лимиты", "Провайдеры ИИ, подключение бесплатного Groq и остаток ресурса"),
        "overview": ("Настройки", "Короткая карта управления WexFlow"),
    }
    settings_title, settings_meta = titles.get(section, titles["salling"])
    return {
        "request": request,
        "profile": profile, "file_info": _profile_file_info(profile),
        "creds": credentials_store.status(), "home": settings_store.get_home(),
        "saved": saved, "geoerror": geoerror,
        "subscription": subscription.status(),
        "account_tg_id": account_mod.load().get("tg_id") or "",
        "account_signed_in": account_mod.is_signed_in(),
        "settings_section": section,
        "settings_title": settings_title,
        "settings_meta": settings_meta,
        "document_rules": [_document_rule_view(rule) for rule in saved_document_rules],
        "document_rule_count": len(saved_document_rules),
        "document_brand_options": document_brand_options,
        "document_store_options": document_store_options,
        "document_target_options": document_target_options,
        "document_import_preview": _document_import_view(document_import.get_preview()),
        "document_import_ai_available": ai_filters.available(),
        "autopilot": ap_view, "brands": labels.BRANDS,
        "categories": labels.CATEGORY, "employments": labels.EMPLOYMENT,
        "autopilot_cities": ap_cities, "autopilot_regions": ap_regions,
        "autopilot_region_labels": labels.REGION,
        "autopilot_cat_options": ap_cat_options,
        "autopilot_brand_options": ap_brand_options,
        "autopilot_mode": autopilot.get_mode(),
        "home_city": _home_city(settings_store.get_home()),
        "ai_available": ai_filters.available(),
        "ai_fill_on": settings_store.get_ai_fill(),
        "apply_mode": settings_store.get_apply_mode(),
        "questions_pending": _questions_pending_badge(),
        "ai_fill_motivation_on": settings_store.get_ai_fill_motivation(),
        "ai_fill_available": ai_gateway.available(),
        "ai_usage": _ai_usage_payload(),
        "ai_providers": ai_gateway.usage_payload(),
        "ai_groq_keys_url": "https://console.groq.com/keys",
        "ai_groq_privacy_url": "https://console.groq.com/docs/your-data",
        "ai_gemini_keys_url": "https://aistudio.google.com/apikey",
        "ai_gemini_privacy_url": "https://ai.google.dev/gemini-api/terms",
        "ai_legacy_gemini": bool(ai_secrets.legacy_gemini_key()) and not ai_secrets.info("gemini")["connected"],
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
        "default_radius_km": autopilot.DEFAULT_HOME_RADIUS_KM,
        "tg_daily_max": autopilot.TG_DAILY_MAX,
        "autostart": autostart.status(),
    }


def _render_settings_section(
    request: Request,
    section: str,
    saved: str = "",
    geoerror: str = "",
    missing: str = "",
):
    return templates.TemplateResponse(
        "settings.html",
        _settings_context(request, saved=saved, geoerror=geoerror, missing=missing, section=section),
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return templates.TemplateResponse(
        "settings_overview.html",
        _settings_context(request, saved=saved, geoerror=geoerror, missing=missing, section="overview"),
    )


@app.get("/settings/salling", response_class=HTMLResponse)
def settings_salling(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "salling", saved=saved, geoerror=geoerror, missing=missing)


@app.get("/settings/documents", response_class=HTMLResponse)
def settings_documents(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "documents", saved=saved, geoerror=geoerror, missing=missing)


@app.get("/settings/autopilot", response_class=HTMLResponse)
def settings_autopilot(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "autopilot", saved=saved, geoerror=geoerror, missing=missing)


@app.get("/settings/telegram", response_class=HTMLResponse)
def settings_telegram(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "telegram", saved=saved, geoerror=geoerror, missing=missing)


@app.get("/settings/forms", response_class=HTMLResponse)
def settings_forms(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "forms", saved=saved, geoerror=geoerror, missing=missing)


@app.get("/settings/ai", response_class=HTMLResponse)
def settings_ai(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "ai", saved=saved, geoerror=geoerror, missing=missing)


@app.post("/settings/ai/migrate-gemini")
async def settings_ai_migrate_gemini(request: Request):
    """Привязать существующий Gemini-ключ (secrets.json) к текущему аккаунту —
    только по явному подтверждению владельца."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    ok = ai_secrets.migrate_legacy_gemini(confirm=bool(body.get("confirm")))
    return JSONResponse({"ok": ok, "usage": ai_gateway.usage_payload()})


@app.post("/account/save")
@app.post("/settings/save")  # legacy-алиас: общий профиль теперь в «Общих настройках»
def account_save(
    first_name: str = Form(""), last_name: str = Form(""), email: str = Form(""),
    phone: str = Form(""), address: str = Form(""), zipcode: str = Form(""),
    city: str = Form(""), country: str = Form(""), linkedin: str = Form(""),
    work_authorization: str = Form(""), languages: str = Form(""),
    experience_years: str = Form(""), current_role: str = Form(""),
    education: str = Form(""), available_from: str = Form(""),
    date_of_birth: str = Form(""), about: str = Form(""),
    gender: str = Form(""), start_date: str = Form(""),
    retail_experience: str = Form(""), work_weekends: str = Form(""),
    work_evenings: str = Form(""), work_early: str = Form(""),
    work_night: str = Form(""), has_drivers_license: str = Form(""),
    profile_visible: str = Form(""),
):
    """Общий профиль — только личные данные. Документы (CV/письмо) — в настройках фирмы.
    Поля после linkedin — необязательные, их использует ИИ-дозаполнение форм (бета).
    Блок «Ответы для анкет» — те самые вопросы магазинов (пол, дата выхода,
    выходные/вечера/раннее утро): заполняются один раз и подставляются как есть."""
    profile = profile_store.load_profile()
    answers_form = {
        "gender": gender, "start_date": start_date,
        "retail_experience": retail_experience, "work_weekends": work_weekends,
        "work_evenings": work_evenings, "work_early": work_early,
        "work_night": work_night, "has_drivers_license": has_drivers_license,
        "profile_visible": profile_visible,
    }
    profile.update({
        key: profile_store.clean_answer(key, value)
        for key, value in answers_form.items()
    })
    profile.update({
        "first_name": first_name.strip(), "last_name": last_name.strip(),
        "email": email.strip(), "phone": phone.strip(), "address": address.strip(),
        "zip": zipcode.strip(), "city": city.strip(), "country": country.strip(),
        "linkedin": linkedin.strip(),
        "work_authorization": work_authorization.strip(), "languages": languages.strip(),
        "experience_years": experience_years.strip(), "current_role": current_role.strip(),
        "education": education.strip(), "available_from": available_from.strip(),
        "date_of_birth": date_of_birth.strip(), "about": about.strip(),
    })
    missing = _profile_missing(profile)
    if missing:
        return RedirectResponse("/account?missing=" + quote_plus(",".join(missing)), status_code=303)
    profile_store.save_profile(profile)
    return RedirectResponse("/account?saved=1", status_code=303)


@app.post("/settings/apply-mode")
async def settings_apply_mode(request: Request):
    """Режим подачи: «auto» — WexFlow жмёт финальную кнопку сам, «fill» — только
    заполняет. Неотвеченный вопрос анкеты останавливает отправку в обоих."""
    form = await request.form()
    mode = settings_store.set_apply_mode(str(form.get("mode") or ""))
    return JSONResponse({"ok": True, "mode": mode})


@app.post("/settings/ai-fill")
async def settings_ai_fill(request: Request):
    """БЕТА-тумблер ИИ-дозаполнения форм. Включая его, пользователь соглашается,
    что данные профиля уходят в Google Gemini для подбора ответов. Отправку анкет
    ИИ по-прежнему НЕ делает — только заполняет, финальную кнопку жмёт человек."""
    form = await request.form()
    raw = str(form.get("enabled") or "").strip().lower()
    enabled = raw in ("1", "true", "yes", "on")
    settings_store.set_ai_fill(enabled)
    return JSONResponse({
        "ok": True,
        "enabled": settings_store.get_ai_fill(),
        "motivation_enabled": settings_store.get_ai_fill_motivation(),
    })


@app.post("/settings/ai-usage-limit")
async def settings_ai_usage_limit(request: Request):
    form = await request.form()
    try:
        limit = int(str(form.get("daily_limit") or "").strip())
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "Укажи дневной лимит целым числом."},
            status_code=400,
        )
    if not 1 <= limit <= ai_usage.MAX_DAILY_LIMIT:
        return JSONResponse(
            {"ok": False, "error": f"Допустимо от 1 до {ai_usage.MAX_DAILY_LIMIT:,} запросов."},
            status_code=400,
        )
    ai_usage.set_daily_limit(limit)
    return JSONResponse({"ok": True, "usage": _ai_usage_payload()})


@app.post("/settings/ai-fill-motivation")
async def settings_ai_fill_motivation(request: Request):
    """Под-тумблер: разрешить ИИ писать ЧЕРНОВИК мотивации (свободные вопросы
    «почему к нам») из «о себе». Единственное место, где ИИ сочиняет текст —
    поэтому отдельно и ВЫКЛ по умолчанию. Черновик всегда проверяет человек."""
    form = await request.form()
    raw = str(form.get("enabled") or "").strip().lower()
    requested = raw in ("1", "true", "yes", "on")
    if requested and not settings_store.get_ai_fill():
        settings_store.set_ai_fill_motivation(False)
        return JSONResponse({
            "ok": False,
            "enabled": False,
            "error": "Сначала включи основное ИИ-заполнение.",
        })
    settings_store.set_ai_fill_motivation(requested)
    return JSONResponse({"ok": True, "enabled": settings_store.get_ai_fill_motivation()})


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
    if not candidate_profiles.is_primary():
        return
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
    if not candidate_profiles.is_primary():
        candidate = candidate_profiles.active_profile()
        binding = cloud_auth.fetch_profile_binding(candidate["id"])
        telegram = binding.get("telegram") if isinstance(binding, dict) else None
        if binding and binding.get("linked") and isinstance(telegram, dict):
            return JSONResponse({
                "signed_in": True,
                "tg_id": telegram.get("tgId") or "",
                "name": telegram.get("name") or "",
                "username": telegram.get("username") or "",
                "plan": telegram.get("plan") or "free",
                "family_profile": candidate["id"],
            })
        return JSONResponse({"signed_in": False, "family_profile": candidate["id"]})

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
    return JSONResponse({
        "signed_in": False,
        "local_signed_in": account_mod.is_signed_in(),
    })


@app.post("/account/logout")
def account_logout():
    """Отвязать Telegram от этого ПК и остановить зависящий от него режим."""
    was_signed_in = account_mod.is_signed_in()
    if autopilot.get_mode() == "telegram":
        autopilot.set_mode("off")
        _reschedule_autopilot_scan()
    # Локально прекращаем слушать команды сразу, до сетевого запроса: даже при
    # медленном/недоступном облаке этот ПК уже безопасно отключён.
    account_mod.sign_out()
    cloud_unlinked = True
    if was_signed_in:
        cloud_unlinked = bool(cloud_auth.unlink_device().get("ok"))
    target = "/account?unlinked=1" if cloud_unlinked else "/account?unlink_warning=cloud"
    return RedirectResponse(target, status_code=303)


@app.post("/account/delete-cloud")
def account_delete_cloud():
    """GDPR: удалить облачные данные, не трогая локальные файлы пользователя."""
    if not account_mod.is_signed_in():
        return RedirectResponse("/account?delete_error=not_signed_in", status_code=303)
    result = cloud_auth.delete_cloud_data()
    if not result.get("ok"):
        return RedirectResponse("/account?delete_error=cloud", status_code=303)
    account_mod.sign_out()
    return RedirectResponse("/account?deleted=1", status_code=303)


@app.post("/account/link/code")
def account_link_code():
    """Получить одноразовый код привязки по ID. Пользователь отправляет его боту
    @wexflowbot — облако логинит аккаунт в это устройство, а /account/login/poll
    подхватит вход. Запасной путь к «Войти через Telegram», без браузера."""
    if not candidate_profiles.is_primary():
        candidate = candidate_profiles.active_profile()
        return JSONResponse(cloud_auth.create_profile_invite(
            candidate["id"], candidate["name"]))
    return JSONResponse(cloud_auth.link_new())


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
    remove_document: str = Form(""),
):
    """Global documents used when no store or brand rule overrides them."""
    profile = profile_store.load_profile()
    remove_key = {
        "cv": "cv_path",
        "cover": "cover_letter_path",
    }.get(str(remove_document or "").strip())
    if remove_key:
        old_path = str(profile.get(remove_key) or "")
        profile[remove_key] = ""
        profile_store.save_profile(profile)
        profile_store.remove_managed_document(old_path)
        return RedirectResponse(
            "/settings/documents?saved=removed#document-rules",
            status_code=303,
        )
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(_url_with_system_response("/settings/documents", error=file_error), status_code=303)
    profile_store.save_profile(profile)
    return RedirectResponse("/settings/documents?saved=1#document-rules", status_code=303)


@app.post("/settings/document-rules/save")
def settings_document_rule_save(
    rule_id: str = Form(""),
    name: str = Form(""),
    scope: str = Form("brand"),
    brand: str = Form(""),
    store_key: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
):
    existing = document_rules.get_rule(rule_id) if rule_id else None
    if rule_id and existing is None:
        return RedirectResponse(
            _url_with_system_response("/settings/documents", error="Комплект документов не найден."),
            status_code=303,
        )

    brand_options, store_options = _document_settings_options()
    brand = document_rules.brand_key(brand)
    brand_option = next((item for item in brand_options if item["key"] == brand), None)
    store_option = next(
        (
            item for item in store_options
            if item["key"] == store_key and item["brand"] == brand
        ),
        None,
    )
    if scope == "store" and store_option is None:
        # An inactive store can still be edited without losing its saved rule.
        if not (
            existing
            and existing["scope"] == "store"
            and existing["store_key"] == store_key
            and existing["brand"] == brand
        ):
            return RedirectResponse(
                _url_with_system_response("/settings/documents", error="Выбери магазин из списка."),
                status_code=303,
            )

    cv_path = (existing or {}).get("cv_path", "")
    cover_path = (existing or {}).get("cover_letter_path", "")
    try:
        if cv_file and cv_file.filename:
            cv_path = profile_store.save_upload(cv_file, "rule_cv")
        if cover_letter_file and cover_letter_file.filename:
            cover_path = profile_store.save_upload(cover_letter_file, "rule_cover")
        document_rules.save_rule(
            rule_id=rule_id,
            name=name,
            scope=scope,
            brand=brand,
            brand_label=(
                (brand_option or {}).get("label")
                or (existing or {}).get("brand_label")
                or brand
            ),
            selected_store_key=store_key,
            store_label=(
                (store_option or {}).get("label")
                or (existing or {}).get("store_label")
                or ""
            ),
            cv_path=cv_path,
            cover_letter_path=cover_path,
        )
    except ValueError as exc:
        return RedirectResponse(
            _url_with_system_response("/settings/documents", error=str(exc)),
            status_code=303,
        )
    return RedirectResponse("/settings/documents?saved=rule#document-rules", status_code=303)


@app.post("/settings/document-rules/delete")
def settings_document_rule_delete(rule_id: str = Form("")):
    document_rules.delete_rule(rule_id)
    return RedirectResponse("/settings/documents?saved=deleted#document-rules", status_code=303)


@app.post("/settings/document-import/analyse")
def settings_document_import_analyse(files: list[UploadFile] = File(default=[])):
    brand_options, store_options = _document_settings_options()
    result = document_import.analyse_uploads(files, brand_options, store_options)
    if not result.get("ok"):
        return RedirectResponse(
            _url_with_system_response(
                "/settings/documents#bulk-import",
                error=str(result.get("error") or "Не удалось разобрать документы."),
            ),
            status_code=303,
        )
    return RedirectResponse(
        "/settings/documents?saved=import-preview#bulk-import",
        status_code=303,
    )


@app.get("/settings/document-import/file/{preview_id}/{file_id}")
def settings_document_import_file(preview_id: str, file_id: str):
    item = _current_document_import_file(preview_id, file_id)
    filename = item["filename"]
    is_pdf = filename.lower().endswith(".pdf")
    return FileResponse(
        item["path"],
        media_type="application/pdf" if is_pdf else "application/octet-stream",
        filename=filename,
        content_disposition_type="inline" if is_pdf else "attachment",
    )


def _current_document_import_file(preview_id: str, file_id: str) -> dict:
    preview = document_import.get_preview()
    if not preview or preview.get("id") != preview_id:
        raise HTTPException(status_code=404, detail="План импорта устарел")
    item = next(
        (
            candidate
            for candidate in preview.get("files", [])
            if candidate.get("id") == file_id
        ),
        None,
    )
    path = str((item or {}).get("path") or "")
    if not path or profile_store.file_status(path) != "ok":
        raise HTTPException(status_code=404, detail="Файл не найден")
    clean_path = profile_store.validate_document_path(path)
    filename = str((item or {}).get("filename") or profile_store.file_label(clean_path))
    return {
        "id": file_id,
        "path": clean_path,
        "filename": filename,
        "is_pdf": filename.lower().endswith(".pdf"),
    }


@app.get(
    "/settings/document-import/preview/{preview_id}/{file_id}",
    response_class=HTMLResponse,
)
def settings_document_import_preview(request: Request, preview_id: str, file_id: str):
    item = _current_document_import_file(preview_id, file_id)
    return templates.TemplateResponse(
        "document_preview.html",
        {
            "request": request,
            "file": item,
            "raw_url": f"/settings/document-import/file/{preview_id}/{file_id}",
            "back_url": "/settings/documents#bulk-import",
        },
    )


@app.post("/settings/document-import/apply")
def settings_document_import_apply(
    preview_id: str = Form(""),
    selections: list[str] = Form(default=[]),
):
    preview = document_import.get_preview()
    if not preview or preview.get("id") != preview_id:
        return RedirectResponse(
            _url_with_system_response(
                "/settings/documents#bulk-import",
                error="План импорта устарел. Загрузи файлы ещё раз.",
            ),
            status_code=303,
        )
    selected_targets: dict[str, str] = {}
    for raw in selections:
        group_id, separator, target = str(raw or "").partition("|")
        if separator and group_id:
            selected_targets[group_id] = target
    brand_options, store_options = _document_settings_options()
    try:
        created = document_import.apply_preview(
            preview,
            selected_targets,
            brand_options,
            store_options,
        )
    except ValueError as exc:
        return RedirectResponse(
            _url_with_system_response("/settings/documents#bulk-import", error=str(exc)),
            status_code=303,
        )
    if not created:
        return RedirectResponse(
            _url_with_system_response(
                "/settings/documents#bulk-import",
                error="Не выбран ни один комплект для закрепления.",
            ),
            status_code=303,
        )
    document_import.clear_preview()
    return RedirectResponse(
        f"/settings/documents?saved=imported&count={len(created)}#document-rules",
        status_code=303,
    )


@app.post("/settings/document-import/cancel")
def settings_document_import_cancel():
    document_import.clear_preview(delete_files=True)
    return RedirectResponse("/settings/documents#bulk-import", status_code=303)


@app.post("/api/telegram/clear_pending")
def api_telegram_clear_pending():
    """Снять все ожидающие Telegram-карточки разом (очередь решений)."""
    if not account_mod.is_signed_in():
        return JSONResponse(
            {
                "ok": False,
                "code": "login_required",
                "error": "Сначала войди через Telegram в разделе «Аккаунт».",
                "setupUrl": "/account#telegram-setup",
            },
            status_code=401,
        )
    return JSONResponse({"ok": True, "cleared": autopilot.tg_pending_clear_all()})


@app.post("/api/telegram/digest")
async def api_telegram_digest(request: Request):
    """Включить/выключить дневной дайджест вместо потока карточек."""
    if not account_mod.is_signed_in():
        return JSONResponse(
            {
                "ok": False,
                "code": "login_required",
                "error": "Сначала войди через Telegram в разделе «Аккаунт».",
                "setupUrl": "/account#telegram-setup",
            },
            status_code=401,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    autopilot.set_tg_digest(bool((body or {}).get("enabled")))
    return JSONResponse({"ok": True, "digest": autopilot.tg_digest_enabled()})


@app.post("/settings/autostart")
def settings_autostart(enable: str = Form("")):
    """Автозапуск WexFlow при входе в Windows (HKCU Run, только своя запись).
    Работает лишь в собранном приложении — в dev переключателя нет."""
    if enable == "1":
        autostart.enable()
    else:
        autostart.disable()
    return RedirectResponse("/settings/autopilot?saved=1", status_code=303)


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

    def _text_csv(v: str) -> str:
        out: list[str] = []
        seen: set[str] = set()
        for p in str(v or "").split(","):
            item = re.sub(r"\s+", " ", p.strip(" ,;"))
            if not item:
                continue
            key = item.casefold()
            if key not in seen:
                seen.add(key)
                out.append(item)
        return ", ".join(out)

    # ФИЛЬТРЫ пишем в выбранный профиль (режим/лимиты/расписание — глобальные).
    # max_km="all" — явный выбор «вся Дания»: отключает дефолтный радиус от дома.
    autopilot.save_profile_filters(profile_id, {
        "max_km": "all" if max_km.strip().lower() == "all" else _num_csv(max_km),
        "min_hours": _num_csv(min_hours),
        "max_hours": _num_csv(max_hours),
        "max_age_days": _num_csv(max_age_days),
        "category": category.strip(),
        "employment_type": employment_type.strip(),
        "age": age.strip(),
        "keywords": _text_csv(keywords),
        "brand": brand.strip(),
        "cities": _text_csv(cities),
        "regions": regions.strip(),
        "exclude_brands": exclude_brands.strip(),
        "exclude_cities": _text_csv(exclude_cities),
        "exclude_keywords": _text_csv(exclude_keywords),
    })
    autopilot.save_rule({"active_from": _hour(active_from, 0), "active_to": _hour(active_to, 24)})
    # «с этого момента»: текущие совпадения считаем уже виденными (без спама о старых).
    autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
    _reschedule_autopilot_scan()
    dest = f"/settings/autopilot?saved=1&profile={profile_id}" if profile_id else "/settings/autopilot?saved=1"
    return RedirectResponse(dest, status_code=303)


@app.post("/autopilot/profile/add")
def autopilot_profile_add(name: str = Form("")):
    default_name = f"Набор {len(autopilot.ensure_profiles()) + 1}"
    pid = autopilot.add_profile((name or "").strip() or default_name)
    return RedirectResponse(f"/settings/autopilot?profile={pid}", status_code=303)


@app.post("/autopilot/profile/delete")
def autopilot_profile_delete(profile_id: str = Form("")):
    autopilot.delete_profile(profile_id)
    return RedirectResponse("/settings/autopilot", status_code=303)


@app.post("/autopilot/profile/rename")
def autopilot_profile_rename(profile_id: str = Form(""), name: str = Form("")):
    autopilot.rename_profile(profile_id, name)
    return RedirectResponse(f"/settings/autopilot?profile={profile_id}", status_code=303)


@app.post("/autopilot/profile/toggle")
def autopilot_profile_toggle(profile_id: str = Form("")):
    autopilot.toggle_profile(profile_id)
    return RedirectResponse(f"/settings/autopilot?profile={profile_id}", status_code=303)


@app.post("/autopilot/prepare")
def autopilot_prepare(request: Request):
    """Автоподготовка (Фаза 2): открыть до 5 свежих подходящих вакансий,
    заполнить анкеты и ОСТАНОВИТЬСЯ перед отправкой. Реальную отправку (--submit)
    не запускаем НИКОГДА — её жмёт пользователь сам."""
    jobs = autopilot.pending_prepare(limit=5)
    if not jobs:
        return _redirect_back(request, "/settings/autopilot",
                              notice="Новых подходящих для подготовки нет — свежие уже готовились.")
    ids = [j.id for j in jobs]
    _launch_salling_apply(ids, submit=False)
    autopilot.mark_prepared(ids)
    return _redirect_back(
        request, "/settings/autopilot",
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
                request, "/settings/autopilot",
                error=f"Охват «все подходящие» сейчас нельзя: под набор фильтров подходит {pool} "
                      f"(предел {autopilot.SCOPE_ALL_GUARD}). Сузь фильтры или оставь «только новые».",
            )

    autopilot.save_rule({"daily_limit": limit, "submit_scope": scope})
    # переключение на «только новые» — обновить baseline (слать лишь новые после этого)
    if scope == "new" and was_scope != "new":
        autopilot.set_autosubmit_baseline()
    return _redirect_back(request, "/settings/autopilot", notice="Настройки автоотправки сохранены.")


@app.post("/autopilot/stop")
def autopilot_stop(request: Request):
    """Аварийная остановка автоотправки — мгновенно выключает."""
    autopilot.save_rule({"auto_submit": False})
    return _redirect_back(request, "/settings/autopilot", notice="Автоотправка остановлена.")


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
def _telegram_cloud_state(signed_in: bool, fail_streak: int, last_ok: float) -> str:
    return (
        "paused" if not signed_in else
        "offline" if fail_streak >= 3 else
        "online" if last_ok > 0 else
        "checking"
    )


@app.get("/api/telegram/status")
def telegram_status():
    acc = account_mod.load()
    rule = autopilot.get_rule()
    signed_in = account_mod.is_signed_in()
    fail_streak = int(_tg_poll_state.get("fail_streak") or 0)
    last_ok = float(_tg_poll_state.get("last_ok") or 0.0)
    cloud_state = _telegram_cloud_state(signed_in, fail_streak, last_ok)
    return {
        "cloud": True,
        "signed_in": signed_in,
        "cloud_state": cloud_state,
        "cloud_fail_streak": fail_streak,
        "cloud_last_ok": last_ok,
        "cloud_error": str(_tg_poll_state.get("last_error") or ""),
        "cloud_error_code": str(_tg_poll_state.get("error_code") or ""),
        "tg_id": acc.get("tg_id") or "",
        "username": acc.get("username") or "",
        "name": acc.get("tg_name") or "",
        "approval": bool(rule.get("tg_approval")),
        "mode": autopilot.get_mode(),
        "within_schedule": autopilot.within_schedule(rule),
        "max_per_send": TG_MAX_PER_SCAN,
        "digest": bool(rule.get("tg_digest")),
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
    if not account_mod.is_signed_in():
        return JSONResponse(
            {
                "ok": False,
                "code": "login_required",
                "error": "Сначала войди через Telegram в разделе «Аккаунт».",
                "setupUrl": "/account#telegram-setup",
            },
            status_code=401,
        )
    if on:
        autopilot.set_mode("telegram")
    else:
        autopilot.save_rule({"tg_approval": False})
    if on:
        # спрашивать только про НОВЫЕ вакансии (текущие фиксируем как baseline),
        # если охват «только новые» — чтобы не завалить бэклогом
        if (autopilot.get_rule().get("submit_scope") or "new") != "all":
            autopilot.set_autosubmit_baseline()
        autopilot.save_rule({"seen_ids": [j.id for j in autopilot.find_matches()]})
    _reschedule_autopilot_scan()
    return {"ok": True, "on": on}


def _send_telegram_demo_card() -> dict:
    """Отправить проверочную карточку — РЕАЛЬНЫЙ вид сообщения автопилота
    на примере подходящей вакансии. Кнопки примера ничего не подают.

    Один и тот же путь для кнопки «Отправить проверочное» в приложении и для
    такой же кнопки в Telegram-панели (команда test)."""
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
    r = cloud_auth.offer(text, "__demo__", demo=True)
    return {"ok": bool(r and r.get("ok")), "error": (r or {}).get("error", "")}


@app.post("/api/telegram/test")
def telegram_test():
    """Проверочное сообщение = РЕАЛЬНЫЙ вид карточки вакансии с кнопками
    (на примере подходящей вакансии). Кнопки в примере ничего не отправляют."""
    if not account_mod.is_signed_in():
        return JSONResponse(
            {
                "ok": False,
                "code": "login_required",
                "error": "Сначала войди через Telegram в разделе «Аккаунт».",
                "setupUrl": "/account#telegram-setup",
            },
            status_code=401,
        )
    return _send_telegram_demo_card()


@app.post("/api/telegram/setup-test")
def telegram_setup_test():
    """Шаг мастера подключения: простое сообщение без вакансии и кнопок подачи."""
    if not _cloud_profile_enabled():
        return JSONResponse(
            {
                "ok": False,
                "code": "login_required",
                "error": "Сначала войди через Telegram.",
                "setupUrl": "/account#telegram-setup",
            },
            status_code=401,
        )
    result = cloud_auth.send_test_message(
        "✅ <b>WexFlow подключён</b>\n"
        "Связь с этим компьютером работает. Теперь можно включать уведомления "
        "и подтверждение подачи в настройках Telegram."
    )
    if str(result.get("error") or "").strip().lower() == "device not linked":
        link = cloud_auth.link_new()
        if link.get("ok") and link.get("code"):
            code = str(link["code"])
            bot_username = str(link.get("botUsername") or "wexflowbot")
            result = {
                "ok": False,
                "code": "device_not_linked",
                "error": (
                    f"Аккаунт сохранён на ПК, но облачная привязка отсутствует. "
                    f"Отправь боту код {code}."
                ),
                "needsRelink": True,
                "linkCode": code,
                "botUrl": f"https://t.me/{bot_username}?start={code}",
            }
    payload = {
        "ok": bool(result.get("ok")),
        "code": str(result.get("code") or ""),
        "error": str(result.get("error") or ""),
        "needsBotStart": bool(result.get("needsBotStart")),
        "botUrl": str(result.get("botUrl") or "https://t.me/wexflowbot"),
    }
    if result.get("needsRelink"):
        payload.update({
            "needsRelink": True,
            "linkCode": str(result.get("linkCode") or ""),
        })
    return JSONResponse(payload)


@app.post("/api/telegram/send-current")
async def telegram_send_current(request: Request, panel: bool = False):
    """Ручная отправка текущих подходящих вакансий в Telegram.
    Нужна для понятного сценария: счётчик «подходит» уже есть, но безопасный
    режим автоматически шлёт только новые после включения."""
    if not account_mod.is_signed_in():
        return JSONResponse(
            {
                "ok": False,
                "code": "login_required",
                "error": "Сначала войди через Telegram в разделе «Аккаунт».",
                "setupUrl": "/account#telegram-setup",
            },
            status_code=401,
        )
    try:
        body = await request.json()
        if isinstance(body, dict) and "panel" in body:
            panel = bool(body.get("panel"))
    except Exception:  # noqa: BLE001
        pass
    autopilot.set_mode("telegram")
    _reschedule_autopilot_scan()
    # Сброса «предложено» здесь больше нет: он заставлял каждую ручную отправку
    # слать те же вакансии заново (гейт F27 «предложено — навсегда»).
    result = _tg_offer_tick(
        include_existing=True,
        ignore_schedule=True,
        limit=30 if panel else None,
        panel=panel,
    )
    if panel:
        _sync_jobs_to_cloud(force=True)
    stats = autopilot.tg_queue_stats()
    if result.get("sent"):
        return {"ok": True, "sent": result["sent"], "remaining": stats["eligible_current"], "panel": panel}
    return {
        "ok": False,
        "sent": 0,
        "remaining": stats["eligible_current"],
        "panel": panel,
        "error": result.get("error") or "Нечего отправлять: текущие уже предложены, пропущены или поданы.",
    }


@app.post("/settings/profile/autosave")
async def settings_profile_autosave(request: Request):
    form = await request.form()
    profile = profile_store.load_profile()
    for form_key, profile_key in [
        ("first_name", "first_name"), ("last_name", "last_name"), ("email", "email"),
        ("phone", "phone"), ("address", "address"), ("zipcode", "zip"),
        ("city", "city"), ("country", "country"), ("linkedin", "linkedin"),
        ("cv_path", "cv_path"), ("cover_letter_path", "cover_letter_path"),
        # необязательные поля для ИИ-дозаполнения (бета)
        ("work_authorization", "work_authorization"), ("languages", "languages"),
        ("experience_years", "experience_years"), ("current_role", "current_role"),
        ("education", "education"), ("available_from", "available_from"),
        ("date_of_birth", "date_of_birth"), ("about", "about"),
    ]:
        if form_key in form:
            profile[profile_key] = str(form.get(form_key) or "").strip()
    # ответы для анкет магазинов сохраняем через нормализацию (да/нет/пусто)
    for key in profile_store.ANSWER_KEYS:
        if key in form:
            profile[key] = profile_store.clean_answer(key, form.get(key))
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


@app.get("/settings/document-rule/{rule_id}/{kind}")
def settings_document_rule_file(rule_id: str, kind: str):
    rule = document_rules.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Комплект документов не найден")
    path = {
        "cv": rule.get("cv_path", ""),
        "cover": rule.get("cover_letter_path", ""),
    }.get(kind)
    if not path or profile_store.file_status(path) != "ok":
        raise HTTPException(status_code=404, detail="Файл не найден")
    clean_path = profile_store.validate_document_path(path)
    filename = profile_store.file_label(clean_path)
    is_pdf = filename.lower().endswith(".pdf")
    return FileResponse(
        clean_path,
        media_type="application/pdf" if is_pdf else "application/octet-stream",
        filename=filename,
        content_disposition_type="inline" if is_pdf else "attachment",
    )


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
    selected_documents = {}
    if job:
        resolved_profile = document_rules.resolve_profile(
            profile_store.load_profile(),
            job,
        )
        selection = resolved_profile.get("_document_selection", {})
        selected_documents = {
            "cv_label": profile_store.file_label(resolved_profile.get("cv_path", "")),
            "cv_status": profile_store.file_status(resolved_profile.get("cv_path", "")),
            "cv_source": (selection.get("cv") or {}).get("label", "Общий комплект"),
            "cover_label": profile_store.file_label(
                resolved_profile.get("cover_letter_path", "")
            ),
            "cover_status": profile_store.file_status(
                resolved_profile.get("cover_letter_path", "")
            ),
            "cover_source": (
                selection.get("cover") or {}
            ).get("label", "Общий комплект"),
        }
        required_profile = {
            "Имя": resolved_profile.get("first_name"),
            "Фамилия": resolved_profile.get("last_name"),
            "Email": resolved_profile.get("email"),
            "Телефон": resolved_profile.get("phone"),
            "Адрес": resolved_profile.get("address"),
            "Индекс": resolved_profile.get("zip"),
            "Город": resolved_profile.get("city"),
            "Страна": resolved_profile.get("country"),
            "CV": (
                resolved_profile.get("cv_path")
                if selected_documents["cv_status"] == "ok" else ""
            ),
            "Письмо": (
                resolved_profile.get("cover_letter_path")
                if selected_documents["cover_status"] == "ok" else ""
            ),
        }
        selected_documents["missing_profile"] = [
            label for label, value in required_profile.items()
            if not str(value or "").strip()
        ]
    return templates.TemplateResponse(
        "detail.html", {
            "request": request,
            "job": job,
            "source_labels": JOB_SOURCE_LABELS,
            "application_state": (
                applications.state_of(job.id, source=job.source)
                if job and job.source != "salling" else ""
            ),
            "selected_documents": selected_documents,
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
    if job and getattr(job, "source", "salling") != "salling":
        return RedirectResponse(
            _url_with_system_response(
                f"/job/{job_id}",
                notice="Для этой компании доступна безопасная подготовка формы с остановкой перед отправкой.",
            ),
            status_code=303,
        )
    profile = document_rules.resolve_profile(profile_store.load_profile(), job) if job else profile_store.load_profile()
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
            "document_selection": profile.get("_document_selection", {}),
            "distance": distance,
            "maps_url": _maps_url(job, home) if job else "",
            "started": started,
            "saved": saved,
            "reset": reset,
            "creds": credentials_store.status(),
        },
    )


@app.post("/credentials/save")
def save_credentials(request: Request, job_id: str = Form(""), sf_email: str = Form(""), sf_password: str = Form("")):
    credentials_store.save(sf_email, sf_password)
    if job_id:
        target = f"/job/{job_id}/apply?saved=login"
    else:
        ref = request.headers.get("referer") or ""
        target = _url_with_system_response(
            ref if "/settings" in ref else "/settings/salling",
            notice="Логин Salling сохранён.",
        )
    return RedirectResponse(target, status_code=303)


@app.post("/apply/reset-browser")
def reset_browser(request: Request, job_id: str = Form("")):
    """Удаляет сохранённую сессию подачи — чтобы выйти из чужого/старого
    аккаунта Salling и войти заново под сохранёнными email/паролем."""
    import shutil
    shutil.rmtree(config.BROWSER_PROFILE_DIR, ignore_errors=True)
    ref = request.headers.get("referer")
    if ref and "/settings" in ref:
        return RedirectResponse("/settings/salling?saved=1", status_code=303)
    target = f"/job/{job_id}/apply?reset=1" if job_id else "/"
    return RedirectResponse(target, status_code=303)


@app.post("/credentials/clear")
def clear_credentials(request: Request):
    credentials_store.clear()
    return _redirect_back(request, "/settings/salling", notice="Логин Salling очищен.")


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


def _partition_submit_ids(picked):
    """Делит снимок [(id, Job|None)] на (safe, applied, leadership) по нерушимым
    правилам авто/пакетной подачи: уже поданные (applied_at или status=applied) и
    руководящие в такую подачу НЕ идут; дубли id отсеиваются. Чистая функция над
    переданным снимком — без обращения к БД, поэтому её легко покрыть тестом.
    Поданные проверяем раньше руководящих: вакансия, что и подана, и руководящая,
    считается поданной. Возвращает (safe: list[str], applied/leadership: list[(id,Job)])."""
    safe, applied, leadership = [], [], []
    seen = set()
    for jid, j in picked:
        jid = str(jid or "").strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        if j is None:
            continue
        if j.status == "applied" or j.applied_at is not None:
            applied.append((jid, j))
            continue
        if labels.is_leadership(j.title or ""):
            leadership.append((jid, j))
            continue
        safe.append(jid)
    return safe, applied, leadership


def _load_jobs_snapshot(ids):
    """Снимок [(id, Job|None)] для переданных id (один заход в БД)."""
    with get_session() as s:
        return [(jid, s.get(Job, jid)) for jid in ids]


def _run_apply_worker(
    ids,
    submit: bool = False,
    auto_close: bool = False,
    ai_fill: bool | None = None,
    phone_confirm: bool = False,
):
    """ЕДИНСТВЕННОЕ место, запускающее воркер подачи apply.py (общая «воротина»).
    Возвращает Popen или None. Здесь действует последний барьер источника;
    остальные правила применяет вызывающий: авто/пакет проходят через
    _partition_submit_ids, одиночная подача передаёт ровно один id."""
    ids = [str(j).strip() for j in ids if str(j or "").strip()]
    # Last-resort source barrier: no caller (including future code) can feed an
    # ATS connector job into the Salling-specific browser worker.
    snapshot = _load_jobs_snapshot(ids)
    blocked = [jid for jid, job in snapshot
               if job is not None and getattr(job, "source", "salling") != "salling"]
    if blocked:
        print(f"  blocked non-Salling ids in Salling worker: {len(blocked)}")
        ids = [jid for jid in ids if jid not in set(blocked)]
    if not ids:
        return None
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if ai_fill is not None:
        env["WEXFLOW_AI_FILL"] = "1" if ai_fill else "0"
    # Ключи ИИ НИКОГДА не передаются воркеру: ни аргументом, ни в окружении.
    # Воркер получает только идентификатор аккаунта и сам достаёт ключ из
    # account-specific защищённого хранилища (DPAPI).
    for secret_var in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        env.pop(secret_var, None)
    env["WEXFLOW_AI_ACCOUNT"] = ai_secrets.current_account_id()
    cmd = _salling_apply_cmd(ids + ["--web"])
    if submit:
        cmd.append("--submit")
    if submit and auto_close:
        cmd.append("--auto-close")
    if ai_fill:
        cmd.append("--ai-fill")
    if phone_confirm:
        # прогон с телефона: воркер дождётся кнопки «Отправить»/«Отмена» из чата
        cmd.append("--phone-confirm")
    log = open(config.DATA_DIR / "apply_last.log", "w", encoding="utf-8")
    global _last_apply_proc, _last_apply_spawn_ts
    try:
        _last_apply_spawn_ts = time.time()
        _last_apply_proc = subprocess.Popen(
            cmd, cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return _last_apply_proc
    except Exception as e:
        # F38: если воркер подачи не удалось ДАЖЕ запустить (нет exe/прав, заблокировал
        # антивирус) — оставляем причину в журнале подачи. Иначе apply_last.log остался
        # бы пустым и сбой самого старта был бы не виден ни пользователю, ни поддержке.
        try:
            log.write(f"⚠ не удалось запустить воркер подачи: {e}\n")
            log.flush()
        except Exception:
            pass
        raise
    finally:
        log.close()  # потомок унаследовал свой хэндл; родительский больше не нужен


def _spawn_salling_apply(ids: list[str], submit: bool = False, auto_close: bool = False,
                         phone_confirm: bool = False):
    """Запуск apply.py для авто/фоновой подачи (очередь автопилота/Mini App).
    Для submit=True применяет страховочный отсев (_partition_submit_ids): руководящие
    и уже поданные не уйдут. Сам запуск — через единый воркер _run_apply_worker."""
    if submit and ids:
        safe, applied, leadership = _partition_submit_ids(_load_jobs_snapshot(ids))
        for _jid, j in applied:
            print(f"  ⛔ пропуск (уже подано): {j.title}")
        for _jid, j in leadership:
            print(f"  ⛔ пропуск (руководящая, авто-подача запрещена): {j.title}")
        if not safe:
            print("  нечего подавать после страховочного отсева — процесс не запускаю")
            return None
        ids = safe
    return _run_apply_worker(ids, submit=submit, auto_close=auto_close,
                             phone_confirm=phone_confirm)


def _launch_salling_apply(ids: list[str], submit: bool = False, track_autopilot: bool = False,
                          phone_confirm: bool = False) -> None:
    """Запустить подачу Salling по списку id.
    submit=False — режим подготовки: WexFlow заполняет и останавливается перед отправкой.
    submit + track_autopilot — фоновая подача из Mini App/автопилота: идёт через
    ОБЩУЮ очередь (_apply_runner_loop), строго по одной пачке за раз, чтобы два
    процесса не дрались за один профиль браузера. Из-за этой драки раньше
    подавалась только одна вакансия, а остальные «зависали в процессе»."""
    if submit and track_autopilot:
        _enqueue_auto_submit(list(ids))
        return
    _spawn_salling_apply(ids, submit=submit, auto_close=False, phone_confirm=phone_confirm)


@app.post("/job/{job_id}/apply/start")
def start_apply(
    job_id: str,
    mode: str = Form("submit"),
    cv_path: str = Form(""),
    cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
    resubmit_ack: str = Form(""),
):
    with get_session() as s:
        job = s.get(Job, job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    if getattr(job, "source", "salling") != "salling":
        return RedirectResponse(
            _url_with_system_response(
                f"/job/{job_id}",
                error="Для этой вакансии автоматическая отправка отключена. Используй кнопку «Подать» — WexFlow заполнит анкету и остановится перед отправкой.",
            ),
            status_code=303,
        )
    # Защита от повторной подачи: реальную отправку на уже поданную вакансию
    # (applied_at заполнен или статус "applied") выполняем ТОЛЬКО при осознанном
    # подтверждении (resubmit_ack из диалога на странице). Иначе — назад с
    # предупреждением: повтор тому же работодателю по случайному клику исключён.
    # На подготовку без отправки (mode != "submit") это не влияет.
    if mode == "submit" and (job.status == "applied" or job.applied_at is not None) and not resubmit_ack:
        return RedirectResponse(
            _url_with_system_response(
                f"/job/{job_id}/apply",
                error="На эту вакансию уже подавали — повторная заявка не отправлена. Если правда нужно подать ещё раз, нажми «Запустить подачу» и подтверди.",
            ),
            status_code=303,
        )
    profile = profile_store.load_profile()
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(
            _url_with_system_response(f"/job/{job_id}/apply", error=file_error),
            status_code=303,
        )
    profile_store.save_profile(profile)
    # Не запускаем вторую подачу поверх идущей (пачка/автопилот/двойной клик):
    # два браузера на одном профиле дерутся и часть заявок может уйти повторно.
    if not _claim_apply_slot():
        return RedirectResponse(
            _url_with_system_response(
                f"/job/{job_id}/apply",
                error="Подача уже идёт — дождись её окончания и нажми снова.",
            ),
            status_code=303,
        )
    # Одиночная подача со страницы вакансии — осознанная: руководящие разрешены,
    # повтор уже отсечён выше (resubmit_ack). Запуск через единый воркер.
    _run_apply_worker([job_id], submit=(mode == "submit"))
    return RedirectResponse(f"/job/{job_id}/apply?started=1", status_code=303)


@app.post("/apply/batch")
def apply_batch(
    request: Request,
    job_ids: list[str] = Form(default=[]),
    mode: str = Form("dry"),
    ai_fill: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
):
    ids = [j for j in job_ids if j]
    use_ai = ai_fill in {"1", "true", "on", "yes"}
    if not ids:
        return _redirect_back(request, "/", error="Сначала выбери хотя бы одну вакансию для пакетной подачи.")
    if use_ai and not ai_gateway.available():
        return _redirect_back(
            request,
            "/",
            error="ИИ-заполнение не запущено: сначала подключи ИИ в «Настройки → ИИ и лимиты».",
        )
    # Коннекторы работают только assisted: пакетный Salling worker не должен
    # получить их id даже через вручную подделанную форму.
    snapshots = _load_jobs_snapshot(ids)
    ids = [jid for jid, job in snapshots if getattr(job, "source", "salling") == "salling"]
    if not ids:
        return _redirect_back(
            request, "/",
            error="Вакансии других компаний заполняются по одной с остановкой перед отправкой.",
        )
    # Страховка: руководящие и уже поданные пачкой не подаём (единый отсев).
    # Если человек правда хочет руководящую — подаёт её осознанно с её страницы.
    safe, already, leadership = _partition_submit_ids(_load_jobs_snapshot(ids))
    if not safe:
        parts = []
        if already:
            parts.append(f"уже поданы ранее: {len(already)}")
        if leadership:
            names = ", ".join((j.title or "?") for _, j in leadership[:4])
            more = f" и ещё {len(leadership) - 4}" if len(leadership) > 4 else ""
            parts.append(f"руководящие (пачкой не подаём): {names}{more}")
        reason = "; ".join(parts) if parts else "нечего подавать"
        return _redirect_back(
            request, "/",
            error=f"Подавать нечего — {reason}. Повторно на одну и ту же вакансию заявка не уходит.",
        )
    # Документы можно заменить прямо в пакетной панели. Сохраняем их до
    # запуска браузерного воркера, чтобы вся пачка использовала один и тот же
    # проверенный набор файлов.
    if (
        (cv_file and cv_file.filename)
        or (cover_letter_file and cover_letter_file.filename)
    ):
        profile = profile_store.load_profile()
        profile, file_error = _profile_files_result(
            profile,
            "",
            "",
            cv_file,
            cover_letter_file,
        )
        if file_error:
            return _redirect_back(request, "/", error=file_error)
        profile_store.save_profile(profile)
    # Гонка/двойной клик: если подача уже идёт (другая пачка или автопилот) —
    # второй процесс не запускаем, чтобы два браузера не дрались за профиль.
    if not _claim_apply_slot():
        return _redirect_back(
            request, "/",
            error="Подача уже идёт — дождись её окончания. Второй раз не запускаю, чтобы не ушли дубли.",
        )
    _run_apply_worker(safe, submit=(mode == "submit"), ai_fill=use_ai)
    url = f"/?batch={len(safe)}&mode={mode}"
    if leadership:
        url += f"&skipped={len(leadership)}"
    if already:
        url += f"&dup={len(already)}"
    return RedirectResponse(url, status_code=303)


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
