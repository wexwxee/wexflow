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
import applications
import application_tracker
from db import Application, ApplicationStatusEvent, Job, select


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
    assert parsed["lidl:728695"]["status"] == "reviewing"
    assert parsed["lidl:728695"]["portal_status"] == "Under behandling"
    assert parsed["lidl:728695"]["match_strength"] == "exact_requisition"
    state_row = lidl_monitor._application_map([parsed["lidl:728695"]])["lidl:728695"]
    assert state_row["portal_status"] == "Under behandling"
    assert parsed["lidl:728700"]["status"] == "interview"
    assert lidl_monitor.classify_status("I proces")["code"] == "reviewing"
    assert lidl_monitor.classify_status("Hired")["code"] == "hired"
    assert lidl_monitor.classify_status("Application accepted")["code"] == "unknown"
    assert lidl_monitor.classify_status("You have not been hired")["code"] == "rejected"
    assert lidl_monitor.classify_status("You have not yet been hired")["code"] == "unknown"
    assert lidl_monitor.classify_status("Du er ikke blevet ansat")["code"] == "rejected"
    assert lidl_monitor.classify_status("Ansøgningen trukket tilbage")["code"] == "withdrawn"
    assert lidl_monitor.classify_status("Dokument modtaget")["code"] == "unknown"
    assert lidl_monitor.classify_status("Tilfældig profiltekst")["code"] == "unknown"


def test_exact_requisition_cannot_borrow_unknown_next_rows_rejection():
    body = """
        12345 Butiksmedarbejder København
        Application status: Application received
        99999 Lagerchef Aarhus
        Application status: Rejected
    """
    parsed = lidl_monitor.extract_applications(body, [{
        "id": "lidl:12345", "title": "Butiksmedarbejder København",
        "requisition_id": "12345",
    }])
    assert len(parsed) == 1
    assert parsed[0]["match_strength"] == "exact_requisition"
    assert parsed[0]["status"] == "applied"
    assert parsed[0]["portal_status"] == "Application received"


def test_status_field_on_one_line_does_not_absorb_the_next_row():
    """A single-line portal layout must not lend the next row's rejection."""
    body = (
        "Søgte jobs (2)\n"
        "Butiksmedarbejder København Requisition ID 12345 "
        "Ansøgningsstatus: Under behandling "
        "Lagerchef Aarhus Requisition ID 99999 Ansøgningsstatus: Afslag\n"
    )
    parsed = lidl_monitor.extract_applications(body, [{
        "id": "lidl:12345", "title": "Butiksmedarbejder København",
        "requisition_id": "12345",
    }])
    assert len(parsed) == 1
    assert parsed[0]["status"] == "reviewing"
    assert parsed[0]["portal_status"] == "Under behandling"


