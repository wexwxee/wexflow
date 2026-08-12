"""«Здесь ничего нет — зато рядом есть»: подсказки вместо пустого экрана.

Зачем. Человеку советуют конкретный магазин: «иди в Netto на Waterfront, там
берут». Он ищет этот магазин, а там сейчас три вакансии «только до 18 лет» и
две руководящие — то есть для него пусто. Приложение молчало, и это читается
как «работы нет». Неправда: в двух соседних Netto того же города в тот момент
было шесть подходящих вакансий.

Что делает модуль. По опорной точке (названный магазин, город или дом) даёт
три слоя подсказок, от самого точного к самому широкому:
  1. тот же магазин рядом — главный случай, ради него всё и написано;
  2. тот же город, другие магазины — когда сеть в городе закончилась;
  3. та же работа рядом — по ключу роли, который уже посчитан для вердиктов
     о языке (`relevance.role_key`), то есть «такая же работа», а не похожее
     название.

Чего модуль не делает — намеренно:
  - **не ходит в сеть.** Расстояние — `geo.haversine_km`, обычная математика
    по уже сохранённым координатам. Подсказка не имеет права ждать чужой
    сервис;
  - **не зовёт ИИ.** Тут нечего решать моделью: есть координаты и адрес;
  - **не советует то, что лента прячет.** Если вакансию не показывают из-за
    доказанного языкового барьера, она не всплывёт и в подсказке — иначе
    получится «вот тебе вариант», по которому нельзя подать;
  - **не врёт про причину.** Почему в опорном магазине пусто, считается теми
    же функциями, что и сами фильтры (`autopilot.job_is_under18`,
    `labels.is_leadership`, `relevance.is_barrier`), а не отдельной догадкой.
"""
from __future__ import annotations

import geo
import labels

# Радиус «рядом». По умолчанию тот же, что у автопилота, — человек уже привык
# к нему в фильтрах. Если в нём пусто, расширяем один раз и говорим об этом:
# в провинции пятнадцать километров это соседняя улица по смыслу.
DEFAULT_RADIUS_KM = 15.0
WIDEN_RADIUS_KM = 30.0

MAX_STORES = 6          # больше карточек человек всё равно не читает
MAX_JOBS = 6


def store_key(job) -> tuple:
    """Один физический магазин: сеть плюс адрес."""
    return (job.brand, job.street, job.zip, job.city)


def stores(jobs, *, distances=None, trips=None, revisited=None) -> list[dict]:
    """Сгруппировать вакансии по магазинам, сохраняя порядок появления.

    Единственное место, где это правило живёт: и группировка ленты «по
    магазинам», и подсказки «рядом» зовут отсюда, иначе они однажды разойдутся.
    """
    distances = distances or {}
    trips = trips or {}
    revisited = revisited or {}
    bucket: dict[tuple, dict] = {}
    order: list[tuple] = []
    for job in jobs:
        key = store_key(job)
        if key not in bucket:
            bucket[key] = {
                "brand": job.brand, "street": job.street, "zip": job.zip,
                "city": job.city, "region": job.region, "country": job.country,
                "dist": distances.get(job.id), "trip": trips.get(job.id),
                "first": job, "jobs": [], "revisited_count": 0,
            }
            order.append(key)
        group = bucket[key]
        group["jobs"].append(job)
        if job.id in revisited:
            group["revisited_count"] += 1
        km = distances.get(job.id)
        if km is not None and (group["dist"] is None or km < group["dist"]):
            group["dist"] = km
    return [bucket[key] for key in order]


def _coords(jobs) -> list[tuple[float, float]]:
    """Точки названных магазинов — все, без усреднения.

    Средняя точка врёт на одинаковых названиях: Tørring в Дании два (под Vejle
    и под Pandrup), их середина — поле посередине страны, и от неё «7.7 км до
    Pandrup» выглядит правдой. Меряем от БЛИЖАЙШЕГО названного магазина.
    """
    return [(j.lat, j.lon) for j in jobs if j.lat is not None and j.lon is not None]


