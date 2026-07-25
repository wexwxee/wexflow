"""Frameless fullscreen must be single-flight and stay on the UI-safe path."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop_app


ROOT = Path(__file__).resolve().parent.parent


class _Native:
    def __init__(self):
        self.is_fullscreen = False


class _Window:
    def __init__(self, fullscreen_fails=False):
        self.native = _Native()
        self.fullscreen_fails = fullscreen_fails
        self.toggles = 0
        self.maximizes = 0
        self.restores = 0
        self.resizes = 0

    def toggle_fullscreen(self):
        self.toggles += 1
        if self.fullscreen_fails:
            raise RuntimeError("backend unavailable")
        self.native.is_fullscreen = not self.native.is_fullscreen

    def maximize(self):
        self.maximizes += 1

    def restore(self):
        self.restores += 1

    def resize(self, *_args):
        self.resizes += 1


def _controls(window):
    controls = desktop_app.WindowControls()
    controls._window = lambda: window
    return controls


def test_fullscreen_uses_pywebview_ui_transition():
    window = _Window()
    controls = _controls(window)

    result = controls.toggle_maximize()

    assert result == {"ok": True, "fullscreen": True}
    assert window.toggles == 1
    assert window.maximizes == 0
    assert controls.window_state()["fullscreen"] is True


def test_quick_second_toggle_is_debounced():
    window = _Window()
    controls = _controls(window)

    assert controls.toggle_maximize()["fullscreen"] is True
    second = controls.toggle_maximize()

    assert second["busy"] is True
    assert window.toggles == 1


def test_fullscreen_fallback_never_escapes_to_caller():
    window = _Window(fullscreen_fails=True)
    controls = _controls(window)

    result = controls.toggle_maximize()

    assert result == {"ok": True, "fullscreen": True}
    assert window.toggles == 1
    assert window.maximizes == 1


def test_resize_is_blocked_while_fullscreen():
    window = _Window()
    controls = _controls(window)
    controls._fullscreen = True

    assert controls.resize_window(1200, 800) is False
    assert window.resizes == 0


def test_frontend_installs_once_and_routes_both_gestures_through_guard():
    chrome = (ROOT / "static" / "window_chrome.js").read_text(encoding="utf-8")
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    desktop = (ROOT / "desktop_app.py").read_text(encoding="utf-8")

    assert "__wexWindowChromeInstalled" in chrome
    assert "togglePending" in chrome
    assert "runOnce" in chrome
    assert "WexFlowWindowChrome.toggleMaximize" in base
    assert "window.toggle_fullscreen()" in desktop
    assert "_native_set_rect" not in desktop
    assert "shadow=False" in desktop


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nALL {len(tests)} TESTS PASSED")
