"""Время в пути на общественном транспорте через Transitous (бесплатно, без ключа).

Считается ПО ЗАПРОСУ для одной вакансии (дом → магазин) и кэшируется на диск,
поэтому повторно — мгновенно. Для всего списка из 2400 не вызываем (Transitous
сериализует запросы по IP и медленный).
"""
import threading
from datetime import timedelta

import httpx

import config
from db import TransitRoute, get_session, select, utcnow
from json_store import read_json

CACHE_PATH = config.DATA_DIR / "transit_cache.json"   # старый файл — только для переноса

# Срок годности маршрута. Расписания меняются редко, поэтому удачный маршрут
# живёт две недели; неудачный (сбой сети, «нет маршрута») перепроверяем быстрее,
# иначе одна осечка Transitous навсегда оставила бы вакансию без времени в пути.
FRESH_DAYS = 14
RETRY_DAYS = 2
URL = "https://api.transitous.org/api/v1/plan"
TRANSIT_MODES = {
    "BUS", "COACH", "TRAM", "SUBWAY", "METRO", "RAIL", "REGIONAL_RAIL",
    "REGIONAL_FAST_RAIL", "SUBURBAN", "HIGHSPEED_RAIL", "LONG_DISTANCE",
    "NIGHT_RAIL", "FERRY",
}
_CACHE_LOCK = threading.RLock()
_MIGRATED = False


def _row_to_dict(row) -> dict:
    if not row.ok:
        return {"ok": False, "error": row.error or "нет маршрута", "ts": row.updated_at}
    return {
        "ok": True,
        "minutes": int(row.minutes or 0),
        "transfers": int(row.transfers or 0),
        "modes": [m.strip() for m in str(row.modes or "").split(",") if m.strip()],
        "ts": row.updated_at,
    }


def _import_old_file() -> None:
    """Разовый перенос transit_cache.json в базу: посчитанное раньше не теряем."""
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True
    try:
        old = read_json(CACHE_PATH, {}, dict)
        if not old:
            return
        with get_session() as s:
            for key, val in old.items():
                if not isinstance(val, dict) or s.get(TransitRoute, key) is not None:
                    continue
                s.add(TransitRoute(
                    key=str(key)[:120],
                    ok=bool(val.get("ok")),
                    minutes=int(val.get("minutes") or 0),
                    transfers=int(val.get("transfers") or 0),
                    modes=", ".join(str(m) for m in (val.get("modes") or []) if m)[:120],
                    error=str(val.get("error") or "")[:120],
                ))
            s.commit()
        CACHE_PATH.replace(CACHE_PATH.with_suffix(".json.imported"))
    except Exception as e:  # noqa: BLE001 — перенос не должен ронять приложение
        print(f"transit: старый кэш не перенёсся — {e}")


def _load() -> dict:
    """Весь кэш маршрутов из базы (ключ → ответ). Используется снимком."""
    _import_old_file()
    out = {}
    try:
        with get_session() as s:
            for row in s.exec(select(TransitRoute)).all():
                out[row.key] = _row_to_dict(row)
    except Exception as e:  # noqa: BLE001
        print(f"transit: кэш не прочитался — {e}")
    return out


def _store(key: str, res: dict) -> None:
    """Записать/обновить один маршрут. Пишется одна строка, а не весь файл."""
    try:
        with get_session() as s:
            row = s.get(TransitRoute, key)
            if row is None:
                row = TransitRoute(key=key)
            row.ok = bool(res.get("ok"))
            row.minutes = int(res.get("minutes") or 0)
            row.transfers = int(res.get("transfers") or 0)
            row.modes = ", ".join(str(m) for m in (res.get("modes") or []) if m)[:120]
            row.error = str(res.get("error") or "")[:120]
            row.updated_at = utcnow()
            s.add(row)
            s.commit()
    except Exception as e:  # noqa: BLE001 — кэш не должен ронять расчёт
        print(f"transit: маршрут не сохранился — {e}")


def is_fresh(res: dict | None) -> bool:
    """Годен ли ответ: удачный живёт FRESH_DAYS, неудачный — RETRY_DAYS."""
    if not res:
        return False
    ts = res.get("ts")
    if ts is None:
        return True          # старая запись без даты — считаем годной, обновится позже
    try:
        age = utcnow() - ts
    except TypeError:
        return True
    return age <= timedelta(days=FRESH_DAYS if res.get("ok") else RETRY_DAYS)


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
    """Готовый маршрут из уже прочитанного кэша (без диска и сети).
    Протухший (см. is_fresh) считается отсутствующим — его пересчитают."""
    if not cache:
        return None
    res = cache.get(cache_key(flat, flng, tlat, tlng))
    return res if is_fresh(res) else None


def has_record(flat: float, flng: float, tlat: float, tlng: float) -> bool:
    """Есть ли вообще запись (пусть и просроченная). Нужно, чтобы сперва считать
    НИКОГДА не считанные, а обновление старых оставить на потом."""
    try:
        with get_session() as s:
            return s.get(TransitRoute, cache_key(flat, flng, tlat, tlng)) is not None
    except Exception:  # noqa: BLE001
        return False


def cached(flat: float, flng: float, tlat: float, tlng: float) -> dict | None:
    """Готовый и ещё годный ответ из кэша, иначе None (значит — надо считать)."""
    try:
        _import_old_file()
        with get_session() as s:
            row = s.get(TransitRoute, cache_key(flat, flng, tlat, tlng))
        if row is None:
            return None
        res = _row_to_dict(row)
        return res if is_fresh(res) else None
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
        res = {"ok": False, "error": str(e)[:80]}
        _store(key, res)      # перепроверим через RETRY_DAYS, а не в цикле
        return res
    its = data.get("itineraries") or []
    if not its:
        res = {"ok": False, "error": "нет маршрута"}
        _store(key, res)
        return res
    best = min(its, key=lambda it: it.get("duration", 1e12))
    minutes = round(best.get("duration", 0) / 60)
    modes = []
    for leg in best.get("legs", []):
        if leg.get("mode") in TRANSIT_MODES:
            modes.append(leg.get("routeShortName") or leg.get("routeLongName") or leg.get("mode"))
    res = {"ok": True, "minutes": minutes, "transfers": max(0, len(modes) - 1), "modes": modes[:5]}
    # Пишется ровно одна строка базы — соседние маршруты не трогаются,
    # параллельный расчёт ничего не теряет.
    _store(key, res)
    return res
