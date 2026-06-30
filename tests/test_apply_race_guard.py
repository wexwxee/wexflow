"""Тесты симметричной защиты от гонки подачи (шаг 1.3, F24).

Проверяем детектор активной подачи app._apply_progress_active (по файлу прогресса)
и что ожидание очереди _wait_for_manual_apply_to_finish мгновенно выходит, когда
ручной подачи нет (иначе очередь зря тормозила бы каждый запуск).

config.DATA_DIR временно подменяется на temp-папку — реальные данные не трогаются.

Запуск:  python tests/test_apply_race_guard.py   (или pytest)
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


def _write_progress(d, active, updated_at):
    (d / "apply_progress.json").write_text(
        json.dumps({"active": active, "updated_at": updated_at}), encoding="utf-8")


def test_progress_inactive_when_no_file():
    with _TempData():
        assert app._apply_progress_active() is False


def test_progress_active_when_fresh():
    with _TempData() as d:
        _write_progress(d, True, datetime.now().isoformat())
        assert app._apply_progress_active() is True


def test_progress_inactive_when_stale():
    with _TempData() as d:
        _write_progress(d, True, (datetime.now() - timedelta(seconds=300)).isoformat())
        assert app._apply_progress_active() is False


def test_progress_inactive_when_flag_false():
    with _TempData() as d:
        _write_progress(d, False, datetime.now().isoformat())
        assert app._apply_progress_active() is False


def test_wait_returns_immediately_when_idle():
    # ручной подачи нет -> очередь не должна ждать
    app._last_manual_apply_ts = 0.0
    orig = app._apply_progress_active
    app._apply_progress_active = lambda: False
    t0 = time.time()
    try:
        app._wait_for_manual_apply_to_finish(timeout=5)
    finally:
        app._apply_progress_active = orig
    assert time.time() - t0 < 1.0, "очередь зря ждала, хотя ручной подачи нет"


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
