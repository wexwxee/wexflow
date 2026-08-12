"""Письмо работодателя — доказательство, но только как проверяемый .eml."""
import io
import hashlib
import os
import sys
from datetime import timezone
from email.message import EmailMessage
from email.utils import format_datetime
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

import applications
import email_evidence
from db import Application, ApplicationEvidence, Job, utcnow


def _eml(*, subject="Tak for din ansøgning: Kasseassistent Herlev",
         body="Vi har modtaget din ansøgning til Kasseassistent i Herlev.",
         sender="jobs@sallinggroup.com", authenticated=True,
         authentication_results=None) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "candidate@example.com"
    message["Date"] = format_datetime(utcnow().replace(tzinfo=timezone.utc))
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
    assert result["sender"] == "jobs@sallinggroup.com"
    assert len(result["fingerprint"]) == 64


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


if __name__ == "__main__":
    # Часть тестов здесь просит фикстуру tmp_path, а вручную её не создать:
    # прямой вызов функций падал с TypeError и останавливал сборку. Отдаём файл
    # pytest — он раздаст фикстуры сам.
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
