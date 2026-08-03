"""Application pipeline status tracking shared by every employer."""
from __future__ import annotations

import datetime as dt
import html
from urllib.parse import urlparse

from db import Job, get_session, select, utcnow


NO_RESPONSE_DAYS = 60

STATUS_LABELS = {
    "applied": "Подано",
    "interview": "Собеседование",
    "offer": "Оффер",
    "rejected": "Отказ",
    "no_response": "Нет ответа",
}

STATUS_SOURCES = {
    "submission": "зафиксировано при подаче",
    "manual": "изменено вручную",
    "lidl_portal": "получено из кабинета Lidl",
    "salling_portal": "получено из кабинета Salling",
    "automatic": "определено WexFlow",
    "recovered": "восстановлено из журнала",
}

_SOURCE_NAMES = {
    "lidl": "Lidl",
    "salling": "Salling Group",
}

_STATUS_EMOJI = {
    "applied": "✅",
    "interview": "📅",
    "offer": "🎉",
    "rejected": "📨",
    "no_response": "⏳",
}

CONFIRMATION_LABELS = {
    "portal": ("Подтверждено кабинетом", "official"),
    "receipt": ("Есть квитанция сайта", "strong"),
    "indirect": ("Подача не подтверждена", "warning"),
    "manual": ("Отмечено вручную", "neutral"),
}


def _day_word(value: int) -> str:
    value = abs(int(value))
    if value % 10 == 1 and value % 100 != 11:
        return "день"
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def _advice(status: str, age_days: int) -> dict:
    """Conservative next step: useful guidance without inventing employer intent."""
    if status == "offer":
        return {
            "action_label": "Разобрать оффер",
            "action": "Проверь зарплату, часы, место работы и срок ответа. Сохрани письмо или договор.",
            "action_required": True,
            "urgency": 4,
            "signal": "Нужен твой ответ",
        }
    if status == "interview":
        return {
            "action_label": "Подготовиться",
            "action": "Подтверди время и адрес или ссылку. Подготовь короткий рассказ о себе и примеры опыта.",
            "action_required": True,
            "urgency": 3,
            "signal": "Следующий этап",
        }
    if status == "rejected":
        return {
            "action_label": "Заявка закрыта",
            "action": "Ничего отправлять не нужно. Отказ считается фактом только по кабинету, письму или твоей отметке.",
            "action_required": False,
            "urgency": 0,
            "signal": "Есть решение",
        }
    if status == "no_response":
        return {
            "action_label": "Продолжать поиск",
            "action": "Это долгое молчание, а не отказ. Работодатель ещё может ответить, но ждать только эту вакансию не стоит.",
            "action_required": False,
            "urgency": 1,
            "signal": "Ответа не было",
        }
    if age_days < 7:
        left = 7 - age_days
        return {
            "action_label": "Пока ждём",
            "action": f"Сейчас всё нормально. Проверь почту и спам через {left} {_day_word(left)}.",
            "action_required": False,
            "urgency": 0,
            "signal": "Свежая заявка",
        }
    if age_days < 14:
        return {
            "action_label": "Проверить почту",
            "action": "Проверь входящие, спам и кандидатский кабинет. Молчание на этом сроке ещё не означает отказ.",
            "action_required": False,
            "urgency": 1,
            "signal": "Ждём ответ",
        }
    if age_days < 30:
        return {
            "action_label": "Можно уточнить",
            "action": "Если есть контакт рекрутера, можно один раз вежливо спросить о статусе. Без повторной подачи.",
            "action_required": True,
            "urgency": 2,
            "signal": "Давно без движения",
        }
    return {
        "action_label": "Не зависать на заявке",
        "action": "Ответ заметно задержался. Продолжай новые подачи; эта заявка останется в истории и обновится, если кабинет даст статус.",
        "action_required": True,
        "urgency": 2,
        "signal": "Долгое ожидание",
    }


def _safe_link(value: str) -> str:
    """Only let Telegram render ordinary web links from an employer record."""
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    return raw if parsed.scheme in ("http", "https") and parsed.netloc else ""


def status_notification(change: dict, *, source_name: str = "") -> str:
    """Build one compact, actionable Telegram status card.

    Portal text and vacancy data are escaped because Telegram parses this
    message as HTML.  The wording only states facts read from the official
    candidate portal; it never turns silence into a rejection.
    """
    status = str(change.get("status") or "").strip()
    previous = str(change.get("previous_status") or "").strip()
    source = str(change.get("source") or "").strip()
    company = source_name or _SOURCE_NAMES.get(source, source.title() or "Работодатель")
    title = html.escape(str(change.get("title") or "Вакансия").strip())
    brand = html.escape(str(change.get("brand") or "").strip())
    city = html.escape(str(change.get("city") or "").strip())
    company_safe = html.escape(company)
    current_label = html.escape(
        str(change.get("status_label") or STATUS_LABELS.get(status) or status).strip()
    )
    previous_label = html.escape(
        str(change.get("previous_label") or STATUS_LABELS.get(previous) or previous).strip()
    )

    headings = {
        "applied": f"✅ <b>{company_safe} подтвердил подачу</b>",
        "interview": f"📅 <b>{company_safe}: приглашение на собеседование</b>",
        "offer": f"🎉 <b>{company_safe}: появился оффер</b>",
        "rejected": f"📨 <b>{company_safe} обновил решение</b>",
        "no_response": "⏳ <b>Долгое ожидание ответа</b>",
    }
    lines = [headings.get(status, f"🔔 <b>{company_safe}: новый статус</b>"), "", f"<b>{title}</b>"]
    meta = " · ".join(part for part in (brand, city) if part)
    if meta:
        lines.append(meta)
    if previous_label and previous_label != current_label:
        lines.extend(["", f"{previous_label} → <b>{current_label}</b>"])
    else:
        lines.extend(["", f"Статус: <b>{current_label}</b>"])

    advice = _advice(status, int(change.get("age_days") or 0))
    if status in ("interview", "offer"):
        lines.extend(["", f"💡 <b>{html.escape(advice['action_label'])}</b>", html.escape(advice["action"])])
    elif status == "rejected":
        lines.extend(["", "WexFlow сохранил решение в «Моих откликах». Повторная подача заблокирована."])
    elif status == "applied":
        lines.extend(["", "Заявка найдена в официальном кабинете и сохранена в «Моих откликах»."])

    link = _safe_link(change.get("url") or "")
    if link:
        lines.extend(["", f'<a href="{html.escape(link, quote=True)}">Открыть вакансию</a>'])
    lines.append("Источник: официальный кабинет работодателя")
    return "\n".join(lines)[:2000]


