"""Возраст человека — факт ленты, а не текст для поиска.

Почти треть датской розницы это ставки «under 18 år»: совершеннолетнего туда
не возьмут, и раньше они занимали треть ленты. PATH настроек подменяется на
временный файл — реальный settings.json НЕ трогаем.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select

import assistant
import feed
import query_parse
import settings_store
from db import Job


def _with_temp_settings(body):
    orig = settings_store.PATH
    settings_store.PATH = Path(tempfile.mkdtemp()) / "settings.json"
    feed._forget()
    try:
        with mock.patch.object(feed, "viewer_age", wraps=feed.viewer_age):
            body()
    finally:
        settings_store.PATH = orig
        feed._forget()


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Job(id="adult", source="salling", title="Kasseassistent",
                        country="DK", status="new"))
        session.add(Job(id="kid-level", source="salling", title="Kasseassistent",
                        country="DK", status="new", job_level="employeeUnder18"))
        session.add(Job(id="kid-title", source="salling", country="DK", status="new",
                        title="Salgsassistent under 18 år til bageriudsalget"))
        session.add(Job(id="kid-dash", source="salling", country="DK", status="new",
                        title="Servicemedarbejder under-18"))
        session.add(Job(id="mentions-kids", source="salling", country="DK", status="new",
                        title="Butiksassistent",
                        description="Unge under 18 år skal have samtykke fra forældre."))
        session.commit()
    return engine


def _visible(engine) -> set:
    with Session(engine) as session:
        return {job.id for job in session.exec(
            select(Job).where(*feed.visible_clauses())).all()}


def test_unknown_age_hides_nothing():
    def body():
        engine = _engine()
        assert feed.viewer_age() is None
        assert _visible(engine) == {
            "adult", "kid-level", "kid-title", "kid-dash", "mentions-kids",
        }
    _with_temp_settings(body)


def test_adult_does_not_see_under18_only_vacancies():
    def body():
        engine = _engine()
        feed.set_viewer_age(20)
        assert feed.viewer_age() == 20
        # Упоминание «under 18» в глубине описания — про согласие родителей для
        # юных коллег, а не про отказ взрослому. Такую вакансию не прячем.
        assert _visible(engine) == {"adult", "mentions-kids"}
    _with_temp_settings(body)


def test_teenager_sees_both_kinds():
    def body():
        engine = _engine()
        feed.set_viewer_age(16)
        assert _visible(engine) == {
            "adult", "kid-level", "kid-title", "kid-dash", "mentions-kids",
        }
    _with_temp_settings(body)


def test_submitted_application_never_disappears_because_of_age():
    def body():
        import datetime as dt

        engine = _engine()
        with Session(engine) as session:
            job = session.get(Job, "kid-level")
            job.applied_at = dt.datetime(2026, 8, 1)
            job.status = "applied"
            session.add(job)
            session.commit()
        feed.set_viewer_age(20)
        with Session(engine) as session:
            visible = {j.id for j in session.exec(
                select(Job).where(*feed.visible_clauses())).all()}
        assert "kid-level" in visible
    _with_temp_settings(body)


def test_in_memory_rule_matches_the_sql_rule():
    def body():
        engine = _engine()
        feed.set_viewer_age(20)
        with Session(engine) as session:
            rows = list(session.exec(select(Job)).all())
            by_sql = _visible(engine)
        for job in rows:
            assert feed.visible(job) is (job.id in by_sql), job.id
    _with_temp_settings(body)


def test_forgetting_the_age_brings_the_vacancies_back():
    def body():
        engine = _engine()
        feed.set_viewer_age(20)
        assert "kid-level" not in _visible(engine)
        assert feed.set_viewer_age(0) is None
        assert feed.viewer_age() is None
        assert "kid-level" in _visible(engine)
    _with_temp_settings(body)


def test_exact_birth_date_from_the_profile_wins_over_a_stated_age():
    def body():
        feed.set_viewer_age(15)
        with mock.patch.object(feed, "_parse_birth_date",
                               return_value=__import__("datetime").date(1990, 1, 1)):
            assert feed.viewer_age() >= 30
    _with_temp_settings(body)


# ── разбор фразы ───────────────────────────────────────────────────────────

def test_person_states_age_in_plain_words():
    assert query_parse.stated_age("мне 20 лет") == 20
    assert query_parse.stated_age("мне 20") == 20
    assert query_parse.stated_age("20 лет") == 20
    assert query_parse.stated_age("мне уже 18") == 18
    assert query_parse.stated_age("мне 8 лет") is None
    # «15 часов» — это часы, а не возраст, и номер магазина тоже не возраст.
    assert query_parse.stated_age("нетто херлев 15 часов") is None
    assert query_parse.stated_age("нетто 18") is None


def test_stated_age_becomes_a_filter_and_leaves_no_search_words():
    parsed = query_parse.parse("мне 20 лет")
    assert parsed.age == "adult"
    assert not parsed.terms
    assert "20" in query_parse.describe(parsed)

    teen = query_parse.parse("мне 16 лет")
    # Подростку открыты оба вида ставок, поэтому его возраст ничего не отсекает.
    assert teen.age == ""
    assert not teen.terms


def test_hours_are_still_hours_next_to_an_age():
    parsed = query_parse.parse("нетто херлев 15 часов мне 20 лет")
    assert parsed.hours_min is not None
    assert parsed.age == "adult"
    assert "херлев" in parsed.raw.lower()


# ── помощник ───────────────────────────────────────────────────────────────

def test_assistant_treats_a_bare_age_as_a_fact_not_a_search():
    assert assistant.guess_tool("мне 20 лет") == ("set_age", {"age": 20})
    assert assistant.guess_tool("мне уже 18") == ("set_age", {"age": 18})
    assert assistant.guess_tool("20 лет") == ("set_age", {"age": 20})


def test_assistant_keeps_searching_when_the_phrase_also_asks_for_something():
    name, args = assistant.guess_tool("нетто херлев мне 20")
    assert name == "search_jobs"
    assert args["query"] == "нетто херлев мне 20"
    assert assistant.guess_tool("что есть рядом")[0] == "nearby_jobs"


def test_set_age_tool_reports_what_disappeared_from_the_feed():
    def body():
        engine = _engine()
        import db

        with mock.patch.object(db, "get_session", lambda: Session(engine)):
            result = assistant.run("set_age", {"age": 20})
        assert result["ok"] is True
        assert "20" in result["reply"]
        assert "under 18" in result["reply"]
        assert feed.viewer_age() == 20
    _with_temp_settings(body)


def test_set_age_refuses_nonsense_instead_of_inventing_an_age():
    for nonsense in (3, 250, "двадцать", None):
        result = assistant.run("set_age", {"age": nonsense})
        assert result["ok"] is False, nonsense
        assert "не понял" in result["reply"].lower()


def test_a_typo_in_the_age_field_forgets_it_instead_of_hiding_the_feed():
    def body():
        engine = _engine()
        feed.set_viewer_age(20)
        assert feed.set_viewer_age(120) is None
        assert feed.viewer_age() is None
        assert "kid-level" in _visible(engine)
    _with_temp_settings(body)
