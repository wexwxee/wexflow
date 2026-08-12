"""UI and route boundaries for the application-stage journal."""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select
from starlette.requests import Request

import app
from db import Application, ApplicationStatusEvent, Job


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def _request(path: str = "/audit") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
    })


def test_new_application_stages_are_safe_and_templates_compile():
    assert {"reviewing", "hired", "withdrawn"} <= app.SAFE_JOB_STATUSES
    for name in ("audit.html", "detail.html", "index.html", "settings.html"):
        app.templates.env.get_template(name)

    detail_source = app.templates.env.loader.get_source(
        app.templates.env, "detail.html",
    )[0]
    assert "application_tracking.status" in detail_source
    assert "application_history" in detail_source
    assert "application_email.count" in detail_source
    assert "Добавить ещё письмо" in detail_source


def test_submitted_application_cannot_be_reset_but_hidden_listing_keeps_stage():
    _engine, sessions = _database()
    applied_at = dt.datetime(2026, 8, 10, 8, 0)
    with sessions() as session:
        session.add(Job(
            id="hidden-live-application",
            source="salling",
            title="Butiksassistent",
            status="hidden",
            application_stage="reviewing",
            applied_at=applied_at,
        ))
        session.commit()

    client = TestClient(app.app, base_url="http://127.0.0.1")
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app, "_start_view_sync"):
        blocked = client.post(
            "/job/hidden-live-application/status",
            data={"status": "seen"},
            follow_redirects=False,
        )
        hidden = client.post(
            "/job/hidden-live-application/status",
            data={"status": "hidden"},
            follow_redirects=False,
        )
        hired = client.post(
            "/job/hidden-live-application/status",
            data={"status": "hired"},
            follow_redirects=False,
        )

    assert blocked.status_code == hidden.status_code == hired.status_code == 303
    assert "error=" in blocked.headers["location"]
    with sessions() as session:
        job = session.get(Job, "hidden-live-application")
        events = session.exec(select(ApplicationStatusEvent)).all()
        assert job.status == "hidden"
        assert job.application_stage == "hired"
        assert job.applied_at == applied_at
        assert [event.stage for event in events] == ["hired"]


def test_application_center_includes_unfinished_salling_attempts():
    _engine, sessions = _database()
    now = dt.datetime(2026, 8, 12, 12, 0)
    with sessions() as session:
        session.add(Job(
            id="salling-failed", source="salling", title="Failed Salling form",
            status="seen",
        ))
        session.add(Job(
            id="salling-open", source="salling", title="Open Salling form",
            status="seen",
        ))
        session.add(Application(
            source="salling", job_id="salling-failed", state="failed",
            updated_at=now,
        ))
        session.add(Application(
            source="salling", job_id="salling-open", state="submitting",
            updated_at=now + dt.timedelta(minutes=1),
        ))
        session.commit()

    monitor = {
        "source": "salling", "name": "Salling", "tone": "muted",
        "headline": "Кабинет не подключён", "settings_url": "/settings/salling",
        "application_count": 0, "last_success_at": "", "last_error": "",
        "last_notification_error": "", "busy": False, "phase": "idle",
        "enabled": False, "connected": False,
    }
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app, "_applied_proofs", return_value={}), \
            mock.patch.object(app.salling_monitor, "load_state", return_value={}), \
            mock.patch.object(app.lidl_monitor, "load_state", return_value={}), \
            mock.patch.object(
                app, "_application_monitor_view",
                side_effect=lambda source: {**monitor, "source": source,
                                             "name": source.title()},
            ), \
            mock.patch.object(app, "_cloud_profile_enabled", return_value=False):
        response = app.audit_log(_request())

    page = response.body.decode("utf-8")
    assert "Failed Salling form" in page
    assert "Open Salling form" in page
    assert "Продолжить анкету" in page
    assert "не завершено" in page


