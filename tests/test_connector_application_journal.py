"""External assisted forms use the shared Application registry honestly."""
import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select
from starlette.requests import Request

import app
import applications
from connectors import apply_dispatch
from db import Application, Job, utcnow


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def _request(path: str) -> Request:
    return Request({
        "type": "http", "method": "POST", "path": path,
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000),
    })


def test_connector_started_and_incomplete_states_are_source_scoped():
    engine, sessions = _database()
    with mock.patch.object(applications, "get_session", sessions):
        applications.mark_submitting(
            ["shared-id"], origin="assisted", source="teamtailor")
        applications.mark_submitting(
            ["shared-id"], origin="assisted", source="greenhouse")
        assert applications.state_of("shared-id", "teamtailor") == "submitting"
        assert applications.state_of("shared-id", "greenhouse") == "submitting"
        assert applications.state_of("shared-id", "salling") == ""
        states = applications.states_for_jobs([
            Job(id="shared-id", source="teamtailor", title="One"),
            Job(id="shared-id", source="greenhouse", title="Two"),
        ])
        assert states == {
            ("teamtailor", "shared-id"): "submitting",
            ("greenhouse", "shared-id"): "submitting",
        }
        applications.mark_failed(["shared-id"], source="teamtailor")
        assert applications.state_of("shared-id", "teamtailor") == "failed"
        assert applications.state_of("shared-id", "greenhouse") == "submitting"
    with Session(engine) as session:
        assert len(session.exec(select(Application)).all()) == 2


def test_stale_assisted_window_becomes_incomplete():
    _engine, sessions = _database()
    now = utcnow()
    with sessions() as session:
        session.add(Application(
            source="teamtailor", job_id="tt:stale", state="submitting",
            origin="assisted", updated_at=now - dt.timedelta(hours=7),
        ))
        session.add(Application(
            source="teamtailor", job_id="tt:fresh", state="submitting",
            origin="assisted", updated_at=now - dt.timedelta(minutes=5),
        ))
        session.commit()
    with mock.patch.object(applications, "get_session", sessions):
        assert applications.expire_stale_assisted(now=now) == 1
        assert applications.state_of("tt:stale", "teamtailor") == "failed"
        assert applications.state_of("tt:fresh", "teamtailor") == "submitting"


def test_manual_connector_submission_keeps_connector_source():
    engine, sessions = _database()
    job = Job(
        id="ashby:demo:1", source="ashby", title="Demo",
        status="applied", applied_at=utcnow(), applied_confidence="manual",
    )
    with mock.patch.object(applications, "get_session", sessions):
        fresh = applications.record_submitted([job])
        assert fresh == [job]
        assert applications.state_of(job.id, "ashby") == "submitted"
        assert applications.state_of(job.id, "salling") == ""
    with Session(engine) as session:
        row = session.exec(select(Application)).one()
        assert row.source == "ashby"
        assert row.origin == ""
        assert row.confidence == "manual"
        assert row.submitted_at == job.applied_at


def test_positive_lidl_receipt_is_persisted_as_real_submission():
    engine, sessions = _database()
    job_id = "lidl:receipt"
    with Session(engine) as session:
        session.add(Job(
            id=job_id,
            source="lidl",
            brand="Lidl Danmark",
            title="Butiksassistent",
            status="seen",
        ))
        session.commit()
    with mock.patch("db.get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions):
        assert apply_dispatch._record_confirmed_submission(job_id) is True

    with Session(engine) as session:
        job = session.get(Job, job_id)
        row = session.exec(select(Application).where(
            Application.job_id == job_id,
            Application.source == "lidl",
        )).one()
        assert job.status == "applied"
        # Seeing the receipt establishes the application, but the platform is
        # trusted only after exact screenshot bytes are registered.
        assert job.applied_confidence == "indirect"
        assert row.state == "submitted"
        assert row.confidence == "indirect"


def test_connector_routes_track_incomplete_then_submitted_result():
    engine, sessions = _database()
    job_id = "tt:demo:route"
    with Session(engine) as session:
        session.add(Job(
            id=job_id, source="teamtailor", title="Demo", status="new",
            application_link="https://demo.teamtailor.com/jobs/1",
        ))
        session.commit()
    launched = []
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions), \
            mock.patch.object(app, "_launch_connector_filler",
                              lambda url, job_id="", submit=False: launched.append(url)):
        response = app.start_connector_apply(job_id, _request(f"/job/{job_id}/connector/apply"))
        assert response.status_code == 303
        assert launched == ["https://demo.teamtailor.com/jobs/1"]
        assert applications.state_of(job_id, "teamtailor") == "submitting"

        response = app.connector_apply_result(
            job_id, _request(f"/job/{job_id}/connector/result"), "incomplete")
        assert response.status_code == 303
        assert applications.state_of(job_id, "teamtailor") == "failed"

        app.start_connector_apply(job_id, _request(f"/job/{job_id}/connector/apply"))
        response = app.connector_apply_result(
            job_id, _request(f"/job/{job_id}/connector/result"), "submitted")
        assert response.status_code == 303
        assert applications.state_of(job_id, "teamtailor") == "submitted"
    with Session(engine) as session:
        stored = session.get(Job, job_id)
        assert stored.status == "applied"
        assert stored.applied_at is not None
        assert stored.applied_confidence == "manual"


