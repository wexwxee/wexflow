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
import uuid

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
    # --- точное нацеливание места и исключения ---
    "cities": "",           # конкретные города (CSV точных названий). Пусто = любой
    "regions": "",          # конкретные регионы (CSV). Пусто = любой
    "max_hours": 0,         # верхняя граница часов/нед (0 = без ограничения)
    "exclude_brands": "",   # НЕ предлагать эти бренды (CSV кодов)
    "exclude_cities": "",   # НЕ предлагать эти города (CSV названий)
    "exclude_keywords": "", # НЕ предлагать, если слово есть в названии/описании (CSV)
    # --- несколько профилей подбора (если пусто — синтезируется один из полей выше) ---
    "profiles": [],         # [{id,name,enabled, ...те же поля-фильтры...}]
    # --- расписание активности (когда автопилот шлёт карточки/подаёт) ---
    "active_from": 0,       # с какого часа (0-23). from==to или 0..24 = круглосуточно
    "active_to": 24,        # по какой час (1-24)
    "seen_ids": [],         # id вакансий, о которых уже уведомляли
    "prepared_ids": [],     # id, которые уже готовили (фаза 2)
    # --- фаза 3: автоотправка (по умолчанию ВЫКЛ, под замком) ---
    "auto_submit": False,       # отправлять автоматически?
    "daily_limit": 3,           # максимум автоотправок в день
    "submit_scope": "new",      # "new" = только появившиеся ПОСЛЕ включения; "all" = все подходящие
    "autosubmit_baseline": [],  # снимок совпадений на момент включения — их НЕ трогаем (для scope=new)
    "submitted_ids": [],        # id, которые автоотправка уже подала
    "submitting_ids": [],       # id, по которым подача запущена, но ещё не подтверждена status=applied
    "submit_day": "",           # день, за который считаем счётчик
    "submit_count_today": 0,    # сколько отправлено сегодня
    "submit_log": [],           # журнал автоотправок [{ts,title}]
    "submitted_total": 0,       # сколько автопилот подал всего (за всё время)
    "event_log": [],            # лента событий автопилота [{ts,kind,text}] (для монитора)
    # --- режим «по разрешению» через Telegram (по умолчанию ВЫКЛ) ---
    "tg_approval": False,       # спрашивать подтверждение в Telegram перед подачей?
    "tg_pending": [],           # ждут ответа в TG [{job_id, message_id, ts}]
    "tg_offered_ids": [],       # уже отправляли карточку в TG — не дублируем
    "tg_skipped": [],           # пользователь нажал «Пропустить» — не предлагать снова
}

# Сколько событий держим в ленте монитора (старые отбрасываем).
EVENT_LOG_MAX = 80


def log_event(kind: str, text: str) -> None:
    """Дописать событие в ленту автопилота (для живого монитора на главной).

    kind: scan | submit | prepare | info — для иконки/цвета в интерфейсе.
    ts — ISO, чтобы фронт сам отформатировал «N мин назад».
    """
    try:
        r = get_rule()
        log = list(r.get("event_log") or [])
        log.insert(0, {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text})
        save_rule({"event_log": log[:EVENT_LOG_MAX]})
    except Exception:  # noqa: BLE001 — лента не должна ронять скан
        pass


def event_log() -> list:
    return get_rule().get("event_log") or []


def submitted_total() -> int:
    return int(get_rule().get("submitted_total") or 0)


def status() -> dict:
    """Сводка для живого монитора автопилота на главной (без полей,
    зависящих от процесса сервера — running/last_scan/next_scan их добавляет app.py).

    find_matches() зовём один раз и считаем всё от него (дешевле, чем
    match_count + pending_count по отдельности)."""
    r = get_rule()
    matches = find_matches()
    match_ids = {j.id for j in matches}
    prepared_ids = set(r.get("prepared_ids") or [])
    payload = {
        "enabled": bool(r.get("enabled")),
        "auto_submit": bool(r.get("auto_submit")),
        "mode": get_mode(),
        "found": len(matches),
        "prepared": len(match_ids & prepared_ids),   # из найденных уже подготовлено
        "pending": len(match_ids - prepared_ids),    # ждут подготовки
        "submitted_today": submitted_today(),
        "submitted_total": submitted_total(),
        "daily_limit": int(r.get("daily_limit") or 0),
        "submit_scope": r.get("submit_scope") or "new",
        "events": event_log()[:50],
    }
    payload.update({f"tg_{k}": v for k, v in tg_queue_stats().items()})
    return payload

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