def notify_status_changes(changes: list[dict], *, source_name: str = "") -> bool:
    """Deliver portal changes to Telegram; callers retain failures for retry."""
    items = [item for item in (changes or []) if str(item.get("status") or "") in STATUS_LABELS]
    if not items:
        return True
    import cloud_auth

    if len(items) == 1:
        return bool(cloud_auth.send_digest(status_notification(items[0], source_name=source_name)))

    company = html.escape(source_name or _SOURCE_NAMES.get(
        str(items[0].get("source") or ""), "Работодатель"
    ))
    lines = [f"🔔 <b>{company}: {len(items)} обновления по откликам</b>", ""]
    for item in items[:10]:
        status = str(item.get("status") or "")
        emoji = _STATUS_EMOJI.get(status, "•")
        title = html.escape(str(item.get("title") or "Вакансия").strip())
        label = html.escape(str(item.get("status_label") or STATUS_LABELS.get(status, status)))
        lines.append(f"{emoji} <b>{title}</b> — {label}")
    if len(items) > 10:
        lines.append(f"…и ещё {len(items) - 10}")
    lines.extend(["", "Все изменения сохранены в «Моих откликах». Источник — официальный кабинет работодателя."])
    return bool(cloud_auth.send_digest("\n".join(lines)[:2000]))


def set_status(job: Job, status: str, *, source: str, now=None) -> bool:
    """Set one application stage and retain when/how it was changed."""
    status = str(status or "").strip()
    if status not in STATUS_LABELS:
        return False
    moment = now or utcnow()
    changed = job.status != status
    job.status = status
    if status == "applied" and job.applied_at is None:
        job.applied_at = moment
    if changed or job.application_status_updated_at is None:
        job.application_status_updated_at = moment
        job.application_status_source = str(source or "manual")[:32]
    return True


def view(job: Job, *, now=None) -> dict:
    """Human-readable tracking data for cards and the application journal."""
    moment = now or utcnow()
    applied_at = job.applied_at
    age_days = max(0, (moment - applied_at).days) if applied_at else 0
    due_at = (
        applied_at + dt.timedelta(days=NO_RESPONSE_DAYS)
        if applied_at and job.status == "applied" else None
    )
    updated_at = job.application_status_updated_at or applied_at
    confirmation_label, confirmation_tone = CONFIRMATION_LABELS.get(
        str(job.applied_confidence or ""),
        ("Запись из истории WexFlow", "neutral"),
    )
    advice = _advice(job.status, age_days)
    # Этап «ответ работодателя» не назначается автоматически: для поданной
    # заявки он остаётся ожиданием, пока кабинет или человек не даст факт.
    progress = {
        "applied": 1,
        "interview": 3,
        "offer": 4,
        "rejected": 2,
        "no_response": 1,
    }.get(job.status, 0)
    return {
        "status": job.status,
        "label": STATUS_LABELS.get(job.status, job.status),
        "source": job.application_status_source or "",
        "source_label": STATUS_SOURCES.get(job.application_status_source or "", ""),
        "updated_at": updated_at,
        "age_days": age_days,
        "no_response_due_at": due_at,
        "no_response_days": NO_RESPONSE_DAYS,
        "age_label": f"{age_days} {_day_word(age_days)}",
        "confirmation_label": confirmation_label,
        "confirmation_tone": confirmation_tone,
        "progress": progress,
        "terminal": job.status in ("rejected", "no_response"),
        **advice,
    }


def mark_no_response(*, now=None) -> list[dict]:
    """Mark applications still at ``applied`` after 60 days as no response.

    Interview, offer and rejected stages are never changed automatically.
    Repeated calls are idempotent because only ``applied`` rows qualify.
    """
    moment = now or utcnow()
    cutoff = moment - dt.timedelta(days=NO_RESPONSE_DAYS)
    changed: list[dict] = []
    with get_session() as session:
        jobs = session.exec(select(Job).where(
            Job.applied_at.is_not(None),
            Job.applied_at <= cutoff,
            Job.status == "applied",
        )).all()
        for job in jobs:
            set_status(job, "no_response", source="automatic", now=moment)
            session.add(job)
            changed.append({
                "id": job.id,
                "title": job.title or "Вакансия",
                "brand": job.brand or "",
                "applied_at": job.applied_at,
            })
        if changed:
            session.commit()
    return changed
