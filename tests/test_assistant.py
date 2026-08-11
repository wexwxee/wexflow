"""Помощник сбоку (этап 4): диспетчер умений, а не рассказчик.

Чат, который «сам всё знает», врёт: модель придумает вакансию, адрес и часы, и
человек поедет в несуществующий магазин. Поэтому помощник только выбирает
инструмент, а факты даёт приложение из своей базы.

Главный тест здесь — последний: **ни один инструмент не может отправить
заявку**. Заявка уходит под настоящим именем человека и не отменяется; фраза
в чате не может быть основанием для отправки.

Запуск:  python -m pytest tests/test_assistant.py
"""
import os
import sys
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

import assistant
from db import Job

HOME = {"lat": 55.70, "lon": 12.55, "address": "Дом 1"}


def _job(job_id, title, *, brand="netto", city="Herlev", point=(55.705, 12.552),
         fit="ok", engine="ai:gemini", status="new"):
    return Job(id=job_id, source="salling", brand=brand, country="DK", city=city,
               title=title, status=status, hours="15", lat=point[0], lon=point[1],
               fit=fit, fit_engine=engine, fit_reason="Оценка по роли.")


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_job("a", "Kasseassistent"))
        session.add(_job("b", "1. assistent", city="Gentofte", point=(55.745, 12.552)))
        session.add(_job("hidden", "Butiksassistent", fit="danish", engine="rules"))
        session.commit()

    @contextmanager
    def factory():
        with Session(engine) as session:
            yield session

    import db as db_mod
    with mock.patch.object(db_mod, "get_session", factory), \
            mock.patch("settings_store.get_home", lambda: HOME):
        yield


# ── белый список и аргументы ───────────────────────────────────────────────

def test_unknown_tool_is_refused_politely():
    out = assistant.run("rm -rf", {})
    assert out["ok"] is False
    assert "не умею" in out["reply"]


def test_arguments_are_cleaned_to_the_schema(db):
    """Кривой аргумент не должен доехать до базы."""
    out = assistant.run("search_jobs", {"query": "x" * 500, "evil": "DROP TABLE job"})
    assert out["ok"] is True
    assert "evil" not in str(out)


def test_tool_failure_never_raises():
    with mock.patch.object(assistant, "_tool_search", side_effect=RuntimeError("бум")):
        broken = assistant.Tool("search_jobs", "Поиск", {"query": ("str", 10)},
                                assistant._tool_search)
        with mock.patch.dict(assistant.TOOLS, {"search_jobs": broken}):
            out = assistant.run("search_jobs", {"query": "нетто"})
    assert out["ok"] is False and "Не получилось" in out["reply"]


# ── разбор просьбы без ИИ ──────────────────────────────────────────────────

def test_guess_tool_understands_plain_russian():
    assert assistant.guess_tool("нетто херлев")[0] == "search_jobs"
    assert assistant.guess_tool("что есть рядом")[0] == "nearby_jobs"
    assert assistant.guess_tool("что мне подходит")[0] == "recommend_jobs"
    assert assistant.guess_tool("чего не хватает для подачи")[0] == "profile_gaps"
    assert assistant.guess_tool("мои заявки")[0] == "application_status"
    assert assistant.guess_tool("почему нужен датский", job_id="a")[0] == "explain_verdict"


def test_works_without_any_ai(db):
    """У большинства людей ключа нет — помощник обязан работать и так."""
    with mock.patch("ai_gateway.available", return_value=False), \
            mock.patch("ai_gateway.chat", side_effect=AssertionError("позвал ИИ")):
        out = assistant.ask("нетто херлев")
    assert out["ok"] is True and out["used_ai"] is False
    assert out["results"], "поиск без ИИ ничего не вернул"


def test_hidden_jobs_are_never_offered(db):
    out = assistant.ask("Butiksassistent")
    assert all(card["id"] != "hidden" for card in out.get("results", []))


def test_nearby_without_a_place_answers_from_home(db):
    out = assistant.run("nearby_jobs", {"query": "что есть рядом"})
    assert out["ok"] is True
    assert out["results"], "самый частый вопрос остался без ответа"
    assert "от дома" in out["results"][0]["why"][0]


def test_explain_verdict_speaks_plainly(db):
    out = assistant.run("explain_verdict", {"job_id": "hidden"})
    assert out["ok"] is True
    assert "текст" in out["reply"] or "руководящая" in out["reply"]


# ── о человеке наружу уходит только нужное ─────────────────────────────────

def test_person_context_hides_contacts():
    profile = {
        "first_name": "Ivan", "city": "København", "languages": "English",
        "email": "ivan@example.com", "phone": "+45 12 34 56 78",
        "address": "Sonnerupvej 104", "date_of_birth": "2005-01-01",
        "cv_path": "C:/secret/cv.pdf", "linkedin": "https://linkedin/in/ivan",
    }
    with mock.patch("profile_store.load_profile", return_value=profile), \
            mock.patch("settings_store.get_home", lambda: HOME):
        known = assistant.person_context()
    blob = str(known)
    for secret in ("ivan@example.com", "+45", "Sonnerupvej", "2005-01-01", "cv.pdf", "linkedin"):
        assert secret not in blob, f"наружу утекло: {secret}"
    assert known["city"] == "København" and known["home_set"] == "да"


# ── главный инвариант ──────────────────────────────────────────────────────

def test_no_tool_can_ever_submit_an_application(db):
    """Заявка уходит под настоящим именем и не отменяется.

    Прогоняем ВСЕ инструменты со всеми аргументами и требуем ноль вызовов у
    всего, что умеет подавать или менять состояние заявки.
    """
    import applications
    import apply as apply_mod

    guards = {
        "apply.run_batch": mock.patch.object(apply_mod, "run_batch", create=True),
        "apply.main": mock.patch.object(apply_mod, "main", create=True),
        "mark_submitting": mock.patch.object(applications, "mark_submitting"),
        "record_submitted": mock.patch.object(applications, "record_submitted"),
        "mark_offered": mock.patch.object(applications, "mark_offered"),
    }
    started = {name: patch.start() for name, patch in guards.items()}
    try:
        for name in assistant.TOOLS:
            assistant.run(name, {"query": "нетто", "job_id": "a"})
        assistant.ask("подайся на эту вакансию", job_id="a")
        assistant.ask("отправь заявку прямо сейчас", job_id="a")
    finally:
        for patch in guards.values():
            patch.stop()
    for name, spy in started.items():
        assert spy.call_count == 0, f"помощник дёрнул {name}"


def test_prepare_application_only_offers_a_button(db):
    out = assistant.run("prepare_application", {"job_id": "a"})
    assert out["kind"] == "confirm"
    assert out["href"] == "/job/a"
    assert "жмёшь ты" in out["reply"]


# ── страница ───────────────────────────────────────────────────────────────

def test_panel_is_wired_into_every_page(db):
    from fastapi.testclient import TestClient
    import app as app_module

    client = TestClient(app_module.app, base_url="http://127.0.0.1")
    page = client.get("/")
    assert "assistant.js" in page.text and "assistant.css" in page.text
    state = client.get("/api/assistant/state").json()
    assert state["ok"] is True and state["tools"]


def test_ask_endpoint_is_closed_for_cross_site(db):
    from fastapi.testclient import TestClient
    import app as app_module

    client = TestClient(app_module.app, base_url="http://127.0.0.1")
    blocked = client.post("/api/assistant/ask", json={"text": "нетто"},
                          headers={"origin": "https://evil.example",
                                   "sec-fetch-site": "cross-site"})
    assert blocked.status_code == 403
