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
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select

import settings_store
import trust
from db import Application, ApplicationEvidence, Job, utcnow


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
    """Подменяет реестр валидных receipt-screen evidence: job id → файл."""
    return mock.patch.object(
        trust, "valid_receipt_screens",
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


def test_unverified_email_headers_cannot_unlock_platform_trust():
    """Editable Authentication-Results is useful evidence, not auto-submit proof."""
    def body():
        job = _applied("mail-1", confidence="email")
        evidence = ApplicationEvidence(
            source="salling", job_id=job.id, kind="email", path="mail.eml",
            fingerprint="a" * 64, sender="jobs@sallinggroup.com",
            authentication="unverified_header", occurred_at=job.applied_at,
        )
        email_row = SimpleNamespace(
            job_id=job.id, occurred_at=job.applied_at, created_at=job.applied_at,
        )
        with mock.patch.object(trust, "get_session", _db([job, evidence])), \
                mock.patch.object(trust.email_evidence, "valid_rows", return_value=[]), \
                _proofs():
            row = trust.stats("salling")
        assert row["proven"] is False
        assert row["emails"] == 0 and row["proofs"] == 0

    _with_temp_settings(body)


def test_connector_receipt_screen_is_bound_to_the_exact_job_id():
    def body():
        rows = [_applied("lidl:7", source="lidl")]
        with mock.patch.object(trust, "get_session", _db(rows)), _proofs("lidl:7"):
            assert trust.stats("lidl")["proven"] is True

    _with_temp_settings(body)


def test_unregistered_failure_png_can_never_prove_a_receipt(tmp_path):
    proof_dir = tmp_path / "logs" / "applied"
    proof_dir.mkdir(parents=True)
    (proof_dir / "20260808_120000_a.png").write_bytes(b"failed page screenshot")
    with mock.patch.object(trust.config, "DATA_DIR", tmp_path), \
            mock.patch.object(trust, "get_session", _db([_applied("a")])):
        assert trust.proof_index().get("a")
        row = trust.stats("salling")
    assert row["proven"] is False and row["receipts_without_proof"] == 1


def test_registered_receipt_screen_is_hash_checked(tmp_path):
    proof_dir = tmp_path / "logs" / "applied"
    proof_dir.mkdir(parents=True)
    path = proof_dir / "20260808_120000_a.png"
    path.write_bytes(b"real receipt bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    job = _applied("a")
    evidence = ApplicationEvidence(
        source="salling", job_id="a", kind="receipt_screen", path=path.name,
        fingerprint=digest, occurred_at=job.applied_at,
    )
    sessions = _db([job, evidence])
    with mock.patch.object(trust.config, "DATA_DIR", tmp_path), \
            mock.patch.object(trust, "get_session", sessions):
        assert trust.stats("salling")["proven"] is True
        path.write_bytes(b"truncated or replaced")
        assert trust.stats("salling")["proven"] is False


def test_attaching_exact_receipt_atomically_upgrades_indirect_submission(tmp_path):
    proof_dir = tmp_path / "logs" / "applied"
    proof_dir.mkdir(parents=True)
    path = proof_dir / "20260812_120000_lidl_7.png"
    path.write_bytes(b"confirmed receipt")
    job_id = "lidl:7"
    job = _applied(job_id, source="lidl", confidence="indirect")
    sessions = _db([job])

    with mock.patch.object(trust.config, "DATA_DIR", tmp_path), \
            mock.patch.object(trust, "get_session", sessions):
        assert trust.attach_receipt_screen(job_id, path) is True

    with sessions() as session:
        stored = session.get(Job, job_id)
        evidence = session.exec(select(ApplicationEvidence)).one()
        application = session.exec(select(Application)).one()
        assert stored.applied_confidence == "receipt"
        assert evidence.fingerprint == hashlib.sha256(path.read_bytes()).hexdigest()
        assert evidence.stage == "applied"
        assert application.state == "submitted"
        assert application.confidence == "receipt"


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
