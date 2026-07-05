"""Тесты шага 4 (Блок 1): воркер подачи возвращает результат.

Приложение читает per-job итог из apply_progress.json (его пишет apply.py),
а не угадывает по базе с дедлайном «180 секунд на заявку». Проверяем:
  - итоги «ok»/«failed» из файла разносятся в реестр и в отчёты;
  - файлу ПРОШЛОЙ пачки (старый started_at) не верим;
  - заявки, которых воркер не касался, в конце решаются по базе;
  - «идёт ли подача» решает живой процесс, а не возраст файла.

config.DATA_DIR временно подменяется на temp-папку — реальные данные не трогаются.

Запуск:  python tests/test_apply_worker_result.py   (или pytest)
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import config


class _TempData:
    def __enter__(self):
        self._orig = config.DATA_DIR
        self.dir = Path(tempfile.mkdtemp())
        config.DATA_DIR = self.dir
        return self.dir

    def __exit__(self, *exc):
        config.DATA_DIR = self._orig


def _write_progress(d, items, started_at=None, active=False):
    (d / "apply_progress.json").write_text(json.dumps({
        "active": active,
        "started_at": started_at or datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
    }), encoding="utf-8")


class _FakeProc:
    """Процесс воркера: poll() -> None (жив) или код выхода (завершился)."""
    def __init__(self, exit_code=0):
        self._code = exit_code

    def poll(self):
        return self._code


class _FakeJob:
    def __init__(self, jid, status="new", title="t"):
        self.id, self.status, self.title = jid, status, title
        self.applied_at = None


class _FakeSession:
    def __init__(self, jobs):
        self._jobs = jobs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def get(self, _model, jid):
        return self._jobs.get(jid)


class _Patched:
    """Подмена реестра/отчётов/базы вокруг _watch_and_report_apply_batch."""
    def __init__(self, db_jobs=None):
        self.submitted, self.failed, self.reports = [], [], []
        self._db = db_jobs or {}

    def __enter__(self):
        self._orig = (app.autopilot, app.get_session, app._report_apply_result_safe)
        me = self

        class _FakeAutopilot:
            @staticmethod
            def record_submitted(jobs):
                me.submitted.extend(j.id for j in jobs)

            @staticmethod
            def clear_submitting(ids):
                me.failed.extend(ids)

        app.autopilot = _FakeAutopilot
        app.get_session = lambda: _FakeSession(me._db)
        app._report_apply_result_safe = lambda jid, state, msg="": me.reports.append((jid, state))
        return self

    def __exit__(self, *exc):
        app.autopilot, app.get_session, app._report_apply_result_safe = self._orig


def test_settles_from_worker_file():
    # воркер сказал: A подана, B нет — верим ЕМУ, а не гаданию по базе
    with _TempData() as d, _Patched(db_jobs={"A": _FakeJob("A", "applied")}) as p:
        _write_progress(d, [{"id": "A", "state": "ok"}, {"id": "B", "state": "failed"}])
        app._watch_and_report_apply_batch(["A", "B"], _FakeProc(0), spawn_ts=time.time() - 30)
        assert p.submitted == ["A"], p.submitted
        assert p.failed == ["B"], p.failed
        assert ("A", "submitted") in p.reports and ("B", "failed") in p.reports


def test_stale_progress_file_ignored():
    # файл от ПРОШЛОЙ пачки говорит «A ok» — не верим; базa тоже пуста → failed
    with _TempData() as d, _Patched() as p:
        old = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        _write_progress(d, [{"id": "A", "state": "ok"}], started_at=old)
        app._watch_and_report_apply_batch(["A"], _FakeProc(0), spawn_ts=time.time() - 5)
        assert p.submitted == [], "итог из чужого файла прогресса засчитан как подача"
        assert p.failed == ["A"]


def test_untouched_id_falls_back_to_db():
    # B отсеян страховкой до запуска воркера (в items его нет), но в базе applied
    with _TempData() as d, _Patched(db_jobs={"A": _FakeJob("A", "applied"),
                                             "B": _FakeJob("B", "applied")}) as p:
        _write_progress(d, [{"id": "A", "state": "ok"}])
        app._watch_and_report_apply_batch(["A", "B"], _FakeProc(0), spawn_ts=time.time() - 30)
        assert sorted(p.submitted) == ["A", "B"], p.submitted
        assert p.failed == []


def test_no_worker_resolves_immediately():
    # воркер вовсе не запускался (proc=None) — не ждём 5 минут, решаем по базе сразу
    with _TempData() as d, _Patched() as p:
        t0 = time.time()
        app._watch_and_report_apply_batch(["A"], None, spawn_ts=time.time())
        assert time.time() - t0 < 2.0, "зря ждали дедлайн без воркера"
        assert p.failed == ["A"]


def test_worker_progress_for_matching():
    with _TempData() as d:
        _write_progress(d, [{"id": "A", "state": "ok"}])
        assert app._worker_progress_for(time.time() - 60) is not None
        assert app._worker_progress_for(time.time() + 60) is None, "чужой файл принят за свой"
    with _TempData():
        assert app._worker_progress_for(time.time()) is None  # файла нет


def test_progress_active_asks_live_process():
    # файл давно не обновлялся (логин затянулся), но наш процесс жив → подача идёт
    with _TempData() as d:
        stale = (datetime.now() - timedelta(seconds=600)).isoformat(timespec="seconds")
        (d / "apply_progress.json").write_text(json.dumps({
            "active": True, "started_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": stale}), encoding="utf-8")
        orig = (app._last_apply_proc, app._last_apply_spawn_ts)
        try:
            app._last_apply_proc = _FakeProc(exit_code=None)  # жив
            app._last_apply_spawn_ts = time.time() - 30
            assert app._apply_progress_active() is True
            app._last_apply_proc = _FakeProc(exit_code=1)  # умер, файл не закрыт
            assert app._apply_progress_active() is False
        finally:
            app._last_apply_proc, app._last_apply_spawn_ts = orig


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
