"""Тест синка полных текстов вакансий в облако (_sync_job_texts_to_cloud).

Проверяем сборку payload и дедуп по отпечатку без реальной сети/БД: подменяем
autopilot.find_matches, applications-множества, translate_worker и
cloud_auth.report_job_texts.

Запуск:  python tests/test_jobtext_sync.py   (или pytest)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import autopilot
import applications
import cloud_auth
import translate_worker
import account as account_mod


def _job(jid, desc="Dansk beskrivelse.", ru="", fs=1.0):
    return SimpleNamespace(id=jid, title="T "+jid, description=desc, description_ru=ru, first_seen=fs)


def _patch(matches, down=False):
    """Подменить окружение синка. Возвращает список отправленных батчей."""
    sent = []
    account_mod.is_signed_in = lambda: True
    autopilot.find_matches = lambda: list(matches)
    applications.submitted_ids = lambda: set()
    applications.skipped_ids = lambda: set()
    applications.submitting_ids = lambda: set()
    translate_worker.is_translator_down = lambda: down
    cloud_auth.report_job_texts = lambda items, timeout=8: (sent.append(list(items)) or True)
    # сброс троттлинга/памяти отпечатков
    app._jobtexts_sync_last = 0.0
    app._jobtexts_sent = {}
    app._cloud_sync_attempt_last["jobtexts"] = 0.0
    return sent


def test_done_and_pending_states():
    sent = _patch([_job("a", ru="Русский перевод"), _job("b")])
    assert app._sync_job_texts_to_cloud(force=True) is True
    batch = sent[0]
    by = {it["id"]: it for it in batch}
    assert by["a"]["st"] == "done" and by["a"]["ru"] == "Русский перевод"
    assert by["b"]["st"] == "pending" and by["b"]["ru"] == ""
    assert by["b"]["orig"] == "Dansk beskrivelse."


def test_unavailable_when_translator_down():
    sent = _patch([_job("b")], down=True)
    assert app._sync_job_texts_to_cloud(force=True) is True
    assert sent[0][0]["st"] == "unavailable"


def test_dedup_skips_resend():
    sent = _patch([_job("a", ru="перевод")])
    app._sync_job_texts_to_cloud(force=True)
    # второй проход: тот же текст → отпечаток совпал → ничего не шлём
    app._cloud_sync_attempt_last["jobtexts"] = 0.0
    app._sync_job_texts_to_cloud(force=True)
    assert len(sent) == 1, "повторно тот же текст слать не должны"


def test_pending_to_done_resends():
    job = _job("a")
    sent = _patch([job])
    app._sync_job_texts_to_cloud(force=True)            # pending
    job.description_ru = "перевод готов"                # перевели
    app._cloud_sync_attempt_last["jobtexts"] = 0.0
    app._sync_job_texts_to_cloud(force=True)            # должен уйти done
    assert len(sent) == 2
    assert sent[1][0]["st"] == "done"


def test_caps_and_empty_description_skipped():
    long_ru = "я" * 9000
    sent = _patch([_job("a", ru=long_ru), _job("empty", desc="")])
    app._sync_job_texts_to_cloud(force=True)
    ids = [it["id"] for it in sent[0]]
    assert "empty" not in ids, "вакансию без описания не синкаем"
    assert len(sent[0][0]["ru"]) == 6000, "текст режется до 6000"


def test_bad_report_does_not_crash():
    sent = _patch([_job("a", ru="x")])
    cloud_auth.report_job_texts = lambda items, timeout=8: (_ for _ in ()).throw(RuntimeError("net down"))
    # исключение внутри проглатывается — возвращаем False, поллер не падает
    assert app._sync_job_texts_to_cloud(force=True) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
