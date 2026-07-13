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
import local_guard
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
from db import Application, Job, init_db, get_session, select, utcnow
import scraper
import connector_sync
import applications
import autopilot
import ai_filters
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
}
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
        account_mod.apply_session(user)


def _report_apply_result_safe(job_id: str, state: str, msg: str = "") -> None:
    try:
        cloud_auth.report_apply_result(job_id, state, msg)
    except Exception:  # noqa: BLE001
        pass


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


def _claim_apply_slot() -> bool:
    """Занять «слот» ручной подачи. False — если подача уже идёт или была запущена
    только что (двойной клик). True — слот занят, можно запускать."""
    global _last_manual_apply_ts
    with _manual_apply_lock:
        if _submit_in_progress() or (time.time() - _last_manual_apply_ts) < 12:
            return False
        _last_manual_apply_ts = time.time()
        return True


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
        """Разнести итоги воркера: ok → в реестр поданных, failed → в failed."""
        ok_ids = [jid for jid in list(pending) if states.get(jid) == "ok"]
        failed_ids = [jid for jid in list(pending) if states.get(jid) == "failed"]
        if ok_ids:
            pending.difference_update(ok_ids)
            try:
                with get_session() as s:
                    jobs = [s.get(Job, jid) for jid in ok_ids]
                autopilot.record_submitted([j for j in jobs if j is not None])
            except Exception:  # noqa: BLE001 — реестр не должен ронять разбор итогов
                pass
            for jid in ok_ids:
                _report_apply_result_safe(jid, "submitted", "Заявка отправлена")
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
                    _report_apply_result_safe(jid, "submitted", "Заявка отправлена")
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
    if state == "submitting":
        return "Подача уже запущена"
    return {
        "missing": "Вакансия больше не доступна",
        "stale": "Карточка больше не подходит под текущие фильтры",
        "launch_error": "Не удалось запустить подачу на ПК",
        "not_offered": "Заявка под эту вакансию не предлагалась — подача отклонена",
    }.get(reason or "", "Подача не запущена")


def _handle_tg_decisions(decisions: list) -> None:
    submit_ids = []
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        jid = d.get("jobId")
        action = d.get("action")
        if not jid or jid == "__demo__" or action not in ("submit", "skip"):
            continue
        if action == "skip":
            autopilot.tg_decide(jid, approve=False, launcher=lambda ids: None)
        else:
            submit_ids.append(jid)

    if not submit_ids:
        return

    result = autopilot.tg_submit_batch(
        submit_ids,
        launcher=lambda ids: _launch_salling_apply(ids, submit=True, track_autopilot=True),
    )
    for item in result.get("skipped") or []:
        jid = item.get("job_id")
        state = item.get("state")
        if jid and state in ("submitting", "submitted", "failed"):
            _report_apply_result_safe(jid, state, _apply_result_msg(state, item.get("reason", "")))


_applied_sync_last = 0.0
_jobs_sync_last = 0.0
_cloud_sync_attempt_last = {"applied": 0.0, "jobs": 0.0, "filters": 0.0}


def _begin_cloud_sync(kind: str, last_success: float, interval: int,
                      force: bool = False) -> float | None:
    """Start a due sync without treating failed attempts as successes."""
    now = time.time()
    if not force and now - last_success < interval:
        return None
    if not force and now - _cloud_sync_attempt_last.get(kind, 0.0) < 15:
        return None
    _cloud_sync_attempt_last[kind] = now
    return now


def _sync_applied_to_cloud(force: bool = False) -> bool:
    """Одно облако: периодически шлём в облако список недавно поданных вакансий —
    чтобы раздел «Поданные» в Mini App был виден (подал на ПК → видно в телефоне),
    а поданные карточки ушли из «ждут решения». Троттлинг 30 сек."""
    global _applied_sync_last
    if not account_mod.is_signed_in():
        return False
    attempt = _begin_cloud_sync("applied", _applied_sync_last, 30, force)
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
            items.append({
                "id": job.id,
                "title": _tg_display_title(job),
                "brand": labels.brand(job.brand) if job.brand else "",
                "city": job.city or "",
                "hours": f"{job.hours} ч/нед" if job.hours else "",
                "url": job.application_link or "",
                "ts": ts,
            })
        if cloud_auth.report_applied(items):
            _applied_sync_last = attempt
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"applied-sync: ошибка — {e}")
    return False


