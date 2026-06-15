"""Этап 2 — локальный веб-дашборд вакансий Salling Group.

Запуск:  python -m uvicorn app:app --reload
Открыть: http://127.0.0.1:8000
"""
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from collections import Counter
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import func

import config
import labels
import geo
import settings_store
import translator
import translator_setup
import html_sanitize
import profile_store
import credentials_store
import transit
from db import Job, init_db, get_session, select, utcnow
import scraper
from apscheduler.schedulers.background import BackgroundScheduler

PROFILE_REQUIRED = [
    ("first_name", "Имя"),
    ("last_name", "Фамилия"),
    ("email", "Email"),
    ("phone", "Телефон"),
    ("address", "Адрес"),
    ("zip", "Индекс"),
    ("city", "Город"),
    ("country", "Страна"),
]

SAFE_JOB_STATUSES = {"new", "seen", "applied", "hidden", "interview", "offer", "rejected", "closed"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    else:
        host = host.split(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1"}


def _is_loopback_url(value: str) -> bool:
    try:
        return _is_loopback_host(urlsplit(value).hostname or "")
    except Exception:
        return False


def _allowed_local_write(request: Request) -> bool:
    """Block cross-site form/fetch writes against the local desktop server."""
    if request.method.upper() not in UNSAFE_METHODS:
        return True
    if not _is_loopback_host(request.headers.get("host", "")):
        return False
    origin = request.headers.get("origin", "")
    if origin:
        return _is_loopback_url(origin)
    referer = request.headers.get("referer", "")
    if referer:
        return _is_loopback_url(referer)
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    return True


def _url_with_system_response(url: str, notice: str = "", error: str = "") -> str:
    """Append one short UI response to a local redirect target."""
    target = url or "/"
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("notice", None)
    query.pop("error", None)
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def _redirect_back(
    request: Request,
    fallback: str = "/",
    notice: str = "",
    error: str = "",
) -> RedirectResponse:
    return RedirectResponse(
        _url_with_system_response(request.headers.get("referer") or fallback, notice, error),
        status_code=303,
    )

# --- автообновление вакансий: каждые 30 минут + при старте, если данные устарели ---
_sync_lock = threading.Lock()
_sync_state = {"running": False, "last_error": ""}


def _sync_jobs():
    """Обновляет базу вакансий. Не запускается параллельно сам с собой."""
    if not _sync_lock.acquire(blocking=False):
        return
    _sync_state["running"] = True
    try:
        scraper.sync()
        _sync_state["last_error"] = ""
    except Exception as e:
        _sync_state["last_error"] = str(e)[:200]
        print(f"автообновление: ошибка — {e}")
    finally:
        _sync_state["running"] = False
        _sync_lock.release()


def _data_age_minutes() -> int | None:
    """Сколько минут назад вакансии обновлялись (по last_seen в базе)."""
    with get_session() as s:
        last = s.exec(select(func.max(Job.last_seen))).one()
    if not last:
        return None
    return max(0, int((utcnow() - last).total_seconds() // 60))


@asynccontextmanager
async def _lifespan(app):
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_sync_jobs, "interval", minutes=30, id="auto_sync")
    sched.start()
    age = _data_age_minutes()
    if age is None or age >= 30:  # данные устарели — обновить сразу, в фоне
        threading.Thread(target=_sync_jobs, daemon=True).start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Salling Jobs", lifespan=_lifespan)


@app.middleware("http")
async def _no_cache(request, call_next):
    if not _allowed_local_write(request):
        return JSONResponse({"ok": False, "error": "blocked cross-site request"}, status_code=403)
    # WebView2 кэширует страницы/скрипты агрессивно — для desktop-приложения это
    # вредно (после обновления показывает старый интерфейс). Запрещаем кэш.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/api/version")
def api_version():
    """Версия и результат проверки обновлений — для баннера и диагностики."""
    try:
        import version
        import update_check
        return {
            "version": version.__version__,
            "repo": version.GITHUB_REPO,
            "update": update_check.check(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"version": "dev", "repo": "", "update": None, "error": str(exc)[:200]}


app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
templates.env.globals["brand_label"] = labels.brand
templates.env.globals["L"] = labels
try:
    import changelog as _changelog
    import version as _version
    templates.env.globals["CHANGELOG"] = _changelog.ENTRIES
    templates.env.globals["APP_VERSION"] = _version.__version__
except Exception:  # noqa: BLE001 — без журнала изменений окно просто не показывается
    templates.env.globals["CHANGELOG"] = []
    templates.env.globals["APP_VERSION"] = ""
init_db()


def _job_address(job: Job) -> str:
    parts = [
        job.street or "",
        " ".join(p for p in [job.zip or "", job.city or ""] if p),
        job.country or "",
    ]
    return ", ".join(p for p in parts if p)


def _maps_url(job: Job, home: dict | None = None) -> str:
    destination = _job_address(job)
    if not destination and job.lat is not None and job.lon is not None:
        destination = f"{job.lat},{job.lon}"
    origin = ""
    if home:
        origin = home.get("lookup_address") or home.get("address") or ""
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(destination)}"
        "&travelmode=transit"
    )


def _profile_missing(profile: dict) -> list[str]:
    return [label for key, label in PROFILE_REQUIRED if not str(profile.get(key) or "").strip()]


def _profile_choices() -> tuple[list[str], list[str]]:
    city_set = {
        "København", "København K", "København N", "København S", "København V",
        "København Ø", "København NV", "København SV", "Frederiksberg",
        "Brønshøj", "Valby", "Vanløse", "Rødovre", "Hvidovre", "Herlev",
        "Glostrup", "Ballerup", "Taastrup", "Kastrup", "Aarhus", "Aarhus C",
        "Odense", "Aalborg", "Esbjerg", "Randers", "Kolding", "Vejle",
        "Roskilde", "Køge", "Greve", "Ishøj", "Kgs. Lyngby", "Hillerød",
        "Helsingør", "Næstved", "Slagelse", "Holbæk", "Svendborg",
        "Sønderborg", "Viborg", "Horsens", "Silkeborg", "Herning",
        "Fredericia", "Hjørring", "Skive", "Ringsted", "Haderslev",
        "Skanderborg", "Nyborg", "Aabenraa", "Kalundborg", "Nørresundby",
        "Farum", "Birkerød", "Værløse", "Allerød", "Solrød Strand",
        "Frederikssund", "Frederiksværk", "Hundested", "Tårnby", "Dragør",
        "Albertslund", "Brøndby", "Hedehusene", "Nivå", "Humlebæk",
        "Fredensborg", "Espergærde", "Rønne", "Nykøbing F", "Nakskov",
        "Vordingborg", "Haslev", "Sorø", "Ringkøbing", "Holstebro",
        "Struer", "Ikast", "Brande", "Billund", "Vejen", "Middelfart",
        "Assens", "Faaborg", "Middelfart", "Frederikshavn", "Thisted",
        "Hobro", "Grenaa", "Ebeltoft", "Skagen", "Ribe", "Tønder",
        "Varde", "Brønderslev", "Lemvig", "Odder", "Nykøbing Mors",
        "Lillerød", "Charlottenlund", "Hellerup", "Gentofte", "Virum",
        "Søborg", "Bagsværd", "Lyngby", "Måløv", "Smørum", "Ølstykke",
    }
    for values in labels.CITY_GROUPS.values():
        city_set.update(values)
    city_set.update(labels.CITY_ALIASES.values())
    try:
        with get_session() as s:
            city_set.update(c for c in s.exec(select(Job.city)).all() if c)
    except Exception:
        pass
    countries = ["Danmark", "Sverige", "Norge", "Tyskland", "Polen"]
    return sorted(city_set, key=str.casefold), countries


def _text(html: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip().lower()


def _job_facts(job: Job, distance: float | None) -> list[dict]:
    facts = []
    if job.brand:
        facts.append({"label": "Бренд", "value": labels.brand(job.brand), "kind": ""})
    address = _job_address(job)
    if address:
        facts.append({"label": "Адрес", "value": address, "kind": "place"})
    if job.region:
        facts.append({"label": "Регион", "value": labels.REGION.get(job.region, job.region), "kind": ""})
    if distance is not None:
        facts.append({"label": "От дома", "value": f"≈ {distance} км по прямой", "kind": "distance"})
    if job.hours:
        facts.append({"label": "Часы", "value": f"{job.hours} ч/нед", "kind": "time"})
    if job.employment_type:
        facts.append({
            "label": "Занятость",
            "value": labels.EMPLOYMENT.get(job.employment_type, job.employment_type),
            "kind": "work",
        })
    if job.job_level:
        facts.append({"label": "Уровень", "value": labels.LEVEL.get(job.job_level, job.job_level), "kind": "level"})
    if job.job_level == "employeeUnder18" or "under 18" in (job.title or "").lower():
        facts.append({"label": "Возраст", "value": "позиция для сотрудников до 18 лет", "kind": "important"})
    if job.start_date:
        facts.append({"label": "Старт", "value": job.start_date, "kind": "date"})
    if job.published:
        facts.append({"label": "Опубликовано", "value": job.published[:10], "kind": "muted"})
    if job.pay_rate:
        facts.append({"label": "Ставка", "value": job.pay_rate, "kind": "money"})
    else:
        facts.append({"label": "Ставка", "value": "не указана в объявлении", "kind": "muted"})
    if job.categories:
        cat_labels = [labels.CATEGORY.get(c, c) for c in job.categories.split(",") if c]
        if cat_labels:
            facts.append({"label": "Категория", "value": ", ".join(cat_labels[:3]), "kind": "category"})
    facts.append({"label": "ID (requisition)", "value": str(job.requisition_id or job.id), "kind": "muted"})

    body = _text(job.description)
    signals = []
    if any(w in body for w in ["oplæring", "uddannelse", "kursus", "training"]):
        signals.append("есть обучение/ввод в работу")
    if any(w in body for w in ["rabat", "personalerabat", "medarbejderrabat", "discount"]):
        signals.append("упоминаются скидки/льготы")
    if any(w in body for w in ["weekend", "aften", "nat", "morgen"]):
        signals.append("в тексте есть смены/вечер/ночь/выходные")
    if any(w in body for w in ["ansvar", "selvstændig", "team", "service", "kunde"]):
        signals.append("важны сервис, команда и ответственность")
    if signals:
        facts.append({"label": "Из описания", "value": "; ".join(signals[:3]), "kind": "note"})
    return facts


def _distinct(session, column):
    rows = session.exec(select(column).distinct()).all()
    return sorted([r for r in rows if r])


def _active_counts(session):
    rows = session.exec(select(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))).all()
    counts = {
        "brand": Counter(),
        "region": Counter(),
        "employment": Counter(),
        "level": Counter(),
        "category": Counter(),
        "city": Counter(),
    }
    for job in rows:
        if job.brand:
            counts["brand"][job.brand] += 1
        if job.region:
            counts["region"][job.region] += 1
        if job.employment_type:
            counts["employment"][job.employment_type] += 1
        if job.job_level:
            counts["level"][job.job_level] += 1
        if job.city:
            counts["city"][job.city] += 1
        for cat in (job.categories or "").split(","):
            if cat:
                counts["category"][cat] += 1
    return counts


def _seven_eleven_state() -> dict:
    """Живые данные модуля 7-Eleven для карточки хаба (читаем его профиль)."""
    import pathlib
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or str(pathlib.Path.home() / "AppData" / "Roaming")
        seven_dir = pathlib.Path(base) / "WexFlow" / "seven11"
    else:
        seven_dir = pathlib.Path(r"C:\seven11-apply")
    try:
        d = json.loads((seven_dir / "profiles" / "me.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет профиля → пустое состояние
        return {"stores": 0, "profile_ready": False, "name": ""}
    loc = d.get("location", {}) or {}
    sel = loc.get("selected_addresses") or loc.get("preferred_addresses") or []
    p = d.get("personal", {}) or {}
    ans = d.get("answers", {}) or {}
    att = d.get("attachments", {}) or {}
    ready = bool(
        p.get("first_name") and p.get("last_name")
        and "@" in str(p.get("email") or "")
        and len(str(p.get("phone") or "")) >= 8
        and str(att.get("cv_path") or "")
        and len(str(ans.get("why_7eleven") or "").strip()) >= 10
    )
    return {"stores": len(sel), "profile_ready": ready, "name": str(p.get("first_name") or "")}


@app.get("/hub", response_class=HTMLResponse)
def hub(request: Request):
    with get_session() as s:
        total_jobs = s.exec(select(func.count(Job.id))).one() or 0
        active_jobs = (
            s.exec(
                select(func.count(Job.id)).where(
                    Job.status.not_in(["closed", "hidden", "applied"])
                )
            ).one()
            or 0
        )
        applied_jobs = s.exec(select(func.count(Job.id)).where(Job.status == "applied")).one() or 0
        last_applied = None
        try:
            last = s.exec(
                select(Job).where(Job.status == "applied").order_by(Job.modified.desc())
            ).first()
            if last:
                last_applied = {"title": last.title, "brand": last.brand}
        except Exception:  # noqa: BLE001
            last_applied = None

    seven = _seven_eleven_state()
    try:
        name = (profile_store.load_profile().get("first_name") or "").strip() or seven["name"]
    except Exception:  # noqa: BLE001
        name = seven["name"]

    return templates.TemplateResponse(
        "hub.html",
        {
            "request": request,
            "total_jobs": total_jobs,
            "active_jobs": active_jobs,
            "applied_jobs": applied_jobs,
            "data_age_min": _data_age_minutes(),
            "sync_running": _sync_state["running"],
            "seven": seven,
            "last_applied": last_applied,
            "user_name": name,
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    city: str = "",
    brand: str = "",
    region: str = "",
    employment_type: str = "",
    category: str = "",
    job_level: str = "",
    status: str = "active",
    sort: str = "",
    radius: str = "",
    group: str = "",
    show_applied: str = "",
    page: str = "1",
    geoerror: str = "",
    batch: str = "",
    mode: str = "",
    reset: str = "",
):
    # запоминаем фильтры в cookie и восстанавливаем при заходе на голую "/"
    _fkeys = ["q", "city", "brand", "region", "employment_type", "category", "job_level", "status", "sort", "show_applied"]
    if not request.query_params and not reset:
        raw = request.cookies.get("saling_filters")
        if raw:
            try:
                saved = json.loads(raw)
                q = saved.get("q", q); city = saved.get("city", city)
                brand = saved.get("brand", brand); region = saved.get("region", region)
                employment_type = saved.get("employment_type", employment_type)
                category = saved.get("category", category); job_level = saved.get("job_level", job_level)
                status = saved.get("status", status); sort = saved.get("sort", sort)
                show_applied = saved.get("show_applied", show_applied)
                radius = saved.get("radius", radius); group = saved.get("group", group)
            except Exception:
                pass

    home = settings_store.get_home()
    if not sort:
        sort = "distance" if home else "published"
    with get_session() as s:
        stmt = select(Job)
        if status == "active":
            excluded_statuses = ["closed", "hidden"]
            if not show_applied:
                excluded_statuses.append("applied")
            stmt = stmt.where(Job.status.not_in(excluded_statuses))
        elif status:
            stmt = stmt.where(Job.status == status)
        city_lookup = labels.city_query(city)
        city_terms = labels.city_terms(city)
        if city_terms:  # русский/англ/датский алиас, район или агломерация
            cond = None
            for term in city_terms:
                c = Job.city.ilike(f"%{term.strip()}%")
                cond = c if cond is None else (cond | c)
            stmt = stmt.where(cond)
        brand_code = labels.resolve(labels.BRANDS, brand)
        if brand_code:
            stmt = stmt.where(Job.brand == brand_code)
        region_code = labels.resolve(labels.REGION, region)
        if region_code:
            stmt = stmt.where(Job.region == region_code)
        employment_code = labels.resolve(labels.EMPLOYMENT, employment_type)
        if employment_code:
            stmt = stmt.where(Job.employment_type == employment_code)
        level_code = labels.resolve(labels.LEVEL, job_level)
        if level_code:
            stmt = stmt.where(Job.job_level == level_code)
        category_code = labels.resolve(labels.CATEGORY, category)
        if category_code:
            stmt = stmt.where(
                (Job.categories == category_code)
                | Job.categories.like(f"{category_code},%")
                | Job.categories.like(f"%,{category_code},%")
                | Job.categories.like(f"%,{category_code}")
            )
        if q:  # умный поиск: русский запрос расширяем датскими синонимами
            import ru_search
            terms = ru_search.expand(q)
            city_term = labels.city_query(q)
            if city_term and city_term != q.strip():
                terms.append(city_term)
            cond = None
            for t in terms:
                like = f"%{t}%"
                c = (
                    Job.title.ilike(like)
                    | Job.description.ilike(like)
                    | Job.city.ilike(like)
                    | Job.street.ilike(like)
                )
                cond = c if cond is None else (cond | c)
            if cond is not None:
                stmt = stmt.where(cond)

        if sort == "title":
            stmt = stmt.order_by(Job.title)
        elif sort == "city":
            stmt = stmt.order_by(Job.city)
        else:  # published (по умолчанию) и distance до пост-сортировки
            stmt = stmt.order_by(Job.published.desc())

        jobs = list(s.exec(stmt).all())

        cities = _distinct(s, Job.city)
        brands = _distinct(s, Job.brand)
        regions = _distinct(s, Job.region)
        etypes = _distinct(s, Job.employment_type)
        levels = _distinct(s, Job.job_level)
        cat_rows = _distinct(s, Job.categories)
        cats = sorted({c for row in cat_rows for c in row.split(",") if c})
        counts = _active_counts(s)
        total_active = s.exec(
            select(func.count()).select_from(Job).where(Job.status.not_in(["closed", "hidden", "applied"]))
        ).one()
        applied_count = s.exec(
            select(func.count()).select_from(Job).where(Job.status == "applied")
        ).one()
        last = s.exec(select(func.max(Job.last_seen))).one()

    # расстояние от дома (если задан) + сортировка по близости
    distances = {}
    if home:
        for j in jobs:
            if j.lat is not None and j.lon is not None:
                distances[j.id] = round(
                    geo.haversine_km(home["lat"], home["lon"], j.lat, j.lon), 1
                )
        # фильтр по радиусу (только в пределах N км от дома)
        try:
            radius_km = float(radius) if radius else 0
        except ValueError:
            radius_km = 0
        if radius_km > 0:
            jobs = [j for j in jobs if j.id in distances and distances[j.id] <= radius_km]
        if sort == "distance":
            jobs.sort(key=lambda j: distances.get(j.id, float("inf")))

    # --- группировка по магазину (адрес) ---
    groups = []
    if group:
        bucket, order = {}, []
        for j in jobs:
            key = (j.brand, j.street, j.zip, j.city)
            if key not in bucket:
                bucket[key] = {
                    "brand": j.brand, "street": j.street, "zip": j.zip, "city": j.city,
                    "region": j.region, "country": j.country, "dist": distances.get(j.id),
                    "first": j, "jobs": [],
                }
                order.append(key)
            g = bucket[key]
            g["jobs"].append(j)
            d = distances.get(j.id)
            if d is not None and (g["dist"] is None or d < g["dist"]):
                g["dist"] = d
        groups = [bucket[k] for k in order]

    # --- пагинация ---
    PER_PAGE = 24 if group else 60
    items = groups if group else jobs
    total = len(items)
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    items = items[(page - 1) * PER_PAGE: page * PER_PAGE]
    if group:
        groups = items
        jobs = [j for g in groups for j in g["jobs"]]  # для maps_urls
    else:
        jobs = items

    maps_urls = {j.id: _maps_url(j, home) for j in jobs}

    # отсортировать опции по русской подписи
    def by_label(items, mapping):
        return sorted(items, key=lambda k: labels.bi(mapping, k).lower())

    resp = templates.TemplateResponse("index.html", {
        "request": request, "jobs": jobs, "count": len(jobs),
        "groups": groups, "group": group,
        "total_filtered": total, "page": page, "pages": pages, "per_page": PER_PAGE,
        "cities": cities,
        # опции отсортированы по популярности (частые сверху) — удобнее выбирать
        "brands": [
            (b, labels.with_count(labels.brand(b), counts["brand"][b]))
            for b in sorted(brands, key=lambda k: -counts["brand"][k])
        ],
        "regions": [
            (r, labels.with_count(labels.REGION.get(r, r), counts["region"][r]))
            for r in sorted(regions, key=lambda k: -counts["region"][k])
        ],
        "etypes": [
            (e, labels.with_count(labels.EMPLOYMENT.get(e, e), counts["employment"][e]))
            for e in sorted(etypes, key=lambda k: -counts["employment"][k])
        ],
        "levels": [
            (l, labels.with_count(labels.LEVEL.get(l, l), counts["level"][l]))
            for l in sorted(levels, key=lambda k: -counts["level"][k])
        ],
        "cats": [
            (c, labels.with_count(labels.CATEGORY.get(c, c), counts["category"][c]))
            for c in sorted(cats, key=lambda k: -counts["category"][k])
        ],
        "city_suggestions": [
            (city, labels.with_count(city, count))
            for city, count in counts["city"].most_common(120)
        ],
        "f": (_f := {"q": q,
              "city": city,
              "brand": brand_code,
              "region": region_code,
              "category": category_code,
              "employment_type": employment_code,
              "job_level": level_code,
              "status": status, "sort": sort, "radius": radius, "group": group,
              "show_applied": show_applied}),
        "total_active": total_active, "applied_count": applied_count, "last_update": last,
        "data_age_min": (max(0, int((utcnow() - last).total_seconds() // 60)) if last else None),
        "sync_running": _sync_state["running"],
        "sync_error": _sync_state["last_error"],
        "home": home, "distances": distances, "geoerror": geoerror,
        "presets": settings_store.get_presets(),
        "batch": batch, "batch_mode": mode,
        "apply_files": {
            "cv": profile_store.file_label((_apply_profile := profile_store.load_profile()).get("cv_path", "")),
            "cover": profile_store.file_label(_apply_profile.get("cover_letter_path", "")),
        },
        "maps_urls": maps_urls,
        "resolved": {
            "city": (
                f"{city_lookup} + районы" if city and len(city_terms) > 1
                else (city_lookup if city and city_lookup != city.strip() else "")
            ),
            "brand": brand_code if brand and brand_code != brand.strip() else "",
            "region": labels.bi(labels.REGION, region_code) if region and region_code and region_code != region.strip() else "",
            "category": labels.bi(labels.CATEGORY, category_code) if category and category_code and category_code != category.strip() else "",
            "employment_type": labels.bi(labels.EMPLOYMENT, employment_code) if employment_type and employment_code and employment_code != employment_type.strip() else "",
            "job_level": labels.bi(labels.LEVEL, level_code) if job_level and level_code and level_code != job_level.strip() else "",
        },
    })
    # запоминаем выбранные фильтры на 30 дней (восстановятся при заходе на "/")
    if reset:
        resp.delete_cookie("saling_filters")
    else:
        resp.set_cookie("saling_filters", json.dumps(_f), max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/job/{job_id}/status")
def set_status(job_id: str, request: Request, status: str = Form(...)):
    if status not in SAFE_JOB_STATUSES:
        return _redirect_back(request, "/", error="Неизвестный статус вакансии.")
    status_labels = {
        "applied": "Статус обновлён: вакансия отмечена как поданная.",
        "hidden": "Вакансия скрыта. Её можно вернуть из статуса «Скрытые».",
        "seen": "Статус сброшен: вакансия снова в просмотренных.",
        "interview": "Статус обновлён: собеседование.",
        "offer": "Статус обновлён: оффер.",
        "rejected": "Статус обновлён: отказ.",
    }
    with get_session() as s:
        job = s.get(Job, job_id)
        if job:
            job.status = status
            if status == "applied":
                job.applied_at = utcnow()
            s.add(job)
            s.commit()
        else:
            return _redirect_back(request, "/", error="Вакансия не найдена. Возможно, список обновился.")
    return _redirect_back(request, "/", notice=status_labels.get(status, "Статус вакансии обновлён."))


@app.post("/refresh")
def refresh(request: Request):
    # обновление уходит в фон: страница не виснет, индикатор в шапке показывает
    # «обновляется…», список сам перезагрузится по окончании
    threading.Thread(target=_sync_jobs, daemon=True).start()
    return _redirect_back(request, "/", notice="Обновление вакансий запущено. Список сам перезагрузится, когда появятся свежие данные.")


@app.post("/presets/save")
def save_preset(request: Request, name: str = Form(...), query: str = Form("")):
    settings_store.add_preset(name, query)
    return _redirect_back(request, "/", notice=f"Фильтр «{name.strip() or 'без названия'}» сохранён.")


@app.post("/presets/delete")
def delete_preset(request: Request, name: str = Form(...)):
    settings_store.delete_preset(name)
    return _redirect_back(request, "/", notice=f"Фильтр «{name.strip() or 'без названия'}» удалён.")


@app.post("/set-home")
def set_home(request: Request, address: str = Form(...)):
    lookup = labels.localize_address(address)
    coords = geo.geocode_address(lookup)
    ref = request.headers.get("referer")
    if coords:
        settings_store.set_home(address, coords[0], coords[1], lookup)
        target = _url_with_system_response(ref or "/?sort=distance", notice="Домашний адрес сохранён. Теперь доступны сортировка и фильтр по расстоянию.")
        return RedirectResponse(target, status_code=303)
    sep = "&" if (ref and "?" in ref) else "?"
    target = (ref + sep + "geoerror=1") if ref else "/?geoerror=1"
    target = _url_with_system_response(target, error="Не удалось распознать адрес. Попробуй улицу с номером дома, город или индекс.")
    return RedirectResponse(target, status_code=303)


@app.post("/set-home-coords")
def set_home_coords(lat: float = Form(...), lon: float = Form(...)):
    """Сохранить дом по координатам (из кнопки «определить местоположение»).
    Координаты превращаем в читаемый датский адрес через reverse-геокод."""
    label = geo.reverse_geocode(lat, lon) or f"Моё местоположение ({lat:.4f}, {lon:.4f})"
    settings_store.set_home(label, lat, lon, label)
    return {"ok": True, "address": label}


@app.get("/api/address-suggestions")
def address_suggestions(q: str = ""):
    """Живые подсказки домашнего адреса из датской адресной базы."""
    return {"items": geo.suggest_addresses(q)}


@app.get("/api/apply-log")
def apply_log():
    """Хвост лога последней подачи — показывается прямо на странице «Подать»."""
    path = config.DATA_DIR / "apply_last.log"
    if not path.exists():
        return JSONResponse({"ok": False, "lines": []})
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)[:120], "lines": []})
    return JSONResponse({"ok": True, "lines": lines[-120:]})


@app.get("/api/sync-status")
def api_sync_status():
    """Идёт ли сейчас фоновое обновление вакансий. Главная опрашивает это
    легко и перезагружается ОДИН раз, когда обновление закончилось — вместо
    того чтобы перезагружать страницу по таймеру снова и снова."""
    return JSONResponse({"running": _sync_state["running"]})


@app.get("/api/transit/{job_id}")
def api_transit(job_id: str):
    home = settings_store.get_home()
    if not home:
        return JSONResponse({"ok": False, "error": "домашний адрес не задан в настройках"})
    with get_session() as s:
        job = s.get(Job, job_id)
    if not job or job.lat is None or job.lon is None:
        return JSONResponse({"ok": False, "error": "у вакансии нет координат для маршрута"})
    try:
        return JSONResponse(transit.summary(home["lat"], home["lon"], job.lat, job.lon))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"маршрут сейчас не посчитался: {str(exc)[:90]}"})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", geoerror: str = "", missing: str = ""):
    profile = profile_store.load_profile()
    file_info = {
        "cv_label": profile_store.file_label(profile.get("cv_path", "")),
        "cv_status": profile_store.file_status(profile.get("cv_path", "")),
        "cv_url": "/settings/file/cv" if profile_store.file_status(profile.get("cv_path", "")) == "ok" else "",
        "cover_label": profile_store.file_label(profile.get("cover_letter_path", "")),
        "cover_status": profile_store.file_status(profile.get("cover_letter_path", "")),
        "cover_url": "/settings/file/cover" if profile_store.file_status(profile.get("cover_letter_path", "")) == "ok" else "",
    }
    city_options, country_options = _profile_choices()
    missing_fields = [x for x in missing.split(",") if x]
    return templates.TemplateResponse("settings.html", {
        "request": request, "profile": profile, "file_info": file_info,
        "creds": credentials_store.status(), "home": settings_store.get_home(),
        "saved": saved, "geoerror": geoerror, "missing_fields": missing_fields,
        "city_options": city_options, "country_options": country_options,
    })


@app.post("/settings/save")
def settings_save(
    first_name: str = Form(""), last_name: str = Form(""), email: str = Form(""),
    phone: str = Form(""), address: str = Form(""), zipcode: str = Form(""),
    city: str = Form(""), country: str = Form(""), linkedin: str = Form(""),
    cv_path: str = Form(""), cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None), cover_letter_file: UploadFile | None = File(None),
):
    profile = profile_store.load_profile()
    profile.update({
        "first_name": first_name.strip(), "last_name": last_name.strip(),
        "email": email.strip(), "phone": phone.strip(), "address": address.strip(),
        "zip": zipcode.strip(), "city": city.strip(), "country": country.strip(),
        "linkedin": linkedin.strip(),
    })
    missing = _profile_missing(profile)
    if missing:
        return RedirectResponse("/settings?missing=" + quote_plus(",".join(missing)), status_code=303)
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(_url_with_system_response("/settings", error=file_error), status_code=303)
    profile_store.save_profile(profile)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/profile/autosave")
async def settings_profile_autosave(request: Request):
    form = await request.form()
    profile = profile_store.load_profile()
    for form_key, profile_key in [
        ("first_name", "first_name"), ("last_name", "last_name"), ("email", "email"),
        ("phone", "phone"), ("address", "address"), ("zipcode", "zip"),
        ("city", "city"), ("country", "country"), ("linkedin", "linkedin"),
        ("cv_path", "cv_path"), ("cover_letter_path", "cover_letter_path"),
    ]:
        if form_key in form:
            profile[profile_key] = str(form.get(form_key) or "").strip()
    profile = profile_store.clean_profile(profile)
    profile_store.save_profile(profile)
    return JSONResponse({"ok": True, "missing": _profile_missing(profile), "profile": profile})


@app.get("/settings/file/{kind}")
def settings_file(kind: str):
    profile = profile_store.load_profile()
    path = {
        "cv": profile.get("cv_path", ""),
        "cover": profile.get("cover_letter_path", ""),
    }.get(kind)
    if not path or profile_store.file_status(path) != "ok":
        raise HTTPException(status_code=404, detail="file not found")
    clean_path = profile_store.validate_document_path(path)
    filename = profile_store.file_label(clean_path)
    is_pdf = filename.lower().endswith(".pdf")
    media_type = "application/pdf" if is_pdf else "application/octet-stream"
    disposition = "inline" if is_pdf else "attachment"
    return FileResponse(clean_path, media_type=media_type, filename=filename, content_disposition_type=disposition)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def detail(request: Request, job_id: str, trerror: str = ""):
    with get_session() as s:
        job = s.get(Job, job_id)
        if job and job.status == "new":
            job.status = "seen"
            s.add(job)
            s.commit()
            s.refresh(job)
    distance = None
    home = settings_store.get_home()
    if job and home and job.lat is not None and job.lon is not None:
        distance = round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon), 1)
    maps_url = _maps_url(job, home) if job else ""
    facts = _job_facts(job, distance) if job else []
    return templates.TemplateResponse(
        "detail.html", {
            "request": request,
            "job": job,
            "distance": distance,
            "has_home": bool(home),
            "maps_url": maps_url,
            "facts": facts,
            "description_html": html_sanitize.sanitize_html(job.description if job else ""),
            "description_ru_html": html_sanitize.sanitize_html(job.description_ru if job else ""),
            "translator_name": translator.provider_name(),
            "translator_install": translator_setup.status(),
            "trerror": trerror,
        }
    )


@app.get("/job/{job_id}/apply", response_class=HTMLResponse)
def apply_prepare(request: Request, job_id: str, started: str = "", saved: str = "", reset: str = ""):
    with get_session() as s:
        job = s.get(Job, job_id)
    profile = profile_store.load_profile()
    file_info = {
        "cv_label": profile_store.file_label(profile.get("cv_path", "")),
        "cv_status": profile_store.file_status(profile.get("cv_path", "")),
        "cover_label": profile_store.file_label(profile.get("cover_letter_path", "")),
        "cover_status": profile_store.file_status(profile.get("cover_letter_path", "")),
    }
    home = settings_store.get_home()
    distance = None
    if job and home and job.lat is not None and job.lon is not None:
        distance = round(geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon), 1)
    return templates.TemplateResponse(
        "apply.html",
        {
            "request": request,
            "job": job,
            "profile": profile,
            "file_info": file_info,
            "distance": distance,
            "maps_url": _maps_url(job, home) if job else "",
            "started": started,
            "saved": saved,
            "reset": reset,
            "creds": credentials_store.status(),
        },
    )


