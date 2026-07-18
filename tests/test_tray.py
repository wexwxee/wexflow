"""Тесты трея (июль 2026): «закрыть окно = свернуть в фон».

GUI в тестах не поднимаем — проверяем чистую логику _on_window_closing:
  - трей активен → закрытие отменяется, окно прячется, уведомление один раз;
  - трея нет / выбран «Выйти» / hide упал → окно закрывается по-настоящему.

Запуск:  python tests/test_tray.py   (или pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import desktop_app


class _FakeWindow:
    def __init__(self, hide_fails=False):
        self.hidden = False
        self._hide_fails = hide_fails

    def hide(self):
        if self._hide_fails:
            raise RuntimeError("no window")
        self.hidden = True


class _FakeIcon:
    def __init__(self):
        self.notices = []

    def notify(self, text, title=""):
        self.notices.append(text)


def _reset(icon=None, quit_=False, hint=False):
    desktop_app._tray_icon = icon
    desktop_app._tray_quit = quit_
    desktop_app._tray_hint_shown = hint


def test_close_hides_to_tray_and_notifies_once():
    icon = _FakeIcon()
    _reset(icon=icon)
    w = _FakeWindow()
    assert desktop_app._on_window_closing(w) is False   # закрытие отменено
    assert w.hidden
    assert len(icon.notices) == 1
    # второе закрытие — снова прячем, но без повторного уведомления
    w2 = _FakeWindow()
    assert desktop_app._on_window_closing(w2) is False
    assert len(icon.notices) == 1


def test_close_is_real_without_tray():
    _reset(icon=None)
    assert desktop_app._on_window_closing(_FakeWindow()) is True


def test_close_is_real_after_tray_quit():
    _reset(icon=_FakeIcon(), quit_=True)
    assert desktop_app._on_window_closing(_FakeWindow()) is True


def test_close_falls_back_when_hide_fails():
    _reset(icon=_FakeIcon())
    assert desktop_app._on_window_closing(_FakeWindow(hide_fails=True)) is True


def test_stop_tray_is_safe_without_icon():
    _reset(icon=None)
    desktop_app._stop_tray()   # не должно бросать


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
