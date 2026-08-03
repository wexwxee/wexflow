"""Official Salling candidate-cockpit monitoring."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine

import applications
import salling_monitor
from db import Job


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


BODY = """
Tilbage
Søgte stillinger
Id
Status
Titel
Dato
Brand
200092
Applied
Salgsassistent til Textil - København V
01.05.2026
.føtex
203328
Invited to interview
1. assistent - Brønshøj
15.06.2026
Netto Danmark
204550
Rejected
Salgsassistent til Nonfood - Vanløse
03.08.2026
.føtex
"""


def test_real_cockpit_table_is_parsed_by_requisition_id():
    known = [
        {"id": "job:a", "requisition_id": "200092", "title": "Local title"},
        {"id": "job:b", "requisition_id": "203328", "title": "1. assistent"},
    ]
    rows = salling_monitor.parse_applied_jobs(BODY, known)
    assert len(rows) == 3
    assert rows[0]["job_id"] == "job:a"
    assert rows[0]["title"] == "Local title"
    assert rows[0]["status"] == "applied"
    assert rows[1]["status"] == "interview"
    assert rows[2]["job_id"] == ""
    assert rows[2]["status"] == "rejected"


def test_current_cockpit_packed_accessibility_rows_are_parsed():
    body = """
Id
Status
Titel
Dato
Brand
200092\tApplied
Salgsassistent til Textil - København V
01.05.2026\t.føtex
203328\tInvited to interview
1. assistent - Brønshøj
15.06.2026\tNetto Danmark
"""
    rows = salling_monitor.parse_applied_jobs(body, [])
    assert [(row["requisition_id"], row["status"]) for row in rows] == [
        ("200092", "applied"),
        ("203328", "interview"),
    ]
    assert rows[0]["brand"] == ".føtex"


def test_profile_count_prevents_treating_unauthorised_empty_table_as_truth():
    main = "Søgte stillinger Du kan følge dine ansøgninger her 42 Søgte stillinger"
    assert salling_monitor.applied_count_hint(main) == 42
    assert salling_monitor.applied_count_hint("Ingen data") == 0


def test_view_keeps_profile_total_when_table_temporarily_fails():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(salling_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch.object(salling_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"):
        salling_monitor.save_state(
            enabled=True, connected=True, phase="error", reported_count=42,
            applications={}, last_error="temporary",
        )
        data = salling_monitor.view()
    assert data["application_count"] == 42


def test_candidate_profile_markers_require_a_real_logged_in_page():
    assert salling_monitor.is_logged_in(
        "Log ud Min profil Du kan opdatere din profil her Søgte stillinger"
    ) is True
    assert salling_monitor.is_logged_in("Email Password Log ind") is False


def test_portal_restores_a_submission_missed_by_wexflow():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="salling:restored",
            source="salling",
            title="Salgsassistent",
            requisition_id="200092",
            status="seen",
        ))
        session.commit()
    item = {
        "job_id": "salling:restored",
        "requisition_id": "200092",
        "title": "Salgsassistent",
        "status": "applied",
        "status_label": "Заявка подана",
    }
    with mock.patch("db.get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions):
        restored = salling_monitor._persist_statuses([item])
    assert restored == [{"id": "salling:restored", "title": "Salgsassistent"}]
    with sessions() as session:
        job = session.get(Job, "salling:restored")
        assert job.status == "applied"
        assert job.applied_at is not None
        assert job.applied_confidence == "portal"
        assert job.application_status_source == "salling_portal"


def test_portal_stage_updates_an_existing_application():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="salling:interview",
            source="salling",
            title="1. assistent",
            requisition_id="203328",
            status="applied",
            applied_at=dt.datetime(2026, 7, 1, 9, 0),
        ))
        session.commit()
    with mock.patch("db.get_session", sessions):
        restored = salling_monitor._persist_statuses([{
            "job_id": "salling:interview",
            "status": "interview",
            "status_label": "Приглашение на собеседование",
        }])
    assert restored == []
    with sessions() as session:
        job = session.get(Job, "salling:interview")
        assert job.status == "interview"
        assert job.application_status_source == "salling_portal"


def test_first_snapshot_is_baseline_but_later_stage_change_notifies():
    current = [{
        "job_id": "salling:1", "requisition_id": "1", "title": "Job",
        "status": "applied", "status_label": "Заявка подана",
    }]
    assert salling_monitor.diff_snapshots({}, current) == []
    previous = {"salling:1": {"status": "applied", "status_label": "Заявка подана"}}
    changed = [dict(current[0], status="offer", status_label="Предложение о работе")]
    assert salling_monitor.diff_snapshots(previous, changed)[0]["previous_status"] == "applied"


def test_monitor_state_contains_no_credentials():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(salling_monitor, "STATE_PATH", Path(tmp) / "monitor.json"):
        salling_monitor.save_state(enabled=True, connected=True, applications={})
        raw = salling_monitor.STATE_PATH.read_text(encoding="utf-8").lower()
    assert "password" not in raw
    assert "email" not in raw
