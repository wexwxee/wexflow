"""Автопилот подбора вакансий — ФАЗА 1 (доп-функция, не основная).

Что делает: хранит ОДНО правило подбора (расстояние, часы/нед, ключевые
слова, бренд). После каждого обновления базы вакансий находит активные
вакансии под правило и, если появились НОВЫЕ, шлёт уведомление Windows.

Чего НЕ делает (это Фазы 2-3): не открывает браузер, не заполняет и не
отправляет анкеты. Полностью изолирован — сбой тихо логируется и не трогает
остальное приложение.
"""
from __future__ import annotations

import re

import geo
import labels
import settings_store
from db import Job, get_session, select

# Значения по умолчанию правила. 0/пусто = «без ограничения».
DEFAULT_RULE = {
    "enabled": False,
    "max_km": 0,        # радиус от дома, км (0 = без ограничения)
    "min_hours": 0,     # минимум часов в неделю (0 = любые)
    "keywords": "",     # ключевые слова через запятую/пробел (пусто = любые)
    "brand": "",        # код бренда или пусто = все бренды
    "seen_ids": [],     # id вакансий, о которых уже уведомляли
}

# Поля, которые пользователь задаёт в интерфейсе (seen_ids — служебное).
_USER_FIELDS = ("enabled", "max_km", "min_hours", "keywords", "brand")


def get_rule() -> dict:
    rule = dict(DEFAULT_RULE)
    rule.update(settings_store.load().get("autopilot", {}) or {})
    return rule


def save_rule(patch: dict) -> dict:
    """Обновить правило частично (мерж), вернуть итоговое правило."""
    data = settings_store.load()
    rule = dict(DEFAULT_RULE)
    rule.update(data.get("autopilot", {}) or {})
    rule.update(patch)
    data["autopilot"] = rule
    settings_store.save(data)
    return rule


def _keyword_match(job: Job, raw: str) -> bool:
    raw = (raw or "").strip()
    if not raw:
        return True
    import ru_search  # расширяем русский запрос датскими синонимами, как на главной
    terms = ru_search.expand(raw)
    hay = f"{job.title or ''} {job.description or ''} {job.city or ''}".lower()
    return any((t or "").lower() in hay for t in terms)


def _job_hours(job: Job) -> float | None:
    """Часы/неделю из job.hours — в БД это строка ('5', '37,5', '5 t/uge')."""
    raw = job.hours
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.search(r"\d+(?:[.,]\d+)?", str(raw))
    return float(m.group().replace(",", ".")) if m else None


def _matches(job: Job, rule: dict, home: dict | None) -> bool:
    if job.status in ("closed", "hidden", "applied"):
        return False
    brand = labels.resolve(labels.BRANDS, rule.get("brand") or "")
    if brand and job.brand != brand:
        return False
    try:
        min_hours = float(rule.get("min_hours") or 0)
    except (TypeError, ValueError):
        min_hours = 0
    if min_hours:
        jh = _job_hours(job)
        if jh is None or jh < min_hours:
            return False
    try:
        max_km = float(rule.get("max_km") or 0)
    except (TypeError, ValueError):
        max_km = 0
    if max_km and home:
        if job.lat is None or job.lon is None:
            return False
        if geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon) > max_km:
            return False
    return _keyword_match(job, rule.get("keywords"))


def find_matches() -> list[Job]:
    """Активные вакансии, подходящие под правило (объекты Job)."""
    rule = get_rule()
    home = settings_store.get_home()
    with get_session() as s:
        jobs = list(
            s.exec(select(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))).all()
        )
    return [j for j in jobs if _matches(j, rule, home)]


def match_count() -> int:
    return len(find_matches())


def scan_and_notify() -> None:
    """Вызывается после обновления базы. Если автопилот включён и появились
    НОВЫЕ совпадения — уведомить и запомнить их id. Тихо переживает сбои."""
    try:
        rule = get_rule()
        if not rule.get("enabled"):
            return
        matches = find_matches()
        ids = [j.id for j in matches]
        seen = set(rule.get("seen_ids") or [])
        fresh = [j for j in matches if j.id not in seen]
        save_rule({"seen_ids": ids})  # помним текущий набор, чистим устаревшее
        if fresh:
            import scheduler
            titles = "; ".join(j.title for j in fresh[:5])
            scheduler.notify(f"Автопилот: новых вакансий {len(fresh)}", titles)
    except Exception as e:  # noqa: BLE001 — автопилот не должен ронять обновление
        print(f"автопилот: ошибка скана — {e}")
