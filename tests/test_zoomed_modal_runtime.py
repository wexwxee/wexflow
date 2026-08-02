"""Всплывающие окна остаются в окне на любом масштабе интерфейса (80–150%)."""
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ZOOMS = (80, 100, 125, 150)
VIEWPORTS = ({"width": 1400, "height": 900}, {"width": 1100, "height": 720})


def _page_css(template: str) -> str:
    text = (ROOT / "templates" / template).read_text(encoding="utf-8")
    return "\n".join(re.findall(r"<style>(.*?)</style>", text, re.S))


def _document(zoom: int, css: str, body: str) -> str:
    return f"""
    <html data-ui-zoom="{zoom}" style="--wex-ui-zoom:{zoom / 100}">
    <head><style>
      #wf-overlay-root {{ position:fixed; inset:0; pointer-events:none; }}
      {css}
    </style></head>
    <body>{body}</body>
    </html>
    """


def _rect(page, selector):
    return page.evaluate(
        "sel => { const r = document.querySelector(sel).getBoundingClientRect();"
        " return {left:r.left, right:r.right, top:r.top, bottom:r.bottom,"
        " width:r.width, height:r.height}; }",
        selector,
    )


def test_batch_panel_stays_inside_the_window_at_every_zoom():
    """Панель пакетной подачи центрируется по рабочей области и не обрезается."""
    css = (ROOT / "static" / "theme.css").read_text(encoding="utf-8") + _page_css("index.html")
    rows = "".join(f"<p>Строка описания {index}</p>" for index in range(24))
    body = f'<div class="batchbar show"><div class="batch-head">Вакансии выбраны</div>{rows}</div>'

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for viewport in VIEWPORTS:
                page = browser.new_page(viewport=viewport)
                for zoom in ZOOMS:
                    page.set_content(_document(zoom, css, body))
                    bar = _rect(page, ".batchbar")
                    scale = zoom / 100
                    rail = 84 * scale  # левое меню тоже увеличивается

                    assert bar["left"] >= rail - 1, (zoom, viewport, bar)
                    assert bar["right"] <= viewport["width"] + 1, (zoom, viewport, bar)
                    assert bar["top"] >= 0, (zoom, viewport, bar)
                    assert bar["bottom"] <= viewport["height"] + 1, (zoom, viewport, bar)

                    centre = (bar["left"] + bar["right"]) / 2
                    expected = rail + (viewport["width"] - rail) / 2
                    assert abs(centre - expected) <= 2, (zoom, viewport, centre, expected)
                page.close()
        finally:
            browser.close()


def test_overlay_layer_widgets_follow_the_zoom():
    """Уведомления и окно перевода живут в слое поверх — но масштаб общий."""
    css = (
        (ROOT / "static" / "theme.css").read_text(encoding="utf-8")
        + (ROOT / "static" / "ui_assist.css").read_text(encoding="utf-8")
    )
    rows = "".join(f"<p>Строка {index}</p>" for index in range(30))
    body = (
        '<div class="system-response-stack"><div class="system-response ok">Готово</div></div>'
        f'<div class="wf-ai-modal"><section class="wf-ai-dialog">{rows}</section></div>'
    )
    move = """() => {
      const root = document.createElement('div');
      root.id = 'wf-overlay-root';
      document.documentElement.appendChild(root);
      root.appendChild(document.querySelector('.system-response-stack'));
      root.appendChild(document.querySelector('.wf-ai-modal'));
    }"""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for viewport in VIEWPORTS:
                page = browser.new_page(viewport=viewport)
                for zoom in ZOOMS:
                    scale = zoom / 100
                    page.set_content(_document(zoom, css, body))
                    page.evaluate(move)

                    stack = _rect(page, ".system-response-stack")
                    # шапка увеличивается вместе с интерфейсом — уведомление тоже
                    assert abs(stack["top"] - (34 + 56) * scale) <= 1, (zoom, stack)
                    assert abs(stack["right"] - (viewport["width"] - 16 * scale)) <= 1
                    assert abs(stack["width"] - min(440 * scale, viewport["width"] - 32 * scale)) <= 1

                    dialog = _rect(page, ".wf-ai-dialog")
                    # поля самого слоя (20px) не масштабируются — это внешний отступ
                    assert abs(dialog["width"] - min(760 * scale, viewport["width"] - 40)) <= 1
                    assert dialog["top"] >= 0 and dialog["bottom"] <= viewport["height"] + 1
                page.close()
        finally:
            browser.close()


def test_centred_dialogs_fit_the_window_at_every_zoom():
    """Онбординг, диалог профиля и «Что нового» не уезжают за нижний край."""
    css = (ROOT / "static" / "theme.css").read_text(encoding="utf-8") + _page_css("index.html")
    rows = "".join(f"<li>Пункт {index}</li>" for index in range(18))
    body = (
        f'<div class="wf-welcome"><div class="wf-welcome-card"><ul>{rows}</ul></div></div>'
        f'<div class="profile-dialog"><div class="profile-dialog-card"><ul>{rows}</ul></div></div>'
        f'<div class="wn-overlay"><div class="wn-modal"><ul>{rows}</ul></div></div>'
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for viewport in VIEWPORTS:
                page = browser.new_page(viewport=viewport)
                for zoom in ZOOMS:
                    page.set_content(_document(zoom, css, body))
                    for selector in (".wf-welcome-card", ".profile-dialog-card", ".wn-modal"):
                        card = _rect(page, selector)
                        assert card["top"] >= 0, (selector, zoom, viewport, card)
                        assert card["bottom"] <= viewport["height"] + 1, (
                            selector, zoom, viewport, card,
                        )
                        assert card["left"] >= 0, (selector, zoom, viewport, card)
                        assert card["right"] <= viewport["width"] + 1, (
                            selector, zoom, viewport, card,
                        )
                page.close()
        finally:
            browser.close()
