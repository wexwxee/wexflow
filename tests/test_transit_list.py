"""Время в пути в списке приложения на ПК — без долгих загрузок.

Иван: «пусть в приложении на ПК, если это возможно без долгих загрузок, тоже
пишется кол-во минут до места и на каком транспорте».

Условие «без загрузок» = берём ТОЛЬКО готовое из кэша и читаем его ОДИН раз на
весь список. Раньше per-job вызов transit.cached() перечитывал бы весь файл
кэша на каждую из сотен вакансий.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import transit


class _Job:
    def __init__(self, jid, lat, lon):
        self.id = jid
        self.lat = lat
        self.lon = lon


HOME = {"lat": 55.7050, "lon": 12.4900}


def test_snapshot_is_read_once_for_many_jobs():
    """Один заход на диск на весь список, а не по разу на вакансию."""
    jobs = [_Job(f"j{i}", 55.70 + i / 1000, 12.49) for i in range(50)]
    with mock.patch.object(transit, "_load", return_value={}) as load:
        cache = transit.snapshot()
        for j in jobs:
            app._transit_fields(j, HOME, cache)
    assert load.call_count == 1, f"кэш прочитан {load.call_count} раз вместо одного"


def test_ready_route_becomes_payload_fields():
    job = _Job("j1", 55.7060, 12.4930)
    key = transit.cache_key(HOME["lat"], HOME["lon"], job.lat, job.lon)
    cache = {key: {"ok": True, "minutes": 18, "transfers": 0, "modes": ["22"],
                   "kinds": ["bus"]}}
    fields = app._transit_fields(job, HOME, cache)
    assert fields == {"transitMin": 18, "transitTransfers": 0,
                      "transitModes": "22", "transitKinds": "bus"}


def test_old_route_without_kinds_still_gets_transport_icon():
    """Маршруты, посчитанные до появления вида транспорта, не остаются без
    иконки: вид читается по номеру линии (M3 — метро, A — S-tog, 5C — автобус)."""
    job = _Job("j4", 55.7060, 12.4930)
    key = transit.cache_key(HOME["lat"], HOME["lon"], job.lat, job.lon)
    cache = {key: {"ok": True, "minutes": 25, "transfers": 2, "modes": ["5C", "A", "M3"]}}
    assert app._transit_fields(job, HOME, cache)["transitKinds"] == "bus, train, metro"


def test_unknown_or_failed_route_adds_nothing():
    """Пока маршрут не посчитан — молчим (в карточке останется «по прямой»)."""
    job = _Job("j2", 55.7060, 12.4930)
    assert app._transit_fields(job, HOME, {}) == {}
    key = transit.cache_key(HOME["lat"], HOME["lon"], job.lat, job.lon)
    assert app._transit_fields(job, HOME, {key: {"ok": False, "error": "нет маршрута"}}) == {}
    assert app._transit_fields(_Job("j3", None, None), HOME, {}) == {}
    assert app._transit_fields(job, None, {}) == {}


def test_list_template_shows_minutes_and_transport():
    """Разметка списка обязана показывать минуты и транспорт, а расстояние —
    честно подписывать «по прямой»."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    assert "trips[j.id].minutes" in html, "в списке нет времени в пути"
    assert "trips[j.id].modes" in html, "в списке не видно, на чём ехать"
    assert "trips[j.id].kinds" in html, "в бейдже нет иконки вида транспорта"
    assert "км по прямой" in html, "расстояние не подписано как «по прямой»"
