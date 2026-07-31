"""Application pipeline status tracking shared by every employer."""
from __future__ import annotations

import datetime as dt

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
    "automatic": "определено WexFlow",
    "recovered": "восстановлено из журнала",
}


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
    return {
        "status": job.status,
        "label": STATUS_LABELS.get(job.status, job.status),
        "source": job.application_status_source or "",
        "source_label": STATUS_SOURCES.get(job.application_status_source or "", ""),
        "updated_at": updated_at,
        "age_days": age_days,
        "no_response_due_at": due_at,
        "no_response_days": NO_RESPONSE_DAYS,
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
