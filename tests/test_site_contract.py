"""Защита от изменений на стороне работодателя.

Смысл проверок: пока форма выглядит как договорились — работаем; переделали
форму — НЕ заполняем, НЕ жмём и объясняем человеку, что делать. Ошибка в эту
сторону дороже всех: у человека одна попытка на вакансию.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

from connectors import site_contract


LIDL_FORM = """
  <label for="first">Fornavn</label><input id="first">
  <label for="last">Efternavn</label><input id="last">
  <label for="mail">E-mail-adresse</label><input id="mail">
  <label for="phone">Mobilnummer</label><input id="phone">
  <span class="sapMLabel">Gade:</span><input class="sapMInputBaseInner">
  <span class="sapMLabel">Postnummer:</span><input class="sapMInputBaseInner">
  <input type="file" name="EACVUploader">
  <input type="file" name="EACoverLetterUploader">
  <button>Ansøg</button>
"""


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    return playwright, browser, browser.new_page()


def test_known_lidl_form_passes_the_contract():
    playwright, browser, page = _page()
    try:
        page.set_content(LIDL_FORM)
        report = site_contract.check(page, "lidl_easy_apply")
        assert report["ok"] is True
        assert report["missing"] == []
        assert report["platform"] == "Lidl EasyApply"
    finally:
        browser.close()
        playwright.stop()


def test_missing_cv_upload_or_submit_button_stops_everything():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="first">Fornavn</label><input id="first">
          <label for="last">Efternavn</label><input id="last">
          <label for="mail">E-mail-adresse</label><input id="mail">
          <label for="phone">Mobilnummer</label><input id="phone">
        """)
        report = site_contract.check(page, "lidl_easy_apply")
        assert report["ok"] is False
        assert "загрузка CV" in report["missing"]
        assert "финальная кнопка (Ansøg)" in report["missing"]
        message = site_contract.human_message(report)
        # человеку: что случилось, что заявка НЕ ушла и что делать дальше
        assert "изменил анкету" in message
        assert "НЕ отправлена" in message
        assert "обновления WexFlow" in message
        assert "@wexwxeee" in message          # куда писать, пока нет канала
    finally:
        browser.close()
        playwright.stop()


def test_optional_anchor_only_warns():
    """Пропал необязательный якорь — работаем дальше, но с предупреждением."""
    playwright, browser, page = _page()
    try:
        page.set_content(LIDL_FORM.replace(
            '<input type="file" name="EACoverLetterUploader">', ""))
        report = site_contract.check(page, "lidl_easy_apply")
        assert report["ok"] is True
        assert "загрузка мотивационного письма" in report["warnings"]
    finally:
        browser.close()
        playwright.stop()


def test_unknown_platform_is_not_treated_as_a_change():
    playwright, browser, page = _page()
    try:
        page.set_content("<p>какая-то форма</p>")
        report = site_contract.check(page, "recruitee")
        assert report["ok"] is True
        assert report["known"] is False
    finally:
        browser.close()
        playwright.stop()


def test_salling_contract_sees_form_inside_an_iframe():
    """У Salling форма живёт во фрейме — проверка обязана смотреть и туда."""
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <iframe srcdoc='<input type="file"><button>Send ansøgning</button>'></iframe>
        """)
        page.wait_for_timeout(200)
        report = site_contract.check(page, "salling")
        assert report["ok"] is True, report["missing"]
    finally:
        browser.close()
        playwright.stop()


def test_salling_without_form_reports_a_change():
    playwright, browser, page = _page()
    try:
        page.set_content("<p>Siden er flyttet</p>")
        report = site_contract.check(page, "salling")
        assert report["ok"] is False
        assert site_contract.short_message(report).startswith("Salling Group")
        assert "не отправлена" in site_contract.short_message(report)
    finally:
        browser.close()
        playwright.stop()
