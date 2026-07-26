"""Lidl EasyApply fills profile facts but leaves screening and submit manual."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

from connectors import lidl_apply


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    return playwright, browser, browser.new_page()


def test_phone_uses_lidl_international_format():
    assert lidl_apply.normalize_phone("+45 12 34 56 78") == "004512345678"
    assert lidl_apply.normalize_phone("12 34 56 78") == "004512345678"
    assert lidl_apply.normalize_phone("0046 12 34 56") == "0046123456"


def test_identity_country_and_named_documents_are_filled_safely():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="first">Fornavn</label><input id="first">
          <label for="last">Efternavn</label><input id="last">
          <label for="mail">E-mail-adresse</label><input id="mail">
          <label for="phone">Mobilnummer</label><input id="phone">
          <label id="country-label" for="country-hidden">Land</label>
          <button role="combobox" aria-labelledby="country-label"
            onclick="document.querySelector('[role=option]').hidden=false"></button>
          <div role="option" hidden
            onclick="document.querySelector('[role=combobox]').textContent=this.textContent">Danmark</div>
          <input type="file" name="EACVUploader">
          <input type="file" name="EACoverLetterUploader">
          <input type="file" name="EAOtherDocumentUploader">
          <label for="__group0">Kan du møde kl. 06.00?</label>
          <button id="btnSend">Ansøg</button>
        """)
        with tempfile.TemporaryDirectory() as directory:
            cv = Path(directory) / "cv.pdf"
            cover = Path(directory) / "cover.pdf"
            cv.write_bytes(b"cv")
            cover.write_bytes(b"cover")
            for label, value in (
                ("Fornavn", "Ada"),
                ("Efternavn", "Lovelace"),
                ("E-mail-adresse", "ada@example.com"),
                ("Mobilnummer", "004512345678"),
            ):
                assert lidl_apply._fill_labeled(page, label, value)
            assert lidl_apply._select_ui5(page, "Land", "Danmark")
            assert lidl_apply._upload(
                page, 'input[name="EACVUploader"]', str(cv)
            )
            assert lidl_apply._upload(
                page, 'input[name="EACoverLetterUploader"]', str(cover)
            )
            assert page.locator('input[name="EAOtherDocumentUploader"]').evaluate(
                "e => e.files.length"
            ) == 0
            assert page.locator("#btnSend").evaluate("e => e.clicks || 0") == 0
            assert lidl_apply._screening_question_count(page) == 1
    finally:
        browser.close()
        playwright.stop()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nALL {len(tests)} TESTS PASSED")
