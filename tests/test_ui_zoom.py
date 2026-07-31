"""Широкое рабочее пространство и общий масштаб интерфейса."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent


def test_zoom_control_is_shared_and_persistent():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    ui = (ROOT / "templates" / "_ui.html").read_text(encoding="utf-8")

    assert 'localStorage.getItem("wexflow-ui-zoom")' in base
    assert 'localStorage.setItem("wexflow-ui-zoom"' in base
    assert "--wex-ui-zoom" in base
    assert "data-ui-zoom-range" in ui
    assert 'min="80" max="150" step="5"' in ui
    assert "data-ui-zoom-reset" in ui


def test_zoom_keyboard_and_mouse_shortcuts_are_available():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")

    assert 'event.key === "+"' in base
    assert 'event.key === "-"' in base
    assert 'event.key === "0"' in base
    assert 'event.addEventListener("wheel"' not in base  # listener belongs to document
    assert 'document.addEventListener("wheel"' in base
    assert "event.preventDefault()" in base


def test_large_screens_use_more_of_the_window_without_widening_every_page_equally():
    css = (ROOT / "static" / "theme.css").read_text(encoding="utf-8")
    account = (ROOT / "templates" / "account.html").read_text(encoding="utf-8")

    assert "width: min(1480px, 100%)" in css
    assert "width: min(1120px, 100%)" in css
    assert "width: min(1360px, 100%)" in css
    assert "account-workbench" in account
    assert "zoom: var(--wex-ui-zoom, 1)" in css


def test_fixed_job_tooltips_compensate_for_css_zoom():
    index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assist = (ROOT / "static" / "ui_assist.js").read_text(encoding="utf-8")

    assert "WexFlowMountOverlay" in base
    assert "WexFlowMountOverlay" in index
    assert "overlayRoot().appendChild(portal)" in assist
    assert "overlayRoot().appendChild(modal)" in assist
