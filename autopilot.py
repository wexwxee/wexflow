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

import applications
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
    "event_log": [],            # лента событий автопилота [{ts,kind,text}] (для монитора)
    # --- режим «по разрешению» через Telegram (по умолчанию ВЫКЛ) ---
    "tg_approval": False,       # спрашивать подтверждение в Telegram перед подачей?
    "tg_pending": [],           # ждут ответа в TG [{job_id, message_id, ts}] (переходное состояние)
    "tg_day": "",               # день, за который считаем карточки (дневной потолок)
    "tg_sent_today": 0,         # сколько карточек автопилот сам отправил сегодня
    "tg_cap_day": "",           # день, когда уже писали в журнал про достигнутый потолок
    "tg_digest": False,         # дайджест раз в день вместо потока карточек
    "tg_digest_day": "",        # день, за который дайджест уже отправлен
    # ЛЕГАСИ (шаг 3 плана): факты «подано/отправляется/предложено/пропущено»
    # переехали в таблицу application (см. applications.py). Ключи оставлены,
    # чтобы старые settings.json читались; applications.ensure_migrated()
    # один раз переносит их в базу и очищает.
    "lists_migrated_to_db": False,
    "submitted_ids": [],
    "submitting_ids": [],
    "submit_day": "",
    "submit_count_today": 0,
    "submit_log": [],
    "submitted_total": 0,
    "tg_offered_ids": [],
    "tg_skipped": [],
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


# Префиксы событий вида «<префикс> — <название вакансии>», серии которых можно
# честно свернуть в «— по N вакансиям». Другие тексты с тире (например
# «TG: не отправилось — <ошибка>») так сворачивать нельзя — исказится смысл.
_GROUPABLE_PREFIXES = (
    "TG: спросил разрешение",
    "TG-панель: добавил вакансию",
    "TG: пропущено",
    "TG: карточка устарела и не подходит под текущие фильтры",
)


def grouped_events(log: list, min_run: int = 3) -> list:
    """Сжимает серии однотипных событий подряд для монитора.

    Ночной скан может дать 40+ строк «TG: спросил разрешение — <вакансия>»
    подряд — журнал становится нечитаем. Серию из min_run и больше событий
    с одинаковым началом (текст до « — ») сворачиваем в одну строку с
    количеством, дословные повторы — в «текст · ×N»; ts берём от самого
    свежего события серии. Исходный event_log не меняется — только вид
    для интерфейса."""
    out: list = []
    i = 0
    while i < len(log):
        ev = log[i]
        text = str(ev.get("text") or "")
        prefix = text.split(" — ")[0]
        j = i
        while (j + 1 < len(log)
               and log[j + 1].get("kind") == ev.get("kind")
               and str(log[j + 1].get("text") or "").split(" — ")[0] == prefix):
            j += 1
        n = j - i + 1
        if n >= min_run and all(str(e.get("text") or "") == text for e in log[i:j + 1]):
            # дословные повторы («Проверил базу: подходящих 122» × 9) — одной строкой
            out.append({"ts": ev.get("ts"), "kind": ev.get("kind"),
                        "text": f"{text} · ×{n}"})
        elif n >= min_run and prefix in _GROUPABLE_PREFIXES:
            word = labels.plural(n, "вакансии", "вакансиям", "вакансиям")
            out.append({"ts": ev.get("ts"), "kind": ev.get("kind"),
                        "text": f"{prefix} — по {n} {word}"})
        else:
            out.extend(log[i:j + 1])
        i = j + 1
    return out


def submitted_total() -> int:
    return applications.submitted_total_count()


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
        "events": grouped_events(event_log())[:50],
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
DEFAULT_HOME_RADIUS_KM = 15  # радиус не выбран, а дом задан → ищем в этом радиусе;
                             # «вся страна» — только явным выбором (max_km="all")
TG_DAILY_MAX = 15         # потолок карточек-запросов в Telegram за календарный день —
                          # чтобы чат не превращался во второй почтовый ящик
