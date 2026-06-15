"""Автопилот подбора вакансий — ФАЗА 1 (доп-функция, не основная).

Что делает: хранит ОДНО правило подбора (расстояние, часы/нед, ключевые
слова, бренд). После каждого обновления базы вакансий находит активные
вакансии под правило и, если появились НОВЫЕ, шлёт уведомление Windows.

Чего НЕ делает (это Фазы 2-3): не открывает браузер, не заполняет и не
отправляет анкеты. Полностью изолирован — сбой тихо логируется и не трогает
остальное приложение.
"""
from __future__ import annotations

import datetime as _dt
import re

import geo
import labels
import settings_store
from db import Job, get_session, select

# Значения по умолчанию правила. 0/пусто = «без ограничения».
DEFAULT_RULE = {
    "enabled": False,
    "max_km": 0,            # радиус от дома, км (0 = без ограничения)
    "min_hours": 0,         # минимум часов в неделю (0 = любые)
    "category": "",         # код категории (как на главной) или пусто = все
    "employment_type": "",  # fullTime / partTime или пусто = любая
    "age": "",              # "" любой / "under18" до 18 / "adult" от 18
    "keywords": "",         # доп. слова через запятую (пусто = не учитывать)
    "brand": "",            # код бренда или пусто = все бренды
    "seen_ids": [],         # id вакансий, о которых уже уведомляли
}

# Поля, которые пользователь задаёт в интерфейсе (seen_ids/prepared_ids — служебные).
_USER_FIELDS = ("enabled", "max_km", "min_hours", "category",
                "employment_type", "age", "keywords", "brand")


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
    hay = f"{job.title or ''} {job.description or ''} {job.city or ''} {job.street or ''}".lower()
    # несколько фраз через запятую: совпало хоть одно — берём (чтобы не упустить)
    for phrase in (p.strip() for p in raw.split(",") if p.strip()):
        for term in ru_search.expand(phrase):
            if (term or "").lower() in hay:
                return True
    return False


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
    # категория, занятость, возраст — те же поля, что и реальные фильтры на главной
    cat = rule.get("category") or ""
    if cat and cat not in (job.categories or "").split(","):
        return False
    emp = rule.get("employment_type") or ""
    if emp and job.employment_type != emp:
        return False
    age = rule.get("age") or ""
    if age == "under18" and job.job_level != "employeeUnder18":
        return False
    if age == "adult" and job.job_level == "employeeUnder18":
        return False
    try:
        min_hours = float(rule.get("min_hours") or 0)
    except (TypeError, ValueError):
        min_hours = 0
    if min_hours:
        jh = _job_hours(job)
        # часы не указаны в вакансии — НЕ отбрасываем (чтобы ничего не упустить),
        # отсекаем только если точно знаем, что меньше минимума
        if jh is not None and jh < min_hours:
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


def _seen_ts(job: Job):
    """Дата «впервые увидели» как naive datetime для сортировки (свежие первыми)."""
    t = job.first_seen
    if t is None:
        return _dt.datetime.min
    return t.replace(tzinfo=None) if getattr(t, "tzinfo", None) else t


def pending_prepare(limit: int = 5) -> list[Job]:
    """Подходящие вакансии, которые ещё НЕ готовили (свежие первыми, не больше limit)."""
    prepared = set(get_rule().get("prepared_ids") or [])
    todo = [j for j in find_matches() if j.id not in prepared]
    todo.sort(key=_seen_ts, reverse=True)
    return todo[: max(1, int(limit))]


def pending_count() -> int:
    """Сколько подходящих ещё не готовили (для подписи на кнопке)."""
    prepared = set(get_rule().get("prepared_ids") or [])
    return sum(1 for j in find_matches() if j.id not in prepared)


def mark_prepared(ids) -> None:
    prepared = set(get_rule().get("prepared_ids") or [])
    prepared.update(ids)
    save_rule({"prepared_ids": list(prepared)})


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
