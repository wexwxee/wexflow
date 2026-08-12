"""Application pipeline status tracking shared by every employer."""
from __future__ import annotations

import datetime as dt
import html
from urllib.parse import urlparse

from db import ApplicationStatusEvent, Job, get_session, select, utcnow


NO_RESPONSE_DAYS = 60

STATUS_LABELS = {
    "applied": "Подано",
    "reviewing": "На рассмотрении",
    "interview": "Собеседование",
    "offer": "Оффер",
    "hired": "Принят на работу",
    "rejected": "Отказ",
    "withdrawn": "Заявка отозвана",
    "no_response": "Нет ответа",
}
POST_APPLICATION_STATUSES = frozenset(STATUS_LABELS)

STATUS_SOURCES = {
    "submission": "зафиксировано при подаче",
    "manual": "изменено вручную",
    "lidl_portal": "получено из кабинета Lidl",
    "salling_portal": "получено из кабинета Salling",
    "email": "из сохранённого письма (ручное свидетельство)",
    "automatic": "определено WexFlow",
    "recovered": "восстановлено из журнала",
}

_SOURCE_NAMES = {
    "lidl": "Lidl",
    "salling": "Salling Group",
}

_STATUS_EMOJI = {
    "applied": "✅",
    "reviewing": "👀",
    "interview": "📅",
    "offer": "🎉",
    "hired": "🏁",
    "rejected": "📨",
    "withdrawn": "↩️",
    "no_response": "⏳",
}

_POSITIVE_RANK = {
    "applied": 1,
    "reviewing": 2,
    "interview": 3,
    "offer": 4,
    "hired": 5,
}
_TERMINAL = frozenset({"hired", "rejected", "withdrawn"})
_SOURCE_STRENGTH = {
    "recovered": 0,
    "automatic": 0,
    "manual": 1,
    "email": 1,
    "submission": 2,
    "lidl_portal": 3,
    "salling_portal": 3,
}