def reset_tg_queue_for_filters() -> None:
    """Drop local Telegram offers that were built for old filters."""
    r = get_rule()
    if not (r.get("tg_pending") or r.get("tg_offered_ids")):
        return
    save_rule({"tg_pending": [], "tg_offered_ids": []})
    log_event("info", "TG: сбросил старую очередь после изменения фильтров")


# ── Единый режим работы (вместо трёх пересекающихся тумблеров) ──────────
# off → выключен; notify → только уведомлять; telegram → спрашивать в TG
# перед подачей; auto → автоотправка. Источник правды — поля enabled/
# auto_submit/tg_approval (остальной код читает их), а это просто удобный
# единый вид сверху.
MODES = ("off", "notify", "telegram", "auto")


def get_mode() -> str:
    r = get_rule()
    if not r.get("enabled"):
        return "off"
    if r.get("auto_submit"):
        return "auto"
    if r.get("tg_approval"):
        return "telegram"
    return "notify"


def set_mode(mode: str) -> str:
    mode = mode if mode in MODES else "off"
    patch = {
        "off":      {"enabled": False},
        "notify":   {"enabled": True, "auto_submit": False, "tg_approval": False},
        "telegram": {"enabled": True, "auto_submit": False, "tg_approval": True},
        "auto":     {"enabled": True, "auto_submit": True,  "tg_approval": False},
    }[mode]
    save_rule(patch)
    return mode


def within_schedule(rule: dict | None = None) -> bool:
    """Сейчас рабочее время автопилота? (для отправки карточек/подачи).
    from==to или диапазон 0..24 = круглосуточно. Поддерживает интервал через полночь."""
    r = rule or get_rule()
    a = int(r.get("active_from") or 0)
    b = int(r.get("active_to") or 24)
    if a == b or (a <= 0 and b >= 24):
        return True
    h = _dt.datetime.now().hour
    return a <= h < b if a < b else (h >= a or h < b)


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


def _keyword_hit(job: Job, raw: str) -> bool:
    """True, если хоть одно слово (через запятую) встречается в названии/описании/городе.
    Пусто = False. Без расширения синонимов — для исключений важна предсказуемость."""
    raw = (raw or "").strip()
    if not raw:
        return False
    hay = f"{job.title or ''} {job.description or ''} {job.city or ''}".lower()
    for phrase in (p.strip().lower() for p in raw.split(",") if p.strip()):
        if phrase and phrase in hay:
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


def _csv(rule: dict, key: str) -> list[str]:
    """Значение фильтра как список кодов (мультивыбор через запятую). Пусто = []."""
    return [x.strip() for x in str(rule.get(key) or "").split(",") if x.strip()]


def _nums(rule: dict, key: str) -> list[float]:
    """Числовой порог-мультивыбор через запятую → список значений > 0. Пусто = []."""
    out: list[float] = []
    for x in _csv(rule, key):
        try:
            f = float(x)
        except ValueError:
            continue
        if f > 0:
            out.append(f)
    return out


