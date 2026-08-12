import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import application_tracker
from db import ApplicationStatusEvent, Job


def _database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine, lambda: Session(engine)


def test_stage_is_separate_from_closed_listing_and_history_is_append_only():
    _engine, sessions = _database()
    moment = dt.datetime(2026, 8, 12, 8, 0)
    with sessions() as session:
        job = Job(
            id="closed-interview", source="lidl", title="Butiksassistent",
            status="closed", applied_at=moment - dt.timedelta(days=4),
        )
        session.add(job)
        session.commit()
        result = application_tracker.record_status_in_session(
            session, job, "interview", source="lidl_portal",
            occurred_at=moment, raw_label="Inviteret til samtale",
            event_key="lidl:closed-interview:interview",
        )
        session.commit()

    assert result["changed"] is True
    with sessions() as session:
        job = session.get(Job, "closed-interview")
        event = session.exec(select(ApplicationStatusEvent)).one()
        assert job.status == "closed"
        assert job.application_stage == "interview"
        assert application_tracker.view(job, now=moment)["label"] == "Собеседование"
        assert event.previous_stage == "applied"
        assert event.raw_label == "Inviteret til samtale"
        assert event.notification_required is True


def test_portal_same_stage_upgrades_manual_provenance_and_deduplicates_event():
    _engine, sessions = _database()
    moment = dt.datetime(2026, 8, 12, 9, 0)
    with sessions() as session:
        job = Job(
            id="manual-then-portal", source="salling", status="applied",
            application_stage="applied", applied_at=moment - dt.timedelta(days=1),
            applied_confidence="manual", application_status_source="manual",
        )
        session.add(job)
        session.commit()
        first = application_tracker.record_status_in_session(
            session, job, "applied", source="salling_portal", occurred_at=moment,
            raw_label="Application received", event_key="salling:req-1:applied",
        )
        session.commit()
        second = application_tracker.record_status_in_session(
            session, job, "applied", source="salling_portal", occurred_at=moment,
            raw_label="Application received", event_key="salling:req-1:applied",
        )
        session.commit()

    assert first["changed"] is False and second["changed"] is False
    with sessions() as session:
        job = session.get(Job, "manual-then-portal")
        events = session.exec(select(ApplicationStatusEvent)).all()
        assert job.applied_confidence == "portal"
        assert job.application_status_source == "salling_portal"
        assert len(events) == 1
        assert events[0].notification_required is True


def test_existing_portal_observation_reasserts_same_stage_after_manual_round_trip():
    _engine, sessions = _database()
    first = dt.datetime(2026, 8, 12, 9, 0)
    key = "portal:salling:same-stage:applied:received"
    with sessions() as session:
        job = Job(
            id="same-stage", source="salling", status="applied",
            application_stage="applied", applied_at=first,
            applied_confidence="manual", application_status_source="manual",
        )
        session.add(job)
        session.commit()
        application_tracker.record_status_in_session(
            session, job, "applied", source="salling_portal",
            occurred_at=first, event_key=key,
        )
        application_tracker.record_status_in_session(
            session, job, "interview", source="manual",
            occurred_at=first + dt.timedelta(hours=1),
        )
        application_tracker.record_status_in_session(
            session, job, "applied", source="manual",
            occurred_at=first + dt.timedelta(hours=2),
        )
        session.commit()
        reasserted = application_tracker.record_status_in_session(
            session, job, "applied", source="salling_portal",
            occurred_at=first + dt.timedelta(hours=3), event_key=key,
        )
        session.commit()

    assert reasserted["accepted"] is True
    assert reasserted["changed"] is False
    with sessions() as session:
        job = session.get(Job, "same-stage")
        events = session.exec(select(ApplicationStatusEvent).order_by(
            ApplicationStatusEvent.id
        )).all()
        assert application_tracker.current_stage(job) == "applied"
        assert job.application_status_source == "salling_portal"
        assert job.applied_confidence == "portal"
        assert len(events) == 4
        assert events[-1].event_key.startswith(f"{key}:reassert:")
        assert events[-1].notification_required is True


