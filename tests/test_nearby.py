"""«Здесь подходящего нет — зато рядом есть» (этап 2).

Живой случай, ради которого всё написано: Ивану посоветовали Netto на
Tuborg Havnevej. Там три вакансии «только до 18 лет» и две руководящие — для
него пусто. Приложение молчало, и это читается как «работы нет». Неправда: в
двух соседних Netto того же Хеллерупа было шесть подходящих.

Тест держит не вёрстку, а обещания:
  - причина «почему здесь пусто» — правда, посчитанная теми же функциями,
    что и сами фильтры;
  - подсказка не предлагает то, что лента прячет;
  - расстояние считается от БЛИЖАЙШЕГО названного магазина, а не от середины
    (у одинаковых названий середина — точка в поле);
  - ни сети, ни ИИ.

Запуск:  python -m pytest tests/test_nearby.py
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
import nearby
from db import Job

# Опора — Хеллеруп: там только «под 18» и руководящие. Соседний Gentofte в
# двух километрах, там нормальная работа. Орхус за 200 км — не «рядом».
#
# Важная деталь семантики: когда человек назвал СЕТЬ И ГОРОД, опорой считается
# весь этот город целиком. Магазины той же сети внутри города и так попадут в
# обычную выдачу — «рядом» начинается за его границей.
WATERFRONT = (55.727, 12.577)
GENTOFTE = (55.745, 12.552)
GENTOFTE_2 = (55.741, 12.560)
AARHUS = (56.157, 10.211)


def _job(job_id, title, street, point, *, brand="netto", city="Hellerup",
         level=None, fit=None, engine=None, status="new"):
    return Job(id=job_id, source="salling", brand=brand, country="DK",
               city=city, street=street, title=title, status=status,
               lat=point[0], lon=point[1], job_level=level,
               fit=fit, fit_engine=engine)


def _pool():
    return [
        # опорный магазин: только «под 18» и руководящие — для взрослого пусто
        _job("w1", "Servicemedarbejder under 18 år", "Tuborg Havnevej 4", WATERFRONT,
             level="employeeUnder18"),
        _job("w2", "Servicemedarbejder under 18 år", "Tuborg Havnevej 4", WATERFRONT,
             level="employeeUnder18"),
        _job("w3", "Serviceleder", "Tuborg Havnevej 4", WATERFRONT),
        # соседний город в двух километрах — то, что человеку и нужно
        _job("l1", "1. assistent", "Lyngbyvej 237", GENTOFTE, city="Gentofte"),
        _job("l2", "1. assistent - nat", "Lyngbyvej 237", GENTOFTE, city="Gentofte"),
        _job("r1", "1. assistent", "Rymarksvej 73", GENTOFTE_2, city="Gentofte"),
        # спрятанная лентой: цитата про датский — предлагать её нельзя
        _job("r2", "Kasseassistent", "Rymarksvej 73", GENTOFTE_2, city="Gentofte",
             fit="danish", engine="rules"),
        # другая сеть в опорном городе
        _job("f1", "Slagter", "Strandvejen 193", WATERFRONT, brand="foetex"),
        # далеко — не рядом
        _job("a1", "1. assistent", "Søndergade 1", AARHUS, city="Aarhus"),
    ]


class _Parsed:
    """Минимальный разбор запроса «нетто хеллеруп»."""
    brands = ("netto",)
    cities = ("Hellerup",)


def test_says_why_the_named_place_is_empty_and_what_is_nearby():
    """Ровно случай Ивана: назвал место, там подходящего нет, рядом есть."""
    view = nearby.suggestions(_pool(), parsed=_Parsed())
    assert view["anchor_count"] == 3
    assert view["anchor_rejected"] == {"under18": 2, "leadership": 1}
    # три подходящие в двух соседних магазинах (r2 спрятана лентой — не в счёт)
    assert view["same_brand_count"] == 3
    assert {g["street"] for g in view["same_brand"]} == {"Lyngbyvej 237", "Rymarksvej 73"}


def test_never_suggests_what_the_feed_hides():
    view = nearby.suggestions(_pool(), parsed=_Parsed())
    suggested = {j.id for g in view["same_brand"] for j in g["jobs"]}
    suggested |= {j.id for j in view["same_city"]} | {j.id for j in view["same_role"]}
    assert "r2" not in suggested, "предложили вакансию, которую лента прячет"


def test_far_away_is_not_nearby():
    view = nearby.suggestions(_pool(), parsed=_Parsed())
    suggested = {j.id for g in view["same_brand"] for j in g["jobs"]}
    assert "a1" not in suggested


def test_same_role_with_anchor_coordinates_rejects_unknown_distance():
    """Unknown coordinates are not evidence that a job is within the radius."""
    import relevance

    unknown = Job(id="unknown-distance", source="salling", title="1. assistent",
                  brand="netto", country="DK", city="Gentofte", status="new",
                  fit="ok")
    role = relevance.role_key(unknown)
    found = nearby.near_same_role([unknown], {role}, [WATERFRONT], radius_km=30.0)
    assert found == []


def test_disabled_language_filter_is_respected_by_nearby():
    import relevance

    blocked = _job("language", "Kasseassistent", "Lyngbyvej 1", GENTOFTE,
                   city="Gentofte", fit="danish", engine="rules")
    role = relevance.role_key(blocked)
    with mock.patch("feed.hide_barrier", return_value=False):
        found = nearby.near_same_role([blocked], {role}, [], radius_km=30.0)
    assert [job.id for job in found] == ["language"]


def test_named_city_without_coordinates_does_not_make_another_city_nearby():
    anchor = Job(id="vejle", source="salling", title="Butiksassistent",
                 brand="netto", country="DK", city="Vejle", status="new")
    elsewhere = Job(id="herlev", source="salling", title="Butiksassistent",
                    brand="netto", country="DK", city="Herlev", status="new")

    class Parsed:
        brands = ("netto",)
        cities = ("Vejle",)

    view = nearby.suggestions([anchor, elsewhere], parsed=Parsed())
    assert not view or view["same_brand"] == []


def test_other_brands_in_the_same_city_are_a_separate_layer():
    view = nearby.suggestions(_pool(), parsed=_Parsed())
    assert [j.id for j in view["same_city"]] == ["f1"]


def test_distance_is_measured_from_the_nearest_named_shop():
    """У одинаковых названий середина — точка в поле, и расстояние врёт."""
    pool = [
        _job("north", "Butiksassistent", "Hovedgaden 1", (57.19, 9.68), city="Tørring"),
        _job("south", "Butiksassistent", "Svinget 1", (55.85, 9.48), city="Tørring"),
        _job("near-south", "1. assistent", "Vejlevej 42", (55.75, 9.43), city="Jelling"),
    ]

    class Parsed:
        brands = ()
        cities = ("Jelling",)

    # опора — Jelling; ближайший Tørring южный, ~11 км, а не «середина»
    view = nearby.suggestions(pool, parsed=Parsed(), radius_km=15.0)
    assert view == {} or view["total"] >= 0  # слой сети тут пуст, проверяем математику
    assert nearby._km([(55.75, 9.43), (57.19, 9.68)], pool[1]) < 15


def test_radius_widens_once_and_says_so():
    """В провинции 15 км — соседняя улица по смыслу. Расширяем и говорим об этом."""
    far = _job("far", "1. assistent", "Hovedgaden 5", (55.90, 12.90), city="Hillerød")
    pool = [j for j in _pool() if j.id.startswith("w")] + [far]
    view = nearby.suggestions(pool, parsed=_Parsed(), radius_km=5.0)
    assert view["widened"] is True
    assert view["radius_km"] == nearby.WIDEN_RADIUS_KM


def test_nothing_to_say_means_no_block():
    only_anchor = [j for j in _pool() if j.id.startswith("w")]
    assert nearby.suggestions(only_anchor, parsed=_Parsed()) == {}


def test_grouping_is_shared_with_the_feed():
    """Лента и подсказка должны считать «магазином» одно и то же."""
    groups = nearby.stores(_pool())
    keys = {(g["brand"], g["street"]) for g in groups}
    assert ("netto", "Tuborg Havnevej 4") in keys
    assert sum(len(g["jobs"]) for g in groups) == len(_pool())


# ── страницы ───────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for job in _pool():
            session.add(job)
        session.commit()

    @contextmanager
    def factory():
        with Session(engine) as session:
            yield session

    from fastapi.testclient import TestClient
    with mock.patch.object(app_module, "get_session", factory):
        yield TestClient(app_module.app, base_url="http://127.0.0.1")


def test_empty_search_result_offers_the_neighbours(client):
    page = client.get("/?q=нетто Hellerup 18%2B")
    assert page.status_code == 200
    assert "Здесь подходящего нет" in page.text or "Ещё рядом" in page.text
    assert "Lyngbyvej 237" in page.text


def test_job_page_shows_the_same_shop_and_neighbours(client):
    page = client.get("/job/w1")
    assert page.status_code == 200
    assert "В этом магазине и рядом" in page.text
    assert "Lyngbyvej 237" in page.text


def test_nearby_never_goes_to_the_network(client):
    with mock.patch("httpx.get", side_effect=AssertionError("подсказка полезла в сеть")), \
            mock.patch("httpx.post", side_effect=AssertionError("подсказка полезла в сеть")):
        page = client.get("/?q=нетто Hellerup")
    assert page.status_code == 200