def _profile_matches(job: Job, rule: dict, home: dict | None) -> bool:
    """Подходит ли вакансия под ОДИН профиль (набор фильтров). `rule` здесь —
    это профиль (или легаси-правило целиком: поля те же)."""
    # бренд/категория/занятость/возраст — мультивыбор: подходит по ЛЮБОМУ из выбранных.
    brands = [labels.resolve(labels.BRANDS, b) or b for b in _csv(rule, "brand")]
    if brands and job.brand not in brands:
        return False
    cats = _csv(rule, "category")  # те же коды, что и фильтры на главной
    if cats and not (set(cats) & set((job.categories or "").split(","))):
        return False
    emps = _csv(rule, "employment_type")
    if emps and job.employment_type not in emps:
        return False
    ages = set(_csv(rule, "age"))
    # оба варианта (или ни одного) = без ограничения по возрасту
    if ages and ages != {"under18", "adult"}:
        if "under18" in ages and job.job_level != "employeeUnder18":
            return False
        if "adult" in ages and job.job_level == "employeeUnder18":
            return False
    # города — по подстроке (введёшь «København» — попадут все районы); пусто = любой.
    city_terms = [c.lower() for c in _csv(rule, "cities")]
    if city_terms:
        jc = (job.city or "").lower()
        if not any(t in jc for t in city_terms):
            return False
    # регионы — точный мультивыбор из данных (их немного). Пусто = любой.
    regions = set(_csv(rule, "regions"))
    if regions and (job.region or "") not in regions:
        return False
    # исключения «не предлагать»: бренд / город (подстрока) / слово в названии-описании
    ex_brands = [labels.resolve(labels.BRANDS, b) or b for b in _csv(rule, "exclude_brands")]
    if ex_brands and job.brand in ex_brands:
        return False
    ex_city_terms = [c.lower() for c in _csv(rule, "exclude_cities")]
    if ex_city_terms and any(t in (job.city or "").lower() for t in ex_city_terms):
        return False
    if _keyword_hit(job, rule.get("exclude_keywords")):
        return False
    # пороги км/часы/свежесть — мультивыбор через запятую: берём самый МЯГКИЙ
    # (наибольший радиус, наибольший срок, наименьшие часы). Пусто = без ограничения.
    # свежесть: берём только вакансии не старше N дней (если задано).
    # дату не знаем — НЕ отбрасываем (чтобы ничего не упустить).
    age_limits = _nums(rule, "max_age_days")
    if age_limits:
        max_age = max(age_limits)
        ad = _age_days(job)
        if ad is not None and ad > max_age:
            return False
    hour_limits = _nums(rule, "min_hours")
    if hour_limits:
        min_hours = min(hour_limits)
        jh = _job_hours(job)
        # часы не указаны в вакансии — НЕ отбрасываем (чтобы ничего не упустить),
        # отсекаем только если точно знаем, что меньше минимума
        if jh is not None and jh < min_hours:
            return False
    max_hour_limits = _nums(rule, "max_hours")
    if max_hour_limits:
        max_h = max(max_hour_limits)  # верхняя граница часов/нед (подработка)
        jh = _job_hours(job)
        if jh is not None and jh > max_h:
            return False
    km_limits = _nums(rule, "max_km")
    if km_limits and home:
        max_km = max(km_limits)
        if job.lat is None or job.lon is None:
            return False
        if geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon) > max_km:
            return False
    return _keyword_match(job, rule.get("keywords"))


# ── Несколько правил (профили подбора) ─────────────────────────────────
# Поля фильтра, из которых состоит один профиль.
_FILTER_FIELDS = ("max_km", "min_hours", "max_hours", "max_age_days", "category",
                  "employment_type", "age", "keywords", "brand", "cities", "regions",
                  "exclude_brands", "exclude_cities", "exclude_keywords")


def get_profiles(rule: dict | None = None) -> list[dict]:
    """Список профилей подбора. Если их ещё нет — синтезируем ОДИН из легаси-полей
    верхнего уровня (миграция-на-чтение), чтобы старая настройка продолжала работать."""
    r = rule or get_rule()
    profs = r.get("profiles")
    if profs:
        return profs
    legacy = {k: r.get(k, DEFAULT_RULE.get(k)) for k in _FILTER_FIELDS}
    legacy.update({"id": "default", "name": "Набор 1", "enabled": True})
    return [legacy]


def _new_profile(name: str = "Новый набор") -> dict:
    p = {k: DEFAULT_RULE.get(k) for k in _FILTER_FIELDS}
    p.update({"id": uuid.uuid4().hex[:8], "name": name, "enabled": True})
    return p


def ensure_profiles() -> list[dict]:
    """Гарантирует, что профили реально лежат в хранилище (а не только синтезируются).
    При первом вызове переносит легаси-поля в profiles[0]."""
    r = get_rule()
    if not r.get("profiles"):
        save_rule({"profiles": get_profiles(r)})
    profs = get_rule().get("profiles") or []
    changed = False
    for p in profs:
        if p.get("name") == "Правило 1":
            p["name"] = "Набор 1"
            changed = True
        elif p.get("name") == "Новое правило":
            p["name"] = "Новый набор"
            changed = True
    if changed:
        save_rule({"profiles": profs})
        profs = get_rule().get("profiles") or []
    return profs


def get_profile(pid: str) -> dict:
    profs = ensure_profiles()
    for p in profs:
        if p.get("id") == pid:
            return p
    return profs[0] if profs else _new_profile("Набор 1")


