import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine

import application_tracker
from db import Job


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
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
