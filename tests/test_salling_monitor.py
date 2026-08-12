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
import application_tracker
import salling_monitor
from db import Application, ApplicationStatusEvent, Job, select


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
    assert rows[0]["portal_status"] == "Applied"
    assert rows[0]["match_strength"] == "exact_requisition"
    assert rows[1]["status"] == "interview"
    assert rows[2]["job_id"] == ""
    assert rows[2]["match_strength"] == "external_requisition"
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


def test_windows_live_lock_check_never_calls_os_kill():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(salling_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"), \
            mock.patch.object(salling_monitor.os, "name", "nt"), \
            mock.patch.object(salling_monitor.os, "kill") as kill, \
            mock.patch("ctypes.WinDLL") as win_dll:
        win_dll.return_value.OpenProcess.return_value = 456
        salling_monitor.LOCK_PATH.write_text(f"{os.getpid()} 2026-08-04", encoding="utf-8")
        assert salling_monitor.is_busy() is True
        kill.assert_not_called()
        win_dll.return_value.CloseHandle.assert_called_once_with(456)


def test_candidate_profile_markers_require_a_real_logged_in_page():
    assert salling_monitor.is_logged_in(
        "Log ud Min profil Du kan opdatere din profil her Søgte stillinger"
    ) is True
    assert salling_monitor.is_logged_in("Email Password Log ind") is False


def test_salling_classifies_reviewing_hired_and_withdrawn_statuses():
    assert salling_monitor.classify_status("Under review")["code"] == "reviewing"
    assert salling_monitor.classify_status("Hired")["code"] == "hired"
    assert salling_monitor.classify_status("Application accepted")["code"] == "unknown"
    assert salling_monitor.classify_status("You have not been hired")["code"] == "rejected"
    assert salling_monitor.classify_status("You have not yet been hired")["code"] == "unknown"
    assert salling_monitor.classify_status("Du er ikke blevet ansat")["code"] == "rejected"
    assert salling_monitor.classify_status("Application withdrawn")["code"] == "withdrawn"


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
        "portal_status": "Applied",
        "match_strength": "exact_requisition",
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
            "portal_status": "Invited to interview",
            "match_strength": "exact_requisition",
        }])
    assert restored == []
    with sessions() as session:
        job = session.get(Job, "salling:interview")
        assert job.status == "interview"
        assert job.application_status_source == "salling_portal"


def test_salling_portal_upgrades_existing_provenance_and_persists_hired():
    _engine, sessions = _database()
    applied_at = dt.datetime(2026, 7, 1, 9, 0)
    with sessions() as session:
        session.add(Job(
            id="salling:hired", source="salling", title="Salgsassistent",
            requisition_id="203999", status="offer", application_stage="offer",
            applied_at=applied_at, applied_confidence="manual",
            application_status_source="manual",
        ))
        session.add(Application(
            source="salling", job_id="salling:hired", state="submitted",
            confidence="manual", submitted_at=applied_at,
        ))
        session.commit()
    snapshot = {
            "job_id": "salling:hired", "requisition_id": "203999",
            "status": "hired", "status_label": "Принят на работу",
            "portal_status": "Hired", "match_strength": "exact_requisition",
    }
    with mock.patch("db.get_session", sessions):
        salling_monitor._persist_statuses([snapshot])
        salling_monitor._persist_statuses([snapshot])
    with sessions() as session:
        job = session.get(Job, "salling:hired")
        application = session.exec(select(Application)).one()
        event = session.exec(select(ApplicationStatusEvent)).one()
    assert job.application_stage == "hired"
    assert job.applied_confidence == "portal"
    assert job.application_status_source == "salling_portal"
    assert application.confidence == "portal"
    assert event.stage == "hired" and event.raw_label == "Hired"


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


def test_disabling_salling_during_check_is_not_undone_by_worker_completion():
    fake_page = mock.MagicMock()
    fake_context = mock.MagicMock()
    fake_context.pages = [fake_page]
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(salling_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch.object(salling_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"), \
            mock.patch("playwright.sync_api.sync_playwright") as sync_playwright, \
            mock.patch.object(salling_monitor, "_launch_context", return_value=fake_context), \
            mock.patch.object(salling_monitor, "_open_portal"), \
            mock.patch.object(salling_monitor, "_page_text", return_value="Ingen data"), \
            mock.patch.object(salling_monitor, "_open_applied"), \
            mock.patch.object(salling_monitor, "_known_jobs", return_value=[]), \
            mock.patch.object(salling_monitor, "parse_applied_jobs", return_value=[]), \
            mock.patch.object(salling_monitor, "_persist_statuses", return_value=[]), \
            mock.patch.object(salling_monitor, "_notify_changes", return_value=True):
        sync_playwright.return_value.__enter__.return_value = mock.MagicMock()
        salling_monitor.save_state(enabled=True, connected=True, phase="connected")

        def login_and_disable(_page):
            salling_monitor.set_enabled(False)
            return True

        with mock.patch.object(salling_monitor, "_login", side_effect=login_and_disable):
            assert salling_monitor.run_check() is True
        assert salling_monitor.load_state()["enabled"] is False
