"""Lidl EasyApply fills profile facts but leaves screening and submit manual."""
import os
import sys
import tempfile
import hashlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

from connectors import lidl_apply
from connectors.fill_common import add_banner


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    return playwright, browser, browser.new_page()


def test_phone_uses_lidl_international_format():
    assert lidl_apply.normalize_phone("+45 12 34 56 78") == "004512345678"
    assert lidl_apply.normalize_phone("12 34 56 78") == "004512345678"
    assert lidl_apply.normalize_phone("0046 12 34 56") == "0046123456"


def test_address_splits_into_street_and_house_number():
    assert lidl_apply.split_address("Sonnerupvej 104") == ("Sonnerupvej", "104")
    assert lidl_apply.split_address("Chr. Xs Vej 53") == ("Chr. Xs Vej", "53")
    assert lidl_apply.split_address("Nørrebrogade 12B, 3. tv") == ("Nørrebrogade", "12B")
    # No number at all: the whole line is the street, nothing is invented.
    assert lidl_apply.split_address("Sonnerupvej") == ("Sonnerupvej", "")
    assert lidl_apply.split_address("") == ("", "")


def _address_page(page):
    """The Lidl address block: bare captions, inputs without any label link."""
    page.set_content("""
      <label for="first">Fornavn</label><input id="first">
      <span class="sapMLabel" id="l6"><span><bdi>Gade:</bdi></span></span>
      <div><input id="street" class="sapMInputBaseInner"></div>
      <span class="sapMLabel" id="l5"><span><bdi>Husnummer:</bdi></span></span>
      <div><input id="houseno" class="sapMInputBaseInner" maxlength="6"></div>
      <span class="sapMLabel" id="l7"><span><bdi>Postnummer:</bdi></span></span>
      <div><input id="zip" class="sapMInputBaseInner"></div>
      <span class="sapMLabel" id="l8"><span><bdi>By:</bdi></span></span>
      <div><input id="city" class="sapMInputBaseInner"></div>
      <label class="sapMLabel" id="l10">Blev du henvist til Lidl?</label>
      <input id="referral" class="sapMInputBaseInner" aria-labelledby="l10">
    """)


def test_address_block_without_labels_is_filled_from_the_profile():
    playwright, browser, page = _page()
    try:
        _address_page(page)
        assert lidl_apply._fill_caption(page, "Gade", "Sonnerupvej")
        assert lidl_apply._fill_caption(page, "Husnummer", "104")
        assert lidl_apply._fill_caption(page, "Postnummer", "2700")
        assert lidl_apply._fill_caption(page, "By", "København")
        assert page.input_value("#street") == "Sonnerupvej"
        assert page.input_value("#houseno") == "104"
        assert page.input_value("#zip") == "2700"
        assert page.input_value("#city") == "København"
        # The next real question has its own label and must stay untouched.
        assert page.input_value("#referral") == ""
    finally:
        browser.close()
        playwright.stop()