def _km(points, job) -> float | None:
    """Расстояние до ближайшей из опорных точек."""
    if not points or job.lat is None or job.lon is None:
        return None
    return round(min(geo.haversine_km(lat, lon, job.lat, job.lon) for lat, lon in points), 1)


def _brand_matches(job, brands) -> bool:
    value = str(getattr(job, "brand", "") or "").casefold()
    return any(str(b).casefold() in value for b in brands)


def _city_matches(job, cities) -> bool:
    """Город с учётом агломерации — как в фильтре ленты (Копенгаген = районы)."""
    value = str(getattr(job, "city", "") or "").casefold()
    for city in cities:
        for term in labels.city_terms(city) or [city]:
            if str(term).strip().casefold() in value:
                return True
    return False


def _city_named(job, cities) -> bool:
    """Ровно тот город, который человек назвал, без агломерации.

    Для опорной точки агломерация не годится: «Hellerup» раскрывается во весь
    Копенгаген, и тогда опорой становится половина столицы — а «рядом» вообще
    ничего не остаётся. Опора должна быть тем местом, которое назвали.
    """
    value = str(getattr(job, "city", "") or "").strip().casefold()
    return any(value == str(city).strip().casefold() for city in cities)


def why_not(jobs) -> dict[str, int]:
    """Почему вакансии магазина не подошли. Считаем ТЕМИ ЖЕ правилами, что фильтры."""
    import autopilot
    import feed
    import relevance

    reasons = {"under18": 0, "leadership": 0, "language": 0}
    for job in jobs:
        title = str(getattr(job, "title", "") or "")
        if labels.is_leadership(title):
            reasons["leadership"] += 1
        elif autopilot.job_is_under18(job):
            reasons["under18"] += 1
        elif feed.hide_barrier() and relevance.is_barrier(job):
            reasons["language"] += 1
    return {key: value for key, value in reasons.items() if value}


def _offerable(jobs, exclude_ids):
    """Что вообще можно предложить: не спрятанное лентой и не показанное уже."""
    import feed
    import relevance

    return [j for j in jobs
            if j.id not in exclude_ids
            and str(getattr(j, "status", "") or "") not in ("closed", "hidden", "applied")
            and getattr(j, "applied_at", None) is None
            and (not feed.hide_barrier() or not relevance.is_barrier(j))]


def near_same_brand(pool, brands, points, *, radius_km=DEFAULT_RADIUS_KM,
                    exclude_ids=(), exclude_keys=()) -> list[dict]:
    """Магазины той же сети в радиусе — главный слой подсказок."""
    exclude_ids = set(exclude_ids)
    exclude_keys = set(exclude_keys)
    found = []
    for job in _offerable(pool, exclude_ids):
        if not _brand_matches(job, brands) or store_key(job) in exclude_keys:
            continue
        km = _km(points, job)
        if points and (km is None or km > radius_km):
            continue
        found.append((job, km))
    groups = stores([job for job, _km_value in found],
                    distances={job.id: km for job, km in found if km is not None})
    groups.sort(key=lambda g: (g["dist"] is None, g["dist"] if g["dist"] is not None else 0))
    return groups[:MAX_STORES]


def near_same_city(pool, cities, *, exclude_brands=(), exclude_ids=()) -> list:
    """Тот же город, но другие сети — когда своя сеть в городе закончилась."""
    exclude_ids = set(exclude_ids)
    out = []
    for job in _offerable(pool, exclude_ids):
        if exclude_brands and _brand_matches(job, exclude_brands):
            continue
        if cities and not _city_matches(job, cities):
            continue
        out.append(job)
    return out[:MAX_JOBS]