def test_repeated_manual_choice_of_the_same_stage_adds_no_duplicate_row():
    _engine, sessions = _database()
    moment = dt.datetime(2026, 8, 12, 9, 0)
    with sessions() as session:
        job = Job(id="manual-twice", source="lidl", status="new")
        session.add(job)
        session.commit()
        application_tracker.record_status_in_session(
            session, job, "interview", source="manual", occurred_at=moment,
        )
        repeat = application_tracker.record_status_in_session(
            session, job, "interview", source="manual",
            occurred_at=moment + dt.timedelta(minutes=5),
        )
        session.commit()

    assert repeat["accepted"] is True and repeat["changed"] is False
    with sessions() as session:
        job = session.get(Job, "manual-twice")
        events = session.exec(select(ApplicationStatusEvent)).all()
        assert application_tracker.current_stage(job) == "interview"
        assert len(events) == 1


def test_replayed_old_event_key_cannot_mutate_a_later_current_stage():
    _engine, sessions = _database()
    first_at = dt.datetime(2026, 6, 1, 9, 0)
    later_at = dt.datetime(2026, 8, 1, 9, 0)
    with sessions() as session:
        job = Job(id="replay", source="lidl", status="applied")
        session.add(job)
        session.commit()
        application_tracker.record_status_in_session(
            session, job, "applied", source="lidl_portal",
            occurred_at=first_at, event_key="portal:lidl:replay:applied:x",
        )
        application_tracker.record_status_in_session(
            session, job, "no_response", source="manual",
            occurred_at=later_at,
        )
        session.commit()
        replay = application_tracker.record_status_in_session(
            session, job, "applied", source="lidl_portal",
            occurred_at=later_at + dt.timedelta(days=1),
            event_key="portal:lidl:replay:applied:x",
        )
        session.commit()

    assert replay["changed"] is False
    with sessions() as session:
        job = session.get(Job, "replay")
        events = session.exec(select(ApplicationStatusEvent)).all()
        assert application_tracker.current_stage(job) == "no_response"
        assert len(events) == 2


def test_live_portal_can_reassert_existing_stage_after_weaker_email_terminal():
    _engine, sessions = _database()
    first = dt.datetime(2026, 8, 10, 9, 0)
    with sessions() as session:
        job = Job(id="reassert", source="lidl", status="applied")
        session.add(job)
        session.commit()
        application_tracker.record_status_in_session(
            session, job, "interview", source="lidl_portal", occurred_at=first,
            event_key="portal:lidl:reassert:interview:raw",
        )
        application_tracker.record_status_in_session(
            session, job, "rejected", source="email",
            occurred_at=first + dt.timedelta(hours=1), event_key="email:rejected",
        )
        session.commit()
        corrected = application_tracker.record_status_in_session(
            session, job, "interview", source="lidl_portal",
            occurred_at=first + dt.timedelta(hours=2),
            event_key="portal:lidl:reassert:interview:raw",
        )
        session.commit()

    assert corrected["changed"] is True
    with sessions() as session:
        job = session.get(Job, "reassert")
        events = session.exec(select(ApplicationStatusEvent).order_by(
            ApplicationStatusEvent.id
        )).all()
        assert application_tracker.current_stage(job) == "interview"
        assert job.application_status_source == "lidl_portal"
        assert [event.stage for event in events] == [
            "interview", "rejected", "interview",
        ]
        assert events[-1].event_key.startswith(
            "portal:lidl:reassert:interview:raw:reassert:"
        )


def test_stale_positive_stage_cannot_downgrade_but_explicit_outcome_can_follow_offer():
    _engine, sessions = _database()
    now = dt.datetime(2026, 8, 12, 10, 0)
    with sessions() as session:
        job = Job(
            id="offer-later", source="lidl", status="offer", application_stage="offer",
            applied_at=now - dt.timedelta(days=10), application_status_updated_at=now,
            application_status_source="lidl_portal",
        )
        session.add(job)
        session.commit()
        stale = application_tracker.record_status_in_session(
            session, job, "reviewing", source="email",
            occurred_at=now - dt.timedelta(days=2), event_key="mail-old",
        )
        refused = application_tracker.record_status_in_session(
            session, job, "rejected", source="lidl_portal",
            occurred_at=now + dt.timedelta(hours=1), event_key="portal-refused",
        )
        session.commit()

    assert stale["accepted"] is False
    assert refused["accepted"] is True
    with sessions() as session:
        job = session.get(Job, "offer-later")
        assert job.application_stage == "rejected"
        assert session.exec(select(ApplicationStatusEvent)).one().stage == "rejected"


