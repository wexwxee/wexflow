"""Языковой фильтр прячет только доказанное (пересмотр 11.08.2026).

Старое правило прятало всё, что ИИ пометил «нужен датский», и лента
схлопнулась: из 2549 открытых датских вакансий пропали 2255. Разбор показал,
что 1707 из них — просто догадка модели о рядовой работе («требует общения с
покупателями»), которой в самом объявлении нет. Пропали именно те роли, ради
которых существует WexFlow: Butiksassistent, Servicemedarbejder, Kasseassistent.
Живой контрпример: в Netto берут без датского, человек туда уже подавался.

Новое правило: прячем цитату из объявления и руководящие должности; догадку
ИИ о рядовой работе показываем с пометкой. Тест держит именно это — и то,
что «поданные» не страдают ни при каком правиле.

Запуск:  python -m pytest tests/test_relevance_soft.py
"""
import os
import re
import sys
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import app as app_module
import feed
import relevance
from db import Job


def _job(job_id, title, fit, engine, status="new"):
    return Job(id=job_id, source="salling", brand="netto", country="DK",
               city="Herlev", title=title, status=status,
               fit=fit, fit_engine=engine,
               fit_reason="Требует общения с покупателями на датском языке.")


@pytest.fixture()
def feed_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        # догадка ИИ о рядовой работе — остаётся в ленте с пометкой
        session.add(_job("guess", "Butiksassistent under 18 år", "danish", "ai:gemini-2.5-flash"))
        # цитата из объявления — прячем
        session.add(_job("quoted", "Kasseassistent", "danish", "rules"))
        # руководящая по мнению ИИ — прячем
        session.add(_job("lead", "Souschef - Hellerup", "danish", "ai:gemini-2.5-flash"))
        # чисто и неоценённое — как было
        session.add(_job("ok", "Morgenopfylder", "ok", "ai:gemini-2.5-flash"))
        session.add(_job("none", "Lagermedarbejder", None, None))
        # поданная с «барьером» — история человека, не прячется никогда
        session.add(_job("applied", "Servicemedarbejder", "danish", "rules", status="applied"))
        session.commit()

    @contextmanager
    def factory():
        with Session(engine) as session:
            yield session

    return engine, factory


def _visible(engine) -> set[str]:
    with Session(engine) as session:
        rows = session.exec(select(Job).where(*feed.visible_clauses())).all()
    return {j.id for j in rows if not relevance.is_barrier(j)}


def test_ai_guess_about_ordinary_work_no_longer_hides(feed_db):
    engine, _ = feed_db
    assert "guess" in _visible(engine), "догадка ИИ снова прячет рядовую работу"


def test_quote_from_the_advert_still_hides(feed_db):
    engine, _ = feed_db
    assert "quoted" not in _visible(engine)
    assert relevance.is_barrier(_job("q", "Kasseassistent", "danish", "rules"))


def test_leadership_still_hides_even_when_only_ai_said_so(feed_db):
    engine, _ = feed_db
    assert "lead" not in _visible(engine)


def test_clean_and_unjudged_are_untouched(feed_db):
    engine, _ = feed_db
    visible = _visible(engine)
    assert {"ok", "none"} <= visible


def test_applied_never_disappears(feed_db):
    """«Подано» — история человека: её не трогает ни одно правило ленты."""
    engine, _ = feed_db
    with Session(engine) as session:
        applied = session.exec(select(Job).where(Job.status == "applied")).all()
    assert [j.id for j in applied] == ["applied"]


def test_soft_badge_is_only_for_the_guess(feed_db):
    guess = _job("g", "Butiksassistent under 18 år", "danish", "ai:gemini-2.5-flash")
    quoted = _job("q", "Kasseassistent", "danish", "rules")
    lead = _job("l", "Souschef", "danish", "ai:gemini-2.5-flash")
    assert relevance.soft_barrier(guess) and not relevance.is_barrier(guess)
    assert not relevance.soft_barrier(quoted) and relevance.is_barrier(quoted)
    assert not relevance.soft_barrier(lead) and relevance.is_barrier(lead)


def test_describe_says_it_is_a_guess(feed_db):
    view = relevance.describe(_job("g", "Butiksassistent", "danish", "ai:gemini-2.5-flash"))
    assert view["soft"] is True and view["barrier"] is False
    assert view["label"] == "возможно, нужен датский"


def test_in_memory_feed_uses_the_same_soft_barrier_rule(feed_db):
    """Background/phone helpers must agree with the SQL-backed web feed."""
    guess = _job("g", "Butiksassistent", "danish", "ai:gemini-2.5-flash")
    quoted = _job("q", "Kasseassistent", "danish", "rules")
    lead = _job("l", "Souschef", "danish", "ai:gemini-2.5-flash")
    with mock.patch.object(feed, "allows", return_value=True), \
         mock.patch.object(feed, "broken_sources", return_value=()), \
         mock.patch.object(feed, "hide_barrier", return_value=True):
        assert feed.visible(guess) is True
        assert feed.visible(quoted) is False
        assert feed.visible(lead) is False


def test_feed_page_shows_the_guess_with_a_mark(feed_db):
    """Человек видит вакансию и видит, что пометка — мнение, а не цитата."""
    from fastapi.testclient import TestClient

    _engine, factory = feed_db
    with mock.patch.object(app_module, "get_session", factory):
        page = TestClient(app_module.app, base_url="http://127.0.0.1").get("/")
    assert page.status_code == 200
    shown = set(re.findall(r'href="/job/([a-z]+)[/"]', page.text))
    assert "guess" in shown and "quoted" not in shown and "lead" not in shown
    assert "возможно, нужен датский" in page.text


def test_show_hidden_still_works(feed_db):
    """Кнопка «показать скрытые» возвращает доказанные — как и раньше."""
    from fastapi.testclient import TestClient

    _engine, factory = feed_db
    with mock.patch.object(app_module, "get_session", factory):
        page = TestClient(app_module.app, base_url="http://127.0.0.1").get("/?fit=all")
    shown = set(re.findall(r'href="/job/([a-z]+)[/"]', page.text))
    assert {"quoted", "lead"} <= shown
