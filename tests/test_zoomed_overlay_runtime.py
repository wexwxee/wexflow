"""Viewport portals stay attached to their controls at every UI zoom level."""
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def test_custom_select_portal_position_at_all_supported_zooms():
    css = (ROOT / "static" / "ui_assist.css").read_text(encoding="utf-8")
    js = (ROOT / "static" / "ui_assist.js").read_text(encoding="utf-8")
    options = "".join(f"<option>Option {index}</option>" for index in range(12))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            for zoom in (80, 100, 125, 150):
                page.set_content(
                    f"""
                    <html data-ui-zoom="{zoom}">
                    <head><style>
                      body {{ margin:0; zoom:{zoom / 100}; }}
                      #wf-overlay-root {{
                        position:fixed; inset:0; z-index:2147482000;
                        pointer-events:none;
                      }}
                      #wf-overlay-root > * {{ pointer-events:auto; }}
                      {css}
                    </style></head>
                    <body>
                      <div style="margin:500px 0 0 260px;width:420px">
                        <select id="subject">{options}</select>
                      </div>
                    </body>
                    </html>
                    """
                )
                page.evaluate(
                    """() => {
                      window.WexFlowOverlayRoot = function () {
                        let root = document.getElementById('wf-overlay-root');
                        if (!root) {
                          root = document.createElement('div');
                          root.id = 'wf-overlay-root';
                          document.documentElement.appendChild(root);
                        }
                        return root;
                      };
                    }"""
                )
                page.add_script_tag(content=js)
                page.locator(".wf-select-button").click()
                geometry = page.evaluate(
                    """() => {
                      const button = document.querySelector('.wf-select-button')
                        .getBoundingClientRect();
                      const portal = document.querySelector('.wf-select-portal')
                        .getBoundingClientRect();
                      return {
                        parent: document.querySelector('.wf-select-portal').parentElement.id,
                        button: {
                          left:button.left, right:button.right,
                          top:button.top, bottom:button.bottom
                        },
                        portal: {
                          left:portal.left, right:portal.right,
                          top:portal.top, bottom:portal.bottom
                        }
                      };
                    }"""
                )
                assert geometry["parent"] == "wf-overlay-root"
                assert abs(geometry["portal"]["left"] - geometry["button"]["left"]) <= 2
                assert abs(geometry["portal"]["right"] - geometry["button"]["right"]) <= 2
                gap_below = geometry["portal"]["top"] - geometry["button"]["bottom"]
                gap_above = geometry["button"]["top"] - geometry["portal"]["bottom"]
                assert (
                    3 <= gap_below <= 8
                    or 3 <= gap_above <= 8
                )
                assert geometry["portal"]["top"] >= 8
                assert geometry["portal"]["bottom"] <= 892
                page.keyboard.press("Escape")
        finally:
            browser.close()