def add_profile(name: str = "Новый набор") -> str:
    profs = list(ensure_profiles())
    p = _new_profile(name)
    profs.append(p)
    save_rule({"profiles": profs})
    reset_tg_queue_for_filters()
    return p["id"]


def delete_profile(pid: str) -> None:
    profs = [p for p in ensure_profiles() if p.get("id") != pid]
    if not profs:                       # хотя бы один набор всегда остаётся
        profs = [_new_profile("Набор 1")]
    save_rule({"profiles": profs})
    reset_tg_queue_for_filters()


def rename_profile(pid: str, name: str) -> None:
    profs = ensure_profiles()
    for p in profs:
        if p.get("id") == pid:
            p["name"] = (name or "").strip() or p.get("name") or "Набор"
    save_rule({"profiles": profs})


def toggle_profile(pid: str) -> None:
    profs = ensure_profiles()
    for p in profs:
        if p.get("id") == pid:
            p["enabled"] = not p.get("enabled", True)
    save_rule({"profiles": profs})
    reset_tg_queue_for_filters()


def save_profile_filters(pid: str, fields: dict) -> None:
    """Записать поля-фильтры в выбранный профиль (создаёт профили при необходимости)."""
    profs = ensure_profiles()
    target = next((p for p in profs if p.get("id") == pid), None) or (profs[0] if profs else None)
    if target is None:
        target = _new_profile("Набор 1")
        profs.append(target)
    before = {k: target.get(k, DEFAULT_RULE.get(k)) for k in _FILTER_FIELDS}
    target.update({k: fields.get(k, target.get(k, DEFAULT_RULE.get(k))) for k in _FILTER_FIELDS})
    save_rule({"profiles": profs})
    after = {k: target.get(k, DEFAULT_RULE.get(k)) for k in _FILTER_FIELDS}
    if before != after:
        reset_tg_queue_for_filters()


def profile_match_count(p: dict) -> int:
    """Сколько активных вакансий подходит под ОДИН профиль (для подписи)."""
    home = settings_store.get_home()
    with get_session() as s:
        jobs = list(s.exec(select(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))).all())
    return sum(1 for j in jobs if _profile_matches(j, p, home))


def _matches(job: Job, rule: dict, home: dict | None) -> bool:
    """Вакансия подходит, если совпала хотя бы с ОДНИМ включённым профилем."""
    if job.status in ("closed", "hidden", "applied"):
        return False
    profs = [p for p in get_profiles(rule) if p.get("enabled", True)]
    if not profs:
        return False
    return any(_profile_matches(job, p, home) for p in profs)


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
    ids = list(ids)
    prepared = set(get_rule().get("prepared_ids") or [])
    prepared.update(ids)
    save_rule({"prepared_ids": list(prepared)})
    if ids:
        log_event("prepare", f"Подготовил анкет: {len(ids)}")


# ── Фаза 3: автоотправка (под замком) ──────────────────────────────────
def _today() -> str:
    return _dt.date.today().isoformat()


def _date_key(value) -> str:
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)[:10]