def test_caption_fill_never_grabs_a_labelled_question():
    """A caption whose next input belongs to another question fills nothing."""
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <span class="sapMLabel" id="l9">Land</span>
          <label class="sapMLabel" id="l10">Blev du henvist til Lidl?</label>
          <input id="referral" class="sapMInputBaseInner" aria-labelledby="l10">
        """)
        assert not lidl_apply._fill_caption(page, "Land", "Danmark")
        assert page.input_value("#referral") == ""
    finally:
        browser.close()
        playwright.stop()


def test_caption_fill_keeps_values_the_person_already_typed():
    playwright, browser, page = _page()
    try:
        _address_page(page)
        page.fill("#city", "Aarhus")
        assert not lidl_apply._fill_caption(page, "By", "København")
        assert page.input_value("#city") == "Aarhus"
    finally:
        browser.close()
        playwright.stop()


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
                page, 'input[name="EACVUploader"]', str(cv), "cv"
            ) == "cv.pdf"
            assert lidl_apply._upload(
                page, 'input[name="EACoverLetterUploader"]', str(cover), "cover"
            ) == "cover.pdf"
            assert page.locator('input[name="EAOtherDocumentUploader"]').evaluate(
                "e => e.files.length"
            ) == 0
            assert page.locator("#btnSend").evaluate("e => e.clicks || 0") == 0
            assert lidl_apply._screening_question_count(page) == 1
    finally:
        browser.close()
        playwright.stop()


def test_bulk_lidl_cover_is_uploaded_with_readable_name_and_identical_bytes():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <input type="file" name="EACVUploader">
          <input type="file" name="EACoverLetterUploader">
          <input type="file" name="EAOtherDocumentUploader">
        """)
        with tempfile.TemporaryDirectory() as directory:
            uploads = Path(directory) / "uploads"
            uploads.mkdir()
            source = uploads / (
                "bulk_doc_ba2538ff07_"
                "Ivan_Malamen_Cover_Letter_Lidl_EN_DA.pdf"
            )
            payload = b"%PDF-1.4\nLidl cover letter test\n%%EOF"
            source.write_bytes(payload)

            with mock.patch.object(
                lidl_apply.profile_store, "UPLOAD_DIR", uploads
            ):
                assert lidl_apply._upload(
                    page,
                    'input[name="EACoverLetterUploader"]',
                    str(source),
                    "cover",
                )

            uploaded = page.locator(
                'input[name="EACoverLetterUploader"]'
            ).evaluate(
                """element => ({
                    name: element.files[0]?.name || '',
                    size: element.files[0]?.size || 0
                })"""
            )
            alias = uploads / uploaded["name"]

            assert uploaded["name"] == (
                "cover_Ivan_Malamen_Cover_Letter_Lidl_EN_DA.pdf"
            )
            assert uploaded["size"] == len(payload)
            assert alias.read_bytes() == source.read_bytes()
            assert hashlib.sha256(alias.read_bytes()).digest() == hashlib.sha256(
                source.read_bytes()
            ).digest()
            assert page.locator(
                'input[name="EACVUploader"]'
            ).evaluate("element => element.files.length") == 0
            assert page.locator(
                'input[name="EAOtherDocumentUploader"]'
            ).evaluate("element => element.files.length") == 0
    finally:
        browser.close()
        playwright.stop()


