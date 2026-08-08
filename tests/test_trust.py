"""Доверие площадкам: первая подача — с подтверждением и квитанцией (шаг 3).

Правила, которые тут закреплены:
- доверие считается ПО ПЛОЩАДКАМ, а не «вообще»: успех Salling не открывает
  автомат непроверенному коннектору;
- доказательство = квитанция (или кабинет работодателя) И сохранённый снимок;
  «вероятно подано» и ручная отметка доверия не дают;
- «спрашивать всегда» главнее автомата и включён по умолчанию;
- первая подача на площадке идёт ОДНА, даже если человек выбрал пачку;
- автомат нельзя включить авансом, а молчание автоотправки объясняется словами.

PATH настроек подменяется на временный файл — реальный settings.json НЕ трогаем.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine

import settings_store
import trust
from db import Application, Job, utcnow


def _with_temp_settings(body):
    orig = settings_store.PATH
    settings_store.PATH = Path(tempfile.mkdtemp()) / "settings.json"
    try:
        body()
    finally:
        settings_store.PATH = orig


def _db(rows):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for row in rows:
            session.add(row)
        session.commit()
    return lambda: Session(engine)


def _applied(job_id, source="salling", confidence="receipt", title="Kasseassistent"):
    return Job(id=job_id, source=source, title=title, country="DK", status="applied",
               applied_at=utcnow(), applied_confidence=confidence)


def _proofs(*keys):
    """Подменяет каталог снимков: ключ → имя файла."""
    return mock.patch.object(
        trust, "proof_index",
        return_value={key: f"20260808_120000_{key}.png" for key in keys})


# ── Что считается доказательством ──────────────────────────────────────────
def test_receipt_with_screenshot_proves_the_platform():
    def body():
        with mock.patch.object(trust, "get_session", _db([_applied("a")])), _proofs("a"):
            row = trust.stats("salling")
        assert row["proven"] is True
        assert row["receipts"] == 1 and row["receipts_without_proof"] == 0

    _with_temp_settings(body)


def test_receipt_without_screenshot_does_not_prove_anything():
    """Обещание «доказательство есть» без файла — это обещание, а не доказательство."""
    def body():
        with mock.patch.object(trust, "get_session", _db([_applied("a")])), _proofs():
            row = trust.stats("salling")
        assert row["proven"] is False
        assert row["receipts"] == 0 and row["receipts_without_proof"] == 1

    _with_temp_settings(body)


def test_probable_and_manual_submissions_earn_no_trust():
    def body():
        rows = [_applied("a", confidence="indirect"), _applied("b", confidence="manual"),
                _applied("c", confidence=None)]
        with mock.patch.object(trust, "get_session", _db(rows)), _proofs("a", "b", "c"):
            row = trust.stats("salling")
        assert row["submitted"] == 3
        assert row["proven"] is False

    _with_temp_settings(body)


def test_employer_portal_proves_without_our_screenshot():
    """Кабинет работодателя — второй, независимый уровень доказательства:
    подтверждает не WexFlow, поэтому наш снимок там не нужен."""
    def body():
        with mock.patch.object(trust, "get_session", _db([_applied("a", confidence="portal")])), \
                _proofs():
            row = trust.stats("salling")
        assert row["proven"] is True
        assert row["portal"] == 1 and row["receipts"] == 0
        assert row["receipts_without_proof"] == 0

    _with_temp_settings(body)


def test_connector_screenshot_name_is_matched():
    """Коннекторы пишут снимок с заменой двоеточий: lidl:7 → lidl_7."""
    def body():
        rows = [_applied("lidl:7", source="lidl")]
        with mock.patch.object(trust, "get_session", _db(rows)), _proofs("lidl_7"):
            assert trust.stats("lidl")["proven"] is True

    _with_temp_settings(body)


# ── Доверие не переносится между площадками ────────────────────────────────
def test_trust_is_earned_per_platform():
    def body():
        with mock.patch.object(trust, "get_session", _db([_applied("a")])), _proofs("a"):
            assert trust.stats("salling")["proven"] is True
            assert trust.stats("lidl")["proven"] is False
            trust.set_always_ask(False)
            trust.set_auto("salling", True)
            assert trust.auto_allowed("salling")[0] is True
            allowed, why = trust.auto_allowed("lidl")
        assert allowed is False
        assert "квитанц" in why

    _with_temp_settings(body)


def test_auto_cannot_be_switched_on_in_advance():
    def body():
        with mock.patch.object(trust, "get_session", _db([])), _proofs():
            enabled, refusal = trust.set_auto("teamtailor", True)
        assert enabled is False and refusal
        assert trust.auto_enabled("teamtailor") is False

    _with_temp_settings(body)


# ── «Спрашивать всегда» ────────────────────────────────────────────────────
def test_always_ask_is_on_by_default_and_beats_auto():
    def body():
        assert trust.always_ask() is True
        with mock.patch.object(trust, "get_session", _db([_applied("a")])), _proofs("a"):
            trust.set_auto("salling", True)
            allowed, why = trust.auto_allowed("salling")
        assert allowed is False
        assert "спрашивать всегда" in why
        assert trust.auto_enabled("salling") is True, "выбор человека не стирается"

    _with_temp_settings(body)


# ── Предложение включить автомат ───────────────────────────────────────────
def test_offer_appears_once_and_remembers_the_answer():
    def body():
        with mock.patch.object(trust, "get_session", _db([_applied("a")])), _proofs("a"):
            offer = trust.pending_offer()
            assert offer and offer["source"] == "salling"
            trust.dismiss_offer("salling")
            assert trust.pending_offer() is None

    _with_temp_settings(body)


def test_no_offer_before_the_platform_proves_itself():
    def body():
        with mock.patch.object(trust, "get_session", _db([_applied("a", confidence="indirect")])), \
                _proofs("a"):
            assert trust.pending_offer() is None

    _with_temp_settings(body)


# ── Первая подача идёт одна ────────────────────────────────────────────────
def test_first_batch_on_a_new_platform_sends_one_job():
    def body():
        with mock.patch.object(trust, "get_session", _db([])), _proofs():
            trimmed, notes = trust.trim_unproven({"lidl": ["1", "2", "3"]})
        assert trimmed["lidl"] == ["1"]
        assert notes and "первая подача" in notes[0]

    _with_temp_settings(body)


def test_proven_platform_keeps_the_whole_batch():
    def body():
        with mock.patch.object(trust, "get_session", _db([_applied("a")])), _proofs("a"):
            trimmed, notes = trust.trim_unproven({"salling": ["1", "2", "3"]})
        assert trimmed["salling"] == ["1", "2", "3"] and notes == []

    _with_temp_settings(body)


# ── Автоотправка ───────────────────────────────────────────────────────────
def test_autopilot_never_auto_submits_on_an_unproven_platform():
    import autopilot

    def body():
        jobs = [Job(id="x", source="teamtailor", title="Job", status="new")]
        with mock.patch.object(autopilot, "find_matches", return_value=jobs), \
                mock.patch.object(autopilot.applications, "submitted_ids", return_value=set()), \
                mock.patch.object(autopilot.applications, "submitting_ids", return_value=set()), \
                mock.patch.object(trust, "get_session", _db([])), _proofs():
            assert autopilot._eligible_all({"submit_scope": "all"}) == []
            # даже когда «спрашивать всегда» выключено, недоверенная площадка
            # остаётся закрытой — и говорит об этом словами
            trust.set_always_ask(False)
            assert autopilot._eligible_all({"submit_scope": "all"}) == []
            assert "квитанц" in autopilot.auto_submit_block()

    _with_temp_settings(body)


def test_autopilot_explains_its_silence_only_once():
    import autopilot

    def body():
        with mock.patch.object(autopilot, "log_event") as logged:
            autopilot._log_trust_block_once("причина")
            autopilot._log_trust_block_once("причина")
        assert logged.call_count == 1, "журнал не должен забиваться одной и той же строкой"

    _with_temp_settings(body)


def test_failed_applications_are_counted_for_the_platform():
    def body():
        rows = [Application(source="teamtailor", job_id="x", state="failed")]
        with mock.patch.object(trust, "get_session", _db(rows)), _proofs():
            row = trust.stats("teamtailor")
        assert row["failed"] == 1 and row["proven"] is False

    _with_temp_settings(body)


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print("ok")
