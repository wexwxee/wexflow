"""Поиск понимает фразу: «нетто херлев 15 часов до 18» (этап 1).

Человек ищет работу фразой, а не ключевыми словами. Раньше вся фраза уходила
одной строкой в LIKE по названию и описанию — «Netto Herlev» не находило
ничего, потому что такой строки внутри вакансии нет. Магазин, город, часы и
возраст — разные поля, и разбирать их надо до запроса.

Что здесь закреплено, кроме самого разбора:
  - ничего не выдумываем: непонятое остаётся обычным текстовым поиском;
  - говорим, что поняли, и даём вернуться к буквальному поиску;
  - не сужаем молча: отсеянное по часам и возрасту посчитано и показано;
  - ИИ и сеть не участвуют — поиск обязан работать без ключа и без интернета.

Запуск:  python -m pytest tests/test_query_parse.py
"""
import os
import re
import sys
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

import app as app_module
import query_parse
from db import Job

CITIES = ["Herlev", "Vejle", "København", "Tørring"]


def _parse(text, **kw):
    kw.setdefault("known_cities", CITIES)
    return query_parse.parse(text, **kw)


# ── разбор фразы ───────────────────────────────────────────────────────────

def test_shop_city_hours_and_age_are_separated():
    q = _parse("нетто херлев 15 часов до 18")
    assert q.brands == ("netto",)
    assert q.cities == ("Herlev",)
    assert q.age == "under18"
    assert (q.hours_min, q.hours_max) == (10.0, 20.0)
    assert q.rest == "", "после разбора не должно остаться мусора"


def test_bare_hours_are_a_range_and_it_is_said_out_loud():
    """«15 часов» — это «примерно 15». Придуманный разбег обязан быть виден."""
    q = _parse("кассир 15 часов")
    assert (q.hours_min, q.hours_max) == (10.0, 20.0)
    assert "около 15" in query_parse.describe(q)
    assert "до 15 часов" in query_parse.describe(q), "нужна подсказка, как уточнить"


def test_explicit_hour_bounds_win():
    assert (_parse("до 15 часов").hours_min, _parse("до 15 часов").hours_max) == (None, 15.0)
    assert (_parse("от 20 часов").hours_min, _parse("от 20 часов").hours_max) == (20.0, None)
    assert (_parse("от 10 до 20 часов").hours_min, _parse("от 10 до 20 часов").hours_max) == (10.0, 20.0)


def test_adult_is_recognised_even_when_written_as_18_plus():
    """Хвостовой \\b после «18+» однажды уже сломал всё выражение."""
    assert _parse("фётекс 18+").age == "adult"
    assert _parse("фётекс от 18").age == "adult"
    assert _parse("склад до 18").age == "under18"


def test_part_time_and_full_time():
    assert _parse("подработка кассир").employment == "partTime"
    assert _parse("полная занятость склад").employment == "fullTime"
    assert _parse("кассир").employment == ""


def test_unknown_words_stay_an_ordinary_text_search():
    q = _parse("кассир")
    assert q.brands == () and q.cities == ()
    assert "kasseassistent" in q.terms, "русский запрос ищется датскими словами"


def test_city_from_the_database_is_recognised():
    """Городов в Дании сотни — словарь алиасов их все не знает."""
    assert _parse("Tørring").cities == ("Tørring",)
    assert query_parse.parse("Tørring", known_cities=[]).cities == ()


# ── опечатки ───────────────────────────────────────────────────────────────

def test_typo_is_fixed_and_reported():
    q = _parse("нето хелев")
    assert q.brands == ("netto",) and q.cities == ("Herlev",)
    text = query_parse.describe(q)
    assert "исправил опечатку" in text and "хелев" in text


def test_meaningful_words_are_never_corrected():
    """«Кассир» — это профессия, а не промах по клавише."""
    q = _parse("кассир")
    assert q.brands == () and q.cities == ()
    assert "исправил" not in query_parse.describe(q)