def test_prepare_checkpoint_reaches_real_submit_without_clicking_it():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <button id="nativeSubmit" onclick="window.nativeClicks=(window.nativeClicks||0)+1">
            Ansøg
          </button>
        """)
        checkpoint = lidl_apply.submission_checkpoint(page)
        assert checkpoint["reached_submit"] is True
        assert checkpoint["submit_requested"] is False
        assert page.evaluate("() => window.nativeClicks || 0") == 0
    finally:
        browser.close()
        playwright.stop()


def test_selected_file_name_reports_the_file_attached_to_browser(tmp_path):
    document = tmp_path / "cv_Lidl.pdf"
    document.write_bytes(b"%PDF-1.4\n%%EOF")
    playwright, browser, page = _page()
    try:
        page.set_content('<input type="file" name="EACVUploader">')
        selector = 'input[type="file"][name="EACVUploader"]'
        page.locator(selector).set_input_files(document)
        assert lidl_apply.selected_file_name(page, selector) == "cv_Lidl.pdf"
    finally:
        browser.close()
        playwright.stop()


def test_submit_signal_is_armed_only_after_explicit_caller_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lidl_apply,
        "arm_explicit_submit",
        lambda page: calls.append(page) or True,
    )
    checkpoint = {"reached_submit": True}

    lidl_apply.set_submit_armed("page", checkpoint, allow_submit=False)
    assert checkpoint["submit_armed"] is False
    assert calls == []

    lidl_apply.set_submit_armed("page", checkpoint, allow_submit=True)
    assert checkpoint["submit_armed"] is True
    assert calls == ["page"]


def test_review_button_is_consumed_then_worker_clicks_native_lidl_button_once():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <button id="nativeSubmit">Ansøg</button>
          <script>
            nativeSubmit.addEventListener('click', () => {
              window.nativeClicks=(window.nativeClicks||0)+1;
              document.body.insertAdjacentHTML(
                'beforeend',
                '<p>Tak for din ansøgning</p>'
                + '<section id="data-dialog">'
                + '<h2>Før vi går videre: Behandling af dine data.</h2>'
                + '<button id="accept-data">ACCEPTER</button></section>'
              );
              document.getElementById('accept-data').addEventListener('click', () => {
                window.consentClicks=(window.consentClicks||0)+1;
                document.getElementById('data-dialog').remove();
              });
            });
          </script>
        """)
        add_banner(page, 0, ["CV", "cover letter"], platform="Lidl EasyApply")
        assert lidl_apply.arm_explicit_submit(page) is True
        assert page.evaluate("() => window.nativeClicks || 0") == 0
        page.locator("#wexflow-banner").locator(
            "#wexflow-real-submit"
        ).click()
        assert page.evaluate("() => window.nativeClicks || 0") == 0
        assert lidl_apply.take_explicit_submit_request(page) is True
        assert lidl_apply.take_explicit_submit_request(page) is False
        result = lidl_apply.submit(
            page,
            {
                "first_name": "Ivan",
                "last_name": "Malamen",
                "email": "ivan@example.com",
                "phone": "12345678",
            },
            wait_seconds=5,
        )
        assert result["state"] == "submitted"
        assert result["proof_ready"] is True
        assert page.evaluate("() => window.nativeClicks || 0") == 1
        assert page.evaluate("() => window.consentClicks || 0") == 1
        assert page.locator("#data-dialog").count() == 0
        assert lidl_apply.submission_checkpoint(page)["submit_requested"] is False
    finally:
        browser.close()
        playwright.stop()


def test_submission_is_confirmed_only_by_positive_lidl_receipt():
    playwright, browser, page = _page()
    try:
        page.set_content("<main>Udfyld venligst alle obligatoriske felter</main>")
        assert lidl_apply.submission_receipt_visible(page) is False
        page.set_content("<main>Tak for din ansøgning. Vi har modtaget din ansøgning.</main>")
        assert lidl_apply.submission_receipt_visible(page) is True
        page.set_content("<main>Спасибо за вашу заявку. Заявка получена.</main>")
        assert lidl_apply.submission_receipt_visible(page) is True
    finally:
        browser.close()
        playwright.stop()


def test_confirmation_data_dialog_is_never_accepted_without_a_receipt():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <main>Din ansøgning er endnu ikke sendt.</main>
          <button onclick="window.consentClicks=(window.consentClicks||0)+1">
            ACCEPTER
          </button>
        """)
        assert lidl_apply.prepare_submission_proof(page) is False
        assert page.evaluate("() => window.consentClicks || 0") == 0
    finally:
        browser.close()
        playwright.stop()


def test_translated_confirmation_data_button_is_accepted_after_receipt():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <main>Спасибо за вашу заявку. Заявка получена.</main>
          <section id="data-dialog">
            <h2>Прежде чем продолжить: обработка ваших данных</h2>
            <button onclick="
              window.consentClicks=(window.consentClicks||0)+1;
              document.getElementById('data-dialog').remove();
            ">ПРИНЯТЬ</button>
          </section>
        """)
        assert lidl_apply.prepare_submission_proof(page) is True
        assert page.evaluate("() => window.consentClicks || 0") == 1
        assert page.locator("#data-dialog").count() == 0
        assert lidl_apply.submission_receipt_visible(page) is True
    finally:
        browser.close()
        playwright.stop()


def test_translated_lidl_submit_button_is_recognised():
    playwright, browser, page = _page()
    try:
        page.set_content("<button>Применять</button>")
        checkpoint = lidl_apply.submission_checkpoint(page)
        assert checkpoint["reached_submit"] is True
        assert checkpoint["submit_enabled"] is True
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
