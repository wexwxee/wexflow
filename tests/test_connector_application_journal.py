"""External assisted forms use the shared Application registry honestly."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select
from starlette.requests import Request

import app
import applications
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
            mock.patch.object(app, "_launch_connector_filler", launched.append):
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


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
