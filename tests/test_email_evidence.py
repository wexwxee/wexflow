"""Письмо работодателя — доказательство, но только как проверяемый .eml."""
import io
import os
import sys
from datetime import timezone
from email.message import EmailMessage
from email.utils import format_datetime
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select

import applications
import email_evidence
from db import Application, ApplicationEvidence, Job, utcnow


def _eml(*, subject="Tak for din ansøgning: Kasseassistent Herlev",
         body="Vi har modtaget din ansøgning til Kasseassistent i Herlev.",
         sender="jobs@sallinggroup.com", authenticated=True) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "candidate@example.com"
    message["Date"] = format_datetime(utcnow().replace(tzinfo=timezone.utc))
    message["Subject"] = subject
    message["Message-ID"] = "<receipt-123@sallinggroup.com>"
    if authenticated:
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
    assert result["authentication"] == "dmarc"
    assert result["sender"] == "jobs@sallinggroup.com"
    assert len(result["fingerprint"]) == 64


def test_plain_text_or_forwarded_copy_is_not_proof():
    try:
        email_evidence.analyse(_eml(authenticated=False), _job())
    except email_evidence.EvidenceError as exc:
        assert "SPF/DKIM/DMARC" in str(exc)
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
        assert job.applied_confidence == "email"
        assert registry.state == "submitted" and registry.confidence == "email"
        assert stored.authentication == "dmarc"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print("ok")
