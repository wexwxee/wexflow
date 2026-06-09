"""Этап 1 — скрейпер вакансий Salling Group через Algolia + сохранение в SQLite.

Стратегия: прямой запрос к Algolia (быстро, структурировано). Algolia ограничивает
hitsPerPage до 1000, поэтому листаем постранично.

Запуск:  python scraper.py
"""
import re
from datetime import datetime

import httpx

import config
from db import Job, init_db, get_session, select

PAGE_SIZE = 1000


def _algolia_page(page: int) -> dict:
    payload = {
        "query": "*",
        "attributesToRetrieve": config.ATTRS,
        "page": page,
        "hitsPerPage": PAGE_SIZE,
        "distinct": False,
    }
    headers = {
        "x-algolia-application-id": config.ALGOLIA_APP_ID,
        "x-algolia-api-key": config.ALGOLIA_API_KEY,
        "content-type": "application/json",
    }
    r = httpx.post(config.ALGOLIA_URL, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_all_hits() -> list[dict]:
    """Тянет все вакансии постранично."""
    first = _algolia_page(0)
    hits = list(first.get("hits", []))
    nb_pages = first.get("nbPages", 1)
    print(f"Algolia: nbHits={first.get('nbHits')} nbPages={nb_pages}")
    for page in range(1, nb_pages):
        hits.extend(_algolia_page(page).get("hits", []))
    print(f"Получено вакансий: {len(hits)}")
    return hits


# ставка иногда мелькает в описании — пробуем выцепить (часто None)
_PAY_RE = re.compile(r"(\d[\d.\s]*\s?(?:kr\.?|DKK)\s?(?:/|pr\.?\s?)?\s?(?:time|t|md|måned)?)", re.I)


def _extract_pay(description: str | None) -> str | None:
    if not description:
        return None
    m = _PAY_RE.search(description)
    return m.group(1).strip() if m else None


def hit_to_job(h: dict) -> Job:
    addr = h.get("address") or {}
    cats = h.get("categories") or []
    return Job(
        id=h.get("objectID") or h.get("id"),
        title=h.get("title") or "",
        brand=h.get("brand"),
        categories=",".join(cats) if isinstance(cats, list) else (cats or None),
        region=h.get("region"),
        city=addr.get("city"),
        street=addr.get("street"),
        zip=addr.get("zip"),
        country=h.get("country") or addr.get("country"),
        hours=h.get("hours"),
        employment_type=h.get("employmentType"),
        job_level=h.get("jobLevel"),
        trainee=bool(h.get("trainee")),
        unsolicited=bool(h.get("unsolicited")),
        pay_rate=_extract_pay(h.get("description")),
        start_date=h.get("start"),
        published=h.get("published"),
        created=h.get("created"),
        modified=h.get("modified"),
        description=h.get("description"),
        application_link=h.get("applicationLink") or h.get("url"),
        requisition_id=str(h.get("requisitionId")) if h.get("requisitionId") else None,
    )


def sync():
    """Главная функция: забрать вакансии, обновить БД, пометить исчезнувшие как closed."""
    init_db()
    hits = fetch_all_hits()
    now = datetime.utcnow()
    seen_ids = set()
    new_count = 0

    with get_session() as s:
        for h in hits:
            job = hit_to_job(h)
            if not job.id:
                continue
            seen_ids.add(job.id)
            existing = s.get(Job, job.id)
            if existing is None:
                job.first_seen = now
                job.last_seen = now
                job.status = "new"
                s.add(job)
                new_count += 1
            else:
                # обновляем поля, но сохраняем пользовательский статус applied
                data = job.model_dump(exclude={"id", "first_seen", "status", "applied_at"})
                for k, v in data.items():
                    setattr(existing, k, v)
                existing.last_seen = now
                if existing.status == "closed":
                    existing.status = "seen"  # вакансия вернулась
                s.add(existing)
        s.commit()

        # пометить исчезнувшие активные вакансии как closed
        closed = 0
        active = s.exec(select(Job).where(Job.status != "closed")).all()
        for job in active:
            if job.id not in seen_ids:
                job.status = "closed"
                s.add(job)
                closed += 1
        s.commit()

        # геокодирование (DK по улице через DAWA, DE/PL по индексу; кэш — повторы мгновенны)
        import geo
        active_jobs = s.exec(select(Job).where(Job.status != "closed")).all()
        missing = [j for j in active_jobs if j.lat is None]
        if missing:
            print(f"Геокодирую вакансии без координат: {len(missing)}…")
            upd = geo.geocode_jobs(missing)
            s.commit()
            print(f"  координат проставлено: {upd}")

    print(f"Новых: {new_count} | Активных всего: {len(seen_ids)} | Закрыто в этот раз: {closed}")


if __name__ == "__main__":
    sync()