TG_PENDING_TTL_DAYS = 3   # карточка без ответа столько дней — снимаем с ожидания


def get_rule() -> dict:
    rule = dict(DEFAULT_RULE)
    rule.update(settings_store.load().get("autopilot", {}) or {})
    return rule


def save_rule(patch: dict) -> dict:
    """Обновить правило частично (мерж), вернуть итоговое правило.

    Идёт через settings_store.mutate — атомарное чтение-изменение-запись под общим
    замком, чтобы параллельные правки из разных потоков (seen_ids из скана,
    submitting_ids из автоотправки, tg_pending из Telegram) не теряли друг друга."""
    def _m(data):
        rule = dict(DEFAULT_RULE)
        rule.update(data.get("autopilot", {}) or {})
        rule.update(patch)
        data["autopilot"] = rule
    return settings_store.mutate(_m)["autopilot"]


def reset_tg_queue_for_filters() -> None:
    """Смена фильтров: снять ожидающие карточки и очистить облачную панель.

    «Предложено» в реестре НЕ трогаем — гейт F27: предложено — навсегда.
    Раньше здесь стоял clear_offers(), который забывал все нерешённые
    предложения; из-за этого каждая смена фильтров отправляла те же самые
    вакансии в Telegram заново — «по кругу одни и те же». Вакансии, ставшие
    подходящими при НОВЫХ фильтрах, и так уйдут сами: их ещё не предлагали,
    и tg_eligible пропустит их без сброса истории."""
    r = get_rule()
    had_local = bool(r.get("tg_pending"))
    if had_local:
        save_rule({"tg_pending": []})
    cleared_cloud = False
    try:
        import cloud_auth
        cleared_cloud = cloud_auth.clear_panel(timeout=3)
    except Exception:  # noqa: BLE001
        cleared_cloud = False
    if had_local or cleared_cloud:
        log_event("info", "TG: снял ожидающие карточки после изменения фильтров")


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
    # Страховка: автопилот никогда не предлагает/не подаёт на руководящие роли.
    # Поле job_level от Salling недостоверно (Souschef и пр. помечены "employee"),
    # поэтому отсекаем по названию. Автопилот рассчитан на рядовые позиции.
    if labels.is_leadership(job.title):
        return False
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
        # под-18 определяем не только по полю job_level: часть вакансий приходит
        # без employeeUnder18, но с «under 18 år» в названии/описании — иначе они
        # протекают сквозь фильтр «от 18».
        _age_hay = f"{job.title or ''} {job.description or ''}".lower()
        is_under18 = (job.job_level == "employeeUnder18") or bool(re.search(r"under\s*-?\s*18", _age_hay))
        if "under18" in ages and not is_under18:
            return False
        if "adult" in ages and is_under18:
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
    elif default_radius_applies(rule, home):
        # Радиус не выбран вовсе → мягкий дефолт: без него набор «категория и всё»
        # предлагал вакансии за 300 км от дома и заливал Telegram. Мягкий — значит
        # вакансии без координат НЕ отбрасываем (в отличие от явного радиуса).
        if job.lat is not None and job.lon is not None:
            if geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon) > DEFAULT_HOME_RADIUS_KM:
                return False
    return _keyword_match(job, rule.get("keywords"))


def default_radius_applies(rule: dict, home: dict | None) -> bool:
    """Дефолтный радиус включается, только когда у профиля нет НИКАКОГО указания
    места: ни радиуса (в т.ч. явного «вся Дания» = max_km="all"), ни городов,
    ни регионов — и при этом дом задан."""
    return bool(home) and not _csv(rule, "max_km") \
        and not _csv(rule, "cities") and not _csv(rule, "regions")


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
        jobs = list(s.exec(select(Job).where(
            Job.status.not_in(["closed", "hidden", "applied"]),
            Job.applied_at.is_(None),
        )).all())
    return sum(1 for j in jobs if _profile_matches(j, p, home))


