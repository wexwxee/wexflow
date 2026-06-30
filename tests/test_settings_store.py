"""Тесты надёжности settings_store (шаг 1.4, F33): атомарная запись,
терпимость к битому JSON, отсутствие потерь при параллельных изменениях.

PATH подменяется на временный файл — реальный settings.json НЕ трогается.

Запуск:  python tests/test_settings_store.py   (или pytest)
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings_store


def _with_temp(body):
    orig = settings_store.PATH
    tmpdir = tempfile.mkdtemp()
    settings_store.PATH = Path(tmpdir) / "settings.json"
    try:
        body()
    finally:
        settings_store.PATH = orig


def test_save_then_load_roundtrip():
    def body():
        settings_store.save({"a": 1, "home": {"lat": 55.0}})
        assert settings_store.load() == {"a": 1, "home": {"lat": 55.0}}
    _with_temp(body)


def test_load_tolerates_corrupt_file():
    def body():
        settings_store.PATH.write_text("{ это не JSON ", encoding="utf-8")
        # не должно бросать исключение — возвращает пустые настройки
        assert settings_store.load() == {}
        # повреждённый файл отложен в копию settings.corrupt-*.json
        backups = [p.name for p in settings_store.PATH.parent.iterdir()
                   if p.name.startswith("settings.corrupt-")]
        assert backups, "копия повреждённого файла не создана"
    _with_temp(body)


def test_mutate_no_lost_updates_under_threads():
    def body():
        settings_store.save({})
        N = 25

        def worker(i):
            settings_store.mutate(lambda d: d.__setitem__(f"k{i}", i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        d = settings_store.load()
        missing = [i for i in range(N) if d.get(f"k{i}") != i]
        assert not missing, f"потеряны параллельные изменения: {missing}"
    _with_temp(body)


def test_no_temp_file_left_after_save():
    def body():
        settings_store.save({"x": 1})
        leftovers = [p.name for p in settings_store.PATH.parent.iterdir()
                     if p.name.endswith(".tmp")]
        assert not leftovers, f"остался временный файл: {leftovers}"
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
