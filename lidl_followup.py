"""Post-application guidance and opt-in Telegram reminders for Lidl.

This module intentionally does not read email or log into the candidate portal.
It stores only checklist/reminder state; no Lidl password or message contents.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from urllib.parse import quote

import config
from json_store import atomic_write_json, read_json


PATH = config.DATA_DIR / "lidl_followups.json"
PORTAL_URL = (
    "https://career5.successfactors.eu/career?"
    "brandUrl=dk&career_ns=job_application&company=lidlstiftuP2"
    "&rcm_site_locale=da_DK"
)
RECRUITMENT_URL = "https://karriere.lidl.dk/lidl-som-arbejdsplads/rekruttering"
FAQ_URL = "https://karriere.lidl.dk/lidl-som-arbejdsplads/faq"
GMAIL_SEARCH_URL = (
    "https://mail.google.com/mail/u/0/#search/"
    + quote('"Lidl Recruiting" OR from:(lidl)', safe="")
)

_LOCK = threading.RLock()
_CHECK_KEYS = {
    "profile_email",
    "password_set",
    "profile_reviewed",
    "status_checked",
}
_REMINDERS = (
    (
        "setup",
        timedelta(0),
        "📬 <b>Lidl: заверши кандидатский профиль</b>\n"
        "Проверь письма от Lidl Recruiting. После подачи Lidl создаёт профиль: "
        "через «Glemt adgangskode?» установи пароль, затем открой кабинет и проверь данные.",
    ),
    (
        "day3",
        timedelta(days=3),
        "🔎 <b>Lidl: проверь следующий этап</b>\n"
        "Открой кандидатский кабинет → «Søgte jobs». Проверь также входящие и спам: "
        "для некоторых вакансий Lidl присылает онлайн‑тест.",
    ),
    (
        "week1",
        timedelta(days=7),
        "🗓 <b>Lidl: неделя после подачи</b>\n"
        "Проверь статус в «Søgte jobs», почту и пропущенные звонки. Следующими этапами "
        "могут быть screening, телефонный разговор и личное собеседование.",
    ),
    (
        "week2",
        timedelta(days=14),
        "⏳ <b>Lidl: две недели после подачи</b>\n"
        "Если новостей пока нет, это ещё не означает отказ. Lidl пишет, что полный "
        "процесс обычно стараются завершить не более чем за 6 недель.",
    ),
)


def _all() -> dict:
    value = read_json(PATH, {"version": 1, "applications": {}}, dict)
    if not isinstance(value.get("applications"), dict):
        value["applications"] = {}
    value["version"] = 1
    return value


def _key(job_id: str) -> str:
    return "lidl:" + str(job_id or "").strip()


def _entry(data: dict, job_id: str) -> dict:
    applications = data.setdefault("applications", {})
    entry = applications.setdefault(_key(job_id), {})
    if not isinstance(entry.get("checks"), dict):
        entry["checks"] = {}
    if not isinstance(entry.get("sent"), list):
        entry["sent"] = []
    return entry


def view(job, email: str = "") -> dict:
    """UI payload for one submitted Lidl application."""
    with _LOCK:
        entry = _entry(_all(), job.id)
    checks = entry.get("checks", {})
    confidence = str(getattr(job, "applied_confidence", "") or "").strip().lower()
    submission_facts = {
        "receipt": {
            "title": "Сайт показал квитанцию о подаче",
            "text": (
                "Lidl показал экран подтверждения. Зарегистрированный снимок, если он "
                "сохранился без изменений, доступен в «Моих откликах»."
            ),
            "done": True,
        },
        "portal": {
            "title": "Заявка найдена в кабинете Lidl",
            "text": (
                "Официальный раздел «Søgte jobs» подтвердил, что заявка связана "
                "с кандидатским профилем."
            ),
            "done": True,
        },
        "indirect": {
            "title": "Подачу нужно подтвердить",
            "text": (
                "Форма завершилась без видимой квитанции. Проверь «Søgte jobs» и "
                "письмо работодателя; WexFlow не выдаёт эту попытку за доказанную подачу."
            ),
            "done": False,
        },
        "manual": {
            "title": "Подача отмечена вручную",
            "text": (
                "Это запись пользователя, а не квитанция сайта. Проверь заявку в "
                "«Søgte jobs» или добавь исходное письмо работодателя."
            ),
            "done": False,
        },
    }.get(confidence, {
        "title": "Подтверждение подачи не найдено",
        "text": (
            "В истории есть дата подачи, но WexFlow не знает её источник. Проверь "
            "кандидатский кабинет или добавь исходное письмо работодателя."
        ),
        "done": False,
    })
    steps = [
        {
            "key": "submission_evidence",
            "title": submission_facts["title"],
            "text": submission_facts["text"],
            "done": submission_facts["done"],
            "automatic": True,
        },
        {
            "key": "profile_email",
            "title": "Найди письмо о кандидатском профиле",
            "text": (
                "Lidl создаёт профиль автоматически. Логин — email из анкеты"
                + (f": {email}" if email else ".")
                + " Если письма не видно, проверь «Спам»."
            ),
            "done": bool(checks.get("profile_email")),
            "automatic": False,
        },
        {
            "key": "password_set",
            "title": "Установи пароль",
            "text": (
                "На странице входа нажми «Glemt adgangskode?» (забыли пароль), "
                "введи email и открой второе письмо Lidl. По ссылке задай новый пароль. "
                "Если сайт пишет, что аккаунт не найден, выбери «Opret en konto»."
            ),
            "done": bool(checks.get("password_set")),
            "automatic": False,
        },
        {
            "key": "profile_reviewed",
            "title": "Проверь кандидатский профиль",
            "text": (
                "Проверь документы и данные. В кабинете можно менять профиль, "
                "настройки видимости, подтверждать встречи и отзывать заявку."
            ),
            "done": bool(checks.get("profile_reviewed")),
            "automatic": False,
        },
        {
            "key": "status_checked",
            "title": "Открой «Søgte jobs»",
            "text": (
                "Там находится поданная вакансия, текущий статус и дальнейшие шаги. "
                "Для некоторых вакансий отдельно приходит онлайн‑тест."
            ),
            "done": bool(checks.get("status_checked")),
            "automatic": False,
        },
    ]
    return {
        "steps": steps,
        "confidence": confidence,
        "submission_confirmed": bool(submission_facts["done"]),
        "reminders": bool(entry.get("reminders")),
        "portal_url": PORTAL_URL,
        "gmail_url": GMAIL_SEARCH_URL,
        "recruitment_url": RECRUITMENT_URL,
        "faq_url": FAQ_URL,
    }


def set_check(job_id: str, step: str, done: bool) -> bool:
    step = str(step or "").strip()
    if step not in _CHECK_KEYS:
        return False
    with _LOCK:
        data = _all()
        entry = _entry(data, job_id)
        entry["checks"][step] = bool(done)
        atomic_write_json(PATH, data, indent=2)
    return True


def set_reminders(job_id: str, enabled: bool) -> None:
    with _LOCK:
        data = _all()
        entry = _entry(data, job_id)
        entry["reminders"] = bool(enabled)
        if enabled:
            entry.setdefault("enabled_at", datetime.utcnow().isoformat(timespec="seconds"))
        atomic_write_json(PATH, data, indent=2)


def due_reminders(jobs: list, now: datetime | None = None) -> list[dict]:
    """Return unsent reminders for still-pending Lidl applications."""
    now = now or datetime.utcnow()
    with _LOCK:
        data = _all()
    result = []
    for job in jobs:
        entry = _entry(data, job.id)
        stage = (
            getattr(job, "application_stage", "")
            or getattr(job, "status", "")
        )
        if not entry.get("reminders") or stage != "applied":
            continue
        applied_at = getattr(job, "applied_at", None)
        if not applied_at:
            continue
        sent = set(str(item) for item in entry.get("sent", []))
        due = [
            (code, text)
            for code, delay, text in _REMINDERS
            if now >= applied_at + delay
        ]
        if due:
            # Send the most relevant current reminder, never a burst of stale
            # day-0/day-3 messages when monitoring is enabled much later.
            code, text = due[-1]
            if code not in sent:
                title = str(getattr(job, "title", "") or "").strip()
                if title:
                    text += "\n\n" + title
                result.append({"job_id": job.id, "code": code, "text": text})
    return result


def mark_sent(job_id: str, code: str) -> None:
    with _LOCK:
        data = _all()
        entry = _entry(data, job_id)
        sent = entry.setdefault("sent", [])
        if code not in sent:
            sent.append(str(code))
        entry["last_sent_at"] = datetime.utcnow().isoformat(timespec="seconds")
        atomic_write_json(PATH, data, indent=2)
