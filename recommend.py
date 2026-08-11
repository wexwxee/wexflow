"""Рекомендации: «сначала то, что подходит именно мне» — с причиной вслух.

Включается человеком (по умолчанию ВЫКЛючено) и **только сортирует**. Ни одна
вакансия отсюда не исчезает: прятать по своей догадке мы уже пробовали — так
из ленты пропали две тысячи вакансий, и разгребали это отдельным этапом.

Как считается. Обычная сумма понятных слагаемых, каждое из которых —
проверяемый факт из базы: расстояние от дома, реальное время в пути (из уже
посчитанного кэша, без сети), вердикт «возьмут без датского», часы в неделю,
свежесть, и история самого человека — куда он подавался, что пропускал, какие
роли открывал. У каждой рекомендации в интерфейсе стоит причина словами:
«рядом с домом (2.4 км) · 15 ч/нед · без датского».

Почему без ИИ и без обучения — решение, а не экономия:
  - **веса живут в коде, а не в настройках.** Настраиваемые веса никто никогда
    не настроит, а объяснить их человеку невозможно;
  - **на истории не учимся.** Двадцать-пятьдесят подач — это шум, а не выборка.
    «Обученный» порядок нельзя объяснить, и первое же необъяснимое место в
    списке убивает доверие ко всему приложению;
  - **вклад истории ограничен** (`HISTORY_CAP`), и в верхушке принудительно
    оставлены места без исторического вклада — иначе человек навсегда
    заперт в одной сети и одной роли.

ИИ здесь и так участвует — но раньше и честнее: вердикт `Job.fit` приходит от
`relevance`, где модель судит РОЛИ, а не вакансии. Переранжировать этим же ИИ
верхушку списка означало бы тратить квоту ради перестановки уже упорядоченного
и потерять объяснимость.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import labels
import settings_store

SETTINGS_KEY = "recommend"

# Веса подобраны так, чтобы «рядом и подходит» побеждало «далеко, но свежее».
# Меняешь число — меняешь порядок у всех: это правка кода со строкой в
# changelog, а не тумблер в настройках.
WEIGHTS = {
    "distance": 25.0,        # у самого дома → 25, на границе радиуса → 0
    "far": -10.0,            # дальше радиуса, но человек не отсёк его фильтром
    "transit_fast": 15.0,    # ≤30 минут реальной дорогой
    "transit_ok": 8.0,       # ≤45 минут
    "fit_ok": 20.0,          # «возьмут без датского» — сильный сигнал
    "fit_soft": -8.0,        # ИИ считает, что датский нужен (но не доказано)
    "hours": 12.0,           # попадает в желаемый коридор часов
    "fresh_today": 10.0,
    "fresh_3d": 6.0,
    "fresh_week": 2.0,
    "history_brand": 8.0,    # уже подавался в эту сеть
    "history_category": 8.0,
    "history_role": 10.0,    # уже подавался на такую же работу
    "seen_role": 4.0,        # открывал такие вакансии — слабый интерес
    "skipped": -25.0,        # осознанно пропускал такое
    "leadership": -30.0,
    "age_mismatch": -40.0,
}

# Потолок вклада истории. Без него человек навсегда заперт в одной сети.
HISTORY_CAP = 20.0
# Сколько мест в верхушке обязаны достаться вакансиям без исторического вклада.
FRESH_BLOOD_IN_TOP = 2
TOP_WINDOW = 10

DEFAULT_RADIUS_KM = 15.0
SKIP_REPEATS = 2          # разовый пропуск — случайность, два подряд — решение


@dataclass
class Context:
    """Всё, что нужно знать о человеке, посчитанное один раз на запрос."""

    home: dict | None = None
    radius_km: float = DEFAULT_RADIUS_KM
    hours_min: float = 0.0
    hours_max: float = 0.0
    age: str = ""                       # "" | "under18" | "adult"
    liked_brands: set = field(default_factory=set)
    liked_categories: set = field(default_factory=set)
    liked_roles: set = field(default_factory=set)
    skipped_brands: set = field(default_factory=set)
    skipped_roles: set = field(default_factory=set)
    seen_roles: set = field(default_factory=set)
    transit: dict = field(default_factory=dict)


def enabled() -> bool:
    """По умолчанию выключено: порядок ленты человек не просил менять."""
    return bool(settings_store.load().get(SETTINGS_KEY))


def set_enabled(on: bool) -> bool:
    value = bool(on)
    settings_store.mutate(lambda data: data.__setitem__(SETTINGS_KEY, value))
    return value


def build_context(session=None, *, home=None, rule=None) -> Context:
    """Собрать историю и настройки одним проходом по базе."""
    import autopilot
    import settings_store as store
    import transit
    from db import get_session

    rule = rule if rule is not None else autopilot.get_rule()
    home = home if home is not None else store.get_home()
    try:
        radius = float(rule.get("max_km") or 0) or DEFAULT_RADIUS_KM
    except (TypeError, ValueError):
        radius = DEFAULT_RADIUS_KM

    ctx = Context(
        home=home,
        radius_km=radius,
        hours_min=float(rule.get("min_hours") or 0),
        hours_max=float(rule.get("max_hours") or 0),
        age=str(rule.get("age") or ""),
        transit=transit.snapshot() if home else {},
    )

    try:
        if session is not None:
            _fill_history(session, ctx)
        else:
            with get_session() as fresh:
                _fill_history(fresh, ctx)
    except Exception:  # noqa: BLE001 — без истории рекомендации просто беднее
        pass
    return ctx


def _fill_history(session, ctx) -> None:
    """История подач и пропусков: что человек выбирал, а что отвергал."""
    import relevance
    from db import Application, Job, select

    rows = session.exec(
        select(Application.job_id, Application.state).where(
            Application.state.in_(["submitted", "skipped"])
        )
    ).all()
    submitted = {job_id for job_id, state in rows if state == "submitted"}
    skipped_count: dict[str, int] = {}
    for job_id, state in rows:
        if state == "skipped":
            skipped_count[job_id] = skipped_count.get(job_id, 0) + 1
    wanted = submitted | set(skipped_count)
    if not wanted:
        return
    jobs = session.exec(select(Job).where(Job.id.in_(list(wanted)))).all()
    skip_brand_hits: dict[str, int] = {}
    skip_role_hits: dict[str, int] = {}
    for job in jobs:
        role = relevance.role_key(job)
        if job.id in submitted:
            if job.brand:
                ctx.liked_brands.add(str(job.brand))
            for code in str(job.categories or "").split(","):
                if code.strip():
                    ctx.liked_categories.add(code.strip())
            ctx.liked_roles.add(role)
        else:
            if job.brand:
                skip_brand_hits[str(job.brand)] = skip_brand_hits.get(str(job.brand), 0) + 1
            skip_role_hits[role] = skip_role_hits.get(role, 0) + 1
    ctx.skipped_brands = {b for b, n in skip_brand_hits.items() if n >= SKIP_REPEATS}
    ctx.skipped_roles = {r for r, n in skip_role_hits.items() if n >= SKIP_REPEATS}
    # «Смотрел такое» — слабый след интереса: статус seen ставится при открытии
    # карточки. Берём роли, а не сами вакансии: интересна работа, а не строка.
    seen = session.exec(select(Job).where(Job.status == "seen")).all()
    ctx.seen_roles = {relevance.role_key(j) for j in seen}


def _hours_of(job):
    import autopilot
    return autopilot._job_hours(job)


def score(job, ctx, *, distance_km=None, trip=None) -> tuple[float, list[str]]:
    """Оценка вакансии и причины — только те, что реально сработали."""
    import autopilot
    import geo
    import relevance
    from db import utcnow

    points = 0.0
    reasons: list[tuple[float, str]] = []
    history = 0.0

    km = distance_km
    if km is None and ctx.home and job.lat is not None and job.lon is not None:
        km = round(geo.haversine_km(ctx.home["lat"], ctx.home["lon"], job.lat, job.lon), 1)
    if km is not None:
        if km <= ctx.radius_km:
            gain = WEIGHTS["distance"] * (1 - km / max(ctx.radius_km, 0.1))
            points += gain
            if gain >= WEIGHTS["distance"] / 3:
                reasons.append((gain, f"рядом с домом ({km:g} км)"))
        else:
            points += WEIGHTS["far"]

    minutes = None
    if trip:
        minutes = trip.get("minutes") if isinstance(trip, dict) else None
    if minutes is not None:
        if minutes <= 30:
            points += WEIGHTS["transit_fast"]
            reasons.append((WEIGHTS["transit_fast"], f"{minutes} мин на транспорте"))
        elif minutes <= 45:
            points += WEIGHTS["transit_ok"]
            reasons.append((WEIGHTS["transit_ok"], f"{minutes} мин на транспорте"))

    verdict = str(getattr(job, "fit", "") or "")
    if verdict == relevance.OK:
        points += WEIGHTS["fit_ok"]
        reasons.append((WEIGHTS["fit_ok"], "возьмут без датского"))
    elif relevance.soft_barrier(job):
        points += WEIGHTS["fit_soft"]

    hours = _hours_of(job)
    if hours is not None and (ctx.hours_min or ctx.hours_max):
        low = ctx.hours_min or 0
        high = ctx.hours_max or 10_000
        if low <= hours <= high:
            points += WEIGHTS["hours"]
            reasons.append((WEIGHTS["hours"], f"{hours:g} ч/нед"))

    first_seen = getattr(job, "first_seen", None)
    if first_seen is not None:
        try:
            days = (utcnow() - first_seen).days
        except Exception:  # noqa: BLE001
            days = None
        if days is not None:
            if days <= 0:
                points += WEIGHTS["fresh_today"]
                reasons.append((WEIGHTS["fresh_today"], "появилась сегодня"))
            elif days <= 3:
                points += WEIGHTS["fresh_3d"]
                reasons.append((WEIGHTS["fresh_3d"], "новая за 3 дня"))
            elif days <= 7:
                points += WEIGHTS["fresh_week"]

    role = relevance.role_key(job)
    brand = str(getattr(job, "brand", "") or "")
    if brand and brand in ctx.liked_brands:
        history += WEIGHTS["history_brand"]
        reasons.append((WEIGHTS["history_brand"], f"ты уже подавался в {labels.brand(brand)}"))
    if any(code.strip() in ctx.liked_categories
           for code in str(getattr(job, "categories", "") or "").split(",") if code.strip()):
        history += WEIGHTS["history_category"]
    if role in ctx.liked_roles:
        history += WEIGHTS["history_role"]
        reasons.append((WEIGHTS["history_role"], "такую работу ты уже выбирал"))
    elif role in ctx.seen_roles:
        history += WEIGHTS["seen_role"]
    points += min(history, HISTORY_CAP)

    if brand in ctx.skipped_brands or role in ctx.skipped_roles:
        points += WEIGHTS["skipped"]
    if labels.is_leadership(str(getattr(job, "title", "") or "")):
        points += WEIGHTS["leadership"]
    if ctx.age:
        under = autopilot.job_is_under18(job)
        if (ctx.age == "under18") != under:
            points += WEIGHTS["age_mismatch"]

    reasons.sort(key=lambda pair: -pair[0])
    return round(points, 2), [text for _weight, text in reasons[:3]]


def rank(jobs, ctx, *, distances=None, trips=None) -> list[tuple]:
    """Отсортировать: сильные наверх, при равенстве — свежие первыми.

    Стабильность важна не меньше порядка: список, который прыгает при каждом
    обновлении страницы, человек перестаёт читать.
    """
    distances = distances or {}
    trips = trips or {}
    scored = []
    for index, job in enumerate(jobs):
        value, reasons = score(job, ctx, distance_km=distances.get(job.id),
                               trip=trips.get(job.id))
        scored.append((job, value, reasons, index))
    scored.sort(key=lambda row: (-row[1], row[3]))
    ordered = [(job, value, reasons) for job, value, reasons, _ in scored]
    return _mix_in_fresh_blood(ordered, ctx)


def _mix_in_fresh_blood(ordered, ctx) -> list[tuple]:
    """Оставить в верхушке места для того, чего человек ещё не пробовал."""
    import relevance

    if len(ordered) <= TOP_WINDOW or not (ctx.liked_brands or ctx.liked_roles):
        return ordered
    top = ordered[:TOP_WINDOW]
    known = [row for row in top
             if str(getattr(row[0], "brand", "") or "") in ctx.liked_brands
             or relevance.role_key(row[0]) in ctx.liked_roles]
    if len(top) - len(known) >= FRESH_BLOOD_IN_TOP:
        return ordered
    need = FRESH_BLOOD_IN_TOP - (len(top) - len(known))
    rest = ordered[TOP_WINDOW:]
    newcomers = [row for row in rest
                 if str(getattr(row[0], "brand", "") or "") not in ctx.liked_brands
                 and relevance.role_key(row[0]) not in ctx.liked_roles][:need]
    if not newcomers:
        return ordered
    drop = {id(row[0]) for row in known[-len(newcomers):]}
    head = [row for row in top if id(row[0]) not in drop] + newcomers
    tail = [row for row in ordered[TOP_WINDOW:] if row not in newcomers]
    tail += [row for row in top if id(row[0]) in drop]
    return head + tail


def explain(job, ctx, *, distance_km=None, trip=None) -> dict:
    """Полный разбор оценки — для подсказки «почему это наверху»."""
    value, reasons = score(job, ctx, distance_km=distance_km, trip=trip)
    return {"score": value, "reasons": reasons}