def _sync_jobs_to_cloud(force: bool = False) -> bool:
    """Фаза 2b: телефон видит не только офферы автопилота, а полный текущий
    список подходящих вакансий. Синк троттлим, чтобы не жечь Upstash.
    """
    global _jobs_sync_last
    if not account_mod.is_signed_in():
        return False
    attempt = _begin_cloud_sync("jobs", _jobs_sync_last, 300, force)
    if attempt is None:
        return False
    try:
        skip = applications.submitted_ids() | applications.skipped_ids() | applications.submitting_ids()
        jobs = [j for j in autopilot.find_matches() if j.id not in skip]
        jobs.sort(key=lambda j: getattr(j, "first_seen", None) or utcnow(), reverse=True)
        jobs = jobs[:100]
        payload = [_tg_job_payload(j) for j in jobs]
        if cloud_auth.report_jobs(payload):
            applications.mark_listed([j.id for j in jobs])
            _jobs_sync_last = attempt
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"jobs-sync: ошибка — {e}")
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


def _sync_filters_to_cloud(force: bool = False) -> bool:
    """Панель Mini App показывает и меняет фильтры первого набора. Шлём текущие
    значения + варианты (категории/сети со счётчиками), чтобы панель ничего не
    выдумывала сама. Троттлинг — как у jobs_sync."""
    global _filters_sync_last
    if not account_mod.is_signed_in():
        return False
    attempt = _begin_cloud_sync("filters", _filters_sync_last, 300, force)
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
            "matchCount": autopilot.profile_match_count(prof),
            # живое состояние автопилота — для карточки в панели (управление с телефона)
            "autopilot": {
                "mode": autopilot.get_mode(),
                "found": autopilot.match_count(),
                "submittedToday": autopilot.submitted_today(),
                "submittedTotal": autopilot.submitted_total(),
                "dailyLimit": int(autopilot.get_rule().get("daily_limit") or 0),
                "submitScope": str(autopilot.get_rule().get("submit_scope") or "new"),
            },
        }
        if cloud_auth.report_filters(payload):
            _filters_sync_last = attempt
            return True
    except Exception as e:  # noqa: BLE001 — синк не должен ронять опрос
        print(f"filters-sync: ошибка — {e}")
    return False


def _tg_poll_delay(fail_streak: int, signed_in: bool, had_work: bool = False) -> int:
    """Адаптивный интервал: быстрый ответ после работы, умеренный heartbeat и
    экспоненциальный backoff при проблемах сети."""
    if fail_streak > 0:
        return min(60, 6 * (2 ** min(fail_streak - 1, 4)))
    if had_work:
        return 2
    return 6 if signed_in else 15


def _tg_poller_loop() -> None:
    """Опрашивает облако: какие решения (✅/❌) принял пользователь под карточками,
    и выполняет их локально (подать/пропустить).

    Заменяет старый getUpdates: бот теперь общий и работает через webhook, поэтому
    нажатия кнопок собирает облако, а приложение забирает готовые решения."""
    while not _tg_stop.is_set():
        signed_in = False
        had_work = False
        try:
            _sync_account_from_cloud()
            signed_in = account_mod.is_signed_in()
            # Явный локальный выход означает «не слушать старый Telegram».
            # Войти снова можно только осознанно со страницы аккаунта.
            if not signed_in:
                _tg_poll_state.update({"fail_streak": 0, "last_error": ""})
                _tg_stop.wait(_tg_poll_delay(0, False))
                continue

            tg_id = account_mod.load().get("tg_id") or ""
            cycle = cloud_auth.fetch_poll(tg_id=str(tg_id))
            if cycle is None:
                _tg_poll_state["fail_streak"] = int(_tg_poll_state.get("fail_streak") or 0) + 1
                _tg_poll_state["last_error"] = "Нет связи с облаком Telegram"
            else:
                _tg_poll_state.update({"fail_streak": 0, "last_ok": time.time(), "last_error": ""})
                decisions = cycle.get("decisions") or []
                commands = cycle.get("commands") or []
                had_work = bool(decisions or commands)
                _handle_tg_decisions(decisions)
                _sync_applied_to_cloud()  # одно облако: держим «Поданные» свежими (троттлинг 30с)
                _sync_jobs_to_cloud()     # фаза 2b: список подходящих вакансий в Mini App
                _sync_filters_to_cloud()  # текущие фильтры + варианты для настройки с телефона
                for cmd in commands:
                    if _tg_remote_command_expired(cmd):
                        continue
                    result_text = _handle_tg_remote_command(cmd)
                    cloud_auth.send_command_result(cmd, result_text)
                if cycle.get("ack"):
                    cloud_auth.acknowledge_poll(decisions, commands)
        except Exception as e:  # noqa: BLE001 — слушатель не должен падать
            _tg_poll_state["fail_streak"] = int(_tg_poll_state.get("fail_streak") or 0) + 1
            _tg_poll_state["last_error"] = str(e)[:180]
            print(f"telegram(cloud): ошибка опроса решений — {e}")
        _tg_stop.wait(_tg_poll_delay(
            int(_tg_poll_state.get("fail_streak") or 0), signed_in, had_work))


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