def test_lidl_real_submit_mode_requires_ack_and_arms_worker():
    _engine, sessions = _database()
    job_id = "lidl:submit-mode"
    with sessions() as session:
        session.add(Job(
            id=job_id,
            source="lidl",
            brand="Lidl Danmark",
            title="Butiksassistent",
            application_link=(
                "https://ea-lidl.cfapps.eu20.hana.ondemand.com/"
                "easyapply/index.html?ReqId=1"
            ),
        ))
        session.commit()
    launches = []
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions), \
            mock.patch.object(app, "_claim_connector_launch", return_value=True), \
            mock.patch.object(
                app,
                "_launch_connector_filler",
                side_effect=lambda url, job_id="", submit=False: launches.append(submit),
            ):
        rejected = app.start_connector_apply(
            job_id,
            _request(f"/job/{job_id}/connector/apply"),
            mode="submit",
            submit_ack="",
        )
        assert rejected.status_code == 303
        assert launches == []

        prepared = app.start_connector_apply(
            job_id,
            _request(f"/job/{job_id}/connector/apply"),
            mode="prepare",
        )
        assert prepared.status_code == 303
        assert launches == [False]

        armed = app.start_connector_apply(
            job_id,
            _request(f"/job/{job_id}/connector/apply"),
            mode="submit",
            submit_ack="1",
        )
        assert armed.status_code == 303
        assert launches == [False, True]


def test_audit_renders_submitted_and_unfinished_connector_forms():
    _engine, sessions = _database()
    now = utcnow()
    with sessions() as session:
        session.add(Job(
            id="ashby:done", source="ashby", title="Submitted role",
            status="applied", applied_at=now, applied_confidence="manual",
        ))
        session.add(Job(
            id="gh:pending", source="greenhouse", title="Pending role", status="new",
        ))
        session.add(Application(
            source="greenhouse", job_id="gh:pending", state="failed",
            origin="assisted", updated_at=now,
        ))
        session.commit()
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app, "_applied_proofs", return_value={}):
        response = app.audit_log(_request("/audit"))
    page = response.body.decode("utf-8")
    assert "Submitted role" in page
    assert "Pending role" in page
    assert "не завершено" in page
    assert "Другие компании · Greenhouse" in page


def test_reconcile_repairs_both_sides_of_application_history():
    engine, sessions = _database()
    submitted_at = utcnow() - dt.timedelta(days=1)
    with Session(engine) as session:
        session.add(Job(
            id="legacy-applied", source="salling", title="Legacy",
            status="applied", applied_at=submitted_at, applied_confidence="receipt",
        ))
        session.add(Job(
            id="journal-only", source="salling", title="Journal only", status="seen",
        ))
        session.add(Application(
            source="salling", job_id="journal-only", state="submitted",
            submitted_at=submitted_at, confidence="manual",
        ))
        session.commit()
    with mock.patch.object(applications, "get_session", sessions):
        result = applications.reconcile_applied_state()
        assert result == {"journal": 1, "jobs": 1}
        # Повторный проход идемпотентен.
        assert applications.reconcile_applied_state() == {"journal": 0, "jobs": 0}
    with Session(engine) as session:
        rows = session.exec(select(Application)).all()
        assert len(rows) == 2
        recovered = session.exec(select(Application).where(
            Application.job_id == "legacy-applied"
        )).one()
        assert recovered.state == "submitted" and recovered.confidence == "receipt"
        journal_only = session.get(Job, "journal-only")
        assert journal_only.status == "applied"
        assert journal_only.applied_at == submitted_at
        assert journal_only.applied_confidence == "manual"


def test_registry_upserts_do_not_duplicate_one_application():
    engine, sessions = _database()
    with mock.patch.object(applications, "get_session", sessions):
        applications.mark_submitting(["same"], origin="telegram")
        applications.mark_submitting(["same"], origin="batch")
        applications.record_submitted([Job(
            id="same", source="salling", status="applied", applied_at=utcnow()
        )])
    with Session(engine) as session:
        rows = session.exec(select(Application)).all()
        assert len(rows) == 1 and rows[0].state == "submitted"


def test_local_day_boundaries_are_converted_to_utc():
    warsaw = ZoneInfo("Europe/Warsaw")
    summer = dt.datetime(2026, 7, 14, 12, tzinfo=warsaw)
    winter = dt.datetime(2026, 1, 14, 12, tzinfo=warsaw)
    assert applications._local_day_utc_bounds(summer) == (
        dt.datetime(2026, 7, 13, 22), dt.datetime(2026, 7, 14, 22)
    )
    assert applications._local_day_utc_bounds(winter) == (
        dt.datetime(2026, 1, 13, 23), dt.datetime(2026, 1, 14, 23)
    )


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
