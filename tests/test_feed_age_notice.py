"""Возрастное правило обязано себя называть.

13.08.2026 Иван написал: «поч из фильтров пропал фильтр, там был сотрудник
старше 18 и до 18». Так и было: правило 1.4.6 скрыло все ставки «under 18 år»,
счётчик у пункта стал нулевым, и сам пункт выпал из выпадающего списка — молча.
В этом проекте скрытое всегда посчитано и показано строкой (так сделан языковой
барьер); возраст был исключением, и это ошибка.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

import app as salling_app
import feed
import settings_store
from db import Job

# Слово «employeeUnder18» встречается и в справке на странице, поэтому пункт
# выпадающего списка проверяем по его разметке, а не по вхождению слова.
OPTION = 'data-value="employeeUnder18"'
HIDDEN_LINE = "Скрыто вакансий «under 18 år»"


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Job(id="adult-1", source="salling", title="Kasseassistent",
                        country="DK", status="new", city="Herlev",
                        job_level="employee", brand="Netto"))
        for n in range(3):
            session.add(Job(id=f"kid-{n}", source="salling", country="DK",
                            status="new", city="Herlev", brand="Netto",
                            title=f"Salgsassistent under 18 ar {n}",
                            job_level="employeeUnder18"))
        session.commit()

    original = settings_store.PATH
    settings_store.PATH = Path(tempfile.mkdtemp()) / "settings.json"
    feed._forget()
    try:
        with mock.patch.object(salling_app, "get_session", lambda: Session(engine)), \
                mock.patch("db.get_session", lambda: Session(engine)):
            yield TestClient(salling_app.app, base_url="http://127.0.0.1")
    finally:
        settings_store.PATH = original
        feed._forget()


def test_hidden_vacancies_are_counted_out_loud(client):
    feed.set_viewer_age(20)
    page = client.get("/").text
    assert HIDDEN_LINE in page
    assert "<b>3</b>" in page
    assert "показать их" in page
    assert "/profile#age" in page, "нет пути изменить возраст"


def test_showing_them_brings_back_the_level_option(client):
    """Главная жалоба: пункт «Сотрудник до 18» исчез из фильтра «Уровень»."""
    feed.set_viewer_age(20)
    assert OPTION not in client.get("/").text

    shown = client.get("/?age=all").text
    assert OPTION in shown, "пункт фильтра так и не вернулся"
    assert "Показаны и вакансии «under 18 år»" in shown
    assert "снова скрыть" in shown


def test_without_an_age_nothing_is_hidden_and_nothing_is_said(client):
    page = client.get("/").text
    assert HIDDEN_LINE not in page
    assert OPTION in page


def test_a_teenager_sees_them_without_any_notice(client):
    feed.set_viewer_age(16)
    page = client.get("/").text
    assert HIDDEN_LINE not in page
    assert OPTION in page


def test_the_feed_still_hides_them_by_default_for_an_adult(client):
    feed.set_viewer_age(20)
    page = client.get("/").text
    assert "Salgsassistent under 18" not in page
    assert "Kasseassistent" in page


def test_showing_them_does_not_change_the_rule_itself(client):
    """«Показать» — это просмотр одной страницы, а не отмена настройки."""
    feed.set_viewer_age(20)
    client.get("/?age=all")
    assert feed.viewer_age() == 20
    assert feed.underage_clause() is not None, "правило ленты выключилось само"