def test_short_words_are_left_alone():
    assert query_parse.fuzzy("бр", labels_vocab := {"нетто": "netto"}) == ""
    assert query_parse.fuzzy("нето", labels_vocab) == "нетто"


# ── буквальный поиск ───────────────────────────────────────────────────────

def test_exact_disables_parsing():
    q = _parse("Netto Herlev", exact=True)
    assert q.brands == () and q.cities == ()
    assert q.terms == ("Netto Herlev",)
    assert "буквально" in query_parse.describe(q)


# ── отсев по часам и возрасту ──────────────────────────────────────────────

def _job(job_id, title, hours=None, level=None):
    return Job(id=job_id, source="salling", brand="netto", country="DK",
               city="Herlev", title=title, hours=hours, job_level=level, status="new")


def test_hours_filter_counts_what_it_removed():
    jobs = [_job("a", "Kasseassistent", hours="15"),
            _job("b", "Kasseassistent", hours="37"),
            _job("c", "Kasseassistent", hours=None)]
    kept, dropped = query_parse.python_filter(_parse("до 20 часов"), jobs)
    assert {j.id for j in kept} == {"a", "c"}, "часы неизвестны — не выбрасываем"
    assert dropped == {"hours": 1}


def test_age_filter_uses_the_same_rule_as_the_autopilot():
    jobs = [_job("kid", "Butiksassistent under 18 år", level="employeeUnder18"),
            _job("adult", "Kasseassistent", level="employee")]
    kept, _ = query_parse.python_filter(_parse("до 18"), jobs)
    assert {j.id for j in kept} == {"kid"}
    kept, _ = query_parse.python_filter(_parse("18+"), jobs)
    assert {j.id for j in kept} == {"adult"}


def test_nothing_is_filtered_when_nothing_was_asked():
    jobs = [_job("a", "Kasseassistent", hours="15")]
    kept, dropped = query_parse.python_filter(_parse("кассир"), jobs)
    assert kept == jobs and dropped == {}


# ── страница ───────────────────────────────────────────────────────────────

@pytest.fixture()
def feed_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_job("n1", "Kasseassistent", hours="15"))
        session.add(_job("n2", "Servicemedarbejder", hours="37"))
        session.commit()

    @contextmanager
    def factory():
        with Session(engine) as session:
            yield session

    with mock.patch.object(app_module, "get_session", factory):
        yield TestClientFactory()


class TestClientFactory:
    def get(self, url):
        from fastapi.testclient import TestClient
        return TestClient(app_module.app, base_url="http://127.0.0.1").get(url)


def test_page_says_what_it_understood(feed_client):
    page = feed_client.get("/?q=нетто херлев")
    assert page.status_code == 200
    assert "Понял так:" in page.text
    assert "магазин: Netto" in page.text and "Herlev" in page.text
    assert "искать фразу буквально" in page.text


def test_page_says_what_it_removed(feed_client):
    page = feed_client.get("/?q=до 20 часов")
    assert "Убрано по твоему запросу" in page.text
    assert "не подошли по часам" in page.text
    shown = set(re.findall(r'href="/job/([a-z0-9]+)[/"]', page.text))
    assert shown == {"n1"}


def test_exact_query_survives_the_page(feed_client):
    page = feed_client.get("/?q=Netto Herlev&exact=1")
    assert "буквально" in page.text
    assert "разобрать запрос" in page.text


def test_search_never_touches_ai_or_network(feed_client):
    with mock.patch("httpx.get", side_effect=AssertionError("поиск полез в сеть")), \
            mock.patch("httpx.post", side_effect=AssertionError("поиск полез в сеть")), \
            mock.patch("ai_gateway.chat", side_effect=AssertionError("поиск позвал ИИ")):
        page = feed_client.get("/?q=нетто херлев 15 часов до 18")
    assert page.status_code == 200
