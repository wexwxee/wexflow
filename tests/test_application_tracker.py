import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

import application_tracker
from db import Application, Job


def _database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def test_only_unanswered_applied_jobs_age_into_no_response():
    _engine, sessions = _database()
    now = dt.datetime(2026, 7, 31, 12, 0)
    with sessions() as session:
        session.add(Job(id="old", title="Old", status="applied", applied_at=now - dt.timedelta(days=61)))
        session.add(Job(id="fresh", title="Fresh", status="applied", applied_at=now - dt.timedelta(days=59)))
        session.add(Job(id="interview", title="Interview", status="interview", applied_at=now - dt.timedelta(days=90)))
        session.commit()

    with mock.patch.object(application_tracker, "get_session", sessions):
        changed = application_tracker.mark_no_response(now=now)
        again = application_tracker.mark_no_response(now=now)

    assert [item["id"] for item in changed] == ["old"]
    assert again == []
    with sessions() as session:
        old = session.get(Job, "old")
        assert old.status == "no_response"
        assert old.application_status_source == "automatic"
        assert session.get(Job, "fresh").status == "applied"
        assert session.get(Job, "interview").status == "interview"


def test_tracker_view_keeps_submission_age_and_due_date():
    applied_at = dt.datetime(2026, 6, 1, 9, 0)
    now = dt.datetime(2026, 6, 11, 9, 0)
    job = Job(id="one", title="One", status="applied", applied_at=applied_at)
    data = application_tracker.view(job, now=now)
    assert data["age_days"] == 10
    assert data["no_response_due_at"] == applied_at + dt.timedelta(days=60)
    assert data["label"] == "Подано"


def test_manual_later_stage_establishes_submission_and_blocks_duplicate():
    import app as app_module
    import autopilot
    from fastapi.testclient import TestClient

    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(id="fresh-interview", source="salling", title="Fresh", status="new"))
        session.commit()

    client = TestClient(app_module.app, base_url="http://127.0.0.1")
    with mock.patch.object(app_module, "get_session", sessions), \
            mock.patch.object(app_module, "_start_view_sync"):
        response = client.post(
            "/job/fresh-interview/status", data={"status": "interview"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    with sessions() as session:
        job = session.get(Job, "fresh-interview")
        registry = session.exec(
            application_tracker.select(Application).where(
                Application.source == "salling", Application.job_id == "fresh-interview"
            )
        ).one()
    assert job.status == "interview" and job.applied_at is not None
    assert job.applied_confidence == "manual"
    assert registry.state == "submitted" and registry.confidence == "manual"
    assert autopilot.can_submit(job) is False


def test_tracker_advice_separates_silence_from_rejection():
    now = dt.datetime(2026, 8, 3, 12, 0)
    quiet = Job(
        id="quiet", title="Quiet", status="no_response",
        applied_at=now - dt.timedelta(days=61),
        application_status_source="automatic",
    )
    rejected = Job(
        id="rejected", title="Rejected", status="rejected",
        applied_at=now - dt.timedelta(days=10),
        application_status_source="salling_portal",
        applied_confidence="portal",
    )

    silence = application_tracker.view(quiet, now=now)
    refusal = application_tracker.view(rejected, now=now)

    assert silence["label"] == "Нет ответа"
    assert "не отказ" in silence["action"]
    assert refusal["label"] == "Отказ"
    assert refusal["source_label"] == "получено из кабинета Salling"
    assert refusal["confirmation_tone"] == "official"


def test_tracker_suggests_one_followup_only_after_two_weeks():
    now = dt.datetime(2026, 8, 3, 12, 0)
    fresh = Job(id="fresh-advice", status="applied", applied_at=now - dt.timedelta(days=5))
    old = Job(id="old-advice", status="applied", applied_at=now - dt.timedelta(days=18))

    assert application_tracker.view(fresh, now=now)["action_required"] is False
    advice = application_tracker.view(old, now=now)
    assert advice["action_required"] is True
    assert advice["action_label"] == "Можно уточнить"


def test_telegram_status_card_is_actionable_and_escapes_portal_text():
    text = application_tracker.status_notification({
        "source": "lidl",
        "title": "Butik <script>",
        "brand": "Lidl & Co",
        "city": "Herlev",
        "previous_status": "applied",
        "status": "interview",
        "status_label": "Inviteret",
        "url": "https://example.test/job/1",
    })

    assert "приглашение на собеседование" in text
    assert "Butik &lt;script&gt;" in text
    assert "Lidl &amp; Co" in text
    assert '<a href="https://example.test/job/1">' in text
    assert "<script>" not in text


def test_multiple_status_changes_are_sent_as_one_calm_digest():
    changes = [
        {"source": "salling", "job_id": str(i), "title": f"Job {i}", "status": "rejected"}
        for i in range(4)
    ]
    with mock.patch("cloud_auth.send_digest", return_value=True) as send:
        assert application_tracker.notify_status_changes(changes, source_name="Salling Group") is True

    send.assert_called_once()
    assert "4 обновления" in send.call_args.args[0]
