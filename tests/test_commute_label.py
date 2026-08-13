"""Время в пути должно читаться как время, а не как ошибка программы.

13.08.2026 Иван сказал, что сортировка «быстрее добраться» «слабо сделана и
багованная». Данные проверку выдержали: 1572 маршрута из 1682, самый быстрый —
9 минут на 0.3 км, ни одного случая «далеко, но быстро». А вот подпись «1276
мин» для поездки через всю Данию человек читает как поломку, хотя это честные
21 час с шестью пересадками.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labels


def test_short_trips_stay_in_minutes():
    assert labels.commute_label(9) == "9 мин"
    assert labels.commute_label(47) == "47 мин"
    assert labels.commute_label(89) == "89 мин"


def test_long_trips_become_hours():
    assert labels.commute_label(90) == "1 ч 30 мин"
    assert labels.commute_label(103) == "1 ч 43 мин"
    assert labels.commute_label(120) == "2 ч"
    assert labels.commute_label(1276) == "21 ч 16 мин"


def test_nonsense_is_shown_as_nothing():
    for value in (0, -5, None, "", "abc"):
        assert labels.commute_label(value) == "", value


def test_the_feed_uses_the_helper_and_not_raw_minutes():
    html = (Path(__file__).resolve().parent.parent
            / "templates" / "index.html").read_text(encoding="utf-8")
    assert "L.commute_label(trips[j.id].minutes)" in html
    assert "L.commute_label(g.trip.minutes)" in html
    assert not re.search(r"trips\[j\.id\]\.minutes\s*\}\}\s*мин", html), (
        "где-то остались сырые минуты"
    )


def test_the_feed_admits_when_routes_are_still_being_counted():
    html = (Path(__file__).resolve().parent.parent
            / "templates" / "index.html").read_text(encoding="utf-8")
    assert "f.sort == 'commute' and route_filter_stats.pending" in html
    assert "маршрут ещё считается" in html
