"""Тест устойчивости обновления из отдельного установщика (WexFlow-Setup.exe).

Баг со скриншота: os.rename(папка WexFlow → .old) падал с [WinError 32], потому
что запущенный WexFlow.exe держал свою папку. Фикс: сперва закрыть приложение
(taskkill), затем повторять rename против коротких блокировок антивируса.

Здесь проверяем чистую логику повторов — os.rename, taskkill и sleep замоканы.

Запуск:  python tests/test_installer_retry.py   (или pytest)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "installer"))
import installer as inst


def _patch(monkey):
    """Заменить побочки на счётчики. Возвращает состояние."""
    st = {"stopped": 0, "sleeps": 0}
    monkey["stop"] = inst._stop_running_wexflow
    monkey["sleep"] = inst.time.sleep
    inst._stop_running_wexflow = lambda: st.__setitem__("stopped", st["stopped"] + 1)
    inst.time.sleep = lambda t: st.__setitem__("sleeps", st["sleeps"] + 1)
    return st


def _unpatch(monkey):
    inst._stop_running_wexflow = monkey["stop"]
    inst.time.sleep = monkey["sleep"]


def test_succeeds_after_transient_lock():
    monkey = {}
    st = _patch(monkey)
    calls = {"n": 0}

    def flaky_rename(a, b):
        calls["n"] += 1
        if calls["n"] < 3:                       # первые две попытки — «занято»
            raise OSError(32, "in use")
        return None                              # третья — успех
    orig = inst.os.rename
    inst.os.rename = flaky_rename
    try:
        inst._rename_with_retry("SRC", "DST", attempts=6, delay=0.0)
    finally:
        inst.os.rename = orig
        _unpatch(monkey)
    assert calls["n"] == 3, "должно хватить трёх попыток"
    assert st["stopped"] == 1, "приложение закрываем один раз (на первой неудаче)"


def test_stop_called_before_first_retry():
    """На первой же неудаче зовём _stop_running_wexflow (закрыть WexFlow),
    а не просто спим — иначе папка так и останется занятой."""
    monkey = {}
    st = _patch(monkey)
    seq = []

    def rename(a, b):
        seq.append("try")
        if len(seq) < 2:
            raise OSError(32, "in use")
    orig = inst.os.rename
    inst.os.rename = rename
    try:
        inst._rename_with_retry("SRC", "DST", attempts=4, delay=0.0)
    finally:
        inst.os.rename = orig
        _unpatch(monkey)
    assert st["stopped"] == 1
    assert st["sleeps"] == 0, "на первой неудаче — закрыть приложение, без sleep"


def test_raises_friendly_after_exhaustion():
    monkey = {}
    _patch(monkey)

    def always_locked(a, b):
        raise OSError(32, "still in use")
    orig = inst.os.rename
    inst.os.rename = always_locked
    err = None
    try:
        inst._rename_with_retry("SRC", "DST", attempts=3, delay=0.0)
    except Exception as e:  # noqa: BLE001
        err = e
    finally:
        inst.os.rename = orig
        _unpatch(monkey)
    assert isinstance(err, RuntimeError), "после всех попыток — понятная ошибка, не голый OSError"
    assert "Закрой WexFlow" in str(err), "сообщение подсказывает закрыть приложение"


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