def test_row_without_a_labelled_status_stays_unknown():
    body = """
        Butiksmedarbejder København
        Requisition ID 12345
        Afslag på en helt anden ansøgning
    """
    parsed = lidl_monitor.extract_applications(body, [{
        "id": "lidl:12345", "title": "Butiksmedarbejder København",
        "requisition_id": "12345",
    }])
    assert parsed[0]["status"] == "unknown"
    assert parsed[0]["portal_status"] == ""


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
        "portal_status": "Inviteret til jobsamtale",
        "match_strength": "exact_requisition",
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
        "portal_status": "Afslag",
        "match_strength": "exact_requisition",
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
            mock.patch("db.get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions):
        assert lidl_monitor.queue_verification("lidl:verify") is True
        known = lidl_monitor._known_jobs()
        assert [item["id"] for item in known] == ["lidl:verify"]
        lidl_monitor._persist_statuses([{
            "job_id": "lidl:verify",
            "title": "Butiksassistent",
            "status": "applied",
            "status_label": "На рассмотрении",
            "portal_status": "Ansøgning modtaget",
            "match_strength": "exact_requisition",
        }])
        assert lidl_monitor.load_state()["pending_verifications"] == []
    with sessions() as session:
        job = session.get(Job, "lidl:verify")
        assert job.status == "applied"
        assert job.applied_at is not None
        assert job.applied_confidence == "portal"
        assert job.application_status_source == "lidl_portal"


def test_exact_portal_match_upgrades_existing_manual_proof_but_keeps_receipt():
    _engine, sessions = _database()
    applied_at = dt.datetime(2026, 8, 1, 9, 0)
    with sessions() as session:
        for suffix, confidence in (("manual", "manual"), ("receipt", "receipt")):
            session.add(Job(
                id=f"lidl:{suffix}", source="lidl", title=suffix,
                requisition_id=f"72{suffix}", status="applied",
                application_stage="applied", applied_at=applied_at,
                applied_confidence=confidence, application_status_source="manual",
            ))
            session.add(Application(
                source="lidl", job_id=f"lidl:{suffix}", state="submitted",
                confidence=confidence, submitted_at=applied_at,
            ))
        session.commit()

    snapshots = [{
        "job_id": f"lidl:{suffix}", "status": "applied",
        "status_label": "Заявка получена", "portal_status": "Ansøgning modtaget",
        "match_strength": "exact_requisition",
    } for suffix in ("manual", "receipt")]
    with mock.patch("db.get_session", sessions):
        lidl_monitor._persist_statuses(snapshots)

    with sessions() as session:
        manual = session.get(Job, "lidl:manual")
        receipt = session.get(Job, "lidl:receipt")
        rows = {
            row.job_id: row for row in session.exec(select(Application)).all()
        }
    assert manual.applied_confidence == "portal"
    assert manual.application_status_source == "lidl_portal"
    assert rows["lidl:manual"].confidence == "portal"
    assert receipt.applied_confidence == "receipt"
    assert rows["lidl:receipt"].confidence == "receipt"


def test_fuzzy_lidl_title_never_confirms_or_clears_a_pending_application():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="lidl:fuzzy", source="lidl", title="Butiksassistent i Herlev",
            status="seen",
        ))
        session.commit()
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch("db.get_session", sessions):
        lidl_monitor.queue_verification("lidl:fuzzy")
        lidl_monitor._persist_statuses([{
            "job_id": "lidl:fuzzy", "title": "Butiksassistent i Herlev",
            "status": "applied", "status_label": "Заявка получена",
            "portal_status": "Ansøgning modtaget", "match_strength": "fuzzy_title",
        }])
        assert lidl_monitor.load_state()["pending_verifications"] == ["lidl:fuzzy"]
    with sessions() as session:
        job = session.get(Job, "lidl:fuzzy")
        assert job.applied_at is None
        assert job.applied_confidence is None
        assert session.exec(select(Application)).all() == []


def test_similar_lidl_titles_are_marked_fuzzy_when_only_a_common_token_is_visible():
    body = """
        Søgte jobs
        Butiksassistent
        Ansøgningsstatus: Ansøgning modtaget
    """
    rows = lidl_monitor.extract_applications(body, [
        {"id": "lidl:a", "title": "Butiksassistent i Herlev"},
        {"id": "lidl:b", "title": "Butiksassistent i Vanløse"},
    ])
    assert len(rows) == 2
    assert {row["match_strength"] for row in rows} == {"fuzzy_title"}


def test_unique_title_is_strong_only_beside_an_explicit_status_field():
    job = {"id": "lidl:title", "title": "Salgsassistent i Roskilde"}
    structured = lidl_monitor.extract_applications(
        "Salgsassistent i Roskilde\nAnsøgningsstatus: Under review", [job]
    )[0]
    unstructured = lidl_monitor.extract_applications(
        "Gemte ansøgninger\nSalgsassistent i Roskilde\nDokument: Ansøgning modtaget",
        [job],
    )[0]
    assert structured["match_strength"] == "structured_title"
    assert structured["status"] == "reviewing"
    # A title is useful for display/diagnostics but is not an immutable
    # application identity and therefore cannot establish portal proof.
    assert not lidl_monitor._is_strong_portal_match(structured)
    assert unstructured["match_strength"] == "exact_title"
    assert not lidl_monitor._is_strong_portal_match(unstructured)


