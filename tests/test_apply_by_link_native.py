"""Apply-by-link lives in the main WexFlow UI, not the old beta feed."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine
from starlette.requests import Request

import app
from db import Job


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
    with mock.patch.object(app, "get_session", sessions):
        response = app.apply_by_link(_request("/apply-by-link"))
    page = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "wex-shell" in page
    assert "Вставь ссылку на вакансию" in page
    assert "Teamtailor" in page and "Greenhouse" in page and "Ashby" in page
    assert "127.0.0.1:8078" not in page
    assert "показаны первые" not in page  # old arbitrary card cap is gone


def test_link_start_rejects_local_file_and_accepts_https():
    launched = []
    request = _request("/apply-by-link/start")
    with mock.patch.object(app, "_launch_connector_filler", side_effect=ValueError("unsafe")):
        response = app.start_apply_by_link(request, "file:///C:/secret.txt")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    with mock.patch.object(app, "_launch_connector_filler", launched.append):
        response = app.start_apply_by_link(
            request, "https://demo.teamtailor.com/jobs/123")
    assert response.status_code == 303
    assert launched == ["https://demo.teamtailor.com/jobs/123"]
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
