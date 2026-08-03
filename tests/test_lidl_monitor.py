"""Lidl candidate-portal monitoring without credential storage."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine

import lidl_monitor
from db import Job


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def test_login_detection_requires_account_content_and_no_password_field():
    account = """
        Kandidatprofil
        Ansøgningsdokumenter
        Profiloplysninger
        Søgte jobs (1)
        Gemte ansøgninger
        Log ud
    """
    assert lidl_monitor.is_logged_in(account) is True
    assert lidl_monitor.is_logged_in(
        "Log på Brugernavn Glemt adgangskode", has_password_field=True
    ) is False


def test_visible_applied_job_statuses_are_parsed_conservatively():
    body = """
        Kandidatprofil
        Søgte jobs (2)
        Butiksassistent - 37 timer - Herlev
        Requisition ID 728695
        Ansøgningsstatus: Under behandling

        Salgsassistent i Vangløse
        Requisition ID 728700
        Ansøgningsstatus: Inviteret til jobsamtale
        Gemte ansøgninger
    """
    jobs = [
        {"id": "lidl:728695", "title": "Butiksassistent - 37 timer - Herlev",
         "requisition_id": "728695"},
        {"id": "lidl:728700", "title": "Salgsassistent i Vangløse",
         "requisition_id": "728700"},
    ]
    parsed = {item["job_id"]: item for item in lidl_monitor.extract_applications(body, jobs)}
    assert parsed["lidl:728695"]["status"] == "applied"
    assert parsed["lidl:728700"]["status"] == "interview"
    assert lidl_monitor.classify_status("I proces")["code"] == "applied"
    assert lidl_monitor.classify_status("Tilfældig profiltekst")["code"] == "unknown"


def test_first_read_is_baseline_and_only_real_status_change_notifies():
    item = {
        "job_id": "lidl:1", "title": "Butiksassistent",
        "status": "applied", "status_label": "На рассмотрении",
    }
    assert lidl_monitor.diff_snapshots({}, [item]) == []
    previous = {
        "lidl:1": {
            "status": "applied",
            "status_label": "На рассмотрении",
        }
    }
    assert lidl_monitor.diff_snapshots(previous, [item]) == []
    changed = dict(item, status="interview", status_label="Приглашение на собеседование")
    result = lidl_monitor.diff_snapshots(previous, [changed])
    assert len(result) == 1
    assert result[0]["previous_status"] == "applied"


def test_state_contains_no_password_or_email_credentials():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch.object(lidl_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"):
        lidl_monitor.set_enabled(True)
        lidl_monitor.save_state(
            connected=True,
            phase="connected",
            applications={
                "lidl:1": {
                    "title": "Butiksassistent",
                    "status": "applied",
                }
            },
        )
        raw = (Path(tmp) / "monitor.json").read_text(encoding="utf-8").lower()
    assert "password" not in raw
    assert "adgangskode" not in raw
    assert "email" not in raw


def test_confident_portal_change_updates_local_job_and_sends_one_message():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="lidl:status",
            source="lidl",
            title="Butiksassistent",
            status="applied",
            applied_at=dt.datetime(2026, 7, 31, 9, 35),
        ))
        session.commit()
    change = {
        "job_id": "lidl:status",
        "title": "Butiksassistent",
        "previous_status": "applied",
        "previous_label": "На рассмотрении",
        "status": "interview",
        "status_label": "Приглашение на собеседование",
    }
    with mock.patch("db.get_session", sessions), \
            mock.patch("cloud_auth.send_digest", return_value=True) as send:
        lidl_monitor._apply_changes([change])
    with sessions() as session:
        assert session.get(Job, "lidl:status").status == "interview"
    send.assert_called_once()
    assert "Butiksassistent" in send.call_args.args[0]


def test_first_portal_snapshot_also_persists_a_known_stage():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="lidl:baseline",
            source="lidl",
            title="Butiksassistent",
            status="no_response",
            applied_at=dt.datetime(2026, 5, 1, 9, 35),
        ))
        session.commit()
    snapshot = {
        "job_id": "lidl:baseline",
        "title": "Butiksassistent",
        "status": "rejected",
        "status_label": "Afslag",
    }
    with mock.patch("db.get_session", sessions):
        lidl_monitor._persist_statuses([snapshot])
    with sessions() as session:
        job = session.get(Job, "lidl:baseline")
        assert job.status == "rejected"
        assert job.application_status_source == "lidl_portal"


def test_no_receipt_job_is_checked_and_portal_confirmation_becomes_submission():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="lidl:verify",
            source="lidl",
            title="Butiksassistent",
            requisition_id="728999",
            status="seen",
        ))
        session.commit()
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch("db.get_session", sessions):
        assert lidl_monitor.queue_verification("lidl:verify") is True
        known = lidl_monitor._known_jobs()
        assert [item["id"] for item in known] == ["lidl:verify"]
        lidl_monitor._persist_statuses([{
            "job_id": "lidl:verify",
            "title": "Butiksassistent",
            "status": "applied",
            "status_label": "На рассмотрении",
        }])
        assert lidl_monitor.load_state()["pending_verifications"] == []
    with sessions() as session:
        job = session.get(Job, "lidl:verify")
        assert job.status == "applied"
        assert job.applied_at is not None
        assert job.applied_confidence == "portal"
        assert job.application_status_source == "lidl_portal"


def test_dead_monitor_lock_is_removed_immediately():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"), \
            mock.patch.object(lidl_monitor.os, "kill", side_effect=ProcessLookupError):
        lidl_monitor.LOCK_PATH.write_text("999999 2026-08-03", encoding="utf-8")
        assert lidl_monitor.is_busy() is False
        assert not lidl_monitor.LOCK_PATH.exists()


def test_disabling_monitor_keeps_browser_session_but_stops_checks():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"):
        lidl_monitor.save_state(enabled=True, connected=True, phase="connected")
        state = lidl_monitor.set_enabled(False)
    assert state["enabled"] is False
    assert state["connected"] is True
    assert state["phase"] == "off"


def test_orphaned_check_becomes_actionable_instead_of_spinning_forever():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch.object(lidl_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"):
        lidl_monitor.save_state(
            enabled=True, connected=False, phase="checking",
            phase_started_at="2026-08-03T00:00:00+00:00",
        )
        state = lidl_monitor.view()

    assert state["busy"] is False
    assert state["phase"] == "needs_login"
    assert "прервал" in state["last_error"]


def test_failed_telegram_delivery_stays_queued_and_retries():
    change = {
        "source": "lidl", "job_id": "lidl:retry", "title": "Butiksassistent",
        "previous_status": "applied", "status": "interview",
        "status_label": "Собеседование",
    }
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch.object(lidl_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"):
        lidl_monitor._queue_notifications([change])
        with mock.patch("cloud_auth.send_digest", return_value=False):
            assert lidl_monitor._flush_notifications() is False
        assert len(lidl_monitor.load_state()["pending_notifications"]) == 1
        with mock.patch("cloud_auth.send_digest", return_value=True):
            assert lidl_monitor._flush_notifications() is True
        assert lidl_monitor.load_state()["pending_notifications"] == []
