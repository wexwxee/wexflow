"""Тесты «в Telegram не приходят одни и те же вакансии» (июль 2026).

Две починенные причины повторов:
  1. reset_tg_queue_for_filters сбрасывал «предложено» (clear_offers) при
     каждой смене фильтров И при каждом нажатии панельной кнопки «Показать
     подходящие» — те же вакансии уходили заново. Теперь сброс снимает только
     ожидающие карточки; гейт F27 «предложено — навсегда» держится.
  2. scan_and_notify перезаписывал seen_ids текущим набором: вакансия,
     мигнувшая из выдачи и вернувшаяся, снова считалась «новой». Теперь
     просмотренные копятся; чистятся только исчезнувшие из базы.

settings_store.PATH подменяется на временный файл; сеть и база подменены.

Запуск:  python tests/test_tg_no_repeat.py   (или pytest)
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applications
import cloud_auth
import scheduler
import settings_store
import autopilot


def _with_temp(body):
    orig = settings_store.PATH
    tmpdir = tempfile.mkdtemp()
    settings_store.PATH = Path(tmpdir) / "settings.json"
    try:
        body()
    finally:
        settings_store.PATH = orig


def _job(jid):
    return SimpleNamespace(id=jid, title=f"Вакансия {jid}", city="Brønshøj")


class _FakeSession:
    """get_session() для скана: отдаёт список id существующих вакансий."""
    def __init__(self, existing_ids):
        self._ids = list(existing_ids)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def exec(self, *_a, **_k):
        ids = self._ids
        return SimpleNamespace(all=lambda: list(ids))


def test_filter_reset_keeps_offered_marks():
    def body():
        calls = {"clear_offers": 0}
        orig_clear = applications.clear_offers
        orig_panel = cloud_auth.clear_panel
        applications.clear_offers = lambda: calls.__setitem__("clear_offers", calls["clear_offers"] + 1) or 0
        cloud_auth.clear_panel = lambda timeout=3: True
        try:
            autopilot.save_rule({"tg_pending": [{"job_id": "a", "message_id": 1, "ts": "2026-07-20T10:00:00"}]})
            autopilot.reset_tg_queue_for_filters()
            # ожидающие карточки сняты, но «предложено» в реестре не тронуто
            assert autopilot.get_rule()["tg_pending"] == []
            assert calls["clear_offers"] == 0, "смена фильтров снова забывает «предложено» — вернутся повторы"
        finally:
            applications.clear_offers = orig_clear
            cloud_auth.clear_panel = orig_panel
    _with_temp(body)


def _run_scan(matches, existing_ids, notifications):
    orig_find = autopilot.find_matches
    orig_session = autopilot.get_session
    orig_notify = scheduler.notify
    autopilot.find_matches = lambda: list(matches)
    autopilot.get_session = lambda: _FakeSession(existing_ids)
    scheduler.notify = lambda title, msg="": notifications.append(title)
    try:
        autopilot.scan_and_notify()
    finally:
        autopilot.find_matches = orig_find
        autopilot.get_session = orig_session
        scheduler.notify = orig_notify


def test_flapping_match_is_not_new_again():
    def body():
        autopilot.save_rule({"enabled": True})
        notes = []
        a, b = _job("a"), _job("b")
        _run_scan([a, b], ["a", "b"], notes)          # первый скан: обе новые
        assert len(notes) == 1
        assert set(autopilot.get_rule()["seen_ids"]) == {"a", "b"}

        _run_scan([b], ["a", "b"], notes)             # «a» мигнула из выдачи
        assert "a" in autopilot.get_rule()["seen_ids"], "просмотренное забылось при мигании выдачи"

        _run_scan([a, b], ["a", "b"], notes)          # «a» вернулась
        assert len(notes) == 1, "вернувшаяся вакансия снова посчиталась «новой»"
    _with_temp(body)


def test_seen_ids_pruned_when_job_gone_from_db():
    def body():
        autopilot.save_rule({"enabled": True})
        notes = []
        _run_scan([_job("a"), _job("b")], ["a", "b"], notes)
        _run_scan([_job("b")], ["b"], notes)          # «a» исчезла из базы совсем
        assert "a" not in autopilot.get_rule()["seen_ids"]
    _with_temp(body)


def test_truly_new_job_still_notifies():
    def body():
        autopilot.save_rule({"enabled": True})
        notes = []
        _run_scan([_job("a")], ["a"], notes)
        _run_scan([_job("a"), _job("c")], ["a", "c"], notes)   # появилась настоящая новая
        assert len(notes) == 2, "настоящая новая вакансия не дала уведомления"
        assert set(autopilot.get_rule()["seen_ids"]) == {"a", "c"}
    _with_temp(body)


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
