"""Shared-feed connector ingestion must stay isolated from Salling."""
import os
import sys
import datetime as dt
from unittest import mock

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
    assert report["raw_hits"] == 2
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
            country="DK", last_seen=connector_sync.utcnow() - dt.timedelta(days=3),
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
            title="Old", country="DK", status="new", last_seen=old,
        ))
        session.add(Job(
            id="tt:healthy:old", source="teamtailor", brand="Healthy ApS",
            title="Old", country="DK", status="new", last_seen=old,
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
    assert report["raw_hits"] == 0 and report["hits"] == 0
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


def test_update_preserves_every_local_owned_field():
    engine, sessions = _factory()
    connector_sync.sync_items("teamtailor", [_item()], sessions)
    marker = connector_sync.utcnow() - dt.timedelta(hours=2)
    with Session(engine) as session:
        job = session.get(Job, "tt:demo:1")
        first_seen = job.first_seen
        job.description_ru = "Локальный перевод"
        job.description_ru_engine = "DeepL"
        job.fit = "ok"
        job.fit_reason = "Локальный вердикт"
        job.fit_engine = "rules"
        job.fit_hash = "local-hash"
        job.fit_at = marker
        job.status = "offer"
        job.applied_at = marker
        job.applied_confidence = "portal"
        job.application_status_updated_at = marker
        job.application_status_source = "email"
        session.add(job)
        session.commit()

    connector_sync.sync_items("teamtailor", [_item(title="Updated title")], sessions)
    with Session(engine) as session:
        job = session.get(Job, "tt:demo:1")
        assert job.title == "Updated title"
        assert job.first_seen == first_seen
        assert (job.description_ru, job.description_ru_engine) == (
            "Локальный перевод", "DeepL")
        assert (job.fit, job.fit_reason, job.fit_engine, job.fit_hash, job.fit_at) == (
            "ok", "Локальный вердикт", "rules", "local-hash", marker)
        assert (job.status, job.applied_at, job.applied_confidence) == (
            "offer", marker, "portal")
        assert (job.application_status_updated_at, job.application_status_source) == (
            marker, "email")


def test_hidden_job_stays_hidden_when_missing_then_reappears():
    engine, sessions = _factory()
    old = connector_sync.utcnow() - dt.timedelta(days=3)
    with Session(engine) as session:
        session.add(Job(
            id="tt:demo:hidden", source="teamtailor", brand="Demo ApS",
            title="Hidden", country="DK", status="hidden", last_seen=old,
        ))
        session.commit()

    report = connector_sync.sync_items(
        "teamtailor", [_item("tt:demo:current", "Current")], sessions)
    assert report["closed"] == 0
    with Session(engine) as session:
        assert session.get(Job, "tt:demo:hidden").status == "hidden"

    connector_sync.sync_items(
        "teamtailor", [_item("tt:demo:hidden", "Hidden is back")], sessions)
    with Session(engine) as session:
        job = session.get(Job, "tt:demo:hidden")
        assert job.title == "Hidden is back"
        assert job.status == "hidden"


def test_stale_close_respects_current_country_selection():
    engine, sessions = _factory()
    old = connector_sync.utcnow() - dt.timedelta(days=3)
    with Session(engine) as session:
        session.add(Job(
            id="tt:demo:dk-old", source="teamtailor", brand="Demo ApS",
            title="Old DK", country="DK", status="new", last_seen=old,
        ))
        session.add(Job(
            id="tt:demo:se-old", source="teamtailor", brand="Demo ApS",
            title="Old SE", country="SE", status="new", last_seen=old,
        ))
        session.commit()

    with mock.patch.object(connector_sync.feed, "countries", return_value=["DK"]):
        report = connector_sync.sync_items(
            "teamtailor", [_item("tt:demo:current", "Current DK")], sessions)

    assert report["closed"] == 1
    with Session(engine) as session:
        assert session.get(Job, "tt:demo:dk-old").status == "closed"
        # Changing the feed from DK+SE to DK is presentation state, not proof
        # that the Swedish vacancy disappeared upstream.
        assert session.get(Job, "tt:demo:se-old").status == "new"


def test_filtered_raw_hit_is_not_mistaken_for_disappearance():
    engine, sessions = _factory()
    old = connector_sync.utcnow() - dt.timedelta(days=3)
    with Session(engine) as session:
        session.add(Job(
            id="tt:demo:moved", source="teamtailor", brand="Demo ApS",
            title="Stored as DK", country="DK", status="new", last_seen=old,
        ))
        session.commit()

    moved = _item("tt:demo:moved", "Now outside selection", "SE")
    with mock.patch.object(connector_sync.feed, "countries", return_value=["DK"]):
        report = connector_sync.sync_items("teamtailor", [moved], sessions)

    assert report["raw_hits"] == 1 and report["hits"] == 0
    assert report["closed"] == 0
    with Session(engine) as session:
        assert session.get(Job, "tt:demo:moved").status == "new"


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


class _FakeConnector:
    """Каталог из N компаний, из которых `failures` не ответили."""

    def __init__(self, total, failures):
        self._companies = [{"slug": f"c{i}"} for i in range(total)]
        self.last_errors = [f"c{i}: HTTP 404" for i in range(failures)]

    def companies(self):
        return self._companies

    def search(self):
        return []


def _sync_with_fake(total, failures):
    conn = _FakeConnector(total, failures)
    empty = {"source": "teamtailor", "hits": 0, "created": 0, "updated": 0, "closed": 0}
    with mock.patch.object(connector_sync, "init_db"), \
            mock.patch.object(connector_sync, "sync_items", return_value=dict(empty)), \
            mock.patch.object(connector_sync, "geocode_missing", return_value=0), \
            mock.patch.object(connector_sync.connectors, "get", return_value=conn):
        return connector_sync.sync(["teamtailor"])


def test_source_health_receives_raw_hits_before_country_filter():
    conn = _FakeConnector(total=2, failures=0)
    conn.search = lambda: [_item(), _item("tt:demo:se", "Stockholm", "SE")]
    filtered = {
        "source": "teamtailor", "raw_hits": 2, "hits": 1,
        "created": 1, "updated": 0, "closed": 0,
    }
    with mock.patch.object(connector_sync, "init_db"), \
            mock.patch.object(connector_sync, "sync_items", return_value=filtered), \
            mock.patch.object(connector_sync, "geocode_missing", return_value=0), \
            mock.patch.object(connector_sync.connectors, "get", return_value=conn), \
            mock.patch.object(connector_sync, "_note_health") as note_health:
        report = connector_sync.sync(["teamtailor"])

    note_health.assert_called_once_with("teamtailor", hits=2, error="")
    assert report["raw_hits"] == 2 and report["hits"] == 1


def test_single_moved_company_is_a_note_not_a_source_failure():
    report = _sync_with_fake(total=50, failures=1)
    assert report["errors"] == []
    assert len(report["warnings"]) == 1 and "(1 из 50)" in report["warnings"][0]


def test_most_of_the_catalog_silent_is_a_real_source_failure():
    report = _sync_with_fake(total=5, failures=3)
    assert report["warnings"] == []
    assert len(report["errors"]) == 1 and "(3 из 5)" in report["errors"][0]


def test_unknown_catalog_size_is_treated_as_failure():
    report = _sync_with_fake(total=0, failures=0)
    assert report["errors"] == [] and report["warnings"] == []
    conn = _FakeConnector(0, 0)
    conn.last_errors = ["mystery: HTTP 500"]
    empty = {"source": "teamtailor", "hits": 0, "created": 0, "updated": 0, "closed": 0}
    with mock.patch.object(connector_sync, "init_db"), \
            mock.patch.object(connector_sync, "sync_items", return_value=dict(empty)), \
            mock.patch.object(connector_sync, "geocode_missing", return_value=0), \
            mock.patch.object(connector_sync.connectors, "get", return_value=conn):
        report = connector_sync.sync(["teamtailor"])
    assert report["warnings"] == [] and len(report["errors"]) == 1


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
