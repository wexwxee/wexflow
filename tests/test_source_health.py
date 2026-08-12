"""Сторож источников (шаг 6): молчащий источник объявляет себя сломанным.

Проверяем не «есть ли модуль», а обещание продукта: пока источник отвечает,
ничего не меняется; когда он молчит сутки — его вакансии уходят из ленты, об
этом говорят словами, поданные заявки остаются, а ожившего источника не нужно
чинить руками.

Часы подменяются явным аргументом now — тест не спит и не зависит от машины.
"""
import datetime as dt
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

import app as app_module
import feed
import labels
import source_health
from db import Job

HOUR = 3600.0
T0 = 1_760_000_000.0  # произвольная точка отсчёта, лишь бы не «сейчас»


@pytest.fixture()
def store(tmp_path):
    """Своя папка состояния: реальный source_health.json не трогаем."""
    with mock.patch.object(source_health, "path",
                           lambda: Path(tmp_path) / "source_health.json"):
        source_health.forget()
        yield
        source_health.forget()


def _silence(source: str, attempts: int = 3, start: float = T0, step: float = HOUR):
    """Несколько подряд пустых попыток источника."""
    for i in range(attempts):
        source_health.report(source, hits=0, now=start + i * step)


def test_working_source_stays_quiet_and_invisible_to_the_feed(store):
    source_health.report("salling", hits=428, now=T0)
    row = source_health.state("salling", now=T0 + HOUR)
    assert (row["state"], row["last_hits"], row["fail_streak"]) == ("ok", 428, 0)
    assert source_health.broken(now=T0 + HOUR) == ()
    assert feed.source_clause() is None, "работающий источник ленту не трогает"


def test_one_hiccup_is_not_a_breakage(store):
    source_health.report("teamtailor", hits=506, now=T0)
    source_health.report("teamtailor", error="timeout", now=T0 + HOUR)
    assert source_health.state("teamtailor", now=T0 + HOUR)["state"] == "ok"
    assert source_health.broken(now=T0 + HOUR) == ()


def test_silence_first_warns_and_only_then_hides(store):
    _silence("lidl", attempts=3, start=T0)
    # три попытки подряд — уже молчание, но лента ещё не меняется
    assert source_health.state("lidl", now=T0 + 3 * HOUR)["state"] == "quiet"
    assert source_health.broken(now=T0 + 3 * HOUR) == ()
    # сутки молчания — источник сломан
    assert source_health.state("lidl", now=T0 + 25 * HOUR)["state"] == "broken"
    assert source_health.broken(now=T0 + 25 * HOUR) == ("lidl",)


def test_silence_duration_starts_at_first_failure_not_old_success(store):
    source_health.report("lidl", hits=188, now=T0 - 30 * 24 * HOUR)
    _silence("lidl", attempts=3, start=T0)
    row = source_health.state("lidl", now=T0 + 3 * HOUR)
    assert row["state"] == "quiet"
    assert 2 * HOUR <= row["silent_seconds"] <= 3 * HOUR
    assert source_health.state("lidl", now=T0 + 25 * HOUR)["state"] == "broken"


def test_source_that_never_answered_is_judged_from_its_first_failure(store):
    """У нового коннектора нет успеха в прошлом — считаем от первой ошибки."""
    for i in range(3):
        source_health.report("ashby", error="404", now=T0 + i * HOUR)
    assert source_health.state("ashby", now=T0 + 3 * HOUR)["state"] == "quiet"
    assert source_health.state("ashby", now=T0 + 30 * HOUR)["state"] == "broken"


def test_recovery_needs_no_repair(store):
    _silence("lidl", attempts=4, start=T0)
    assert source_health.broken(now=T0 + 40 * HOUR) == ("lidl",)
    source_health.report("lidl", hits=188, now=T0 + 41 * HOUR)
    assert source_health.broken(now=T0 + 41 * HOUR) == ()
    assert source_health.state("lidl", now=T0 + 41 * HOUR)["fail_streak"] == 0


def _feed_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Job(id="s1", source="salling", title="Kasseassistent",
                        country="DK", status="new"))
        session.add(Job(id="l1", source="lidl", title="Butiksassistent",
                        country="DK", status="new"))
        session.add(Job(id="l2", source="lidl", title="Поданная",
                        country="DK", status="applied"))
        session.add(Job(id="l3", source="lidl", title="Собеседование",
                        country="DK", status="interview",
                        applied_at=dt.datetime(2026, 8, 1, 10, 0)))
        session.commit()
    return engine


