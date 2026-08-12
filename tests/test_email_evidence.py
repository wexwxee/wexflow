"""Письмо работодателя — доказательство, но только как проверяемый .eml."""
import io
import hashlib
import os
import sys
from datetime import timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

import applications
import email_evidence
from db import Application, ApplicationEvidence, ApplicationStatusEvent, Job, utcnow


def _eml(*, subject="Tak for din ansøgning: Kasseassistent Herlev",
         body="Vi har modtaget din ansøgning til Kasseassistent i Herlev.",
         sender="jobs@sallinggroup.com", authenticated=True,
         authentication_results=None, occurred_at=None) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "candidate@example.com"
    moment = occurred_at or utcnow()
    message["Date"] = format_datetime(moment.replace(tzinfo=timezone.utc))
    message["Subject"] = subject
    message["Message-ID"] = "<receipt-123@sallinggroup.com>"
    if authentication_results is not None:
        message["Authentication-Results"] = authentication_results
    elif authenticated:
        message["Authentication-Results"] = (
            "mx.example; dkim=pass header.i=@sallinggroup.com; "
            "spf=pass smtp.mailfrom=sallinggroup.com; dmarc=pass header.from=sallinggroup.com"
        )
    message.set_content(body)
    return message.as_bytes()


def _job(job_id="job-123"):
    return Job(
        id=job_id,
        source="salling",
        title="Kasseassistent Herlev",
        country="DK",
        status="seen",
        application_link="https://sallinggroup.com/jobs/job-123",
    )


def test_authenticated_confirmation_matches_the_selected_job():
    result = email_evidence.analyse(_eml(), _job())
    assert result["authentication"] == "unverified_header"
    assert result["stage"] == "applied"
    assert result["sender"] == "jobs@sallinggroup.com"
    assert len(result["fingerprint"]) == 64


@pytest.mark.parametrize(("text", "expected"), [
    ("Your application has been accepted for Kasseassistent Herlev.", "applied"),
    ("We are currently reviewing your application for Kasseassistent Herlev.", "reviewing"),
    ("We invite you to an interview for Kasseassistent Herlev.", "interview"),
    ("We are pleased to offer you the Kasseassistent Herlev position.", "offer"),
    ("We are pleased to confirm your employment as Kasseassistent Herlev.", "hired"),
    ("Your application has been withdrawn for Kasseassistent Herlev.", "withdrawn"),
])
def test_deterministic_stage_classifier_covers_the_application_lifecycle(text, expected):
    assert email_evidence.classify_stage(text)["stage"] == expected


def test_rejection_wins_over_an_interview_mentioned_in_the_same_message():
    text = (
        "Thank you for attending the interview. We have decided to move "
        "forward with another candidate for Kasseassistent Herlev."
    )
    assert email_evidence.classify_stage(text)["stage"] == "rejected"


@pytest.mark.parametrize(("text", "expected"), [
    ("Vi har modtaget din ansøgning til Kasseassistent Herlev.", "applied"),
    ("Din ansøgning er under behandling til Kasseassistent Herlev.", "reviewing"),
    ("Vi inviterer dig til en jobsamtale om Kasseassistent Herlev.", "interview"),
    ("Vi vil gerne tilbyde dig stillingen som Kasseassistent Herlev.", "offer"),
    ("Du er blevet ansat som Kasseassistent Herlev.", "hired"),
    ("Afslag på din ansøgning til Kasseassistent Herlev.", "rejected"),
    ("Din ansøgning er blevet trukket tilbage: Kasseassistent Herlev.", "withdrawn"),
])
def test_danish_stage_phrases_are_classified_locally(text, expected):
    assert email_evidence.classify_stage(text)["stage"] == expected


def test_late_status_email_is_valid_but_a_late_receipt_is_not():
    now = utcnow()
    job = _job()
    job.applied_at = now - timedelta(days=45)
    rejection = _eml(
        subject="Update: Kasseassistent Herlev",
        body=("After your interview, we have decided to move forward with "
              "another candidate for Kasseassistent Herlev."),
        occurred_at=now,
    )
    assert email_evidence.analyse(rejection, job)["stage"] == "rejected"

    late_receipt = _eml(occurred_at=now)
    with pytest.raises(email_evidence.EvidenceError, match="слишком далеко"):
        email_evidence.analyse(late_receipt, job)