def test_no_response_is_replaced_by_a_real_employer_response():
    job = Job(
        id="late-reply", status="no_response", application_stage="no_response",
        applied_at=dt.datetime(2026, 5, 1),
        application_status_updated_at=dt.datetime(2026, 7, 1),
        application_status_source="automatic",
    )
    assert application_tracker.set_status(
        job, "interview", source="email", now=dt.datetime(2026, 6, 15)
    ) is True
    assert application_tracker.current_stage(job) == "interview"


def test_repeated_submission_receipt_does_not_reset_no_response():
    now = dt.datetime(2026, 8, 12, 9, 0)
    job = Job(
        id="quiet-receipt", status="no_response", application_stage="no_response",
        applied_at=now - dt.timedelta(days=80),
        application_status_updated_at=now, application_status_source="automatic",
    )
    assert application_tracker.set_status(
        job, "applied", source="email", now=now - dt.timedelta(days=80)
    ) is False
    assert application_tracker.current_stage(job) == "no_response"


def test_official_portal_can_correct_a_terminal_stage_from_uploaded_email():
    old = dt.datetime(2026, 8, 10, 9, 0)
    job = Job(
        id="email-terminal", source="lidl", status="rejected",
        application_stage="rejected", applied_at=old - dt.timedelta(days=10),
        application_status_updated_at=old, application_status_source="email",
    )
    assert application_tracker.set_status(
        job, "interview", source="lidl_portal", now=old + dt.timedelta(days=1)
    ) is True
    assert application_tracker.current_stage(job) == "interview"
    assert job.application_status_source == "lidl_portal"


def test_weaker_or_stale_channel_cannot_override_official_terminal_stage():
    now = dt.datetime(2026, 8, 12, 9, 0)
    job = Job(
        id="official-terminal", source="salling", status="rejected",
        application_stage="rejected", applied_at=now - dt.timedelta(days=10),
        application_status_updated_at=now, application_status_source="salling_portal",
    )
    assert application_tracker.set_status(
        job, "offer", source="email", now=now + dt.timedelta(hours=1)
    ) is False
    assert application_tracker.set_status(
        job, "hired", source="salling_portal", now=now - dt.timedelta(hours=1)
    ) is False
    assert application_tracker.current_stage(job) == "rejected"


def test_failed_notification_remains_in_database_outbox_for_retry():
    _engine, sessions = _database()
    now = dt.datetime(2026, 8, 12, 11, 0)
    with sessions() as session:
        job = Job(id="notify", source="lidl", title="Job", status="applied")
        session.add(job)
        session.commit()
        application_tracker.record_status_in_session(
            session, job, "interview", source="lidl_portal", occurred_at=now,
            event_key="lidl:notify:interview",
        )
        session.commit()

    with mock.patch.object(application_tracker, "get_session", sessions), \
            mock.patch.object(application_tracker, "notify_status_changes", side_effect=[False, True]):
        assert application_tracker.flush_pending_notifications(
            origin="lidl_portal", source_name="Lidl"
        ) is False
        assert application_tracker.flush_pending_notifications(
            origin="lidl_portal", source_name="Lidl"
        ) is True

    with sessions() as session:
        event = session.exec(select(ApplicationStatusEvent)).one()
        assert event.notification_attempts == 2
        assert event.notified_at is not None
        assert event.notification_error == ""


def test_history_map_is_newest_first_and_scoped_by_source():
    _engine, sessions = _database()
    with sessions() as session:
        job = Job(id="same", source="lidl", status="applied")
        other = Job(id="same-other", source="salling", status="applied")
        session.add(job)
        session.add(other)
        session.commit()
        application_tracker.record_status_in_session(
            session, job, "applied", source="manual",
            occurred_at=dt.datetime(2026, 8, 10), event_key="first",
        )
        application_tracker.record_status_in_session(
            session, job, "reviewing", source="lidl_portal",
            occurred_at=dt.datetime(2026, 8, 11), event_key="second",
        )
        session.commit()
        history = application_tracker.history_map(session, [job, other])

    assert [item["stage"] for item in history[("lidl", "same")]] == [
        "reviewing", "applied",
    ]
    assert history[("salling", "same-other")] == []
