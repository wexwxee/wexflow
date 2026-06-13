"""Время в пути на общественном транспорте через Transitous (бесплатно, без ключа).

Считается ПО ЗАПРОСУ для одной вакансии (дом → магазин) и кэшируется на диск,
поэтому повторно — мгновенно. Для всего списка из 2400 не вызываем (Transitous
сериализует запросы по IP и медленный).
"""
import json

import httpx

import config

CACHE_PATH = config.DATA_DIR / "transit_cache.json"
URL = "https://api.transitous.org/api/v1/plan"
TRANSIT_MODES = {
    "BUS", "COACH", "TRAM", "SUBWAY", "METRO", "RAIL", "REGIONAL_RAIL",
    "REGIONAL_FAST_RAIL", "SUBURBAN", "HIGHSPEED_RAIL", "LONG_DISTANCE",
    "NIGHT_RAIL", "FERRY",
}


def _load() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(d: dict):
    try:
        CACHE_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def summary(flat: float, flng: float, tlat: float, tlng: float) -> dict:
    """Лучший маршрут на ОТ: {ok, minutes, transfers, modes:[...]}."""
    key = f"{round(flat, 4)},{round(flng, 4)}|{round(tlat, 4)},{round(tlng, 4)}"
    cache = _load()
    if key in cache:
        return cache[key]
    try:
        r = httpx.get(
            URL,
            params={"fromPlace": f"{flat},{flng}", "toPlace": f"{tlat},{tlng}", "arriveBy": "false"},
            headers={"User-Agent": "salling-jobs-personal/1.0"},
            timeout=30,
        )
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    its = data.get("itineraries") or []
    if not its:
        res = {"ok": False, "error": "нет маршрута"}
        cache[key] = res
        _save(cache)
        return res
    best = min(its, key=lambda it: it.get("duration", 1e12))
    minutes = round(best.get("duration", 0) / 60)
    modes = []
    for leg in best.get("legs", []):
        if leg.get("mode") in TRANSIT_MODES:
            modes.append(leg.get("routeShortName") or leg.get("routeLongName") or leg.get("mode"))
    res = {"ok": True, "minutes": minutes, "transfers": max(0, len(modes) - 1), "modes": modes[:5]}
    cache[key] = res
    _save(cache)
    return res