def _feed_ids(engine) -> set:
    with Session(engine) as session:
        return {job.id for job in session.exec(
            select(Job).where(*feed.visible_clauses())).all()}


def test_broken_source_leaves_the_feed_but_never_takes_applied_with_it(store):
    engine = _feed_engine()
    assert _feed_ids(engine) == {"s1", "l1", "l2", "l3"}

    _silence("lidl", attempts=3, start=T0)
    with mock.patch.object(source_health, "broken", lambda now=None: ("lidl",)):
        ids = _feed_ids(engine)
    assert "l1" not in ids, "вакансии сломанного источника остались в ленте"
    assert "l2" in ids, "поданная заявка пропала — это история человека"
    assert "l3" in ids, "post-application stage with applied_at disappeared"
    assert "s1" in ids, "здоровый источник пострадал от чужой поломки"


def test_broken_source_does_not_touch_the_database(store):
    """Скрытие живёт только в запросе: ни один статус в базе не меняется."""
    engine = _feed_engine()
    with mock.patch.object(source_health, "broken", lambda now=None: ("lidl",)):
        _feed_ids(engine)
    with Session(engine) as session:
        assert session.get(Job, "l1").status == "new"


def test_hidden_count_does_not_include_post_application_rows(store):
    engine = _feed_engine()

    @contextmanager
    def factory():
        with Session(engine) as session:
            yield session

    import db as db_module
    with mock.patch.object(db_module, "get_session", factory):
        counts = source_health.hidden_counts(("lidl",))
    assert counts == {"lidl": 1}


def test_same_rule_for_a_loaded_job(store):
    with mock.patch.object(source_health, "broken", lambda now=None: ("lidl",)):
        assert feed.visible(Job(id="a", source="salling", country="DK", status="new"))
        assert not feed.visible(Job(id="b", source="lidl", country="DK", status="new"))
        assert feed.visible(Job(id="c", source="lidl", country="DK", status="applied"))
        assert feed.visible(Job(id="d", source="lidl", country="DK", status="interview",
                                applied_at=dt.datetime(2026, 8, 1, 10, 0)))


def test_broken_source_is_named_out_loud_in_the_banner():
    warnings = app_module._health_warnings(
        428, False, 0, 0, [], "", "", "", broken_sources=("lidl",))
    text = " ".join(w["text"] for w in warnings if w["id"] == "source-broken")
    assert labels.source("lidl") in text
    assert "Поданные заявки на месте" in text
    # здоровое состояние молчит
    assert not [w for w in app_module._health_warnings(428, False, 0)
                if w["id"] == "source-broken"]


def test_status_page_explains_the_silence(store):
    _silence("lidl", attempts=3, start=T0)
    with mock.patch.object(source_health, "hidden_counts", lambda sources=None: {"lidl": 7}):
        rows = {row["source"]: row for row in source_health.view(now=T0 + 30 * HOUR)}
    row = rows["lidl"]
    assert row["state"] == "broken"
    assert row["label"] == "Lidl Danmark"
    assert row["hidden"] == 7
    assert "убраны из ленты" in row["explain"]
    assert "Вернётся сам" in row["explain"]


def test_status_page_shows_the_broken_source_card():
    """Человек должен УВИДЕТЬ поломку, а не догадаться по короткой ленте."""
    from fastapi.testclient import TestClient

    row = {
        "source": "lidl", "label": "Lidl Danmark", "state": "broken",
        "hits": 0, "fail_streak": 5, "last_ok_at": T0, "silent_label": "2 дн",
        "hidden": 188, "error": "HTTP 503", "explain": "Не отдаёт вакансии 2 дн.",
    }
    with mock.patch.object(app_module.source_health, "view", lambda now=None: [row]):
        page = TestClient(app_module.app).get("/status")
    assert page.status_code == 200
    assert "Источники вакансий" in page.text
    assert "Lidl Danmark" in page.text
    assert "не отвечает 2 дн" in page.text
    assert "скрыто вакансий: 188" in page.text


def test_watchdog_failure_never_breaks_the_feed(store):
    """Сторож — вспомогательный. Его поломка не имеет права гасить ленту."""
    with mock.patch.object(source_health, "broken", side_effect=OSError("нет файла")):
        assert feed.broken_sources() == ()
        assert feed.source_clause() is None