def test_unregistered_png_is_never_shown_as_application_proof(tmp_path: Path):
    _engine, sessions = _database()
    proof_dir = tmp_path / "logs" / "applied"
    proof_dir.mkdir(parents=True)
    (proof_dir / "applied_lidl_forged-job.png").write_bytes(b"not a receipt")
    job = Job(id="forged-job", source="lidl", applied_at=dt.datetime(2026, 8, 12))

    with mock.patch.object(app.config, "DATA_DIR", tmp_path), \
            mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app.trust, "valid_receipt_screens", return_value={}):
        proofs = app._applied_proofs(object(), [job])
        with pytest.raises(app.HTTPException) as denied:
            app.applied_proof("applied_lidl_forged-job.png")

    assert proofs == {}
    assert denied.value.status_code == 404

    verified = proof_dir / "verified-receipt.png"
    verified.write_bytes(b"registered receipt bytes")
    with mock.patch.object(app.config, "DATA_DIR", tmp_path):
        with sessions() as session:
            saved_job = Job(
                id="verified-job", source="lidl", status="applied",
                applied_at=dt.datetime(2026, 8, 12),
            )
            session.add(saved_job)
            session.commit()
            assert app.trust.record_receipt_screen(
                session, saved_job, verified,
            ) is not None
            session.commit()
    with mock.patch.object(app.config, "DATA_DIR", tmp_path), \
            mock.patch.object(app, "get_session", sessions):
        response = app.applied_proof("verified-receipt.png")
    assert Path(response.path).name == "verified-receipt.png"


def test_hub_counts_every_submission_and_orders_by_applied_at():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="latest-hidden", source="salling", title="Latest hidden",
            status="hidden", application_stage="hired",
            applied_at=dt.datetime(2026, 8, 12), modified="2020-01-01",
        ))
        session.add(Job(
            id="older-applied", source="salling", title="Older applied",
            status="applied", application_stage="applied",
            applied_at=dt.datetime(2026, 8, 1), modified="2099-01-01",
        ))
        session.commit()

    captured = {}

    def capture(_name, context):
        captured.update(context)
        return context

    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app.templates, "TemplateResponse", side_effect=capture), \
            mock.patch.object(
                app, "_seven_eleven_state",
                return_value={"stores": 0, "profile_ready": False, "name": ""},
            ), \
            mock.patch.object(app.profile_store, "load_profile", return_value={}), \
            mock.patch.object(app.autopilot, "get_rule", return_value={"enabled": False}), \
            mock.patch.object(app, "_autopilot_status_payload", return_value={}), \
            mock.patch.object(app, "_data_age_minutes", return_value=0), \
            mock.patch.object(app.subscription, "status", return_value={}):
        app.hub(_request("/hub"))

    assert captured["applied_jobs"] == 2
    assert captured["last_applied"]["title"] == "Latest hidden"


def test_automatic_status_tick_retries_the_database_outbox():
    with mock.patch.object(
        app.application_tracker, "mark_no_response", return_value=[],
    ), mock.patch.object(
        app.application_tracker, "flush_pending_notifications", return_value=True,
    ) as flush, mock.patch.object(
        app, "_cloud_profile_enabled", return_value=True,
    ), mock.patch.object(app, "_start_view_sync") as sync:
        app._application_tracking_tick()

    sync.assert_not_called()
    assert flush.call_args_list == [
        mock.call(origin="automatic", source_name="WexFlow"),
    ]


def test_phone_sync_uses_application_stage_for_hidden_listing():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="hidden-phone-stage",
            source="lidl",
            title="Hidden but interviewing",
            status="hidden",
            application_stage="interview",
            applied_at=dt.datetime(2026, 8, 12, 9, 0),
        ))
        session.commit()

    sent = []
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app, "_cloud_profile_enabled", return_value=True), \
            mock.patch.object(app, "_begin_cloud_sync", return_value=123.0), \
            mock.patch.object(app, "_finish_cloud_sync"), \
            mock.patch.object(
                app.cloud_auth, "report_applied",
                side_effect=lambda items: sent.extend(items) or True,
            ):
        assert app._sync_applied_to_cloud(force=True) is True

    assert sent[0]["id"] == "hidden-phone-stage"
    assert sent[0]["status"] == "interview"


def test_feed_stage_filter_uses_application_stage_not_listing_status():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="hidden-hired-filter",
            source="salling",
            title="Hired hidden listing",
            status="hidden",
            application_stage="hired",
            applied_at=dt.datetime(2026, 8, 12, 9, 0),
            country="DK",
        ))
        session.commit()

    client = TestClient(app.app, base_url="http://127.0.0.1")
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app.settings_store, "get_home", return_value=None):
        response = client.get("/?status=hired")

    assert response.status_code == 200
    assert "Hired hidden listing" in response.text
