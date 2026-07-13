"""Safely ingest ATS connector jobs into the shared WexFlow feed.

Connector rows are isolated by ``Job.source``. Each connector closes only its
own disappeared rows, so a failed or partial ATS feed can never mutate Salling.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import connectors
from connectors.base import JobItem, is_denmark
from db import Job, get_session, init_db, select, utcnow

DEFAULT_SOURCES = ("teamtailor",)
STALE_AFTER = dt.timedelta(hours=48)


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("addressCountry") or value.get("value") or ""
    return str(value or "").strip()


def _is_danish(item: JobItem) -> bool:
    country = _text(item.country)
    if country.upper() in {"DK", "DNK"} or country.casefold() in {"denmark", "danmark"}:
        return True
    # An explicit foreign country wins over a city-like substring. Only fall
    # back to city/street when the feed omitted country altogether.
    if country:
        return is_denmark(country)
    return is_denmark(_text(item.city), _text(item.street))


def job_from_item(item: JobItem, now=None) -> Job:
    now = now or utcnow()
    country = _text(item.country)
    if country.upper() in {"DK", "DNK"} or country.casefold() in {"denmark", "danmark"}:
        country = "DK"
    return Job(
        id=str(item.id or "")[:220],
        source=str(item.source or "")[:40],
        title=_text(item.title)[:500],
        brand=_text(item.company)[:180] or None,
        city=_text(item.city)[:180] or None,
        street=_text(item.street)[:240] or None,
        zip=_text(item.zip)[:30] or None,
        country=country[:60] or None,
        published=_text(item.published)[:80] or None,
        description=item.description or None,
        application_link=_text(item.url)[:1000] or None,
        requisition_id=str(item.id or "")[:220] or None,
        first_seen=now,
        last_seen=now,
        status="new",
    )


def _item_scope(source: str, item_id: str, company: str = "") -> str:
    """Return a conservative company scope used when closing stale rows.

    Teamtailor ids are ``tt:<company>:<job>``. A missing row may be closed only
    when another row from that same company was present in the fresh snapshot.
    Thus a timed-out or currently empty company feed cannot cause false closes.
    """
    value = str(item_id or "")
    if source == "teamtailor" and value.startswith("tt:") and ":" in value[3:]:
        return value.rsplit(":", 1)[0]
    return _text(company).casefold()


def sync_items(source: str, items: Iterable[JobItem], session_factory=get_session) -> dict:
    """Upsert one complete connector snapshot and close only its missing rows."""
    now = utcnow()
    clean = [item for item in items
             if item.source == source and item.id and item.title and _is_danish(item)]
    seen_ids = {str(item.id) for item in clean}
    seen_scopes = {_item_scope(source, item.id, item.company) for item in clean}
    seen_scopes.discard("")
    created = updated = closed = 0

    with session_factory() as session:
        for item in clean:
            fresh = job_from_item(item, now)
            existing = session.get(Job, fresh.id)
            if existing is None:
                session.add(fresh)
                created += 1
                continue
            data = fresh.model_dump(exclude={
                "id", "first_seen", "status", "applied_at", "applied_confidence", "lat", "lon"
            })
            for key, value in data.items():
                setattr(existing, key, value)
            existing.last_seen = now
            if existing.status == "closed" and existing.applied_at is None:
                existing.status = "seen"
            session.add(existing)
            updated += 1
        session.commit()

        active = session.exec(select(Job).where(
            Job.source == source,
            Job.status.not_in(["closed", "applied"]),
            Job.last_seen < now - STALE_AFTER,
        )).all()
        for job in active:
            scope = _item_scope(source, job.id, job.brand or "")
            if (job.id not in seen_ids and scope in seen_scopes
                    and job.applied_at is None):
                job.status = "closed"
                session.add(job)
                closed += 1
        session.commit()

    return {"source": source, "hits": len(clean), "created": created,
            "updated": updated, "closed": closed}


def sync(sources: Iterable[str] = DEFAULT_SOURCES) -> dict:
    init_db()
    reports, errors = [], []
    for source in sources:
        conn = connectors.get(source)
        if conn is None:
            errors.append(f"{source}: connector not registered")
            continue
        try:
            reports.append(sync_items(source, conn.search()))
        except Exception as exc:  # one ATS must not break the working Salling feed
            errors.append(f"{source}: {str(exc)[:180]}")
    return {
        "hits": sum(row["hits"] for row in reports),
        "created": sum(row["created"] for row in reports),
        "updated": sum(row["updated"] for row in reports),
        "closed": sum(row["closed"] for row in reports),
        "sources": reports,
        "errors": errors,
    }
