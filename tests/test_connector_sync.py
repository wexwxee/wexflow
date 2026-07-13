"""Shared-feed connector ingestion must stay isolated from Salling."""
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select

import connector_sync
from connectors.base import JobItem
from db import Job


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def _item(job_id="tt:demo:1", title="Butiksmedarbejder", country="DK"):
    return JobItem(
        source="teamtailor", id=job_id, title=title, company="Demo ApS",
        url="https://demo.teamtailor.com/jobs/1", city="København", country=country,
    )


def test_ingest_filters_non_danish_and_sets_source():
    engine, sessions = _factory()
    report = connector_sync.sync_items(
        "teamtailor", [_item(), _item("tt:demo:se", "Stockholm", "SE")], sessions)
    assert report["hits"] == 1 and report["created"] == 1
    with Session(engine) as session:
        rows = session.exec(select(Job)).all()
        assert len(rows) == 1
        assert rows[0].source == "teamtailor"
        assert rows[0].country == "DK"


def test_connector_closure_never_touches_salling():
    engine, sessions = _factory()
    with Session(engine) as session:
        session.add(Job(id="salling-1", source="salling", title="Salling", status="new"))
        session.add(Job(
            id="tt:demo:old", source="teamtailor", title="Old", status="new",
            last_seen=connector_sync.utcnow() - dt.timedelta(days=3),
        ))
        session.commit()
    report = connector_sync.sync_items(
        "teamtailor", [_item("tt:demo:current", "Current")], sessions)
    assert report["closed"] == 1
    with Session(engine) as session:
        assert session.get(Job, "salling-1").status == "new"
        assert session.get(Job, "tt:demo:old").status == "closed"


def test_one_missed_snapshot_does_not_close_fresh_job():
    engine, sessions = _factory()
    connector_sync.sync_items("teamtailor", [_item()], sessions)
    report = connector_sync.sync_items("teamtailor", [], sessions)
    assert report["closed"] == 0
    with Session(engine) as session:
        assert session.get(Job, "tt:demo:1").status == "new"


def test_failed_company_scope_does_not_close_its_stale_jobs():
    engine, sessions = _factory()
    old = connector_sync.utcnow() - dt.timedelta(days=3)
    with Session(engine) as session:
        session.add(Job(
            id="tt:failed:old", source="teamtailor", brand="Failed ApS",
            title="Old", status="new", last_seen=old,
        ))
        session.add(Job(
            id="tt:healthy:old", source="teamtailor", brand="Healthy ApS",
            title="Old", status="new", last_seen=old,
        ))
        session.commit()
    current = _item("tt:healthy:new", "New role")
    current.company = "Healthy ApS"
    report = connector_sync.sync_items("teamtailor", [current], sessions)
    assert report["closed"] == 1
    with Session(engine) as session:
        assert session.get(Job, "tt:failed:old").status == "new"
        assert session.get(Job, "tt:healthy:old").status == "closed"


def test_sync_rejects_item_from_another_source():
    engine, sessions = _factory()
    item = _item()
    item.source = "greenhouse"
    report = connector_sync.sync_items("teamtailor", [item], sessions)
    assert report["hits"] == 0
    with Session(engine) as session:
        assert session.exec(select(Job)).all() == []


def test_update_preserves_application_state():
    engine, sessions = _factory()
    connector_sync.sync_items("teamtailor", [_item()], sessions)
    with Session(engine) as session:
        job = session.get(Job, "tt:demo:1")
        job.status = "applied"
        job.applied_at = connector_sync.utcnow()
        session.add(job)
        session.commit()
    connector_sync.sync_items("teamtailor", [_item(title="Updated title")], sessions)
    with Session(engine) as session:
        job = session.get(Job, "tt:demo:1")
        assert job.title == "Updated title"
        assert job.status == "applied" and job.applied_at is not None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
