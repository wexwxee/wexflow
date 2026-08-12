"""Lidl post-application checklist and opt-in Telegram reminders."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine
from starlette.requests import Request

import app
import applications
import lidl_followup
from db import Job


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def _request(path: str, method: str = "GET") -> Request:
    return Request({
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000),
    })


def test_lidl_guide_explains_profile_password_portal_and_next_steps():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"):
        job = Job(
            id="lidl:guide",
            source="lidl",
            status="applied",
            applied_at=dt.datetime(2026, 7, 31, 9, 35),
        )
        guide = lidl_followup.view(job, "ivan@example.com")

    text = " ".join(
        step["title"] + " " + step["text"] for step in guide["steps"]
    )
    assert "кандидатском профиле" in text
    assert "Glemt adgangskode?" in text
    assert "Søgte jobs" in text
    assert "онлайн‑тест" in text
    assert "ivan@example.com" in text
    assert "career5.successfactors.eu" in guide["portal_url"]
    assert guide["reminders"] is False


def test_checklist_and_reminders_are_saved_without_credentials():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"):
        assert lidl_followup.set_check("job-1", "password_set", True) is True
        assert lidl_followup.set_check("job-1", "unknown", True) is False
        lidl_followup.set_reminders("job-1", True)
        raw = (Path(tmp) / "followups.json").read_text(encoding="utf-8")
        assert '"password_set": true' in raw
        assert '"reminders": true' in raw
        assert "password_enc" not in raw
        assert '"password"' not in raw


def test_reminders_are_due_in_order_once_and_stop_after_status_changes():
    now = dt.datetime(2026, 7, 31, 12, 0)
    job = Job(
        id="lidl:reminders",
        source="lidl",
        status="applied",
        title="Butiksassistent",
        applied_at=now,
    )
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"):
        assert lidl_followup.due_reminders([job], now=now) == []
        lidl_followup.set_reminders(job.id, True)

        expected = [
            ("setup", now),
            ("day3", now + dt.timedelta(days=3)),
            ("week1", now + dt.timedelta(days=7)),
            ("week2", now + dt.timedelta(days=14)),
        ]
        for code, at in expected:
            due = lidl_followup.due_reminders([job], now=at)
            assert len(due) == 1 and due[0]["code"] == code
            assert "Butiksassistent" in due[0]["text"]
            lidl_followup.mark_sent(job.id, code)

        assert lidl_followup.due_reminders(
            [job], now=now + dt.timedelta(days=30)
        ) == []
        job.status = "interview"
        assert lidl_followup.due_reminders([job], now=now + dt.timedelta(days=30)) == []


def test_enabling_old_application_sends_only_the_current_reminder():
    now = dt.datetime(2026, 7, 31, 12, 0)
    job = Job(
        id="lidl:old",
        source="lidl",
        status="applied",
        applied_at=now - dt.timedelta(days=10),
    )
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"):
        lidl_followup.set_reminders(job.id, True)
        due = lidl_followup.due_reminders([job], now=now)
    assert len(due) == 1
    assert due[0]["code"] == "week1"


def test_reminders_follow_application_stage_not_listing_visibility():
    now = dt.datetime(2026, 8, 12, 12, 0)
    job = Job(
        id="lidl:hidden-live",
        source="lidl",
        status="hidden",
        application_stage="applied",
        applied_at=now - dt.timedelta(days=3),
    )
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"):
        lidl_followup.set_reminders(job.id, True)
        due = lidl_followup.due_reminders([job], now=now)
        assert len(due) == 1 and due[0]["code"] == "day3"

        job.application_stage = "hired"
        assert lidl_followup.due_reminders([job], now=now) == []


def test_submitted_lidl_detail_renders_the_full_followup_card():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="lidl:detail",
            source="lidl",
            brand="Lidl Danmark",
            title="Butiksassistent",
            city="Herlev",
            status="applied",
            applied_at=dt.datetime(2026, 7, 31, 9, 35),
            applied_confidence="receipt",
        ))
        session.commit()

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"), \
            mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions), \
            mock.patch.object(app.settings_store, "get_home", return_value=None), \
            mock.patch.object(
                app.profile_store,
                "load_profile",
                return_value={"email": "ivan@example.com"},
            ):
        response = app.detail(_request("/job/lidl:detail"), "lidl:detail")

    html = response.body.decode("utf-8")
    for phrase in (
        "Что делать дальше",
        "Найди письмо о кандидатском профиле",
        "Glemt adgangskode?",
        "Открой «Søgte jobs»",
        "Включить напоминания",
        "Автомониторинг кабинета",
        "Подключить кабинет",
        "каждые 30 мин",
        "Вход в кабинет Lidl",
        "Сохранить и подключить",
        "Windows DPAPI",
        "Screening:",
        "не более чем за 6 недель",
        "Ansøgningsdokumenter:",
        "Gemte ansøgninger:",
        "не удаляй весь профиль",
    ):
        assert phrase in html
    assert "Открыть кабинет Lidl" in html
    assert "career5.successfactors.eu" in html


def test_manual_lidl_mark_never_claims_a_receipt_or_confirmed_submission():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"):
        job = Job(
            id="lidl:manual-proof",
            source="lidl",
            status="applied",
            applied_at=dt.datetime(2026, 8, 12, 9, 0),
            applied_confidence="manual",
        )
        guide = lidl_followup.view(job, "ivan@example.com")

    first = guide["steps"][0]
    assert first["done"] is False
    assert guide["submission_confirmed"] is False
    assert "вручную" in first["title"].lower()
    assert "квитанц" not in first["title"].lower()
    assert "квитанция сайта" in first["text"].lower()


def test_followup_tick_marks_reminder_only_after_telegram_accepts_it():
    _engine, sessions = _database()
    now = dt.datetime.utcnow()
    with sessions() as session:
        session.add(Job(
            id="lidl:tick",
            source="lidl",
            status="applied",
            applied_at=now - dt.timedelta(minutes=1),
        ))
        session.commit()

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_followup, "PATH", Path(tmp) / "followups.json"), \
            mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app, "_cloud_profile_enabled", return_value=True), \
            mock.patch.object(app.cloud_auth, "send_digest", return_value=False):
        lidl_followup.set_reminders("lidl:tick", True)
        app._lidl_followup_tick()
        assert lidl_followup.due_reminders(
            [Job(id="lidl:tick", source="lidl", status="applied",
                 applied_at=now - dt.timedelta(minutes=1))],
            now=now,
        )[0]["code"] == "setup"

        with mock.patch.object(app.cloud_auth, "send_digest", return_value=True):
            app._lidl_followup_tick()
        assert lidl_followup.due_reminders(
            [Job(id="lidl:tick", source="lidl", status="applied",
                 applied_at=now - dt.timedelta(minutes=1))],
            now=now,
        ) == []
