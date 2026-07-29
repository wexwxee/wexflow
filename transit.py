"""Время в пути на общественном транспорте через Transitous (бесплатно, без ключа).

Считается ПО ЗАПРОСУ для одной вакансии (дом → магазин) и кэшируется на диск,
поэтому повторно — мгновенно. Для всего списка из 2400 не вызываем (Transitous
сериализует запросы по IP и медленный).
"""
import threading

import httpx

import config
from json_store import atomic_write_json, read_json

CACHE_PATH = config.DATA_DIR / "transit_cache.json"
URL = "https://api.transitous.org/api/v1/plan"
TRANSIT_MODES = {
    "BUS", "COACH", "TRAM", "SUBWAY", "METRO", "RAIL", "REGIONAL_RAIL",
    "REGIONAL_FAST_RAIL", "SUBURBAN", "HIGHSPEED_RAIL", "LONG_DISTANCE",
    "NIGHT_RAIL", "FERRY",
}
_CACHE_LOCK = threading.RLock()


def _load() -> dict:
    return read_json(CACHE_PATH, {}, dict)


def _save(d: dict):
    try:
        atomic_write_json(CACHE_PATH, d)
    except OSError:
        pass


def cache_key(flat: float, flng: float, tlat: float, tlng: float) -> str:
    return f"{round(flat, 4)},{round(flng, 4)}|{round(tlat, 4)},{round(tlng, 4)}"


def snapshot() -> dict:
    """Весь кэш маршрутов одним чтением. Нужен там, где ищем время в пути сразу
    для сотен вакансий (список приложения, синк в телефон): по-элементно это
    было бы сотнями чтений одного и того же файла."""
    try:
        with _CACHE_LOCK:
            return dict(_load())
    except Exception:  # noqa: BLE001
        return {}


def from_snapshot(cache: dict, flat: float, flng: float, tlat: float, tlng: float) -> dict | None:
    """Готовый маршрут из уже прочитанного кэша (без диска и сети)."""
    if not cache:
        return None
    return cache.get(cache_key(flat, flng, tlat, tlng))


def cached(flat: float, flng: float, tlat: float, tlng: float) -> dict | None:
    """Готовый ответ из кэша или None. Без сети — годится и для списка из 500
    вакансий (payload в телефон), и для отбора кандидатов фоновым воркером."""
    try:
        with _CACHE_LOCK:
            return _load().get(cache_key(flat, flng, tlat, tlng))
    except Exception:  # noqa: BLE001 — кэш не должен ронять вызывающего
        return None


def summary(flat: float, flng: float, tlat: float, tlng: float) -> dict:
    """Лучший маршрут на ОТ: {ok, minutes, transfers, modes:[...]}."""
    key = cache_key(flat, flng, tlat, tlng)
    with _CACHE_LOCK:
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
        with _CACHE_LOCK:
            cache = _load()
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
    # Другой запрос мог сохранить свой маршрут, пока сеть отвечала: перечитываем
    # последнюю версию и добавляем результат, не теряя соседнюю запись.
    with _CACHE_LOCK:
        cache = _load()
        cache[key] = res
        _save(cache)
    return res
