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
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import func

import config
import labels
import geo
import settings_store
import translator
import translator_setup
import profile_store
import credentials_store
import transit
from db import Job, init_db, get_session, select, utcnow
import scraper
from apscheduler.schedulers.background import BackgroundScheduler

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
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["brand_label"] = labels.brand
templates.env.globals["L"] = labels
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
    rows = session.exec(select(Job).where(Job.status.not_in(["closed", "hidden"]))).all()
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
    page: str = "1",
    geoerror: str = "",
    batch: str = "",
    mode: str = "",
    reset: str = "",
):
    # запоминаем фильтры в cookie и восстанавливаем при заходе на голую "/"
    _fkeys = ["q", "city", "brand", "region", "employment_type", "category", "job_level", "status", "sort"]
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
                radius = saved.get("radius", radius); group = saved.get("group", group)
            except Exception:
                pass

    home = settings_store.get_home()
    if not sort:
        sort = "distance" if home else "published"
    with get_session() as s:
        stmt = select(Job)
        if status == "active":
            stmt = stmt.where(Job.status.not_in(["closed", "hidden"]))
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
            select(func.count()).select_from(Job).where(Job.status.not_in(["closed", "hidden"]))
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
              "status": status, "sort": sort, "radius": radius, "group": group}),
        "total_active": total_active, "applied_count": applied_count, "last_update": last,
        "data_age_min": (max(0, int((utcnow() - last).total_seconds() // 60)) if last else None),
        "sync_running": _sync_state["running"],
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
    with get_session() as s:
        job = s.get(Job, job_id)
        if job:
            job.status = status
            if status == "applied":
                job.applied_at = utcnow()
            s.add(job)
            s.commit()
    ref = request.headers.get("referer")
    return RedirectResponse(ref or "/", status_code=303)


@app.post("/refresh")
def refresh(request: Request):
    # обновление уходит в фон: страница не виснет, индикатор в шапке показывает
    # «обновляется…», список сам перезагрузится по окончании
    threading.Thread(target=_sync_jobs, daemon=True).start()
    ref = request.headers.get("referer")
    return RedirectResponse(ref or "/", status_code=303)


@app.post("/presets/save")
def save_preset(request: Request, name: str = Form(...), query: str = Form("")):
    settings_store.add_preset(name, query)
    ref = request.headers.get("referer")
    return RedirectResponse(ref or "/", status_code=303)


@app.post("/presets/delete")
def delete_preset(request: Request, name: str = Form(...)):
    settings_store.delete_preset(name)
    ref = request.headers.get("referer")
    return RedirectResponse(ref or "/", status_code=303)


@app.post("/set-home")
def set_home(request: Request, address: str = Form(...)):
    lookup = labels.localize_address(address)
    coords = geo.geocode_address(lookup)
    ref = request.headers.get("referer")
    if coords:
        settings_store.set_home(address, coords[0], coords[1], lookup)
        return RedirectResponse(ref or "/?sort=distance", status_code=303)
    sep = "&" if (ref and "?" in ref) else "?"
    return RedirectResponse((ref + sep + "geoerror=1") if ref else "/?geoerror=1", status_code=303)


@app.get("/api/apply-log")
def apply_log():
    """Хвост лога последней подачи — показывается прямо на странице «Подать»."""
    path = config.BASE_DIR / "apply_last.log"
    if not path.exists():
        return JSONResponse({"ok": False, "lines": []})
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)[:120], "lines": []})
    return JSONResponse({"ok": True, "lines": lines[-120:]})


@app.get("/api/transit/{job_id}")
def api_transit(job_id: str):
    home = settings_store.get_home()
    if not home:
        return JSONResponse({"ok": False, "error": "дом не задан"})
    with get_session() as s:
        job = s.get(Job, job_id)
    if not job or job.lat is None or job.lon is None:
        return JSONResponse({"ok": False, "error": "нет координат вакансии"})
    return JSONResponse(transit.summary(home["lat"], home["lon"], job.lat, job.lon))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", geoerror: str = ""):
    profile = profile_store.load_profile()
    file_info = {
        "cv_label": profile_store.file_label(profile.get("cv_path", "")),
        "cv_status": profile_store.file_status(profile.get("cv_path", "")),
        "cover_label": profile_store.file_label(profile.get("cover_letter_path", "")),
        "cover_status": profile_store.file_status(profile.get("cover_letter_path", "")),
    }
    return templates.TemplateResponse("settings.html", {
        "request": request, "profile": profile, "file_info": file_info,
        "creds": credentials_store.status(), "home": settings_store.get_home(),
        "saved": saved, "geoerror": geoerror,
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
    profile_store.save_profile(
        _update_profile_files(profile, cv_path, cover_letter_path, cv_file, cover_letter_file))
    return RedirectResponse("/settings?saved=1", status_code=303)


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
    target = f"/job/{job_id}/apply?saved=1" if job_id else "/"
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
    ref = request.headers.get("referer")
    return RedirectResponse(ref or "/settings", status_code=303)


def _update_profile_files(profile: dict, cv_path: str, cover_letter_path: str, cv_file, cover_letter_file) -> dict:
    # приоритет: загруженный файл > указанный путь > СОХРАНИТЬ прежний (не стираем случайно)
    if cv_file and cv_file.filename:
        profile["cv_path"] = profile_store.save_upload(cv_file, "cv")
    elif cv_path.strip():
        profile["cv_path"] = cv_path.strip()

    if cover_letter_file and cover_letter_file.filename:
        profile["cover_letter_path"] = profile_store.save_upload(cover_letter_file, "cover_letter")
    elif cover_letter_path.strip():
        profile["cover_letter_path"] = cover_letter_path.strip()
    return profile


@app.post("/job/{job_id}/apply/save")
def save_apply_files(
    job_id: str,
    cv_path: str = Form(""),
    cover_letter_path: str = Form(""),
    cv_file: UploadFile | None = File(None),
    cover_letter_file: UploadFile | None = File(None),
):
    profile = profile_store.load_profile()
    profile_store.save_profile(_update_profile_files(profile, cv_path, cover_letter_path, cv_file, cover_letter_file))
    return RedirectResponse(f"/job/{job_id}/apply", status_code=303)


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
    profile_store.save_profile(_update_profile_files(profile, cv_path, cover_letter_path, cv_file, cover_letter_file))
    # вывод apply.py пишем в лог, чтобы сбои не были «молчаливыми»
    log = open(config.BASE_DIR / "apply_last.log", "w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # иначе print датских/русских символов падает (cp1251)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"        # чтобы лог писался сразу, а не после закрытия браузера
    cmd = [sys.executable, "-u", str(config.BASE_DIR / "apply.py"), job_id, "--web"]
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
def apply_batch(job_ids: list[str] = Form(default=[]), mode: str = Form("dry")):
    ids = [j for j in job_ids if j]
    if not ids:
        return RedirectResponse("/", status_code=303)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [sys.executable, "-u", str(config.BASE_DIR / "apply.py")] + ids + ["--web"]
    if mode == "submit":
        cmd.append("--submit")
    log = open(config.BASE_DIR / "apply_last.log", "w", encoding="utf-8")
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
    target = f"/job/{job_id}" + (f"?trerror={quote_plus(error)}" if error else "")
    return RedirectResponse(target, status_code=303)


@app.post("/translator/install")
def install_translator(job_id: str = Form("")):
    translator_setup.start_install()
    target = f"/job/{job_id}" if job_id else "/"
    return RedirectResponse(target, status_code=303)