@app.post("/credentials/save")
def save_credentials(job_id: str = Form(""), sf_email: str = Form(""), sf_password: str = Form("")):
    credentials_store.save(sf_email, sf_password)
    target = f"/job/{job_id}/apply?saved=login" if job_id else _url_with_system_response("/", notice="Логин Salling сохранён.")
    return RedirectResponse(target, status_code=303)


@app.post("/apply/reset-browser")
def reset_browser(request: Request, job_id: str = Form("")):
    """Удаляет сохранённую сессию браузера бота — чтобы выйти из чужого/старого
    аккаунта Salling и войти заново под сохранёнными email/паролем."""
    import shutil
    shutil.rmtree(config.BROWSER_PROFILE_DIR, ignore_errors=True)
    ref = request.headers.get("referer")
    if ref and "/settings" in ref:
        return RedirectResponse("/settings?saved=1", status_code=303)
    target = f"/job/{job_id}/apply?reset=1" if job_id else "/"
    return RedirectResponse(target, status_code=303)


@app.post("/credentials/clear")
def clear_credentials(request: Request):
    credentials_store.clear()
    return _redirect_back(request, "/settings", notice="Логин Salling очищен.")


def _update_profile_files(profile: dict, cv_path: str, cover_letter_path: str, cv_file, cover_letter_file) -> dict:
    # приоритет: загруженный файл > указанный путь > СОХРАНИТЬ прежний (не стираем случайно)
    if cv_file and cv_file.filename:
        profile["cv_path"] = profile_store.save_upload(cv_file, "cv")
    elif cv_path.strip():
        profile["cv_path"] = profile_store.validate_document_path(cv_path)

    if cover_letter_file and cover_letter_file.filename:
        profile["cover_letter_path"] = profile_store.save_upload(cover_letter_file, "cover_letter")
    elif cover_letter_path.strip():
        profile["cover_letter_path"] = profile_store.validate_document_path(cover_letter_path)
    return profile


