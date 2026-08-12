"""Salling refresh must not overwrite WexFlow-owned job state."""
import datetime as dt
from unittest import mock

from sqlmodel import SQLModel, Session, create_engine

import scraper
from db import Job


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def _hit(job_id="salling-local", title="Updated title"):
    return {
        "objectID": job_id,
        "title": title,
        "brand": "Netto",
        "country": "DK",
        "description": "Updated source description",
        "applicationLink": "https://example.test/jobs/updated",
    }


def _sync_with(hits, sessions):
    with mock.patch.object(scraper, "init_db"), \
            mock.patch.object(scraper, "fetch_all_hits", return_value=hits), \
            mock.patch.object(scraper, "get_session", sessions):
        return scraper.sync()


def test_refresh_preserves_every_local_owned_field():
    engine, sessions = _factory()
    marker = scraper.utcnow() - dt.timedelta(hours=2)
    first_seen = scraper.utcnow() - dt.timedelta(days=10)
    with Session(engine) as session:
        session.add(Job(
            id="salling-local", source="salling", title="Old title",
            country="DK", description="Old source description",
            description_ru="Локальный перевод", description_ru_engine="DeepL",
            status="offer", fit="ok", fit_reason="Локальный вердикт",
            fit_engine="rules", fit_hash="local-hash", fit_at=marker,
            first_seen=first_seen, last_seen=marker,
            applied_at=marker, applied_confidence="portal",
            application_status_updated_at=marker,
            application_status_source="email", lat=55.67, lon=12.56,
        ))
        session.commit()

    report = _sync_with([_hit()], sessions)
    assert report["hits"] == 1
    with Session(engine) as session:
        job = session.get(Job, "salling-local")
        assert job.title == "Updated title"
        assert job.description == "Updated source description"
        assert job.first_seen == first_seen
        assert (job.description_ru, job.description_ru_engine) == (
            "Локальный перевод", "DeepL")
        assert (job.fit, job.fit_reason, job.fit_engine, job.fit_hash, job.fit_at) == (
            "ok", "Локальный вердикт", "rules", "local-hash", marker)
        assert (job.status, job.applied_at, job.applied_confidence) == (
            "offer", marker, "portal")
        assert (job.application_status_updated_at, job.application_status_source) == (
            marker, "email")
        assert (job.lat, job.lon) == (55.67, 12.56)


def test_hidden_job_stays_hidden_when_missing_then_reappears():
    engine, sessions = _factory()
    with Session(engine) as session:
        session.add(Job(
            id="salling-hidden", source="salling", title="Hidden",
            country="DK", status="hidden", lat=55.67, lon=12.56,
        ))
        session.commit()

    missing = _sync_with([], sessions)
    assert missing["closed"] == 0
    with Session(engine) as session:
        assert session.get(Job, "salling-hidden").status == "hidden"

    _sync_with([_hit("salling-hidden", "Hidden is back")], sessions)
    with Session(engine) as session:
        job = session.get(Job, "salling-hidden")
        assert job.title == "Hidden is back"
        assert job.status == "hidden"