def test_portal_event_survives_crash_before_notification_flush():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="lidl:outbox", source="lidl", title="Butiksassistent",
            status="applied", application_stage="applied",
            applied_at=dt.datetime(2026, 8, 1, 9, 0),
        ))
        session.commit()
    snapshot = {
        "job_id": "lidl:outbox", "status": "interview",
        "status_label": "Приглашение на собеседование",
        "portal_status": "Inviteret til jobsamtale",
        "match_strength": "exact_requisition",
    }
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch("db.get_session", sessions), \
            mock.patch.object(application_tracker, "get_session", sessions), \
            mock.patch.object(
                application_tracker, "flush_pending_notifications",
                side_effect=RuntimeError("worker stopped after commit"),
            ):
        try:
            lidl_monitor._apply_changes([snapshot], durable=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the injected post-commit crash did not happen")

    with sessions() as session:
        events = session.exec(select(ApplicationStatusEvent)).all()
        assert len(events) == 1
        assert events[0].stage == "interview"
        assert events[0].notified_at is None

    with mock.patch.object(application_tracker, "get_session", sessions), \
            mock.patch("cloud_auth.send_digest", return_value=True):
        assert application_tracker.flush_pending_notifications(
            origin="lidl_portal", source_name="Lidl"
        ) is True
    with sessions() as session:
        assert session.exec(select(ApplicationStatusEvent)).one().notified_at is not None


def test_disabling_lidl_during_check_is_not_undone_by_worker_completion():
    fake_context = mock.MagicMock()
    fake_page = mock.MagicMock()
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "STATE_PATH", Path(tmp) / "monitor.json"), \
            mock.patch.object(lidl_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"), \
            mock.patch("playwright.sync_api.sync_playwright") as sync_playwright, \
            mock.patch.object(lidl_monitor, "_launch_context", return_value=fake_context), \
            mock.patch.object(lidl_monitor, "_portal_page", return_value=fake_page), \
            mock.patch.object(lidl_monitor, "is_logged_in", return_value=True), \
            mock.patch.object(lidl_monitor, "_profile_page", return_value=fake_page), \
            mock.patch.object(lidl_monitor, "_open_applied_jobs"), \
            mock.patch.object(lidl_monitor, "_known_jobs", return_value=[]), \
            mock.patch.object(lidl_monitor, "extract_applications", return_value=[]), \
            mock.patch.object(lidl_monitor, "_apply_changes", return_value=[]):
        sync_playwright.return_value.__enter__.return_value = mock.MagicMock()
        lidl_monitor.save_state(enabled=True, connected=True, phase="connected")

        def body_and_disable(_page):
            lidl_monitor.set_enabled(False)
            return "Kandidatprofil Søgte jobs Log ud", False

        with mock.patch.object(lidl_monitor, "_body", side_effect=body_and_disable):
            assert lidl_monitor.run_check() is True
        assert lidl_monitor.load_state()["enabled"] is False


def test_dead_monitor_lock_is_removed_immediately():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"), \
            mock.patch.object(lidl_monitor.os, "kill", side_effect=ProcessLookupError):
        lidl_monitor.LOCK_PATH.write_text("999999 2026-08-03", encoding="utf-8")
        assert lidl_monitor.is_busy() is False
        assert not lidl_monitor.LOCK_PATH.exists()


def test_windows_live_lock_check_never_calls_os_kill():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(lidl_monitor, "LOCK_PATH", Path(tmp) / "monitor.lock"), \
            mock.patch.object(lidl_monitor.os, "name", "nt"), \
            mock.patch.object(lidl_monitor.os, "kill") as kill, \
            mock.patch("ctypes.WinDLL") as win_dll:
        win_dll.return_value.OpenProcess.return_value = 123
        lidl_monitor.LOCK_PATH.write_text(f"{os.getpid()} 2026-08-04", encoding="utf-8")
        assert lidl_monitor.is_busy() is True
        kill.assert_not_called()
        win_dll.return_value.CloseHandle.assert_called_once_with(123)


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
