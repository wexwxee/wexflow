"""Apply-by-link lives in the main WexFlow UI, not the old beta feed."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select
from starlette.requests import Request

import app
import applications
from db import Application, Job


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def _request(path: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path,
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000),
    })


def test_native_page_uses_shared_shell_and_vetted_counts():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(id="tt:1", source="teamtailor", title="One", status="new"))
        session.add(Job(id="gh:1", source="greenhouse", title="Two", status="new"))
        session.add(Job(id="ashby:closed", source="ashby", title="Old", status="closed"))
        session.commit()
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app.profile_store, "load_profile", return_value={}), \
            mock.patch.object(app.profile_store, "file_status", return_value="missing"):
        response = app.apply_by_link(_request("/apply-by-link"))
    page = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "wex-shell" in page
    assert "Вставь ссылку на вакансию" in page
    assert "Teamtailor" in page and "Greenhouse" in page and "Ashby" in page
    assert "127.0.0.1:8078" not in page
    assert "показаны первые" not in page  # old arbitrary card cap is gone
    assert "Не заполнено полей: 8" in page


def test_link_start_rejects_local_file_and_accepts_https():
    _engine, sessions = _database()
    launched = []
    request = _request("/apply-by-link/start")
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions):
        response = app.start_apply_by_link(request, "file:///C:/secret.txt")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions), \
            mock.patch.object(app, "_launch_connector_filler",
                              lambda url, job_id="": launched.append(url)):
        response = app.start_apply_by_link(
            request, "https://demo.teamtailor.com/jobs/123")
    assert response.status_code == 303
    assert launched == ["https://demo.teamtailor.com/jobs/123"]
    assert "pending=" in response.headers["location"]
    assert "notice=" in response.headers["location"]
    with sessions() as session:
        job = session.exec(select(Job)).one()
        entry = session.exec(select(Application)).one()
        assert job.source == "manual_link"
        assert entry.job_id == job.id and entry.state == "submitting"

    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions), \
            mock.patch.object(app, "_launch_connector_filler",
                              lambda url, job_id="": launched.append(url)):
        duplicate = app.start_apply_by_link(
            request, "https://demo.teamtailor.com/jobs/123")
    assert duplicate.status_code == 303
    assert launched == ["https://demo.teamtailor.com/jobs/123"]


def test_manual_link_result_returns_to_workspace_and_enters_journal():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="link:test", source="manual_link", title="Demo role",
            brand="jobs.example.com", application_link="https://jobs.example.com/demo",
        ))
        session.add(Application(
            source="manual_link", job_id="link:test", state="submitting",
            origin="assisted",
        ))
        session.commit()
    request = _request("/job/link:test/connector/result")
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions):
        response = app.connector_apply_result(
            "link:test", request, "submitted", "/apply-by-link")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/apply-by-link?")
    with sessions() as session:
        job = session.get(Job, "link:test")
        entry = session.exec(select(Application)).one()
        assert job.status == "applied" and job.applied_confidence == "manual"
        assert entry.state == "submitted"


def test_pending_manual_link_renders_honest_result_choices():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(
            id="link:pending", source="manual_link", title="Pending role",
            brand="jobs.example.com", application_link="https://jobs.example.com/pending",
        ))
        session.add(Application(
            source="manual_link", job_id="link:pending", state="submitting",
            origin="assisted",
        ))
        session.commit()
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app.profile_store, "load_profile", return_value={}), \
            mock.patch.object(app.profile_store, "file_status", return_value="missing"):
        response = app.apply_by_link(
            _request("/apply-by-link"), pending="link:pending")
    page = response.body.decode("utf-8")
    assert "Чем закончилась анкета?" in page
    assert "Я отправил анкету" in page
    assert "Не завершил" in page
    assert 'name="return_to" value="/apply-by-link"' in page


def test_submitted_manual_link_is_not_opened_twice():
    _engine, sessions = _database()
    url = "https://jobs.example.com/already-sent"
    with sessions() as session:
        session.add(Job(
            id="link:sent", source="manual_link", title="Already sent",
            application_link=url, status="applied",
        ))
        session.commit()
    launched = []
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(applications, "get_session", sessions), \
            mock.patch.object(app, "_launch_connector_filler",
                              lambda url, job_id="": launched.append(url)):
        response = app.start_apply_by_link(_request("/apply-by-link/start"), url)
    assert response.status_code == 303
    assert launched == []
    assert "notice=" in response.headers["location"]


def test_desktop_no_longer_starts_beta_server():
    source = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "desktop_app.py"),
                  encoding="utf-8").read()
    start_servers = source.split("def start_servers():", 1)[1].split("def wait_for_hub", 1)[0]
    assert '_ensure(BETA_PORT' not in start_servers


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