def test_status_email_before_the_selected_application_is_rejected():
    now = utcnow()
    job = _job()
    job.applied_at = now - timedelta(days=10)
    raw = _eml(
        subject="Update: Kasseassistent Herlev",
        body="We invite you to an interview for Kasseassistent Herlev.",
        occurred_at=now - timedelta(days=13),
    )
    with pytest.raises(email_evidence.EvidenceError, match="раньше выбранной подачи"):
        email_evidence.analyse(raw, job)


def test_plain_text_or_forwarded_copy_is_not_proof():
    try:
        email_evidence.analyse(_eml(authenticated=False), _job())
    except email_evidence.EvidenceError as exc:
        assert "DMARC=pass" in str(exc)
    else:
        raise AssertionError("письмо без почтовой аутентификации было засчитано")


def test_confirmation_for_another_job_is_rejected():
    raw = _eml(subject="Application received: Warehouse worker Odense",
               body="We have received your application for Warehouse worker in Odense.")
    try:
        email_evidence.analyse(raw, _job())
    except email_evidence.EvidenceError as exc:
        assert "выбранной вакансии" in str(exc)
    else:
        raise AssertionError("чужое письмо было прикреплено к вакансии")


def test_spf_for_attacker_domain_and_dmarc_fail_is_rejected():
    raw = _eml(authentication_results=(
        "mx.example; spf=pass smtp.mailfrom=attacker.example; "
        "dkim=fail header.d=sallinggroup.com; "
        "dmarc=fail header.from=sallinggroup.com"
    ))
    with pytest.raises(email_evidence.EvidenceError, match="DMARC=pass"):
        email_evidence.analyse(raw, _job())


def test_dmarc_for_a_foreign_domain_does_not_authenticate_spoofed_from():
    raw = _eml(authentication_results=(
        "mx.example; dmarc=pass header.from=attacker.example; "
        "spf=pass smtp.mailfrom=attacker.example"
    ))
    with pytest.raises(email_evidence.EvidenceError, match="DMARC=pass"):
        email_evidence.analyse(raw, _job())


def test_import_stores_local_artifact_and_repairs_registry(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_job())
        session.commit()

    session_factory = lambda: Session(engine)
    upload = SimpleNamespace(filename="original.eml", file=io.BytesIO(_eml()))
    with (
        mock.patch.object(email_evidence, "get_session", session_factory),
        mock.patch.object(applications, "get_session", session_factory),
        mock.patch.object(email_evidence, "EMAIL_DIR", tmp_path / "email"),
    ):
        evidence = email_evidence.import_upload("job-123", upload)
        assert (tmp_path / "email" / evidence.path).is_file()
        with Session(engine) as session:
            job = session.get(Job, "job-123")
            registry = session.exec(select(Application)).one()
            stored = session.exec(select(ApplicationEvidence)).one()
        assert job.applied_at is not None and job.status == "applied"
        assert job.applied_confidence == "manual"
        assert registry.state == "submitted" and registry.confidence == "manual"
        assert stored.authentication == "unverified_header"
        assert email_evidence.valid_rows(session, "salling") == []