def _profile_files_result(profile: dict, cv_path: str, cover_letter_path: str, cv_file, cover_letter_file) -> tuple[dict, str]:
    try:
        return _update_profile_files(profile, cv_path, cover_letter_path, cv_file, cover_letter_file), ""
    except ValueError as exc:
        return profile, str(exc)


@app.post("/job/{job_id}/apply/save")
def save_apply_files(
    job_id: str,
    cv_path: str = Form(""),
    cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
):
    profile = profile_store.load_profile()
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(
            _url_with_system_response(f"/job/{job_id}/apply", error=file_error),
            status_code=303,
        )
    profile_store.save_profile(profile)
    return RedirectResponse(f"/job/{job_id}/apply?saved=files", status_code=303)


def _salling_apply_cmd(extra: list[str]) -> list[str]:
    """Команда запуска подачи Salling.

    dev: [python, -u, apply.py, ...]. Собранное приложение: exe запускает сам
    себя как воркер [WexFlow.exe, --worker-salling-apply, ...] — Python/.venv
    на чужом ПК нет.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker-salling-apply", *extra]
    return [sys.executable, "-u", str(config.BASE_DIR / "apply.py"), *extra]


@app.post("/job/{job_id}/apply/start")
def start_apply(
    job_id: str,
    mode: str = Form("submit"),
    cv_path: str = Form(""),
    cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
):
    with get_session() as s:
        job = s.get(Job, job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    profile = profile_store.load_profile()
    profile, file_error = _profile_files_result(profile, cv_path, cover_letter_path, cv_file, cover_letter_file)
    if file_error:
        return RedirectResponse(
            _url_with_system_response(f"/job/{job_id}/apply", error=file_error),
            status_code=303,
        )
    profile_store.save_profile(profile)
    # вывод apply.py пишем в лог, чтобы сбои не были «молчаливыми»
    log = open(config.DATA_DIR / "apply_last.log", "w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # иначе print датских/русских символов падает (cp1251)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"        # чтобы лог писался сразу, а не после закрытия браузера
    cmd = _salling_apply_cmd([job_id, "--web"])
    if mode == "submit":
        cmd.append("--submit")
    try:
        subprocess.Popen(
            cmd,
            cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    finally:
        log.close()  # потомок унаследовал свой хэндл; родительский больше не нужен
    return RedirectResponse(f"/job/{job_id}/apply?started=1", status_code=303)


@app.post("/apply/batch")
def apply_batch(request: Request, job_ids: list[str] = Form(default=[]), mode: str = Form("dry")):
    ids = [j for j in job_ids if j]
    if not ids:
        return _redirect_back(request, "/", error="Сначала выбери хотя бы одну вакансию для пакетной подачи.")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = _salling_apply_cmd(ids + ["--web"])
    if mode == "submit":
        cmd.append("--submit")
    log = open(config.DATA_DIR / "apply_last.log", "w", encoding="utf-8")
    try:
        subprocess.Popen(
            cmd, cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    finally:
        log.close()
    return RedirectResponse(f"/?batch={len(ids)}&mode={mode}", status_code=303)


@app.post("/job/{job_id}/translate")
def translate_job(job_id: str):
    error = ""
    with get_session() as s:
        job = s.get(Job, job_id)
        if job and job.description:
            try:
                job.description_ru = translator.translate_to_ru(job.description, title=job.title)
                s.add(job)
                s.commit()
            except translator.TranslationError as e:
                error = str(e)[:120]
    target = f"/job/{job_id}?trerror={quote_plus(error)}" if error else _url_with_system_response(f"/job/{job_id}", notice="Перевод обновлён.")
    return RedirectResponse(target, status_code=303)


@app.post("/translator/install")
def install_translator(job_id: str = Form("")):
    translator_setup.start_install()
    target = _url_with_system_response(
        f"/job/{job_id}" if job_id else "/",
        notice="Установка переводчика запущена в фоне.",
    )
    return RedirectResponse(target, status_code=303)
