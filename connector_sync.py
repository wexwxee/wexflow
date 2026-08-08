"""Safely ingest ATS connector jobs into the shared WexFlow feed.

Connector rows are isolated by ``Job.source``. Each connector closes only its
own disappeared rows, so a failed or partial ATS feed can never mutate Salling.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import connectors
import feed
import geo
from connectors.base import JobItem, is_denmark
from db import Job, get_session, init_db, select, utcnow

DEFAULT_SOURCES = ("teamtailor", "greenhouse", "ashby", "lidl")
STALE_AFTER = dt.timedelta(hours=48)
GEOCODE_BATCH = 40
# Каталоги ATS живут своей жизнью: фирма может переехать на свой домен или
# закрыть карьерный сайт. Пока молчит лишь малая часть каталога, источник в
# целом здоров — это заметка на странице «Состояние», а не тревога на весь
# экран. Тревога остаётся для настоящей поломки (лёг весь Teamtailor и т.п.).
COMPANY_FAIL_SHARE = 0.2


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("addressCountry") or value.get("value") or ""
    return str(value or "").strip()


def item_country(item: JobItem) -> str:
    """Код страны вакансии: явное поле каталога, иначе догадка по городу.

    Каталоги ATS пишут страну как попало («DK», «Denmark», «Danmark»), а часть
    вовсе её не присылает — там остаётся смотреть на город и улицу.
    """
    raw = _text(item.country)
    code = feed.normalize(raw)
    if code:
        return code
    if raw and is_denmark(raw):
        return "DK"
    if not raw and is_denmark(_text(item.city), _text(item.street)):
        return "DK"
    return ""


def _wanted(item: JobItem) -> bool:
    """Берём ли вакансию в базу. Страна — настройка ленты (по умолчанию DK).

    Нераспознанную страну в базу не тащим: каталоги международные, и без
    опознания сюда полился бы весь мир.
    """
    return feed.allows(item_country(item), unknown_ok=False)


def job_from_item(item: JobItem, now=None) -> Job:
    now = now or utcnow()
    country = item_country(item) or _text(item.country)
    return Job(
        id=str(item.id or "")[:220],
        source=str(item.source or "")[:40],
        title=_text(item.title)[:500],
        brand=_text(item.company)[:180] or None,
        categories=_text(item.categories)[:500] or None,
        region=_text(item.region)[:180] or None,
        city=_text(item.city)[:180] or None,
        street=_text(item.street)[:240] or None,
        zip=_text(item.zip)[:30] or None,
        country=country[:60] or None,
        lat=item.lat,
        lon=item.lon,
        hours=_text(item.hours)[:100] or None,
        employment_type=_text(item.employment_type)[:80] or None,
        job_level=_text(item.job_level)[:80] or None,
        pay_rate=_text(item.pay_rate)[:240] or None,
        published=_text(item.published)[:80] or None,
        modified=_text(item.modified)[:80] or None,
        description=item.description or None,
        application_link=_text(item.url)[:1000] or None,
        requisition_id=_text(item.requisition_id)[:220] or str(item.id or "")[:220] or None,
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
             if item.source == source and item.id and item.title and _wanted(item)]
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
                "id", "first_seen", "status", "applied_at", "applied_confidence",
                "lat", "lon",
            })
            for key, value in data.items():
                setattr(existing, key, value)
            # Most ATS feeds omit coordinates and rely on WexFlow geocoding.
            # Lidl provides precise coordinates, so accept them when present
            # without erasing an existing geocode for other connectors.
            if fresh.lat is not None and fresh.lon is not None:
                existing.lat, existing.lon = fresh.lat, fresh.lon
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


def geocode_missing(source: str, limit: int = GEOCODE_BATCH,
                    session_factory=get_session, geocoder=None) -> int:
    """Gradually add coordinates without making a refresh wait on all rows.

    A bounded batch is enough for distance sorting to improve after every sync;
    the shared disk cache makes repeated company addresses effectively free.
    """
    if limit <= 0:
        return 0
    with session_factory() as session:
        jobs = session.exec(
            select(Job).where(
                Job.source == source,
                *feed.visible_clauses(),
                Job.lat.is_(None),
                (Job.zip.is_not(None) | Job.city.is_not(None)),
            ).order_by(Job.last_seen.desc()).limit(limit)
        ).all()
        if not jobs:
            return 0
        updated = int((geocoder or geo.geocode_jobs)(jobs) or 0)
        session.commit()
        return updated


def _enabled_companies(conn) -> int:
    """Сколько компаний каталога вообще опрашивается (0 — каталога нет)."""
    try:
        return sum(1 for c in conn.companies() if c.get("enabled", True))
    except Exception:  # каталог не обязателен (например, у Lidl)
        return 0


def sync(sources: Iterable[str] = DEFAULT_SOURCES) -> dict:
    init_db()
    reports, errors, warnings = [], [], []
    for source in sources:
        conn = connectors.get(source)
        if conn is None:
            errors.append(f"{source}: connector not registered")
            continue
        try:
            items = conn.search()
            company_errors = list(getattr(conn, "last_errors", []) or [])
            report = sync_items(source, items)
            if company_errors:
                report["company_errors"] = company_errors[:20]
                total = _enabled_companies(conn)
                scope = f"{len(company_errors)} из {total}" if total else str(len(company_errors))
                message = (
                    f"{source}: не отвечают компании ({scope}); "
                    f"первая ошибка: {company_errors[0]}"
                )
                # Неизвестный размер каталога считаем поломкой: молчать о том,
                # чего не измерили, опаснее лишнего баннера.
                if total and len(company_errors) <= int(total * COMPANY_FAIL_SHARE):
                    warnings.append(message)
                else:
                    errors.append(message)
            try:
                report["geocoded"] = geocode_missing(source)
            except Exception as exc:  # coordinates are useful, never critical
                report["geocoded"] = 0
                report["geocode_error"] = str(exc)[:180]
                print(f"  {source}: geocoding skipped — {exc}")
            reports.append(report)
        except Exception as exc:  # one ATS must not break the working Salling feed
            errors.append(f"{source}: {str(exc)[:180]}")
    return {
        "hits": sum(row["hits"] for row in reports),
        "created": sum(row["created"] for row in reports),
        "updated": sum(row["updated"] for row in reports),
        "closed": sum(row["closed"] for row in reports),
        "geocoded": sum(row.get("geocoded", 0) for row in reports),
        "sources": reports,
        "errors": errors,
        "warnings": warnings,
    }