CONFIRMATION_LABELS = {
    "portal": ("Подтверждено кабинетом", "official"),
    "receipt": ("Есть квитанция сайта", "strong"),
    "email": ("Письмо сохранено без криптопроверки", "neutral"),
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
    if status == "hired":
        return {
            "action_label": "Подтвердить детали выхода",
            "action": "Сверь дату выхода, адрес, часы и договор. Заявка завершена успешно.",
            "action_required": True,
            "urgency": 4,
            "signal": "Ты принят",
        }
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
    if status == "reviewing":
        return {
            "action_label": "Ждать решения",
            "action": "Работодатель рассматривает заявку. Следи за почтой, спамом и кабинетом кандидата.",
            "action_required": False,
            "urgency": 1,
            "signal": "Заявка рассматривается",
        }
    if status == "rejected":
        return {
            "action_label": "Заявка закрыта",
            "action": "Ничего отправлять не нужно. Отказ считается фактом только по кабинету, письму или твоей отметке.",
            "action_required": False,
            "urgency": 0,
            "signal": "Есть решение",
        }
    if status == "withdrawn":
        return {
            "action_label": "Заявка закрыта",
            "action": "Заявка отозвана. Повторно ничего не отправится без нового явного действия.",
            "action_required": False,
            "urgency": 0,
            "signal": "Заявка отозвана",
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
        "reviewing": f"👀 <b>{company_safe}: заявка на рассмотрении</b>",
        "interview": f"📅 <b>{company_safe}: приглашение на собеседование</b>",
        "offer": f"🎉 <b>{company_safe}: появился оффер</b>",
        "hired": f"🏁 <b>{company_safe}: решение о найме</b>",
        "rejected": f"📨 <b>{company_safe} обновил решение</b>",
        "withdrawn": f"↩️ <b>{company_safe}: заявка отозвана</b>",
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
    if status in ("interview", "offer", "hired"):
        lines.extend(["", f"💡 <b>{html.escape(advice['action_label'])}</b>", html.escape(advice["action"])])
    elif status == "rejected":
        lines.extend(["", "WexFlow сохранил решение в «Моих откликах». Повторная подача заблокирована."])
    elif status == "applied":
        lines.extend(["", "Заявка найдена в официальном кабинете и сохранена в «Моих откликах»."])

    link = _safe_link(change.get("url") or "")
    if link:
        lines.extend(["", f'<a href="{html.escape(link, quote=True)}">Открыть вакансию</a>'])
    origin = str(change.get("origin") or change.get("status_source") or "").strip()
    if origin.endswith("_portal"):
        footer = "Источник: официальный кабинет работодателя"
    elif origin == "automatic":
        footer = "Источник: локальное правило WexFlow"
    elif origin == "email":
        footer = "Источник: сохранённое пользователем письмо (.eml)"
    else:
        footer = "Источник: журнал WexFlow"
    lines.append(footer)
    return "\n".join(lines)[:2000]


def notify_status_changes(changes: list[dict], *, source_name: str = "") -> bool:
    """Deliver portal changes to Telegram; callers retain failures for retry."""
    items = [item for item in (changes or []) if str(item.get("status") or "") in STATUS_LABELS]
    if not items:
        return True
    import cloud_auth

    if len(items) == 1:
        return bool(cloud_auth.send_digest(status_notification(items[0], source_name=source_name)))

    sources = {str(item.get("source") or "") for item in items}
    origins = {str(item.get("origin") or item.get("status_source") or "") for item in items}
    if source_name:
        digest_name = source_name
    elif origins == {"automatic"} or len(sources) != 1:
        digest_name = "WexFlow"
    else:
        digest_name = _SOURCE_NAMES.get(next(iter(sources)), "Работодатель")
    company = html.escape(digest_name)
    lines = [f"🔔 <b>{company}: {len(items)} обновления по откликам</b>", ""]
    for item in items[:10]:
        status = str(item.get("status") or "")
        emoji = _STATUS_EMOJI.get(status, "•")
        title = html.escape(str(item.get("title") or "Вакансия").strip())
        label = html.escape(str(item.get("status_label") or STATUS_LABELS.get(status, status)))
        lines.append(f"{emoji} <b>{title}</b> — {label}")
    if len(items) > 10:
        lines.append(f"…и ещё {len(items) - 10}")
    if origins == {"automatic"}:
        footer = "Все изменения сохранены в «Моих откликах». Источник — локальное правило WexFlow."
    elif origins and all(origin.endswith("_portal") for origin in origins):
        footer = "Все изменения сохранены в «Моих откликах». Источник — официальный кабинет работодателя."
    else:
        footer = "Все изменения сохранены в «Моих откликах» с указанием источника."
    lines.extend(["", footer])
    return bool(cloud_auth.send_digest("\n".join(lines)[:2000]))


def current_stage(job: Job | None) -> str:
    """Return the recruitment stage without confusing it with listing state."""
    if job is None:
        return ""
    stage = str(getattr(job, "application_stage", None) or "").strip()
    if stage in STATUS_LABELS:
        return stage
    legacy = str(getattr(job, "status", None) or "").strip()
    if legacy in STATUS_LABELS:
        return legacy
    if getattr(job, "applied_at", None) is not None:
        return "applied"
    return ""


def _transition_allowed(job: Job, target: str, *, source: str, occurred_at) -> bool:
    """Reject stale automatic regressions while accepting explicit outcomes."""
    current = current_stage(job)
    if not current or current == target or source == "manual":
        return True
    if current == "no_response":
        # Silence is derived, never stronger than a factual employer response.
        # A repeated receipt/portal label "applied" is not a response and must
        # not restart the 60-day clock.  A real progression or explicit
        # outcome may replace silence even if its email was imported later.
        return target not in {"no_response", "applied"}
    updated_at = getattr(job, "application_status_updated_at", None)
    if current in _TERMINAL:
        # A terminal label from a manual/imported email is not allowed to
        # permanently block a later official portal fact.  Likewise, the same
        # factual channel may correct its own earlier decision (offer revoked,
        # rejection reversed, application withdrawn after hire).  We still
        # reject weaker/stale automatic data.
        current_source = str(job.application_status_source or "")
        factual = source == "email" or source.endswith("_portal")
        not_weaker = _SOURCE_STRENGTH.get(source, 0) >= _SOURCE_STRENGTH.get(
            current_source, 0
        )
        not_stale = not (occurred_at and updated_at and occurred_at < updated_at)
        return factual and not_weaker and not_stale
    if occurred_at and updated_at and occurred_at < updated_at:
        return False
    if target in _TERMINAL:
        return True
    if target == "no_response":
        return source == "automatic" and current == "applied"
    return _POSITIVE_RANK.get(target, 0) >= _POSITIVE_RANK.get(current, 0)


def _apply_status(job: Job, status: str, *, source: str, moment) -> tuple[bool, bool]:
    """Apply a normalized stage and return ``(accepted, changed)``."""
    if status not in STATUS_LABELS:
        return False, False
    previous = current_stage(job)
    if not _transition_allowed(job, status, source=source, occurred_at=moment):
        return False, False
    changed = previous != status
    stronger = _SOURCE_STRENGTH.get(source, 0) > _SOURCE_STRENGTH.get(
        str(job.application_status_source or ""), 0
    )
    job.application_stage = status
    # Keep old readers working, but never overwrite listing-only state.  A
    # closed/hidden advert can still have a live interview or an offer.
    if str(job.status or "") not in {"closed", "hidden"}:
        job.status = status
    if job.applied_at is None:
        job.applied_at = moment
    if changed or stronger or job.application_status_updated_at is None:
        job.application_status_updated_at = moment
        job.application_status_source = str(source or "manual")[:32]
    if source.endswith("_portal"):
        if str(job.applied_confidence or "") != "receipt":
            job.applied_confidence = "portal"
    elif source == "email" and str(job.applied_confidence or "") not in {"portal", "receipt"}:
        job.applied_confidence = "manual"
    elif source == "manual" and not job.applied_confidence:
        job.applied_confidence = "manual"
    return True, changed


def set_status(job: Job, status: str, *, source: str, now=None) -> bool:
    """Compatibility helper for code that does not own a DB session.

    New write paths should use :func:`record_status_in_session`, which also
    appends history and creates the durable notification outbox entry.
    """
    status = str(status or "").strip()
    moment = now or utcnow()
    accepted, _changed = _apply_status(job, status, source=source, moment=moment)
    return accepted


def record_status_in_session(
    session,
    job: Job,
    status: str,
    *,
    source: str = "",
    origin: str = "",
    occurred_at=None,
    raw_label: str = "",
    evidence_fingerprint: str = "",
    event_key: str = "",
) -> dict:
    """Atomically cache a stage and append one idempotent timeline event.

    The caller owns the transaction.  Portal/automatic events are marked for
    notification in the same commit, closing the old crash window between a
    status commit and a JSON notification queue.
    """
    status = str(status or "").strip()
    event_origin = str(origin or source or "manual").strip()[:32]
    moment = occurred_at or utcnow()
    previous = current_stage(job)
    previous_source = str(job.application_status_source or "")
    key = str(event_key or "").strip()[:500] or None
    existing = None
    if key:
        existing = session.exec(select(ApplicationStatusEvent).where(
            ApplicationStatusEvent.event_key == key
        )).first()
    if existing is not None:
        # Uploaded files/submission receipts are immutable observations and a
        # retry must be a no-op.  A portal scan is different: it describes the
        # portal's *current* row and may legitimately reassert an already seen
        # stage after a weaker manual/email event. Record that correction as a
        # new idempotent event so the cache never diverges from the timeline.
        current_source = str(job.application_status_source or "")
        portal_reassert_needed = event_origin.endswith("_portal") and (
            previous != status
            or _SOURCE_STRENGTH.get(event_origin, 0)
            > _SOURCE_STRENGTH.get(current_source, 0)
        )
        if not portal_reassert_needed:
            return {
                "accepted": True, "changed": False, "event": existing,
                "previous_stage": previous, "stage": current_stage(job),
            }
        updated = getattr(job, "application_status_updated_at", None)
        reassert_key = (
            f"{key}:reassert:{previous}:"
            f"{str(job.application_status_source or '')}:"
            f"{updated.isoformat(timespec='microseconds') if updated else 'unknown'}"
        )[:500]
        reasserted = session.exec(select(ApplicationStatusEvent).where(
            ApplicationStatusEvent.event_key == reassert_key
        )).first()
        if reasserted is not None:
            return {
                "accepted": True, "changed": False, "event": reasserted,
                "previous_stage": previous, "stage": current_stage(job),
            }
        key = reassert_key
    accepted, changed = _apply_status(
        job, status, source=event_origin, moment=moment
    )
    if not accepted:
        return {
            "accepted": False, "changed": False, "event": existing,
            "previous_stage": previous, "stage": current_stage(job),
        }
    session.add(job)
    stronger_confirmation = (
        previous == status
        and event_origin.endswith("_portal")
        and _SOURCE_STRENGTH.get(event_origin, 0)
        > _SOURCE_STRENGTH.get(previous_source, 0)
    )
    if not changed and not stronger_confirmation and event_origin == "manual":
        # Picking the stage the card already shows is not a new fact.  Manual
        # writes carry no event key, so without this the journal would fill
        # with identical "изменено вручную" rows on every repeated click.  The
        # very first record still seeds the timeline for a legacy application.
        latest = session.exec(select(ApplicationStatusEvent).where(
            ApplicationStatusEvent.source == str(job.source or "salling"),
            ApplicationStatusEvent.job_id == str(job.id),
        ).order_by(
            ApplicationStatusEvent.occurred_at.desc(),
            ApplicationStatusEvent.id.desc(),
        )).first()
        if latest is not None and str(latest.stage or "") == status:
            return {
                "accepted": True, "changed": False, "event": latest,
                "previous_stage": previous, "stage": current_stage(job),
            }
    # Repeated writes without a stable external identity should not fill the
    # timeline with identical rows.  Email and portal callers always pass a
    # fingerprint/event key, while manual changes are intentionally recorded.
    event = ApplicationStatusEvent(
        source=str(job.source or "salling"),
        job_id=str(job.id),
        stage=status,
        previous_stage=previous if previous != status else "",
        origin=event_origin,
        raw_label=str(raw_label or "")[:500],
        evidence_fingerprint=str(evidence_fingerprint or "")[:128],
        event_key=key,
        occurred_at=moment,
        observed_at=utcnow(),
        notification_required=bool(
            (changed or stronger_confirmation)
            and (event_origin.endswith("_portal") or event_origin == "automatic")
        ),
    )
    session.add(event)
    return {
        "accepted": True, "changed": changed, "event": event,
        "previous_stage": previous, "stage": current_stage(job),
    }


def view(job: Job, *, now=None) -> dict:
    """Human-readable tracking data for cards and the application journal."""
    moment = now or utcnow()
    applied_at = job.applied_at
    age_days = max(0, (moment - applied_at).days) if applied_at else 0
    stage = current_stage(job)
    due_at = (
        applied_at + dt.timedelta(days=NO_RESPONSE_DAYS)
        if applied_at and stage == "applied" else None
    )
    updated_at = job.application_status_updated_at or applied_at
    confirmation_label, confirmation_tone = CONFIRMATION_LABELS.get(
        str(job.applied_confidence or ""),
        ("Запись из истории WexFlow", "neutral"),
    )
    advice = _advice(stage, age_days)
    # Этап «ответ работодателя» не назначается автоматически: для поданной
    # заявки он остаётся ожиданием, пока кабинет или человек не даст факт.
    progress = {
        "applied": 1,
        "reviewing": 2,
        "interview": 3,
        "offer": 4,
        "hired": 5,
        "rejected": 2,
        "withdrawn": 1,
        "no_response": 1,
    }.get(stage, 0)
    return {
        "status": stage,
        "label": STATUS_LABELS.get(stage, stage),
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
        "terminal": stage in _TERMINAL,
        **advice,
    }


def history_map(session, jobs) -> dict[tuple[str, str], list[dict]]:
    """Return newest-first application timelines for a collection of jobs."""
    keys = {
        (str(getattr(job, "source", None) or "salling"), str(getattr(job, "id", "")))
        for job in jobs or [] if getattr(job, "id", None)
    }
    if not keys:
        return {}
    ids = {job_id for _source, job_id in keys}
    rows = session.exec(select(ApplicationStatusEvent).where(
        ApplicationStatusEvent.job_id.in_(ids)
    ).order_by(
        ApplicationStatusEvent.occurred_at.desc(),
        ApplicationStatusEvent.id.desc(),
    )).all()
    result: dict[tuple[str, str], list[dict]] = {key: [] for key in keys}
    for row in rows:
        key = (str(row.source), str(row.job_id))
        if key not in result or len(result[key]) >= 20:
            continue
        result[key].append({
            "id": row.id,
            "stage": str(row.stage or ""),
            "label": STATUS_LABELS.get(str(row.stage or ""), str(row.stage or "")),
            "previous_stage": str(row.previous_stage or ""),
            "previous_label": STATUS_LABELS.get(
                str(row.previous_stage or ""), str(row.previous_stage or "")
            ),
            "origin": str(row.origin or ""),
            "origin_label": STATUS_SOURCES.get(str(row.origin or ""), str(row.origin or "")),
            "raw_label": str(row.raw_label or ""),
            "occurred_at": row.occurred_at,
            "observed_at": row.observed_at,
            "evidence_fingerprint": str(row.evidence_fingerprint or ""),
            "notified": bool(row.notified_at),
        })
    return result


def pending_notification_changes(*, origin: str = "", limit: int = 50) -> list[dict]:
    """Load undelivered DB-outbox rows as Telegram-safe change dictionaries."""
    with get_session() as session:
        stmt = select(ApplicationStatusEvent).where(
            ApplicationStatusEvent.notification_required == True,  # noqa: E712
            ApplicationStatusEvent.notified_at.is_(None),
        )
        if origin:
            stmt = stmt.where(ApplicationStatusEvent.origin == str(origin))
        events = session.exec(stmt.order_by(
            ApplicationStatusEvent.occurred_at.asc(), ApplicationStatusEvent.id.asc()
        ).limit(max(1, min(int(limit), 200)))).all()
        result = []
        for event in events:
            job = session.get(Job, event.job_id)
            if job is None or str(job.source or "salling") != str(event.source):
                continue
            result.append({
                "event_id": event.id,
                "source": event.source,
                "origin": event.origin,
                "job_id": event.job_id,
                "title": job.title or "Вакансия",
                "brand": job.brand or "",
                "city": job.city or "",
                "url": job.application_link or "",
                "previous_status": event.previous_stage,
                "previous_label": STATUS_LABELS.get(event.previous_stage, event.previous_stage),
                "status": event.stage,
                "status_label": STATUS_LABELS.get(event.stage, event.stage),
                "age_days": max(0, (utcnow() - (job.applied_at or utcnow())).days),
            })
        return result


def flush_pending_notifications(*, origin: str, source_name: str = "") -> bool:
    """Deliver one origin's DB outbox and durably mark success/failure."""
    changes = pending_notification_changes(origin=origin)
    if not changes:
        return True
    ok = notify_status_changes(changes, source_name=source_name)
    ids = [int(item["event_id"]) for item in changes if item.get("event_id") is not None]
    with get_session() as session:
        rows = session.exec(select(ApplicationStatusEvent).where(
            ApplicationStatusEvent.id.in_(ids)
        )).all()
        for row in rows:
            row.notification_attempts = int(row.notification_attempts or 0) + 1
            if ok:
                row.notified_at = utcnow()
                row.notification_error = ""
            else:
                row.notification_error = "Telegram не принял уведомление; повторим позже."
            session.add(row)
        session.commit()
    return ok


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
            (
                (Job.application_stage == "applied")
                | (
                    Job.application_stage.is_(None)
                    & (Job.status == "applied")
                )
            ),
        )).all()
        for job in jobs:
            record_status_in_session(
                session,
                job,
                "no_response",
                source="automatic",
                occurred_at=moment,
                raw_label=f"{NO_RESPONSE_DAYS} дней без ответа",
                event_key=f"automatic:no_response:{job.source}:{job.id}:{moment.date().isoformat()}",
            )
            changed.append({
                "id": job.id,
                "title": job.title or "Вакансия",
                "brand": job.brand or "",
                "applied_at": job.applied_at,
            })
        if changed:
            session.commit()
    return changed
