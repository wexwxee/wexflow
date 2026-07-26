"""Shared-feed connector ingestion must stay isolated from Salling."""
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select

import connector_sync
from connectors.base import JobItem, search_companies
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


def test_danish_city_without_country_is_normalized_to_dk():
    item = _item(country="")
    item.city = "Copenhagen"
    job = connector_sync.job_from_item(item)
    assert job.country == "DK"


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


def test_connector_geocoding_is_bounded_and_persists_coordinates():
    engine, sessions = _factory()
    with Session(engine) as session:
        for index in range(3):
            session.add(Job(
                id=f"tt:demo:{index}", source="teamtailor", title="Demo",
                country="DK", zip="2100", status="new",
            ))
        session.add(Job(
            id="salling-geo", source="salling", title="Salling",
            country="DK", zip="2100", status="new",
        ))
        session.commit()

    seen = []

    def fake_geocoder(jobs):
        seen.extend(job.id for job in jobs)
        for job in jobs:
            job.lat, job.lon = 55.7, 12.5
        return len(jobs)

    updated = connector_sync.geocode_missing(
        "teamtailor", limit=2, session_factory=sessions, geocoder=fake_geocoder)
    assert updated == 2 and len(seen) == 2
    assert all(job_id.startswith("tt:") for job_id in seen)
    with Session(engine) as session:
        geocoded = session.exec(select(Job).where(Job.lat.is_not(None))).all()
        assert len(geocoded) == 2
        assert session.get(Job, "salling-geo").lat is None


def test_update_preserves_geocode_when_feed_has_no_coordinates():
    engine, sessions = _factory()
    connector_sync.sync_items("teamtailor", [_item()], sessions)
    with Session(engine) as session:
        job = session.get(Job, "tt:demo:1")
        job.lat, job.lon = 55.6761, 12.5683
        session.add(job)
        session.commit()
    connector_sync.sync_items("teamtailor", [_item(title="Updated")], sessions)
    with Session(engine) as session:
        job = session.get(Job, "tt:demo:1")
        assert (job.lat, job.lon) == (55.6761, 12.5683)


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


def test_partial_company_failures_are_reported():
    errors = []

    def fetch(company):
        if company["slug"] == "broken":
            raise RuntimeError("HTTP 503")
        return [_item("tt:healthy:1")]

    items = search_companies(
        [{"slug": "healthy"}, {"slug": "broken"}],
        fetch,
        workers=2,
        errors=errors,
    )
    assert len(items) == 1
    assert len(errors) == 1 and "broken" in errors[0] and "503" in errors[0]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
