"""Рекомендации: порядок с причиной, и только по желанию (этап 3).

Главное, что здесь закреплено, — не формула, а обещания продукта:
  - выключено по умолчанию, и пока выключено, лента не меняется ни на байт;
  - это СОРТИРОВКА, а не фильтр: ни одна вакансия не исчезает;
  - у каждой рекомендации есть причина, и причина — правда;
  - человек не заперт в пузыре: вклад истории ограничен, в верхушке есть
    места для того, где он ещё не пробовал;
  - порядок стабильный: список, который прыгает при обновлении, не читают;
  - ни ИИ, ни сети.

Запуск:  python -m pytest tests/test_recommend.py
"""
import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import recommend
from db import Job, utcnow

HOME = {"lat": 55.70, "lon": 12.55}
NEAR = (55.705, 12.552)     # ~0.6 км от дома
FAR = (55.95, 12.90)        # далеко за радиусом


def _job(job_id, title, point=NEAR, *, brand="netto", hours="15", fit="ok",
         engine="ai:gemini", level=None, days=0, categories="salesGeneral",
         status="new"):
    return Job(id=job_id, source="salling", brand=brand, country="DK",
               city="Herlev", title=title, status=status, hours=hours,
               categories=categories, job_level=level,
               lat=point[0], lon=point[1], fit=fit, fit_engine=engine,
               first_seen=utcnow() - dt.timedelta(days=days))


def _ctx(**kw):
    kw.setdefault("home", HOME)
    kw.setdefault("radius_km", 15.0)
    return recommend.Context(**kw)


def test_off_by_default():
    """Порядок ленты человек не просил менять — значит, не меняем."""
    with mock.patch.object(recommend.settings_store, "load", lambda: {}):
        assert recommend.enabled() is False


def test_nothing_disappears_only_the_order_changes():
    jobs = [_job("a", "Kasseassistent"), _job("b", "Souschef"), _job("c", "Slagter")]
    ranked = recommend.rank(jobs, _ctx())
    assert {row[0].id for row in ranked} == {"a", "b", "c"}, "рекомендации что-то потеряли"


def test_reason_is_true_and_not_empty():
    job = _job("a", "Kasseassistent", NEAR, hours="15", fit="ok")
    value, reasons = recommend.score(job, _ctx(hours_min=10, hours_max=20))
    assert value > 0
    assert any("рядом с домом" in r for r in reasons)
    assert any("без датского" in r for r in reasons)
    assert any("15 ч/нед" in r for r in reasons)
    # причина не выдумана: убираем факт — исчезает и строка
    _v, plain = recommend.score(_job("b", "Kasseassistent", FAR, fit="unclear"), _ctx())
    assert not any("рядом с домом" in r for r in plain)
    assert not any("без датского" in r for r in plain)


def test_near_beats_far():
    near, far = _job("near", "Kasseassistent", NEAR), _job("far", "Kasseassistent", FAR)
    ranked = recommend.rank([far, near], _ctx())
    assert [row[0].id for row in ranked] == ["near", "far"]


def test_leadership_is_not_recommended():
    plain = _job("plain", "Kasseassistent")
    boss = _job("boss", "Souschef")
    ranked = recommend.rank([boss, plain], _ctx())
    assert [row[0].id for row in ranked] == ["plain", "boss"]


def test_skipped_goes_down():
    """Пропустил дважды — это решение человека, а не случайность."""
    ctx = _ctx(skipped_brands={"netto"})
    skipped = _job("s", "Kasseassistent", brand="netto")
    other = _job("o", "Kasseassistent", brand="foetex")
    ranked = recommend.rank([skipped, other], ctx)
    assert [row[0].id for row in ranked] == ["o", "s"]


def test_age_mismatch_sinks():
    ctx = _ctx(age="adult")
    kid = _job("kid", "Butiksassistent under 18 år", level="employeeUnder18")
    grown = _job("grown", "Kasseassistent")
    ranked = recommend.rank([kid, grown], ctx)
    assert [row[0].id for row in ranked] == ["grown", "kid"]


def test_history_contribution_is_capped():
    """Иначе человек навсегда заперт в одной сети и одной роли."""
    ctx = _ctx(liked_brands={"netto"}, liked_categories={"salesGeneral"},
               liked_roles={"whatever"}, seen_roles=set())
    job = _job("a", "Kasseassistent")
    with_history, _ = recommend.score(job, ctx)
    without, _ = recommend.score(job, _ctx())
    assert with_history - without <= recommend.HISTORY_CAP + 0.01


def test_top_keeps_room_for_something_new():
    ctx = _ctx(liked_brands={"netto"})
    known = [_job(f"n{i}", "Kasseassistent", brand="netto") for i in range(12)]
    fresh = [_job("new1", "Kasseassistent", brand="foetex"),
             _job("new2", "Kasseassistent", brand="bilka")]
    ranked = recommend.rank(known + fresh, ctx)
    top = [row[0] for row in ranked[:recommend.TOP_WINDOW]]
    outsiders = [j for j in top if str(j.brand) not in ctx.liked_brands]
    assert len(outsiders) >= recommend.FRESH_BLOOD_IN_TOP


def test_order_is_stable_for_equal_scores():
    jobs = [_job("a", "Kasseassistent"), _job("b", "Kasseassistent"),
            _job("c", "Kasseassistent")]
    first = [row[0].id for row in recommend.rank(jobs, _ctx())]
    second = [row[0].id for row in recommend.rank(jobs, _ctx())]
    assert first == second == ["a", "b", "c"]


def test_scoring_never_calls_ai_or_network():
    with mock.patch("httpx.get", side_effect=AssertionError("рекомендации полезли в сеть")), \
            mock.patch("httpx.post", side_effect=AssertionError("рекомендации полезли в сеть")), \
            mock.patch("ai_gateway.chat", side_effect=AssertionError("рекомендации позвали ИИ")):
        recommend.rank([_job("a", "Kasseassistent")], _ctx())


def test_sort_value_survives_the_filter_cleaner():
    """Без белого списка кука фильтров и профили поиска выбросили бы «fit»."""
    import app as app_module

    cleaned = app_module._clean_filter_query("q=%D0%BD%D0%B5%D1%82%D1%82%D0%BE&sort=fit")
    assert "sort=fit" in cleaned
    # а выдуманная сортировка по-прежнему выбрасывается
    assert "sort=" not in app_module._clean_filter_query("sort=magic")


def test_toggle_writes_the_setting():
    store = {}
    with mock.patch.object(recommend.settings_store, "load", lambda: store), \
            mock.patch.object(recommend.settings_store, "mutate",
                              lambda fn: (fn(store), store)[1]):
        assert recommend.set_enabled(True) is True
        assert store[recommend.SETTINGS_KEY] is True
        assert recommend.enabled() is True
        recommend.set_enabled(False)
        assert recommend.enabled() is False
