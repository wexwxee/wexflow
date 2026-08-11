"""Поиск по названию магазина: «друг посоветовал Netto — посмотрю, что там».

Так человек и ищет работу: не по слову «кассир», а по месту, которое ему
назвали. Раньше поиск смотрел только в название, описание, город и улицу
вакансии и про поле «магазин» не знал вообще — запрос «нетто» не находил
НИЧЕГО, хотя в базе лежали сотни открытых вакансий Netto.

Второе обещание, которое проверяется здесь: если у магазина всё найденное
требует датского, приложение говорит это словами и даёт посмотреть. Пустой
экран вместо ответа означал бы «в Netto работы нет», а это неправда.

Запуск:  python -m pytest tests/test_brand_search.py
"""
import os
import re
import sys
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

import app as app_module
import labels
from db import Job


@pytest.fixture()
def feed_with_shops():
    """Маленькая лента: два Netto, один Føtex, один Lidl."""
    # StaticPool: TestClient обслуживает запрос в другом потоке, а обычный
    # пул отдал бы там НОВОЕ пустое in-memory соединение («no such table»).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Job(id="n1", source="salling", brand="netto", country="DK",
                        title="Kasseassistent", city="Herlev", street="Herlev Hovedgade 1",
                        status="new"))
        # fit_engine="rules" — датский требуется прямо в тексте объявления,
        # только такое (и руководящие) лента прячет с 11.08.2026
        session.add(Job(id="n2", source="salling", brand="netto", country="DK",
                        title="Butiksassistent under 18 år", city="Vejle",
                        status="new", fit="danish", fit_engine="rules",
                        fit_reason="в тексте: «dansk i tale og skrift»"))
        session.add(Job(id="f1", source="salling", brand="foetex", country="DK",
                        title="Slagter", city="Herlev", status="new"))
        session.add(Job(id="l1", source="lidl", brand="Lidl Danmark", country="DK",
                        title="Lagermedarbejder", city="Køge", status="new"))
        session.commit()

    @contextmanager
    def factory():
        with Session(engine) as session:
            yield session

    with mock.patch.object(app_module, "get_session", factory):
        yield TestClient(app_module.app, base_url="http://127.0.0.1",
                         follow_redirects=False)


def _shown(client, query: str) -> set[str]:
    """Что реально в списке результатов.

    Считаем только карточки: ниже на странице живёт блок подсказок «рядом», и
    его ссылки — не результат поиска, а предложение посмотреть соседей.
    """
    page = client.get(f"/?q={query}")
    assert page.status_code == 200, page.text[:400]
    body = page.text.split('class="nearby-box"')[0]
    return set(re.findall(r'href="/job/([a-z0-9]+)[/"]', body))


def test_shop_name_finds_the_shop(feed_with_shops):
    """«нетто» и «Netto» — одно и то же место, и это не текст в вакансии."""
    assert _shown(feed_with_shops, "нетто") == {"n1"}
    assert _shown(feed_with_shops, "Netto") == {"n1"}
    assert _shown(feed_with_shops, "фётекс") == {"f1"}
    assert _shown(feed_with_shops, "лидл") == {"l1"}


def test_shop_plus_city_means_that_shop_in_that_city(feed_with_shops):
    """«Netto Herlev» — конкретный магазин, а не такая строка внутри вакансии."""
    assert _shown(feed_with_shops, "Netto Herlev") == {"n1"}
    assert _shown(feed_with_shops, "нетто Vejle") == set()  # там всё за языком


def test_ordinary_search_is_untouched(feed_with_shops):
    """Обычный поиск по профессии работает как раньше."""
    assert _shown(feed_with_shops, "Slagter") == {"f1"}
    assert _shown(feed_with_shops, "kasseassistent") == {"n1"}


def test_hidden_by_language_is_explained_not_swallowed(feed_with_shops):
    """У магазина есть вакансии, но все за языковым барьером — говорим прямо."""
    page = feed_with_shops.get("/?q=нетто Vejle")
    assert "Все найденные вакансии требуют датского" in page.text
    assert "нашлось <b>1</b>" in page.text
    assert "fit=all" in page.text, "должна быть ссылка «показать их»"

    # человек нажал «показать их» — вакансия появляется
    assert _shown(feed_with_shops, "нетто Vejle&fit=all") == {"n2"}


def test_shop_search_counts_hidden_above_the_list(feed_with_shops):
    """Когда что-то нашлось, число скрытого видно строкой над списком."""
    page = feed_with_shops.get("/?q=нетто")
    assert "Скрыто вакансий: <b>1</b>" in page.text
    assert "прямо в объявлении" in page.text


def test_query_is_split_into_shop_and_the_rest():
    assert labels.split_brand_query("Netto") == (["netto"], "")
    assert labels.split_brand_query("нетто Хернинг") == (["netto"], "Хернинг")
    assert labels.split_brand_query("jem og fix") == (["jem"], "")
    assert labels.split_brand_query("кассир") == ([], "кассир")
    assert labels.split_brand_query("") == ([], "")
    # не магазин, а профессия с похожим словом — поиск остаётся обычным
    assert labels.split_brand_query("kasseassistent")[0] == []
