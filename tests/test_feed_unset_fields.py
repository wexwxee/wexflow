"""Молчание источника — не ответ и в фильтрах ленты.

12.08.2026: Иван выбрал «Регион: Столичный» + «Уровень: Сотрудник» и увидел 3
вакансии вместо сотен. Причина не в данных: регион, уровень, занятость и
категорию присылает Salling, а Teamtailor, Ashby и Greenhouse не присылают
никогда. Строгое сравнение вычёркивало эти компании целиком — 515 вакансий из
1648 только по уровню.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import app as salling_app
from db import Job


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        # Salling заполняет всё.
        session.add(Job(id="salling-employee", source="salling", title="Kasseassistent",
                        country="DK", status="new", city="København",
                        region="hovedstaden", job_level="employee",
                        employment_type="fullTime", categories="baker", brand="Netto"))
        session.add(Job(id="salling-manager", source="salling", title="Souschef",
                        country="DK", status="new", city="København",
                        region="hovedstaden", job_level="manager",
                        employment_type="fullTime", categories="baker", brand="Netto"))
        # Teamtailor молчит про регион, уровень, занятость и категорию.
        session.add(Job(id="tt-silent", source="teamtailor", title="Butiksmedarbejder",
                        country="DK", status="new", city="København", brand="Normal"))
        # Lidl присылает уровень, но не регион.
        session.add(Job(id="lidl-no-region", source="lidl", title="Butiksassistent",
                        country="DK", status="new", city="København",
                        job_level="employee", brand="Lidl"))
        session.commit()

    # TestClient создаём БЕЗ контекстного менеджера, как весь остальной набор:
    # `with` поднимает lifespan приложения, а с ним планировщик и фоновые
    # потоки, которые потом ломают соседние тесты.
    with mock.patch.object(salling_app, "get_session", lambda: Session(engine)), \
            mock.patch("db.get_session", lambda: Session(engine)):
        yield TestClient(salling_app.app, base_url="http://127.0.0.1")


def _ids(page: str) -> set[str]:
    return {job_id for job_id in
            ("salling-employee", "salling-manager", "tt-silent", "lidl-no-region")
            if job_id in page}


def test_level_filter_keeps_sources_that_never_send_a_level(client):
    page = client.get("/?job_level=employee").text
    shown = _ids(page)
    assert "salling-employee" in shown
    assert "tt-silent" in shown, "Teamtailor исчезал целиком"
    assert "lidl-no-region" in shown


def test_region_filter_keeps_sources_that_never_send_a_region(client):
    page = client.get("/?region=hovedstaden").text
    shown = _ids(page)
    assert "salling-employee" in shown
    assert "tt-silent" in shown
    assert "lidl-no-region" in shown, "весь Lidl исчезал из «Столичного региона»"


def test_a_known_wrong_value_is_still_filtered_out(client):
    """Починка не должна превращать фильтр в «показывать всё подряд»."""
    page = client.get("/?job_level=manager").text
    shown = _ids(page)
    assert "salling-manager" in shown
    assert "salling-employee" not in shown, "уровень известен и не совпал"


def test_employment_and_category_behave_the_same(client):
    for query in ("employment_type=fullTime", "category=baker"):
        page = client.get(f"/?{query}").text
        shown = _ids(page)
        assert "tt-silent" in shown, query
        assert "salling-employee" in shown, query


def test_the_page_says_out_loud_what_it_kept(client):
    page = client.get("/?job_level=employee").text
    assert "не заполнено источником" in page
    assert "Teamtailor" in page


def test_nothing_is_said_when_no_such_filter_is_active(client):
    page = client.get("/").text
    assert "не заполнено источником" not in page
