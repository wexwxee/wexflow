"""Чистка ленты (шаг 1 пересмотра 08.08.2026): страна — настройка, а не
константа, и закрытые вакансии в ленту не попадают.

PATH настроек подменяется на временный файл — реальный settings.json НЕ трогаем.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select

import feed
import settings_store
from db import Job


def _with_temp_settings(body):
    orig = settings_store.PATH
    settings_store.PATH = Path(tempfile.mkdtemp()) / "settings.json"
    feed._cache.update(stamp=None, codes=None)
    try:
        body()
    finally:
        settings_store.PATH = orig
        feed._cache.update(stamp=None, codes=None)


def _sessions():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Job(id="dk-open", source="salling", title="Kasseassistent",
                        country="DK", status="new"))
        session.add(Job(id="dk-closed", source="salling", title="Закрытая",
                        country="DK", status="closed"))
        session.add(Job(id="dk-hidden", source="salling", title="Скрытая",
                        country="DK", status="hidden"))
        session.add(Job(id="pl-open", source="salling", title="Magazynier",
                        country="PL", status="new"))
        session.add(Job(id="de-open", source="salling", title="Verkäufer",
                        country="Deutschland", status="new"))
        session.add(Job(id="link-open", source="manual_link", title="По ссылке",
                        country=None, status="new"))
        session.commit()
    return engine


def _feed_ids(engine) -> set:
    with Session(engine) as session:
        return {j.id for j in session.exec(
            select(Job).where(*feed.visible_clauses())).all()}


def test_default_feed_is_denmark_and_open_only():
    def body():
        engine = _sessions()
        assert feed.countries() == ["DK"]
        # закрытая и скрытая не в ленте; PL/DE отсечены настройкой по умолчанию;
        # вакансия без страны (добавленная по ссылке) остаётся видимой
        assert _feed_ids(engine) == {"dk-open", "link-open"}

    _with_temp_settings(body)


def test_country_is_a_setting_not_a_constant():
    def body():
        engine = _sessions()
        feed.set_countries(["DK", "PL"])
        assert _feed_ids(engine) == {"dk-open", "pl-open", "link-open"}
        # закрытые не возвращаются даже при расширении стран
        assert "dk-closed" not in _feed_ids(engine)

    _with_temp_settings(body)


def test_any_country_shows_everything_open():
    def body():
        engine = _sessions()
        feed.set_countries([feed.ANY])
        assert feed.any_country() is True
        assert feed.country_clause() is None
        assert _feed_ids(engine) == {"dk-open", "pl-open", "de-open", "link-open"}

    _with_temp_settings(body)


def test_country_written_by_name_is_recognised():
    def body():
        engine = _sessions()
        feed.set_countries(["Deutschland"])       # настройку тоже нормализуем
        assert feed.countries() == ["DE"]
        # в базе страна лежит словом — в ленту всё равно попадает
        assert "de-open" in _feed_ids(engine)

    _with_temp_settings(body)


def test_empty_choice_falls_back_to_denmark():
    def body():
        # Пустая настройка означала бы ленту без единой вакансии — это поломка,
        # а не выбор пользователя.
        assert feed.set_countries([]) == ["DK"]
        settings_store.save({"countries": ["   ", "??"]})
        assert feed.countries() == ["DK"]

    _with_temp_settings(body)


def test_exclude_applied_keeps_only_active():
    def body():
        engine = _sessions()
        with Session(engine) as session:
            session.add(Job(id="dk-applied", source="salling", title="Подано",
                            country="DK", status="applied"))
            session.commit()
        with Session(engine) as session:
            rows = session.exec(select(Job).where(
                *feed.visible_clauses(exclude_applied=True))).all()
        assert {j.id for j in rows} == {"dk-open", "link-open"}

    _with_temp_settings(body)


def test_connector_ingest_follows_the_country_setting():
    """Каталоги ATS брали только Данию константой — теперь тоже по настройке."""
    def body():
        import connector_sync
        from connectors.base import JobItem

        def _item(job_id, country, city="København"):
            return JobItem(source="teamtailor", id=job_id, title="Butiksmedarbejder",
                           company="Demo ApS", url="https://demo.teamtailor.com/jobs/1",
                           city=city, country=country)

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        sessions = lambda: Session(engine)   # noqa: E731 — фабрика на одну строку
        batch = [_item("tt:demo:dk", "DK"), _item("tt:demo:se", "Sverige", "Stockholm")]

        assert connector_sync.sync_items("teamtailor", batch, sessions)["hits"] == 1
        feed.set_countries(["DK", "SE"])
        report = connector_sync.sync_items("teamtailor", batch, sessions)
        assert report["hits"] == 2
        with Session(engine) as session:
            stored = {j.id: j.country for j in session.exec(select(Job)).all()}
        assert stored == {"tt:demo:dk": "DK", "tt:demo:se": "SE"}

    _with_temp_settings(body)


def test_connector_ingest_drops_unrecognised_country():
    """Неопознанную страну в базу не тащим — иначе польётся весь мир.
    Явное «любая страна» отменяет и это ограничение."""
    def body():
        import connector_sync
        from connectors.base import JobItem

        item = JobItem(source="teamtailor", id="tt:demo:x", title="Remote job",
                       company="Demo ApS", url="https://demo.teamtailor.com/jobs/x",
                       city="Anywhere", country="Remote")

        def _fresh():
            engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
            SQLModel.metadata.create_all(engine)
            return lambda: Session(engine)

        assert connector_sync.sync_items("teamtailor", [item], _fresh())["hits"] == 0
        feed.set_countries([feed.ANY])
        assert connector_sync.sync_items("teamtailor", [item], _fresh())["hits"] == 1

    _with_temp_settings(body)


def test_normalize_understands_common_spellings():
    assert feed.normalize("danmark") == "DK"
    assert feed.normalize(" DNK ") == "DK"
    assert feed.normalize({"name": "Poland"}) == "PL"
    assert feed.normalize("Sverige") == "SE"
    assert feed.normalize("") == ""
    assert feed.normalize("Remote") == ""
    assert feed.normalize("es") == "ES"


def test_visible_matches_sql_rule():
    def body():
        assert feed.visible(Job(id="a", country="DK", status="new")) is True
        assert feed.visible(Job(id="b", country="DK", status="closed")) is False
        assert feed.visible(Job(id="c", country="PL", status="new")) is False
        assert feed.visible(Job(id="d", country=None, status="new")) is True
        assert feed.visible(
            Job(id="e", country="DK", status="applied"), exclude_applied=True) is False

    _with_temp_settings(body)


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print("ok")