def test_multiple_status_emails_are_saved_with_history_and_latest_count(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    now = utcnow().replace(microsecond=0)
    job = _job()
    job.status = "applied"
    job.application_stage = "applied"
    job.applied_at = now - timedelta(days=40)
    with Session(engine) as session:
        session.add(job)
        session.commit()

    session_factory = lambda: Session(engine)
    interview = SimpleNamespace(
        filename="interview.eml",
        file=io.BytesIO(_eml(
            subject="Interview: Kasseassistent Herlev",
            body="We invite you to an interview for Kasseassistent Herlev.",
            occurred_at=now - timedelta(days=5),
        )),
    )
    offer = SimpleNamespace(
        filename="offer.eml",
        file=io.BytesIO(_eml(
            subject="Job offer: Kasseassistent Herlev",
            body="We are pleased to offer you the Kasseassistent Herlev position.",
            occurred_at=now,
        )),
    )
    with (
        mock.patch.object(email_evidence, "get_session", session_factory),
        mock.patch.object(email_evidence, "EMAIL_DIR", tmp_path / "email"),
    ):
        first = email_evidence.import_upload("job-123", interview)
        second = email_evidence.import_upload("job-123", offer)
        assert first.stage == "interview"
        assert second.stage == "offer"
        with Session(engine) as session:
            stored_job = session.get(Job, "job-123")
            history = email_evidence.history_map(session, [stored_job])
            latest = email_evidence.existing_map(session, [stored_job])
            events = session.exec(select(ApplicationStatusEvent)).all()

    key = ("salling", "job-123")
    assert stored_job.application_stage == "offer"
    assert [item["stage"] for item in history[key]] == ["offer", "interview"]
    assert latest[key]["stage"] == "offer" and latest[key]["count"] == 2
    assert [event.stage for event in events] == ["interview", "offer"]


def test_weaker_later_email_is_kept_as_evidence_but_cannot_downgrade_stage(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    now = utcnow().replace(microsecond=0)
    job = _job()
    job.status = "applied"
    job.application_stage = "applied"
    job.applied_at = now - timedelta(days=30)
    with Session(engine) as session:
        session.add(job)
        session.commit()

    session_factory = lambda: Session(engine)
    uploads = [
        SimpleNamespace(filename="interview.eml", file=io.BytesIO(_eml(
            subject="Interview: Kasseassistent Herlev",
            body="We invite you to an interview for Kasseassistent Herlev.",
            occurred_at=now - timedelta(days=2),
        ))),
        SimpleNamespace(filename="review.eml", file=io.BytesIO(_eml(
            subject="Review: Kasseassistent Herlev",
            body="We are currently reviewing your application for Kasseassistent Herlev.",
            occurred_at=now,
        ))),
    ]
    with (
        mock.patch.object(email_evidence, "get_session", session_factory),
        mock.patch.object(email_evidence, "EMAIL_DIR", tmp_path / "email"),
    ):
        for upload in uploads:
            email_evidence.import_upload("job-123", upload)

    with Session(engine) as session:
        stored_job = session.get(Job, "job-123")
        evidence = session.exec(select(ApplicationEvidence)).all()
        events = session.exec(select(ApplicationStatusEvent)).all()
    assert stored_job.application_stage == "interview"
    assert {row.stage for row in evidence} == {"interview", "reviewing"}
    assert [event.stage for event in events] == ["interview"]


def test_retry_is_idempotent_for_artifact_status_event_and_registry(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    now = utcnow().replace(microsecond=0)
    job = _job()
    job.status = "applied"
    job.application_stage = "applied"
    job.applied_at = now - timedelta(days=20)
    with Session(engine) as session:
        session.add(job)
        session.commit()

    raw = _eml(
        subject="Interview: Kasseassistent Herlev",
        body="We invite you to an interview for Kasseassistent Herlev.",
        occurred_at=now,
    )
    session_factory = lambda: Session(engine)
    with (
        mock.patch.object(email_evidence, "get_session", session_factory),
        mock.patch.object(email_evidence, "EMAIL_DIR", tmp_path / "email"),
    ):
        first = email_evidence.import_upload(
            "job-123", SimpleNamespace(filename="one.eml", file=io.BytesIO(raw))
        )
        second = email_evidence.import_upload(
            "job-123", SimpleNamespace(filename="again.eml", file=io.BytesIO(raw))
        )

    assert first.id == second.id
    with Session(engine) as session:
        assert len(session.exec(select(ApplicationEvidence)).all()) == 1
        assert len(session.exec(select(ApplicationStatusEvent)).all()) == 1
        assert len(session.exec(select(Application)).all()) == 1


def test_post_commit_refresh_failure_never_deletes_durable_email(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    job = _job()
    with Session(engine) as session:
        session.add(job)
        session.commit()

    class RefreshFails(Session):
        def refresh(self, instance, *args, **kwargs):
            raise OSError("read failed after commit")

    raw = _eml()
    session_factory = lambda: RefreshFails(engine)
    email_dir = tmp_path / "email"
    with (
        mock.patch.object(email_evidence, "get_session", session_factory),
        mock.patch.object(email_evidence, "EMAIL_DIR", email_dir),
        pytest.raises(OSError, match="after commit"),
    ):
        email_evidence.import_upload(
            "job-123", SimpleNamespace(filename="receipt.eml", file=io.BytesIO(raw))
        )

    with Session(engine) as session:
        evidence = session.exec(select(ApplicationEvidence)).one()
        assert (email_dir / evidence.path).read_bytes() == raw
        assert session.exec(select(ApplicationStatusEvent)).one().stage == "applied"


def test_forged_header_status_stays_manual_and_never_earns_trust(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    now = utcnow().replace(microsecond=0)
    job = _job()
    job.status = "applied"
    job.application_stage = "applied"
    job.applied_at = now - timedelta(days=20)
    with Session(engine) as session:
        session.add(job)
        session.commit()

    raw = _eml(
        subject="Job offer: Kasseassistent Herlev",
        body="We are pleased to offer you the Kasseassistent Herlev position.",
        occurred_at=now,
        # This text is editable by the uploader and must remain unverified.
        authentication_results=(
            "mx.example; dkim=pass header.i=@sallinggroup.com; "
            "dmarc=pass header.from=sallinggroup.com"
        ),
    )
    session_factory = lambda: Session(engine)
    with (
        mock.patch.object(email_evidence, "get_session", session_factory),
        mock.patch.object(email_evidence, "EMAIL_DIR", tmp_path / "email"),
    ):
        evidence = email_evidence.import_upload(
            "job-123", SimpleNamespace(filename="forged.eml", file=io.BytesIO(raw))
        )
        with Session(engine) as session:
            stored_job = session.get(Job, "job-123")
            trusted = email_evidence.valid_rows(session, "salling")
    assert evidence.authentication == "unverified_header"
    assert stored_job.applied_confidence == "manual"
    assert trusted == []


def test_rejected_spoof_cannot_mutate_job_registry_or_evidence(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_job())
        session.commit()

    session_factory = lambda: Session(engine)
    raw = _eml(authentication_results=(
        "mx.example; spf=pass smtp.mailfrom=attacker.example; "
        "dkim=fail; dmarc=fail header.from=sallinggroup.com"
    ))
    upload = SimpleNamespace(filename="spoof.eml", file=io.BytesIO(raw))
    with (
        mock.patch.object(email_evidence, "get_session", session_factory),
        mock.patch.object(applications, "get_session", session_factory),
        mock.patch.object(email_evidence, "EMAIL_DIR", tmp_path / "email"),
        pytest.raises(email_evidence.EvidenceError),
    ):
        email_evidence.import_upload("job-123", upload)

    with Session(engine) as session:
        job = session.get(Job, "job-123")
        assert job.applied_at is None and job.status == "seen"
        assert session.exec(select(Application)).all() == []
        assert session.exec(select(ApplicationEvidence)).all() == []
    assert not (tmp_path / "email").exists()


def test_verified_email_stops_counting_if_saved_file_is_replaced(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    email_dir = tmp_path / "email"
    email_dir.mkdir()
    artifact = email_dir / "receipt.eml"
    original = b"cryptographically verified original"
    artifact.write_bytes(original)

    with Session(engine) as session:
        session.add(ApplicationEvidence(
            source="salling",
            job_id="job-123",
            kind="email",
            path=artifact.name,
            fingerprint=hashlib.sha256(original).hexdigest(),
            authentication="dkim_verified",
        ))
        session.commit()
        with mock.patch.object(email_evidence, "EMAIL_DIR", email_dir):
            assert len(email_evidence.valid_rows(session, "salling")) == 1
            artifact.write_bytes(b"replaced or truncated")
            assert email_evidence.valid_rows(session, "salling") == []


def test_even_verified_status_email_is_not_a_submission_receipt(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    email_dir = tmp_path / "email"
    email_dir.mkdir()
    receipt = b"provider fetched receipt"
    rejection = b"provider fetched rejection"
    (email_dir / "receipt.eml").write_bytes(receipt)
    (email_dir / "rejection.eml").write_bytes(rejection)

    with Session(engine) as session:
        session.add(ApplicationEvidence(
            source="salling", job_id="receipt", kind="email",
            path="receipt.eml", fingerprint=hashlib.sha256(receipt).hexdigest(),
            authentication="provider_verified", stage="applied",
        ))
        session.add(ApplicationEvidence(
            source="salling", job_id="rejection", kind="email",
            path="rejection.eml", fingerprint=hashlib.sha256(rejection).hexdigest(),
            authentication="provider_verified", stage="rejected",
        ))
        session.commit()
        with mock.patch.object(email_evidence, "EMAIL_DIR", email_dir):
            rows = email_evidence.valid_rows(session, "salling")
    assert [row.job_id for row in rows] == ["receipt"]


if __name__ == "__main__":
    # Часть тестов здесь просит фикстуру tmp_path, а вручную её не создать:
    # прямой вызов функций падал с TypeError и останавливал сборку. Отдаём файл
    # pytest — он раздаст фикстуры сам.
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
