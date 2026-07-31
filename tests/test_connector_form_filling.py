"""Assisted external forms use the canonical profile and fill conservatively."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

from connectors import fill_common, generic_apply


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    return playwright, browser, browser.new_page()


def test_load_profile_uses_canonical_shared_store():
    expected = {"first_name": "Ada", "email": "ada@example.com"}
    with mock.patch.object(fill_common.profile_store, "load_profile", return_value=expected):
        assert fill_common.load_profile() == expected


def test_semantic_labels_and_country_select_are_filled_without_overwrite():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="q101">LinkedIn profile</label><input id="q101">
          <label for="q102">Country of residence</label>
          <select id="q102"><option value="">Choose</option><option value="DK">Denmark</option></select>
          <label for="q103">Email</label><input id="q103" type="email" value="kept@example.com">
        """)
        assert generic_apply._fill_by_keywords(page, generic_apply.LINKEDIN, "https://linkedin.com/in/ada")
        assert generic_apply._fill_select_by_keywords(page, generic_apply.COUNTRY, "Danmark")
        assert not generic_apply._fill_email(page, "replace@example.com")
        assert page.locator("#q101").input_value() == "https://linkedin.com/in/ada"
        assert page.locator("#q102").input_value() == "DK"
        assert page.locator("#q103").input_value() == "kept@example.com"
    finally:
        browser.close()
        playwright.stop()


def test_generic_filler_uses_resolved_common_company_answers():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="citizenship">Citizenship</label>
          <input id="citizenship">
          <label for="permit">Do you have a valid work permit?</label>
          <select id="permit"><option value="">Choose</option><option>No</option><option>Yes</option></select>
          <fieldset>
            <legend>Are you willing to work every second weekend?</legend>
            <label><input type="radio" name="weekend" value="yes">Yes</label>
            <label><input type="radio" name="weekend" value="no">No</label>
          </fieldset>
        """)
        filled = generic_apply.fill_answer_fields(page, {
            "citizenship": "Ukraine",
            "work_permit": "yes",
            "work_weekends": "no",
        })
        assert page.input_value("#citizenship") == "Ukraine"
        assert page.locator("#permit").input_value() == "Yes"
        assert page.locator('input[name="weekend"][value="no"]').is_checked()
        assert {"citizenship", "work_permit", "work_weekends"} <= set(filled)
    finally:
        browser.close()
        playwright.stop()


def test_cv_and_cover_letter_go_to_their_own_inputs():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="cover-file">Motivation letter</label><input id="cover-file" type="file">
          <label for="resume-file">CV / Résumé</label><input id="resume-file" type="file">
        """)
        with tempfile.TemporaryDirectory() as directory:
            cv = Path(directory) / "resume.pdf"
            cover = Path(directory) / "motivation.pdf"
            cv.write_bytes(b"cv")
            cover.write_bytes(b"cover")
            profile = {"cv_path": str(cv), "cover_letter_path": str(cover)}
            assert fill_common.upload_cv(page, profile)
            assert fill_common.attach_cover_letter(page, profile)
            assert page.locator("#resume-file").evaluate("e => e.files[0].name") == "resume.pdf"
            assert page.locator("#cover-file").evaluate("e => e.files[0].name") == "motivation.pdf"
    finally:
        browser.close()
        playwright.stop()


def test_required_consent_and_radio_group_are_reported_when_unchecked():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label><input name="privacy" type="checkbox" required> I accept the privacy policy</label>
          <fieldset><legend>Work permit</legend>
            <label><input name="permit" type="radio" value="yes" required> Yes</label>
            <label><input name="permit" type="radio" value="no" required> No</label>
          </fieldset>
        """)
        missing = fill_common.missing_required(page)
        assert any("privacy" in item.lower() for item in missing)
        assert any("permit" in item.lower() or "yes" in item.lower() for item in missing)
        page.locator('input[name="privacy"]').check()
        page.locator('input[name="permit"][value="yes"]').check()
        assert fill_common.missing_required(page) == []
    finally:
        browser.close()
        playwright.stop()


def test_summary_card_is_isolated_collapsible_closable_and_does_not_shift_site():
    playwright, browser, page = _page()
    try:
        page.set_content('<body style="padding-top:7px"><main>Employer form</main></body>')
        fill_common.add_banner(
            page, 2, ["email", "CV"], platform="Greenhouse",
            missing=["Work permit"],
        )
        host = page.locator("#wexflow-banner")
        assert host.count() == 1
        text = host.locator(".card").inner_text()
        assert "Greenhouse" in text and "email, CV" in text
        assert "Дополнительных вопросов: 2" in text
        assert "Work permit" in text
        assert page.locator("body").evaluate("e => e.style.paddingTop") == "7px"
        host.locator('button[aria-label="Свернуть"]').click()
        assert host.locator(".card").evaluate("e => e.classList.contains('collapsed')")
        assert host.locator(".body").is_hidden()
        assert host.locator('button[aria-label="Развернуть"]').count() == 1
        host.locator('button[aria-label="Развернуть"]').click()
        assert host.locator(".body").is_visible()
        host.locator('button[aria-label="Закрыть"]').click()
        assert page.locator("#wexflow-banner").count() == 0
    finally:
        browser.close()
        playwright.stop()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