def near_same_role(pool, role_keys, points, *, radius_km=DEFAULT_RADIUS_KM,
                   exclude_ids=()) -> list:
    """Такая же работа рядом — по ключу роли из вердиктов о языке."""
    import relevance

    exclude_ids = set(exclude_ids)
    wanted = {k for k in role_keys if k}
    if not wanted:
        return []
    found = []
    for job in _offerable(pool, exclude_ids):
        if relevance.role_key(job) not in wanted:
            continue
        km = _km(points, job)
        if points and (km is None or km > radius_km):
            continue
        found.append((job, km if km is not None else 9_999.0))
    found.sort(key=lambda pair: pair[1])
    return [job for job, _ in found[:MAX_JOBS]]


def suggestions(pool, *, parsed=None, job=None, home=None,
                radius_km=DEFAULT_RADIUS_KM, exclude_ids=()) -> dict:
    """Собрать подсказки для запроса (лента) или для одной вакансии (страница).

    Возвращает пустой словарь, когда сказать нечего: пустой блок в интерфейсе
    хуже его отсутствия.
    """
    brands: list[str] = []
    cities: list[str] = []
    anchor_jobs: list = []
    anchor_label = ""

    if job is not None:
        brands = [str(job.brand or "")] if job.brand else []
        cities = [str(job.city or "")] if job.city else []
        anchor_jobs = [j for j in pool if store_key(j) == store_key(job)]
        anchor_label = " ".join(x for x in (labels.brand(job.brand or ""), job.city) if x)
    elif parsed is not None:
        brands = list(parsed.brands)
        cities = list(parsed.cities)
        if brands:
            anchor_jobs = [j for j in pool if _brand_matches(j, brands)
                           and (not cities or _city_named(j, cities))]
            anchor_label = " ".join(
                x for x in (", ".join(labels.source(b) for b in brands),
                            ", ".join(cities)) if x
            )
    if not brands and not cities:
        return {}

    exclude_ids = set(exclude_ids) | {j.id for j in anchor_jobs}
    anchor_keys = {store_key(j) for j in anchor_jobs}
    # Опорные точки есть, только когда место названо: без города «Netto вообще»
    # — это вся страна, и радиус от неё ничего не значит. Тогда меряем от дома.
    point = _coords(anchor_jobs) if (job is not None or cities) else []
    if not point and home and home.get("lat") is not None:
        point = [(home["lat"], home["lon"])]

    widened = False
    # A named city without coordinates is not a radius anchor.  Showing the
    # same chain in another city would call an unknown (possibly country-wide)
    # distance "nearby".  Same-city suggestions below still work by name.
    same_brand = near_same_brand(
        pool, brands, point, radius_km=radius_km,
        exclude_ids=exclude_ids, exclude_keys=anchor_keys,
    ) if brands and point else []
    if brands and not same_brand and point and radius_km < WIDEN_RADIUS_KM:
        same_brand = near_same_brand(pool, brands, point, radius_km=WIDEN_RADIUS_KM,
                                     exclude_ids=exclude_ids, exclude_keys=anchor_keys)
        widened = bool(same_brand)

    shown = set(exclude_ids) | {j.id for group in same_brand for j in group["jobs"]}
    same_city = near_same_city(pool, cities, exclude_brands=brands, exclude_ids=shown) if cities else []
    shown |= {j.id for j in same_city}

    import relevance
    role_keys = {relevance.role_key(j) for j in (anchor_jobs or ([job] if job else []))}
    same_role = near_same_role(pool, role_keys, point, radius_km=max(radius_km, WIDEN_RADIUS_KM),
                               exclude_ids=shown)

    same_brand_count = sum(len(group["jobs"]) for group in same_brand)
    total = same_brand_count + len(same_city) + len(same_role)
    if not total:
        return {}
    return {
        "same_brand_count": same_brand_count,
        "anchor_label": anchor_label.strip(),
        "anchor_count": len(anchor_jobs),
        "anchor_rejected": why_not(anchor_jobs),
        "radius_km": WIDEN_RADIUS_KM if widened else radius_km,
        "widened": widened,
        "same_brand": same_brand,
        "same_city": same_city,
        "same_role": same_role,
        "total": total,
    }
