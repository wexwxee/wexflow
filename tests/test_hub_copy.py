"""Главная показывает честные счётчики и законченные русские подписи."""
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


def _request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/hub",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000),
    })


def test_hub_separates_salling_and_connector_counts_and_uses_clean_copy():
    _engine, sessions = _database()
    with sessions() as session:
        session.add(Job(id="s:active", source="salling", title="Salling active", status="new"))
        session.add(Job(id="s:sent", source="salling", title="Salling sent", status="applied"))
        session.add(Job(id="tt:1", source="teamtailor", title="Teamtailor", status="new"))
        session.add(Job(id="gh:1", source="greenhouse", title="Greenhouse", status="seen"))
        session.add(Job(id="ash:1", source="ashby", title="Ashby", status="closed"))
        session.add(Job(id="link:1", source="manual_link", title="Manual", status="new"))
        session.commit()

    status = {
        "enabled": False, "running": False, "found": 0, "prepared": 0,
        "submitted_today": 0, "submitted_total": 0, "events": [], "now": 0,
        "ai_usage": {
            "connected": True, "percent_remaining": 84, "remaining": 210,
            "limit": 250, "reset_at": 1784962800,
        },
    }
    with mock.patch.object(app, "get_session", sessions), \
            mock.patch.object(app, "_seven_eleven_state", return_value={
                "stores": 9, "profile_ready": True, "name": "Ivan",
            }), \
            mock.patch.object(app.profile_store, "load_profile", return_value={"first_name": "Ivan"}), \
            mock.patch.object(app.autopilot, "get_rule", return_value={"enabled": False}), \
            mock.patch.object(app, "_autopilot_status_payload", return_value=status), \
            mock.patch.object(app, "_data_age_minutes", return_value=None), \
            mock.patch.object(app.subscription, "status", return_value={}):
        response = app.hub(_request())

    page = response.body.decode("utf-8")
    salling = page.split('hub-module salling', 1)[1].split("</section>", 1)[0]
    connectors = page.split('hub-module connectors', 1)[1].split("</section>", 1)[0]

    assert response.status_code == 200
    assert "Вакансии Salling" in salling
    assert "<b>1</b><span>активные</span>" in salling
    assert "<b>1</b><span>подано</span>" in salling
    assert "<b>2</b><span>всего</span>" in salling
    assert "Другие компании" in connectors
    assert "<b>2</b><span>активные</span>" in connectors
    assert "<b>3</b><span>источника</span>" in connectors
    assert "<b>3</b><span>всего</span>" in connectors
    assert "9 магазинов 7-Eleven выбрано" in page
    assert "магазин(ов)" not in page
    assert "Salling Jobs" not in page
    assert "Apply Studio" not in page
    assert ">active<" not in page and ">applied<" not in page and ">total<" not in page
    assert "Ресурс ИИ" in page
    assert "Осталось 210 из 250 запросов" in page
    assert "Искать новые сейчас" in page
    assert "Важное за последние 24 часа" in page


if __name__ == "__main__":
    test_hub_separates_salling_and_connector_counts_and_uses_clean_copy()
    print("OK   test_hub_separates_salling_and_connector_counts_and_uses_clean_copy")