def _matches(job: Job, rule: dict, home: dict | None) -> bool:
    """Вакансия подходит, если совпала хотя бы с ОДНИМ включённым профилем."""
    if job.status in ("closed", "hidden", "applied"):
        return False
    # ATS-коннекторы пока работают в безопасном assisted-режиме: форма
    # заполняется и останавливается перед отправкой. Не передаём такие вакансии
    # в Salling auto-submit/Telegram submit worker.
    if getattr(job, "source", "salling") != "salling":
        return False
    # «Подавали хоть раз» (applied_at заполнен) — навсегда исключаем из автопилота,
    # даже если статус ушёл вперёд по воронке (interview/offer/rejected). Иначе
    # после подачи и перевода в «Собеседование» вакансия снова стала бы «подходящей»
    # и тихий автопилот подал бы на неё повторно. applied_at — нерушимая правда.
    if job.applied_at is not None:
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
            s.exec(select(Job).where(
                Job.status.not_in(["closed", "hidden", "applied"]),
                Job.applied_at.is_(None),
            )).all()
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
def _dedupe_ids(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def submitted_today() -> int:
    """Сколько автоотправок сделано сегодня. Вычисляется из реестра заявок —
    прежний ручной счётчик с «ремонтом» (reconcile) больше не нужен: строкам
    таблицы, в отличие от списка в settings.json, нечего терять."""
    return applications.submitted_today_count()


def submit_log() -> list:
    return applications.submit_log()


def set_autosubmit_baseline() -> None:
    """Запомнить текущие совпадения как «не трогать» — чтобы при включении
    автоотправка не разослала разом весь существующий список, а ждала НОВЫЕ."""
    save_rule({"autosubmit_baseline": [j.id for j in find_matches()]})


def _eligible_all(rule: dict) -> list[Job]:
    """Подходящие, которые автоотправка ещё НЕ подавала, с учётом охвата:
    - scope=new (по умолчанию): только появившиеся ПОСЛЕ включения (нет в baseline);
    - scope=all: все подходящие сейчас (baseline игнорируется).
    Уже отправленные ботом исключаются всегда. Свежие — первыми."""
    done = applications.submitted_ids() | applications.submitting_ids()
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


def mark_submitting(ids, origin: str = "autopilot") -> None:
    """Пометить в реестре: подача запущена (браузер пошёл заполнять)."""
    applications.mark_submitting(ids, origin=origin)


def clear_submitting(ids) -> None:
    """Подача не подтвердилась: строки переходят в failed. Автоотправка сможет
    попробовать снова (как и раньше), а в TG повторно не предложим."""
    applications.mark_failed(ids)


def record_submitted(jobs) -> None:
    """Зафиксировать реально поданные в реестре. Счётчики (сегодня/всего/журнал)
    вычисляются из реестра — им больше нечего терять. Идемпотентно."""
    fresh = applications.record_submitted(jobs)
    if fresh:
        titles = "; ".join(j.title for j in fresh[:5])
        log_event("submit", f"Подал заявок: {len(fresh)} — {titles}")


# ── Режим «по разрешению» через Telegram ───────────────────────────────
def _get_job(job_id: str):
    with get_session() as s:
        return s.get(Job, job_id)


def tg_pending_ids() -> set:
    return {p.get("job_id") for p in (get_rule().get("tg_pending") or [])}


# ── Дневной потолок карточек (чтобы чат не заливало) ────────────────────
def _today_str() -> str:
    return _dt.date.today().isoformat()


def tg_sent_today() -> int:
    """Сколько карточек автопилот САМ отправил сегодня (ручные кнопки не считаем)."""
    r = get_rule()
    return int(r.get("tg_sent_today") or 0) if r.get("tg_day") == _today_str() else 0


def tg_daily_remaining() -> int:
    return max(0, TG_DAILY_MAX - tg_sent_today())


def tg_note_sent(n: int) -> None:
    """Учесть n автоматически отправленных карточек (с переходом через полночь)."""
    if n <= 0:
        return
    today = _today_str()
    base = tg_sent_today()
    save_rule({"tg_day": today, "tg_sent_today": base + int(n)})


def tg_log_cap_once(waiting: int) -> None:
    """Записать в журнал про достигнутый дневной потолок — не чаще раза в день
    (скан идёт каждые 3 минуты, иначе журнал зальёт одной и той же строкой)."""
    today = _today_str()
    if get_rule().get("tg_cap_day") == today:
        return
    save_rule({"tg_cap_day": today})
    log_event("info", f"TG: дневной потолок карточек ({TG_DAILY_MAX}) достигнут — "
                      f"ещё подходят {waiting}, пришлю завтра (или открой панель)")


# ── Дайджест: одно сообщение в день вместо потока карточек ─────────────
def tg_digest_enabled() -> bool:
    return bool(get_rule().get("tg_digest"))


def set_tg_digest(on: bool) -> None:
    save_rule({"tg_digest": bool(on)})
    log_event("info", f"TG: дайджест {'включён — раз в день одно сообщение' if on else 'выключен — снова карточки'}")


def tg_digest_due() -> bool:
    """Дайджест за сегодня ещё не отправляли?"""
    return get_rule().get("tg_digest_day") != _today_str()


def tg_digest_mark_sent() -> None:
    save_rule({"tg_digest_day": _today_str()})


def _esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_digest_text(jobs, home: dict | None = None, max_titles: int = 5) -> str:
    """Текст дневного дайджеста (чистая функция — легко тестировать).
    jobs — новые подходящие вакансии (объекты Job)."""
    n = len(jobs)
    lines = [f"🔎 <b>WexFlow: новых подходящих вакансий — {n}</b>"]
    for j in jobs[:max_titles]:
        bits = [_esc_html(j.title or "Вакансия")]
        if getattr(j, "city", None):
            bits.append(_esc_html(j.city))
        if home and getattr(j, "lat", None) is not None and getattr(j, "lon", None) is not None:
            km = geo.haversine_km(home["lat"], home["lon"], j.lat, j.lon)
            bits.append(f"~{round(km)} км")
        lines.append("• " + " · ".join(bits))
    if n > max_titles:
        lines.append(f"…и ещё {n - max_titles}.")
    lines.append("")
    lines.append("Открой панель, чтобы посмотреть и подать. Карточки в чат не приходят — включён дайджест.")
    return "\n".join(lines)


def tg_pending_clear_all() -> int:
    """Снять с ожидания ВСЕ карточки разом (кнопка в настройках). В реестре они
    остаются «предложенными» — повторно не пришлём; поздний ✅ по карточке из
    чата всё равно пройдёт проверку актуальности в tg_decide."""
    r = get_rule()
    pend = list(r.get("tg_pending") or [])
    if not pend:
        return 0
    save_rule({"tg_pending": []})
    log_event("info", f"TG: снял с ожидания все карточки — {len(pend)} (по кнопке)")
    return len(pend)


def tg_pending_expire(days: int = TG_PENDING_TTL_DAYS) -> int:
    """Снять с ожидания карточки, на которые не ответили N дней. Они остаются
    «предложенными» в реестре (повторно не пришлём), а поздний ✅ по старой
    карточке всё равно пройдёт проверку актуальности в tg_decide."""
    r = get_rule()
    pend = list(r.get("tg_pending") or [])
    if not pend:
        return 0
    cutoff = _dt.datetime.now() - _dt.timedelta(days=days)
    keep, dropped = [], 0
    for p in pend:
        try:
            ts = _dt.datetime.fromisoformat(str(p.get("ts") or ""))
        except ValueError:
            ts = None
        if ts is not None and ts < cutoff:
            dropped += 1
        else:
            keep.append(p)
    if dropped:
        save_rule({"tg_pending": keep})
        log_event("info", f"TG: снял с ожидания карточки без ответа — {dropped} (старше {days} дн)")
    return dropped


def tg_pending_add(job_id: str, message_id) -> None:
    """Запомнить, что по вакансии отправлен запрос в TG и ждём ответа."""
    r = get_rule()
    pend = [p for p in (r.get("tg_pending") or []) if p.get("job_id") != job_id]
    pend.append({"job_id": job_id, "message_id": message_id,
                 "ts": _dt.datetime.now().isoformat(timespec="seconds")})
    save_rule({"tg_pending": pend[-100:]})
    applications.mark_offered(job_id)   # гейт F27: «предложено» — навсегда в реестре


def tg_eligible(limit: int = 5, include_existing: bool = False) -> list[Job]:
    """Подходящие вакансии, которые ещё НЕ предлагали в TG и не подавали/не пропускали.
    Свежие первыми.

    include_existing=False — штатный безопасный режим: если охват «только новые»,
    текущий бэклог из baseline не шлём автоматически.
    include_existing=True — ручная кнопка «прислать текущие»: игнорирует baseline,
    но всё равно не дублирует уже предложенные/пропущенные/поданные.
    """
    r = get_rule()
    skip = (applications.submitted_ids() | applications.offered_ids()
            | applications.skipped_ids() | applications.submitting_ids())
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
        "offered": len(applications.offered_ids()),
        "skipped": len(applications.skipped_ids()),
        "baseline": len(r.get("autosubmit_baseline") or []),
        "eligible_new": len(tg_eligible(10000, include_existing=False)),
        "eligible_current": len(tg_eligible(10000, include_existing=True)),
        "sent_today": tg_sent_today(),
        "daily_max": TG_DAILY_MAX,
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
        applications.mark_skipped(job_id)
        log_event("info", f"TG: пропущено — {title}")
        return f"❌ <b>Пропущено</b>\n{t}"
    latest = get_rule()
    state = applications.state_of(job_id)
    if state == "submitted":
        return f"ℹ️ <b>Уже подавалось ранее</b>\n{t}"
    if state == "submitting":
        return f"ℹ️ <b>Подача уже запущена</b>\n{t}"
    if not job:
        return "⚠️ Вакансия больше недоступна."
    if not _matches(job, latest, settings_store.get_home()):
        log_event("info", f"TG: карточка устарела и не подходит под текущие фильтры — {title}")
        return f"⚠️ <b>Карточка устарела</b>\n{t}\n\nЭта вакансия больше не подходит под текущие фильтры."
    mark_submitting([job_id], origin="telegram")
    try:
        launcher([job_id])
    except Exception as e:  # noqa: BLE001
        clear_submitting([job_id])
        log_event("info", f"TG: не смог запустить подачу — {title}: {e}")
        return f"⚠️ <b>Не смог запустить подачу</b>\n{t}"
    return f"✅ <b>Отправляю заявку…</b>\n{t}\n\nWexFlow заполнит форму и подаст за тебя."


def partition_offered(job_ids, offered_ids):
    """Разделить id на (offered, not_offered): подаём ТОЛЬКО то, что сами предлагали.

    Защита F27: реальную (необратимую) подачу запускает решение из облака. Мы
    честим только те вакансии, карточки которых приложение само отправляло
    пользователю (реестр заявок хранит offered_at). Решение по «непредложенной»
    вакансии (сбой/подмена в облаке) сюда не попадёт. Чистая функция, дублирует
    dedupe и отсеивает пустые id. Порядок сохраняется."""
    offered = set(offered_ids or [])
    known, unknown, seen = [], [], set()
    for jid in job_ids or []:
        jid = str(jid or "").strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        (known if jid in offered else unknown).append(jid)
    return known, unknown


def tg_submit_batch(job_ids, launcher) -> dict:
    """Start one submit worker for many Telegram Mini App decisions.

    The single-card tg_decide path intentionally launches one id at a time. The
    Mini App can send many submit decisions in one poll, so starting one browser
    worker per id would make those workers fight over the same browser profile.
    """
    ids = _dedupe_ids(job_ids)
    if not ids:
        return {"started": [], "skipped": []}

    r = get_rule()
    # F27: подаём только вакансии, которые приложение само показывало —
    # карточкой (offered) или списком в панели (listed, jobs_sync). Решение
    # из облака по «непоказанной» вакансии отклоняем (сбой/подмена).
    ids, not_offered = partition_offered(
        ids, applications.offered_ids() | applications.listed_ids()
    )
    skipped: list[dict] = [
        {"job_id": jid, "state": "failed", "reason": "not_offered", "title": ""}
        for jid in not_offered
    ]
    if not_offered:
        log_event("info", f"TG: отклонено решений по непредложенным вакансиям — {len(not_offered)}")
    if not ids:
        return {"started": [], "skipped": skipped}

    pending_ids = set(ids)
    pend = [p for p in (r.get("tg_pending") or []) if p.get("job_id") not in pending_ids]
    save_rule({"tg_pending": pend})

    latest = get_rule()
    home = settings_store.get_home()
    submitted = applications.submitted_ids()
    submitting = applications.submitting_ids()
    started: list[str] = []
    started_jobs: list[Job] = []

    for job_id in ids:
        job = _get_job(job_id)
        title = job.title if job else "vacancy"
        if job_id in submitted:
            skipped.append({
                "job_id": job_id, "state": "submitted",
                "reason": "already_submitted", "title": title,
            })
        elif job is not None and (job.status == "applied" or job.applied_at is not None):
            skipped.append({
                "job_id": job_id, "state": "submitted",
                "reason": "already_submitted", "title": title,
            })
        elif job_id in submitting:
            skipped.append({
                "job_id": job_id, "state": "submitting",
                "reason": "already_submitting", "title": title,
            })
        elif not job:
            skipped.append({
                "job_id": job_id, "state": "failed",
                "reason": "missing", "title": title,
            })
        elif not _matches(job, latest, home):
            log_event("info", f"TG: карточка устарела и не подходит под текущие фильтры — {title}")
            skipped.append({
                "job_id": job_id, "state": "failed",
                "reason": "stale", "title": title,
            })
        else:
            started.append(job_id)
            started_jobs.append(job)

    if not started:
        return {"started": [], "skipped": skipped}

    mark_submitting(started, origin="telegram")
    try:
        launcher(started)
    except Exception as e:  # noqa: BLE001
        clear_submitting(started)
        for job in started_jobs:
            skipped.append({
                "job_id": job.id, "state": "failed",
                "reason": "launch_error", "title": job.title,
            })
        log_event("info", f"TG: не смог запустить пакетную подачу: {e}")
        return {"started": [], "skipped": skipped, "error": str(e)[:160]}

    titles = "; ".join(j.title for j in started_jobs[:5])
    log_event("submit", f"TG: запущена пакетная подача: {len(started)} — {titles}")
    return {"started": started, "skipped": skipped}


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
        seen_list = list(rule.get("seen_ids") or [])
        seen = set(seen_list)
        fresh = [j for j in matches if j.id not in seen]
        # Просмотренные КОПИМ, а не перезаписываем текущим набором: вакансия,
        # мигнувшая из выдачи (сузили фильтр, задержка расстояния) и вернувшаяся,
        # не должна считаться «новой» повторно. Чтобы список не рос вечно,
        # выбрасываем id, которых больше нет в базе, и держим потолок.
        with get_session() as s:
            existing = set(s.exec(select(Job.id)).all())
        merged = [i for i in seen_list if i in existing] + [i for i in ids if i not in seen]
        save_rule({"seen_ids": merged[-4000:]})
        # В ленту пишем только событие с НОВЫМИ совпадениями: при скане каждые
        # 3 минуты записи «проверил базу, ничего нового» вытесняли из журнала
        # (EVENT_LOG_MAX) реальные подачи и решения за считанные часы. Время
        # последней проверки монитор берёт из last_scan, а не из ленты.
        if fresh:
            log_event("scan", f"Проверил базу: подходящих {len(matches)}, из них новых {len(fresh)}")
            import scheduler
            titles = "; ".join(j.title for j in fresh[:5])
            scheduler.notify(f"Автопилот: новых вакансий {len(fresh)}", titles)
    except Exception as e:  # noqa: BLE001 — автопилот не должен ронять обновление
        print(f"автопилот: ошибка скана — {e}")
