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
    "max_age_days": 0,      # брать только вакансии не старше N дней (0 = любой возраст)
    "category": "",         # код категории (как на главной) или пусто = все
    "employment_type": "",  # fullTime / partTime или пусто = любая
    "age": "",              # "" любой / "under18" до 18 / "adult" от 18
    "keywords": "",         # доп. слова через запятую (пусто = не учитывать)
    "brand": "",            # код бренда или пусто = все бренды
    "seen_ids": [],         # id вакансий, о которых уже уведомляли
    "prepared_ids": [],     # id, которые уже готовили (фаза 2)
    # --- фаза 3: автоотправка (по умолчанию ВЫКЛ, под замком) ---
    "auto_submit": False,       # отправлять автоматически?
    "daily_limit": 3,           # максимум автоотправок в день
    "submit_scope": "new",      # "new" = только появившиеся ПОСЛЕ включения; "all" = все подходящие
    "autosubmit_baseline": [],  # снимок совпадений на момент включения — их НЕ трогаем (для scope=new)
    "submitted_ids": [],        # id, которые автоотправка уже подала
    "submit_day": "",           # день, за который считаем счётчик
    "submit_count_today": 0,    # сколько отправлено сегодня
    "submit_log": [],           # журнал автоотправок [{ts,title}]
}

# Поля, которые пользователь задаёт в интерфейсе (seen_ids/prepared_ids — служебные).
_USER_FIELDS = ("enabled", "max_km", "min_hours", "max_age_days", "category",
                "employment_type", "age", "keywords", "brand")

# ── Предохранители автоотправки (жёсткие, не настраиваются из интерфейса) ──
MAX_PER_SCAN = 2          # максимум автоотправок за ОДИН фоновый скан — чтобы
                          # даже при большом лимите ничего не «улетало пачкой»
SCOPE_ALL_GUARD = 25      # нельзя включить охват «все подходящие», если под
                          # правило сейчас попадает больше этого числа (защита
                          # от «подалось на всё подряд» — заставляет сузить фильтры)


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


def _age_days(job: Job) -> float | None:
    """Возраст вакансии в днях по first_seen (None — если даты нет)."""
    t = job.first_seen
    if t is None:
        return None
    t = t.replace(tzinfo=None) if getattr(t, "tzinfo", None) else t
    return (_dt.datetime.now() - t).total_seconds() / 86400.0


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
    # свежесть: берём только вакансии не старше N дней (если задано).
    # дату не знаем — НЕ отбрасываем (чтобы ничего не упустить).
    try:
        max_age = float(rule.get("max_age_days") or 0)
    except (TypeError, ValueError):
        max_age = 0
    if max_age:
        ad = _age_days(job)
        if ad is not None and ad > max_age:
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


# ── Фаза 3: автоотправка (под замком) ──────────────────────────────────
def _today() -> str:
    return _dt.date.today().isoformat()


def submitted_today() -> int:
    """Сколько автоотправок сделано сегодня (счётчик сбрасывается в новый день)."""
    r = get_rule()
    return int(r.get("submit_count_today") or 0) if r.get("submit_day") == _today() else 0


def submit_log() -> list:
    return get_rule().get("submit_log") or []


def set_autosubmit_baseline() -> None:
    """Запомнить текущие совпадения как «не трогать» — чтобы при включении
    автоотправка не разослала разом весь существующий список, а ждала НОВЫЕ."""
    save_rule({"autosubmit_baseline": [j.id for j in find_matches()]})


def _eligible_all(rule: dict) -> list[Job]:
    """Подходящие, которые автоотправка ещё НЕ подавала, с учётом охвата:
    - scope=new (по умолчанию): только появившиеся ПОСЛЕ включения (нет в baseline);
    - scope=all: все подходящие сейчас (baseline игнорируется).
    Уже отправленные ботом исключаются всегда. Свежие — первыми."""
    done = set(rule.get("submitted_ids") or [])
    if (rule.get("submit_scope") or "new") == "all":
        todo = [j for j in find_matches() if j.id not in done]
    else:
        baseline = set(rule.get("autosubmit_baseline") or [])
        todo = [j for j in find_matches() if j.id not in baseline and j.id not in done]
    todo.sort(key=_seen_ts, reverse=True)
    return todo


def eligible_for_submit(limit_remaining: int) -> list[Job]:
    """Сколько реально отправим за этот скан: с учётом охвата и оставшегося лимита."""
    todo = _eligible_all(get_rule())
    return todo[: max(0, int(limit_remaining))]


def eligible_count() -> int:
    """Сколько вакансий сейчас в очереди на автоотправку (для подписи в интерфейсе)."""
    return len(_eligible_all(get_rule()))


def scope_all_pool(rule: dict | None = None) -> int:
    """Сколько подходящих (ещё не поданных ботом) попадёт под охват «все подходящие».
    Нужно для предохранителя при включении — чтобы не разрешить массовую отправку."""
    r = dict(rule) if rule else get_rule()
    r = dict(r); r["submit_scope"] = "all"
    return len(_eligible_all(r))


def record_submitted(jobs) -> None:
    r = get_rule()
    day = _today()
    count = int(r.get("submit_count_today") or 0) if r.get("submit_day") == day else 0
    log = list(r.get("submit_log") or [])
    done = set(r.get("submitted_ids") or [])
    now = _dt.datetime.now().strftime("%d.%m %H:%M")
    for j in jobs:
        done.add(j.id)
        log.insert(0, {"ts": now, "title": j.title})
    save_rule({
        "submit_day": day,
        "submit_count_today": count + len(jobs),
        "submit_log": log[:50],
        "submitted_ids": list(done),
    })


def auto_submit_tick(launcher) -> None:
    """Вызывается после скана базы. Если автоотправка включена и есть дневной
    лимит — отправить до (лимит − сегодня) свежих подходящих. launcher(ids)
    делает реальную отправку. Логика отделена от запуска, чтобы её можно было
    проверить без настоящей подачи."""
    try:
        r = get_rule()
        if not (r.get("enabled") and r.get("auto_submit")):
            return
        remaining = int(r.get("daily_limit") or 0) - submitted_today()
        # жёсткий потолок за один скан: даже при большом дневном лимите за раз
        # отправляем не больше MAX_PER_SCAN — ничего не «улетает пачкой».
        remaining = min(remaining, MAX_PER_SCAN)
        if remaining <= 0:
            return
        jobs = eligible_for_submit(remaining)
        if not jobs:
            return
        launcher([j.id for j in jobs])
        record_submitted(jobs)
        import scheduler
        scheduler.notify(f"Автопилот отправил заявки: {len(jobs)}",
                         "; ".join(j.title for j in jobs[:5]))
    except Exception as e:  # noqa: BLE001 — автоотправка не должна ронять обновление
        print(f"автопилот: автоотправка — ошибка {e}")


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