def _tg_job_payload(job) -> dict:
    """Структурные поля для Mini App-панели: фильтры не должны парсить только текст."""
    home = None
    distance = None
    try:
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
    description = _plain_snippet(job.description_ru or job.description)
    return {
        "id": job.id,
        "jobId": job.id,
        "publicId": public_id,
        "shortId": short_id,
        "requisitionId": job.requisition_id or "",
        "title": display_title,
        "displayTitle": display_title,
        "titleBase": title,
        "titleRu": _title_ru(title),
        "summary": summary,
        "subtitle": summary,
        "description": description,
        "descriptionSnippet": description,
        "brand": labels.brand(job.brand) if job.brand else "",
        "city": job.city or "",
        "location": loc,
        "address": address,
        "hours": job.hours or "",
        "hoursLabel": f"{job.hours} ч/нед" if job.hours else "",
        "published": job.published or "",
        "publishedDate": (job.published or "")[:10],
        "distanceKm": distance,
        "url": job.application_link or "",
        "mapsUrl": _maps_url(job, home) if (address or job.lat is not None) else "",
        "lat": job.lat,
        "lon": job.lon,
    }


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
        jobs = autopilot.tg_eligible(limit=limit or TG_MAX_PER_SCAN, include_existing=include_existing)
        if not jobs:
            return {"sent": 0, "error": ""}
        result = _tg_offer_jobs(jobs, panel=panel)
        result["remaining"] = len(autopilot.tg_eligible(10000, include_existing=include_existing))
        return result
    except Exception as e:  # noqa: BLE001 — не должно ронять фоновый скан
        print(f"telegram(cloud): ошибка отправки карточек — {e}")
        return {"sent": 0, "error": str(e)[:120]}


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
            if is_panel:
                autopilot.reset_tg_queue_for_filters()
            limit = 30 if is_panel else None
            result = _tg_offer_tick(include_existing=True, ignore_schedule=True, limit=limit, panel=is_panel)
            if is_panel:
                _sync_jobs_to_cloud(force=True)
            stats = autopilot.tg_queue_stats()
            if result.get("sent"):
                label = "Добавил в панель" if is_panel else "Отправил карточек"
                return (
                    f"📨 {label}: {result['sent']}.\n"
                    f"Осталось доступных текущих: {stats.get('eligible_current', 0)}."
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
                     cloud_fail_streak: int = 0, connector_errors=None) -> list:
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
                    "Проверьте почту, подались ли заявки, и напишите в поддержку @wexwxeee.",
        })
    if cloud_fail_streak >= 3:
        warns.append({
            "id": "telegram-cloud-down",
            "text": "Нет устойчивой связи с Telegram: команды с телефона временно "
                    "не доходят до ПК. WexFlow продолжит попытки автоматически. "
                    "Проверьте интернет; локальный поиск и ручная подача работают.",
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
    return {"warnings": _health_warnings(
        _sync_state.get("last_hits"), bool(_sync_state.get("sync_failed")), streak,
        int(_tg_poll_state.get("fail_streak") or 0),
        _sync_state.get("connector_errors") or [])}


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
    if job.modified:
        facts.append({"label": "Обновлено", "value": job.modified[:10], "kind": "muted"})
    if job.requisition_id:
        facts.append({"label": "ID вакансии", "value": job.requisition_id, "kind": "muted"})
    if job.first_seen:
        facts.append({"label": "Найдено WexFlow", "value": job.first_seen.strftime("%Y-%m-%d"), "kind": "muted"})
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
def apply_by_link(request: Request):
    with get_session() as session:
        rows = session.exec(select(Job).where(
            Job.source != "salling",
            Job.status.not_in(["closed", "hidden"]),
        )).all()
    counts = Counter(job.source for job in rows)
    sources = [
        {"key": key, "label": JOB_SOURCE_LABELS[key],
         "count": counts.get(key, 0), "href": f"/?source={key}"}
        for key in ("teamtailor", "greenhouse", "ashby")
    ]
    return templates.TemplateResponse("apply_by_link.html", {
        "request": request, "sources": sources,
        "total": sum(item["count"] for item in sources),
    })


def _launch_connector_filler(url: str) -> None:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("У вакансии нет безопасной ссылки на форму")
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--worker-connector-apply", url]
    else:
        cmd = [sys.executable, "-m", "connectors.apply_dispatch", url, "--keep-open"]
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    subprocess.Popen(cmd, **kwargs)


@app.post("/apply-by-link/start")
def start_apply_by_link(request: Request, url: str = Form(...)):
    value = str(url or "").strip()
    try:
        _launch_connector_filler(value)
    except Exception as exc:  # noqa: BLE001
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
    return _redirect_back(
        request, "/apply-by-link",
        notice=f"Открываю {platform}. Проверь заполненные поля и отправь анкету сам.",
    )


@app.post("/job/{job_id}/connector/apply")
def start_connector_apply(job_id: str, request: Request):
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
    applications.mark_submitting([job.id], origin="assisted", source=job.source)
    try:
        _launch_connector_filler(job.application_link or "")
    except Exception as exc:  # noqa: BLE001
        applications.mark_failed([job.id], source=job.source)
        return _redirect_back(
            request, f"/job/{job_id}",
            error=f"Не удалось открыть форму: {str(exc)[:140]}",
        )
    return _redirect_back(
        request, f"/job/{job_id}",
        notice="Форма открывается в отдельном окне. WexFlow заполнит доступные поля и остановится перед отправкой.",
    )


@app.post("/job/{job_id}/connector/result")
def connector_apply_result(job_id: str, request: Request, outcome: str = Form(...)):
    if outcome not in {"submitted", "incomplete"}:
        return _redirect_back(request, f"/job/{job_id}", error="Неизвестный результат анкеты.")
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
    if outcome == "submitted":
        applications.record_submitted([job])
        return _redirect_back(
            request, f"/job/{job_id}",
            notice="Отмечено как поданное вручную. Запись добавлена в журнал.",
        )
    applications.mark_failed([job_id], source=source)
    return _redirect_back(
        request, f"/job/{job_id}",
        notice="Сохранил как незавершённую анкету — к ней можно вернуться позже.",
    )


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
    page: str = "1",
    geoerror: str = "",
    batch: str = "",
    mode: str = "",
    skipped: str = "",
    dup: str = "",
    reset: str = "",
):
    # запоминаем фильтры в cookie и восстанавливаем при заходе на голую "/"
    _fkeys = ["q", "source", "city", "brand", "region", "employment_type", "category", "job_level", "status", "sort", "show_applied"]
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
            except Exception:
                pass

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
        "f": (_f := {"q": q, "source": source_key,
              "city": city,
              "brand": brand_code,
              "region": region_code,
              "category": category_code,
              "employment_type": employment_code,
              "job_level": level_code,
              "status": status, "sort": sort, "radius": radius, "group": group,
              "show_applied": show_applied}),
        "total_active": total_active, "applied_count": applied_count, "last_update": last,
        "autopilot": _ap_rule,
        "autopilot_count": _ap_count,
        "data_age_min": (max(0, int((utcnow() - last).total_seconds() // 60)) if last else None),
        "sync_running": _sync_state["running"],
        "sync_error": _sync_state["last_error"],
        "home": home, "distances": distances, "geoerror": geoerror,
        "presets": settings_store.get_presets(),
        "batch": batch, "batch_mode": mode, "skipped": skipped, "dup": dup,
        "apply_files": {
            "cv": profile_store.file_label(_profile.get("cv_path", "")),
            "cover": profile_store.file_label(_profile.get("cover_letter_path", "")),
        },
        "setup": _setup,
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
            "error": "ИИ не подключён. Добавь бесплатный ключ Gemini в файл secrets.json "
                     "(ключ gemini_api_key) — см. AI Studio.",
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
                 deleted: str = "", delete_error: str = ""):
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
        "deleted": deleted, "delete_error": delete_error,
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
    titles = {
        "salling": ("Salling", "Логин, документы, домашний адрес и сброс входа"),
        "autopilot": ("Автопилот", "Наборы фильтров, режим работы и автоотправка"),
        "telegram": ("Telegram", "Статус @wexflowbot, проверка и ручная отправка текущих"),
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
        "settings_section": section,
        "settings_title": settings_title,
        "settings_meta": settings_meta,
        "autopilot": ap_view, "brands": labels.BRANDS,
        "categories": labels.CATEGORY, "employments": labels.EMPLOYMENT,
        "autopilot_cities": ap_cities, "autopilot_regions": ap_regions,
        "autopilot_region_labels": labels.REGION,
        "autopilot_cat_options": ap_cat_options,
        "autopilot_brand_options": ap_brand_options,
        "autopilot_mode": autopilot.get_mode(),
        "home_city": _home_city(settings_store.get_home()),
        "ai_available": ai_filters.available(),
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


@app.get("/settings/autopilot", response_class=HTMLResponse)
def settings_autopilot(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "autopilot", saved=saved, geoerror=geoerror, missing=missing)


@app.get("/settings/telegram", response_class=HTMLResponse)
def settings_telegram(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    return _render_settings_section(request, "telegram", saved=saved, geoerror=geoerror, missing=missing)


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
):
    """Документы для Salling (CV/письмо). WexFlow использует их в анкетах Salling."""
    profile = profile_store.load_profile()
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(_url_with_system_response("/settings/salling", error=file_error), status_code=303)
    profile_store.save_profile(profile)
    return RedirectResponse("/settings/salling?saved=1#documents", status_code=303)


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
    autopilot.save_profile_filters(profile_id, {
        "max_km": _num_csv(max_km),
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
async def telegram_send_current(request: Request, panel: bool = False):
    """Ручная отправка текущих подходящих вакансий в Telegram.
    Нужна для понятного сценария: счётчик «подходит» уже есть, но безопасный
    режим автоматически шлёт только новые после включения."""
    if not account_mod.is_signed_in():
        return {"ok": False, "error": "Сначала войди через Telegram (раздел «Аккаунт»)."}
    try:
        body = await request.json()
        if isinstance(body, dict) and "panel" in body:
            panel = bool(body.get("panel"))
    except Exception:  # noqa: BLE001
        pass
    autopilot.set_mode("telegram")
    _reschedule_autopilot_scan()
    if panel:
        autopilot.reset_tg_queue_for_filters()
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
            "source_labels": JOB_SOURCE_LABELS,
            "application_state": (
                applications.state_of(job.id, source=job.source)
                if job and job.source != "salling" else ""
            ),
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


def _run_apply_worker(ids, submit: bool = False, auto_close: bool = False):
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
    cmd = _salling_apply_cmd(ids + ["--web"])
    if submit:
        cmd.append("--submit")
    if submit and auto_close:
        cmd.append("--auto-close")
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


def _spawn_salling_apply(ids: list[str], submit: bool = False, auto_close: bool = False):
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
    return _run_apply_worker(ids, submit=submit, auto_close=auto_close)


def _launch_salling_apply(ids: list[str], submit: bool = False, track_autopilot: bool = False) -> None:
    """Запустить подачу Salling по списку id.
    submit=False — режим подготовки: WexFlow заполняет и останавливается перед отправкой.
    submit + track_autopilot — фоновая подача из Mini App/автопилота: идёт через
    ОБЩУЮ очередь (_apply_runner_loop), строго по одной пачке за раз, чтобы два
    процесса не дрались за один профиль браузера. Из-за этой драки раньше
    подавалась только одна вакансия, а остальные «зависали в процессе»."""
    if submit and track_autopilot:
        _enqueue_auto_submit(list(ids))
        return
    _spawn_salling_apply(ids, submit=submit, auto_close=False)


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
                error="Для этой вакансии автоматическая отправка отключена. Используй «Заполнить форму» — WexFlow остановится перед отправкой.",
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
def apply_batch(request: Request, job_ids: list[str] = Form(default=[]), mode: str = Form("dry")):
    ids = [j for j in job_ids if j]
    if not ids:
        return _redirect_back(request, "/", error="Сначала выбери хотя бы одну вакансию для пакетной подачи.")
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
    # Гонка/двойной клик: если подача уже идёт (другая пачка или автопилот) —
    # второй процесс не запускаем, чтобы два браузера не дрались за профиль.
    if not _claim_apply_slot():
        return _redirect_back(
            request, "/",
            error="Подача уже идёт — дождись её окончания. Второй раз не запускаю, чтобы не ушли дубли.",
        )
    _run_apply_worker(safe, submit=(mode == "submit"))
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