def _dedupe_ids(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def reconcile_submitted_state() -> dict:
    """Repair old optimistic counters: only applied jobs remain counted."""
    r = get_rule()
    ids = _dedupe_ids(r.get("submitted_ids") or [])
    day = _today()
    if not ids:
        patch = {}
        if r.get("submitted_ids"):
            patch["submitted_ids"] = []
        if int(r.get("submit_count_today") or 0) != 0:
            patch["submit_day"] = day
            patch["submit_count_today"] = 0
        if int(r.get("submitted_total") or 0) != 0:
            patch["submitted_total"] = 0
        if r.get("submit_log"):
            patch["submit_log"] = []
        return save_rule(patch) if patch else r

    with get_session() as s:
        jobs = list(s.exec(select(Job).where(Job.id.in_(ids))).all())
    by_id = {str(j.id): j for j in jobs if j and (j.status == "applied" or j.applied_at is not None)}
    valid_ids = [jid for jid in ids if jid in by_id]
    removed_count = max(0, len(ids) - len(valid_ids))
    today_count = sum(1 for jid in valid_ids if _date_key(by_id[jid].applied_at) == day)
    old_total = int(r.get("submitted_total") or 0)
    fixed_total = max(len(valid_ids), old_total - removed_count)

    patch = {}
    if ids != valid_ids:
        patch["submitted_ids"] = valid_ids
        valid_titles = {by_id[jid].title for jid in valid_ids}
        patch["submit_log"] = [
            entry for entry in (r.get("submit_log") or [])
            if entry.get("title") in valid_titles
        ][:50]
    if r.get("submit_day") != day or int(r.get("submit_count_today") or 0) != today_count:
        patch["submit_day"] = day
        patch["submit_count_today"] = today_count
    if old_total != fixed_total:
        patch["submitted_total"] = fixed_total
    return save_rule(patch) if patch else r


def submitted_today() -> int:
    """Сколько автоотправок сделано сегодня (счётчик сбрасывается в новый день)."""
    r = reconcile_submitted_state()
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
    done = set(rule.get("submitted_ids") or []) | set(rule.get("submitting_ids") or [])
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


def mark_submitting(ids) -> None:
    r = get_rule()
    current = _dedupe_ids(r.get("submitting_ids") or [])
    incoming = _dedupe_ids(ids)
    merged = _dedupe_ids(current + incoming)
    if merged != current:
        save_rule({"submitting_ids": merged[-500:]})


def clear_submitting(ids) -> None:
    remove = set(_dedupe_ids(ids))
    if not remove:
        return
    r = get_rule()
    current = _dedupe_ids(r.get("submitting_ids") or [])
    kept = [jid for jid in current if jid not in remove]
    if kept != current:
        save_rule({"submitting_ids": kept})


def record_submitted(jobs) -> None:
    r = get_rule()
    day = _today()
    count = int(r.get("submit_count_today") or 0) if r.get("submit_day") == day else 0
    log = list(r.get("submit_log") or [])
    done = set(r.get("submitted_ids") or [])
    new_jobs = [j for j in jobs if j and j.id not in done]
    if not new_jobs:
        return
    now = _dt.datetime.now().strftime("%d.%m %H:%M")
    for j in new_jobs:
        done.add(j.id)
        log.insert(0, {"ts": now, "title": j.title})
    total = int(r.get("submitted_total") or 0) + len(new_jobs)
    save_rule({
        "submit_day": day,
        "submit_count_today": count + len(new_jobs),
        "submit_log": log[:50],
        "submitted_ids": list(done),
        "submitted_total": total,
    })
    clear_submitting([j.id for j in new_jobs])
    titles = "; ".join(j.title for j in new_jobs[:5])
    log_event("submit", f"Подал заявок: {len(new_jobs)} — {titles}")


# ── Режим «по разрешению» через Telegram ───────────────────────────────
def _get_job(job_id: str):
    with get_session() as s:
        return s.get(Job, job_id)


def tg_pending_ids() -> set:
    return {p.get("job_id") for p in (get_rule().get("tg_pending") or [])}


def tg_pending_add(job_id: str, message_id) -> None:
    """Запомнить, что по вакансии отправлен запрос в TG и ждём ответа."""
    r = get_rule()
    pend = [p for p in (r.get("tg_pending") or []) if p.get("job_id") != job_id]
    pend.append({"job_id": job_id, "message_id": message_id,
                 "ts": _dt.datetime.now().isoformat(timespec="seconds")})
    offered = set(r.get("tg_offered_ids") or []); offered.add(job_id)
    save_rule({"tg_pending": pend[-100:], "tg_offered_ids": list(offered)[-500:]})


def tg_eligible(limit: int = 5, include_existing: bool = False) -> list[Job]:
    """Подходящие вакансии, которые ещё НЕ предлагали в TG и не подавали/не пропускали.
    Свежие первыми.

    include_existing=False — штатный безопасный режим: если охват «только новые»,
    текущий бэклог из baseline не шлём автоматически.
    include_existing=True — ручная кнопка «прислать текущие»: игнорирует baseline,
    но всё равно не дублирует уже предложенные/пропущенные/поданные.
    """
    r = get_rule()
    skip = (set(r.get("submitted_ids") or []) | set(r.get("tg_offered_ids") or [])
            | set(r.get("tg_skipped") or []) | set(r.get("submitting_ids") or []))
    if not include_existing and (r.get("submit_scope") or "new") != "all":
        skip |= set(r.get("autosubmit_baseline") or [])
    todo = [j for j in find_matches() if j.id not in skip]
    todo.sort(key=_seen_ts, reverse=True)
    return todo[: max(1, int(limit))]


def tg_queue_stats() -> dict:
    """Счётчики для интерфейса: почему «нашёл N», но в TG может ничего не уйти."""
    r = get_rule()
    return {
        "found": len(find_matches()),
        "pending": len(r.get("tg_pending") or []),
        "offered": len(r.get("tg_offered_ids") or []),
        "skipped": len(r.get("tg_skipped") or []),
        "baseline": len(r.get("autosubmit_baseline") or []),
        "eligible_new": len(tg_eligible(10000, include_existing=False)),
        "eligible_current": len(tg_eligible(10000, include_existing=True)),
    }


def tg_decide(job_id: str, approve: bool, launcher) -> str:
    """Ответ на карточку в Telegram. approve=True → подать (launcher), иначе пропустить.
    Возвращает короткий текст, которым перепишем сообщение в Telegram."""
    r = get_rule()
    pend = [p for p in (r.get("tg_pending") or []) if p.get("job_id") != job_id]
    save_rule({"tg_pending": pend})  # убрать из ожидающих в любом случае
    job = _get_job(job_id)
    title = (job.title if job else "вакансия")
    t = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if not approve:
        skipped = set(r.get("tg_skipped") or []); skipped.add(job_id)
        save_rule({"tg_skipped": list(skipped)[-1000:]})
        log_event("info", f"TG: пропущено — {title}")
        return f"❌ <b>Пропущено</b>\n{t}"
    latest = get_rule()
    if job_id in set(latest.get("submitted_ids") or []):
        return f"ℹ️ <b>Уже подавалось ранее</b>\n{t}"
    if job_id in set(latest.get("submitting_ids") or []):
        return f"ℹ️ <b>Подача уже запущена</b>\n{t}"
    if not job:
        return "⚠️ Вакансия больше недоступна."
    if not _matches(job, latest, settings_store.get_home()):
        log_event("info", f"TG: карточка устарела и не подходит под текущие фильтры — {title}")
        return f"⚠️ <b>Карточка устарела</b>\n{t}\n\nЭта вакансия больше не подходит под текущие фильтры."
    mark_submitting([job_id])
    try:
        launcher([job_id])
    except Exception as e:  # noqa: BLE001
        clear_submitting([job_id])
        log_event("info", f"TG: не смог запустить подачу — {title}: {e}")
        return f"⚠️ <b>Не смог запустить подачу</b>\n{t}"
    return f"✅ <b>Отправляю заявку…</b>\n{t}\n\nWexFlow заполнит форму и подаст за тебя."


def auto_submit_tick(launcher) -> None:
    """Вызывается после скана базы. Если автоотправка включена и есть дневной
    лимит — отправить до (лимит − сегодня) свежих подходящих. launcher(ids)
    делает реальную отправку. Логика отделена от запуска, чтобы её можно было
    проверить без настоящей подачи."""
    try:
        r = get_rule()
        if not (r.get("enabled") and r.get("auto_submit")):
            return
        if r.get("tg_approval"):
            return  # режим «по разрешению» главнее: тихую автоотправку не делаем
        if not within_schedule(r):
            return  # вне рабочих часов автопилота
        remaining = int(r.get("daily_limit") or 0) - submitted_today()
        # жёсткий потолок за один скан: даже при большом дневном лимите за раз
        # отправляем не больше MAX_PER_SCAN — ничего не «улетает пачкой».
        remaining = min(remaining, MAX_PER_SCAN)
        if remaining <= 0:
            return
        jobs = eligible_for_submit(remaining)
        if not jobs:
            return
        ids = [j.id for j in jobs]
        mark_submitting(ids)
        try:
            launcher(ids)
        except Exception:
            clear_submitting(ids)
            raise
        import scheduler
        scheduler.notify(f"Автопилот запустил подачу: {len(jobs)}",
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
        log_event("scan", f"Проверил базу: подходящих {len(matches)}"
                  + (f", из них новых {len(fresh)}" if fresh else ""))
        if fresh:
            import scheduler
            titles = "; ".join(j.title for j in fresh[:5])
            scheduler.notify(f"Автопилот: новых вакансий {len(fresh)}", titles)
    except Exception as e:  # noqa: BLE001 — автопилот не должен ронять обновление
        print(f"автопилот: ошибка скана — {e}")
