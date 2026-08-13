"""Пересланное боту письмо двигает этап нужного отклика — и только нужного.

Работодатели в Дании чаще отвечают письмом, чем меняют статус в кабинете, а
почтовый ящик WexFlow не читает. Пересылка закрывает эту дыру, но угаданный не
тот отклик хуже, чем отсутствие записи: поэтому при сомнении мы отказываемся.
"""
import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import application_tracker
import email_forward
from db import ApplicationStatusEvent, Job

APPLIED = dt.datetime(2026, 8, 1, 9, 0)


@pytest.fixture()
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Job(id="lidl:728695", source="lidl", brand="Lidl",
                        title="Butiksassistent - 37 timer - Herlev",
                        requisition_id="728695", applied_at=APPLIED,
                        application_stage="applied", status="applied"))
        session.add(Job(id="s-1", source="salling", brand="Netto",
                        title="Salgsassistent til Frugt og Grønt - Valby",
                        applied_at=APPLIED, application_stage="applied",
                        status="applied"))
        session.add(Job(id="s-2", source="salling", brand="Føtex",
                        title="Kasseassistent - Herlev", status="new"))
        session.commit()

    def factory():
        return Session(engine)

    with mock.patch.object(email_forward, "get_session", factory), \
            mock.patch("db.get_session", factory), \
            mock.patch.object(application_tracker, "get_session", factory):
        yield factory


def _stage_of(sessions, job_id: str) -> str:
    with sessions() as session:
        return application_tracker.current_stage(session.get(Job, job_id))


def test_interview_letter_moves_exactly_the_right_application(sessions):
    letter = ("Kære Ivan, tak for din ansøgning til Butiksassistent - 37 timer "
              "- Herlev (job 728695). Vi vil gerne invitere dig til en samtale "
              "i næste uge. Med venlig hilsen, Lidl Danmark")
    result = email_forward.import_text(letter)

    assert result["status"] == "saved"
    assert result["job_id"] == "lidl:728695"
    assert result["stage"] == "interview"
    assert _stage_of(sessions, "lidl:728695") == "interview"
    assert _stage_of(sessions, "s-1") == "applied", "тронут чужой отклик"


def test_the_letter_lands_in_history_as_a_manual_witness(sessions):
    email_forward.import_text(
        "Vedr. Butiksassistent - 37 timer - Herlev, 728695: vi inviterer dig til samtale."
    )
    with sessions() as session:
        event = session.exec(select(ApplicationStatusEvent)).one()
    assert event.origin == "email", "пересланный текст выдан за официальный источник"
    assert event.stage == "interview"
    assert event.event_key.startswith("forward:")


def test_the_same_letter_twice_changes_nothing(sessions):
    letter = "Butiksassistent - 37 timer - Herlev 728695: invitation til jobsamtale."
    email_forward.import_text(letter)
    again = email_forward.import_text(letter)
    assert again["changed"] is False
    with sessions() as session:
        assert len(session.exec(select(ApplicationStatusEvent)).all()) == 1


def test_a_letter_about_an_unknown_vacancy_changes_nothing(sessions):
    result = email_forward.import_text(
        "Kære ansøger, vi inviterer dig til samtale om stillingen som "
        "Lagerchef i Aarhus. Venlig hilsen."
    )
    assert result["status"] == "no_match"
    assert _stage_of(sessions, "lidl:728695") == "applied"


def test_a_vacancy_that_was_never_applied_to_is_not_touched(sessions):
    result = email_forward.import_text(
        "Kasseassistent - Herlev: desværre må vi meddele, at vi har valgt en anden kandidat."
    )
    assert result["status"] in ("no_match", "not_applied")
    assert _stage_of(sessions, "s-2") == ""


def test_ambiguous_letter_refuses_to_guess(sessions):
    """Два отклика в Herlev: приписать письмо наугад — хуже, чем не записать."""
    with sessions() as session:
        job = session.get(Job, "s-2")
        job.applied_at = APPLIED
        job.application_stage = "applied"
        job.status = "applied"
        job.title = "Butiksassistent - 37 timer - Herlev"
        session.add(job)
        session.commit()

    result = email_forward.import_text(
        "Butiksassistent - 37 timer - Herlev: vi inviterer dig til jobsamtale."
    )
    assert result["status"] == "many"
    assert len(result["matches"]) == 2
    assert _stage_of(sessions, "lidl:728695") == "applied"
    assert "не буду" in email_forward.reply_text(result)


def test_a_chosen_application_resolves_the_ambiguity(sessions):
    with sessions() as session:
        job = session.get(Job, "s-2")
        job.applied_at = APPLIED
        job.title = "Butiksassistent - 37 timer - Herlev"
        job.application_stage = "applied"
        session.add(job)
        session.commit()

    result = email_forward.import_text(
        "Butiksassistent - 37 timer - Herlev: vi inviterer dig til jobsamtale.",
        job_id="lidl:728695",
    )
    assert result["status"] == "saved"
    assert _stage_of(sessions, "lidl:728695") == "interview"
    assert _stage_of(sessions, "s-2") == "applied"


def test_a_letter_without_a_decision_moves_nothing(sessions):
    result = email_forward.import_text(
        "Butiksassistent - 37 timer - Herlev 728695. Nyhedsbrev: se vores nye "
        "tilbud i ugens avis og besøg butikken."
    )
    assert result["status"] == "unknown_stage"
    assert _stage_of(sessions, "lidl:728695") == "applied"


def test_the_most_common_danish_rejection_is_recognised():
    """«Мы выбрали другого кандидата» — так отказывают чаще всего."""
    import email_evidence

    for letter in (
        "Vi har desværre valgt en anden kandidat til stillingen.",
        "Vi har fundet en anden kandidat.",
        "Stillingen er desværre nu besat.",
    ):
        assert email_evidence.classify_stage(letter)["stage"] == "rejected", letter


def test_too_short_text_is_refused(sessions):
    assert email_forward.import_text("ок")["status"] == "short"
    assert email_forward.import_text("")["status"] == "short"


def test_rejection_is_recognised_and_worded_plainly(sessions):
    result = email_forward.import_text(
        "Vedr. Butiksassistent - 37 timer - Herlev, 728695. Desværre må vi "
        "meddele, at vi har valgt en anden kandidat til stillingen."
    )
    assert result["status"] == "saved" and result["stage"] == "rejected"
    reply = email_forward.reply_text(result)
    assert "Отказ" in reply
    assert "ручное свидетельство" in reply, "ответ выдаёт письмо за доказательство подачи"
