"""Тесты автозапуска с Windows (задача В, июль 2026).

Реальный реестр НЕ меняем: проверяем чистую сборку командной строки и то,
что в dev-режиме (не frozen) переключатель честно недоступен. enabled()
только читает HKCU Run — это безопасно.

Запуск:  python tests/test_autostart.py   (или pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart


def test_command_line_quotes_path_with_spaces():
    cmd = autostart.command_line(r"C:\Program Files\WexFlow\WexFlow.exe")
    assert cmd == '"C:\\Program Files\\WexFlow\\WexFlow.exe" --minimized'


def test_not_supported_in_dev():
    # тесты бегут обычным python (не PyInstaller) → frozen=False → недоступно
    assert not autostart.is_frozen()
    assert not autostart.supported()


def test_enable_refuses_in_dev():
    # в dev enable() не должен трогать реестр и возвращает False
    assert autostart.enable() is False


def test_status_shape():
    st = autostart.status()
    assert set(st) == {"supported", "enabled"}
    assert isinstance(st["enabled"], bool)


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
