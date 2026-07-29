"""Маршруты живут в базе, а не в JSON-файле, и имеют срок годности.

Иван: «сделай базу, где уже загруженные показывают маршрут, чтобы не грузилось —
и в боте, и в приложении; маршрут устареет, но можно обновлять раз в 2 недели».

Проверяем ровно это:
  * посчитанное отдаётся мгновенно и не пересчитывается;
  * один адрес = одна запись (три вакансии в магазине делят маршрут);
  * удачный маршрут годен 14 дней, неудачный перепроверяется через 2;
  * запись одного маршрута не переписывает остальные.
"""
import os
import sys
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import transit
from db import TransitRoute, get_session, utcnow

# Синтетические координаты (нулевой меридиан у экватора): тесты пишут в ту же
# базу, что и приложение, поэтому не должны задевать НАСТОЯЩИЕ маршруты Ивана.
HOME = (0.1000, 0.1000)
SHOP = (0.2000, 0.2000)


def _clean(key):
    with get_session() as s:
        row = s.get(TransitRoute, key)
        if row is not None:
            s.delete(row)
            s.commit()


def test_route_is_stored_and_reused_without_network():
    db.init_db()
    key = transit.cache_key(*HOME, *SHOP)
    _clean(key)
    payload = {"itineraries": [{"duration": 480, "legs": [
        {"mode": "BUS", "routeShortName": "5C"}]}]}
    with mock.patch.object(transit.httpx, "get",
                           return_value=mock.Mock(json=lambda: payload)) as net:
        first = transit.summary(*HOME, *SHOP)
        assert first["ok"] and first["minutes"] == 8 and first["modes"] == ["5C"]
        assert net.call_count == 1
        # второй раз сеть не трогаем — берём из базы
        again = transit.cached(*HOME, *SHOP)
        assert again["minutes"] == 8 and net.call_count == 1
    _clean(key)


def test_same_address_shares_one_row():
    """Три вакансии в одном магазине — один расчёт на всех."""
    db.init_db()
    key = transit.cache_key(*HOME, *SHOP)
    _clean(key)
    with get_session() as s:
        s.add(TransitRoute(key=key, ok=True, minutes=8, transfers=0, modes="5C"))
        s.commit()
    # координаты округляются до 4 знаков (~11 м) — соседние двери дают тот же ключ
    assert transit.cache_key(0.10000004, 0.10000002, *SHOP) == key
    assert transit.cached(*HOME, *SHOP)["minutes"] == 8
    _clean(key)


def test_old_route_expires_and_is_recomputed():
    db.init_db()
    key = transit.cache_key(*HOME, *SHOP)
    _clean(key)
    with get_session() as s:
        s.add(TransitRoute(key=key, ok=True, minutes=8, modes="5C",
                           updated_at=utcnow() - timedelta(days=transit.FRESH_DAYS + 1)))
        s.commit()
    assert transit.cached(*HOME, *SHOP) is None, "просроченный маршрут выдаётся за свежий"
    assert transit.has_record(*HOME, *SHOP) is True, "запись есть — это ОБНОВЛЕНИЕ, не первый расчёт"
    _clean(key)


def test_failed_route_is_retried_sooner_than_good_one():
    """Осечка сети не должна навсегда оставлять вакансию без времени в пути."""
    db.init_db()
    key = transit.cache_key(*HOME, *SHOP)
    _clean(key)
    with get_session() as s:
        s.add(TransitRoute(key=key, ok=False, error="нет маршрута",
                           updated_at=utcnow() - timedelta(days=transit.RETRY_DAYS + 1)))
        s.commit()
    assert transit.cached(*HOME, *SHOP) is None
    # но свежая неудача ещё держится — не долбим сеть по кругу
    with get_session() as s:
        row = s.get(TransitRoute, key)
        row.updated_at = utcnow()
        s.add(row)
        s.commit()
    res = transit.cached(*HOME, *SHOP)
    assert res is not None and res["ok"] is False
    _clean(key)


def test_writing_one_route_keeps_neighbours():
    """Раньше файл переписывался целиком — теперь пишется одна строка."""
    db.init_db()
    other_shop = (0.3000, 0.3000)
    k1, k2 = transit.cache_key(*HOME, *SHOP), transit.cache_key(*HOME, *other_shop)
    _clean(k1)
    _clean(k2)
    with get_session() as s:
        s.add(TransitRoute(key=k1, ok=True, minutes=8, modes="5C"))
        s.commit()
    transit._store(k2, {"ok": True, "minutes": 18, "transfers": 0, "modes": ["22"]})
    assert transit.cached(*HOME, *SHOP)["minutes"] == 8, "соседний маршрут потерялся"
    assert transit.cached(*HOME, *other_shop)["minutes"] == 18
    _clean(k1)
    _clean(k2)
