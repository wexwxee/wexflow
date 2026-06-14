"""Геокодирование и расстояния.

- DK вакансии: точный адрес через DAWA/Dataforsyningen, fallback на индекс.
- DE/PL вакансии: центр индекса через Zippopotam, кэш на диск.
- Домашний адрес пользователя: русский город -> оригинальный, затем DAWA/Nominatim.
- Расстояние: формула гаверсинуса (км).
"""
import json
import math
import time
from pathlib import Path

import httpx

import config
import labels

CACHE_PATH = config.DATA_DIR / "geocache.json"


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_cache(cache: dict):
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")


def geocode_zip(country: str, zip_code: str, cache: dict) -> tuple[float, float] | None:
    """Координаты центра индекса. Использует и обновляет переданный cache."""
    if not country or not zip_code:
        return None
    key = f"{country}:{zip_code}"
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else None
    try:
        url = f"https://api.zippopotam.us/{country.lower()}/{zip_code}"
        r = httpx.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            place = data["places"][0]
            lat, lon = float(place["latitude"]), float(place["longitude"])
            cache[key] = [lat, lon]
            return lat, lon
    except Exception:
        pass
    cache[key] = None  # запоминаем неудачу, чтобы не долбить повторно
    return None


def geocode_dk_address(street: str, zip_code: str, cache: dict) -> tuple[float, float] | None:
    """Точные координаты датского адреса (улица+индекс) через DAWA (dataforsyningen).
    Бесплатно и без лимита. Возвращает (lat, lon)."""
    if not street or not zip_code:
        return None
    key = f"DKADDR:{zip_code}:{street.lower()}"
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else None
    try:
        r = httpx.get(
            "https://api.dataforsyningen.dk/adresser",
            params={"q": street, "postnr": zip_code, "struktur": "mini", "per_side": 1},
            timeout=15,
        )
        if r.status_code == 200:
            arr = r.json()
            if arr:
                lat, lon = float(arr[0]["y"]), float(arr[0]["x"])  # DAWA: x=lon, y=lat
                cache[key] = [lat, lon]
                return lat, lon
    except Exception:
        pass
    cache[key] = None
    return None


def geocode_address(text: str) -> tuple[float, float] | None:
    """Геокодирует свободный адрес пользователя.

    Приоритет — Дания (приложение про датские вакансии): сначала точный разбор
    через DAWA, затем щадящий поиск по части адреса, и только потом мировой
    Nominatim. Это убирает «плохой поиск», когда из-за неполного адреса
    («Sonnerupvej 104» без города) выбирался случайный объект в другой стране."""
    text = labels.localize_address(text)
    if not text:
        return None
    # 1) DAWA datavask — точный разбор полного датского адреса
    try:
        r = httpx.get(
            "https://api.dataforsyningen.dk/datavask/adresser",
            params={"betegnelse": text}, timeout=15,
        )
        if r.status_code == 200:
            res = r.json().get("resultater") or []
            if res:
                a = res[0].get("adresse") or {}
                if a.get("x") and a.get("y"):
                    return float(a["y"]), float(a["x"])
    except Exception:
        pass
    # 2) DAWA поиск по части адреса — щадящий, понимает неполный ввод (DK)
    try:
        r = httpx.get(
            "https://api.dataforsyningen.dk/adresser",
            params={"q": text, "struktur": "mini", "per_side": 1}, timeout=15,
        )
        if r.status_code == 200:
            arr = r.json()
            if arr and arr[0].get("x") and arr[0].get("y"):
                return float(arr[0]["y"]), float(arr[0]["x"])
    except Exception:
        pass
    # 3) Nominatim, но с привязкой к Дании
    try:
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": text, "format": "json", "limit": 1, "countrycodes": "dk"},
            headers={"User-Agent": "salling-jobs-personal/1.0"},
            timeout=15,
        )
        arr = r.json()
        if arr:
            return float(arr[0]["lat"]), float(arr[0]["lon"])
    except Exception:
        pass
    # 4) Nominatim без ограничения страны (если адрес реально не датский)
    try:
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": text, "format": "json", "limit": 1},
            headers={"User-Agent": "salling-jobs-personal/1.0"},
            timeout=15,
        )
        arr = r.json()
        if arr:
            return float(arr[0]["lat"]), float(arr[0]["lon"])
    except Exception:
        pass
    return None


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Координаты -> ближайший датский адрес (для кнопки «определить местоположение»)."""
    try:
        r = httpx.get(
            "https://api.dataforsyningen.dk/adgangsadresser/reverse",
            params={"x": lon, "y": lat, "struktur": "mini"}, timeout=15,
        )
        if r.status_code == 200:
            a = r.json()
            label = a.get("betegnelse")
            if label:
                return label
            parts = [a.get("vejnavn"), a.get("husnr"), a.get("postnr"), a.get("postnrnavn")]
            joined = " ".join(p for p in parts if p)
            return joined or None
    except Exception:
        pass
    return None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def geocode_jobs(jobs, force=False):
    """Заполняет lat/lon у вакансий.
    DK — точно по улице (DAWA); DE/PL — по центру индекса (Zippopotam).
    force=True перегеокодирует даже уже заполненные (для уточнения)."""
    cache = _load_cache()
    updated = 0
    new_lookups = 0
    for j in jobs:
        if not force and j.lat is not None and j.lon is not None:
            continue
        coords = None
        is_dk = (j.country or "").upper() == "DK"
        if is_dk and j.street:
            key = f"DKADDR:{j.zip}:{(j.street or '').lower()}"
            was_cached = key in cache
            coords = geocode_dk_address(j.street, j.zip, cache)
            if not was_cached:
                new_lookups += 1
                time.sleep(0.1)  # DAWA быстрый, лёгкая вежливость
        if not coords:  # фолбэк на центр индекса (и для DE/PL)
            was_cached = f"{j.country}:{j.zip}" in cache
            coords = geocode_zip(j.country, j.zip, cache)
            if not was_cached:
                new_lookups += 1
                time.sleep(0.3)
        if new_lookups and new_lookups % 50 == 0:
            _save_cache(cache)
            print(f"  обработано адресов/индексов: {new_lookups}")
        if coords:
            j.lat, j.lon = coords
            updated += 1
    _save_cache(cache)
    return updated
