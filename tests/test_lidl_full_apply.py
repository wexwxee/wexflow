"""Полная подача Lidl: отвечаем сохранёнными ответами и жмём только наверняка.

Главное правило, которое здесь и проверяется: WexFlow НИКОГДА не выдумывает
ответ за человека. Нет сохранённого ответа на вопрос анкеты — автоматическая
отправка не происходит вообще, окно остаётся человеку.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from playwright.sync_api import sync_playwright

import form_questions
from connectors import lidl_apply


@pytest.fixture(autouse=True)
def _bank(tmp_path, monkeypatch):
    """Банк вопросов у каждого теста свой: подача не должна писать в рабочие данные."""
    monkeypatch.setattr(form_questions, "path", lambda: tmp_path / "form_questions.json")

PROFILE = {
    "first_name": "Ivan", "last_name": "Malamen",
    "email": "ivan@example.com", "phone": "12345678",
    "gender": "male", "start_date": "2026-08-15",
    "retail_experience": "yes", "work_weekends": "yes",
    "work_evenings": "yes", "work_early": "no",
}

QUESTIONS = """
  <label id="q1lbl" for="__group1">Har du erfaring med (detail) branchen?</label>
  <div class="sapMRbG" id="__group1" role="radiogroup" aria-labelledby="q1lbl">
    <div class="sapMRb" id="q1ja" role="radio" aria-checked="false"
         onclick="this.setAttribute('aria-checked','true')">Ja</div>
    <div class="sapMRb" id="q1nej" role="radio" aria-checked="false"
         onclick="this.setAttribute('aria-checked','true')">Nej</div>
  </div>
  <label id="q2lbl" for="__group2">Er du villig til at arbejde hver 2. weekend?</label>
  <div class="sapMRbG" id="__group2" role="radiogroup" aria-labelledby="q2lbl">
    <div class="sapMRb" id="q2ja" role="radio" aria-checked="false"
         onclick="this.setAttribute('aria-checked','true')">Ja</div>
    <div class="sapMRb" id="q2nej" role="radio" aria-checked="false"
         onclick="this.setAttribute('aria-checked','true')">Nej</div>
  </div>
  <label id="q3lbl" for="__group3">Kan du møde kl 06.00 om morgenen?</label>
  <div class="sapMRbG" id="__group3" role="radiogroup" aria-labelledby="q3lbl">
    <div class="sapMRb" id="q3ja" role="radio" aria-checked="false"
         onclick="this.setAttribute('aria-checked','true')">Ja</div>
    <div class="sapMRb" id="q3nej" role="radio" aria-checked="false"
         onclick="this.setAttribute('aria-checked','true')">Nej</div>
  </div>
"""

SUBMIT_BUTTON = """
  <button id="send" onclick="document.body.insertAdjacentHTML('beforeend',
    '<p>Tak for din ansøgning</p>')">Ansøg</button>
"""


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    return playwright, browser, browser.new_page()


def test_stored_answers_fill_the_screening_questions():
    playwright, browser, page = _page()
    try:
        page.set_content(QUESTIONS + SUBMIT_BUTTON)
        report = lidl_apply.fill_answers(page, PROFILE)
        assert report["unanswered"] == []
        assert page.get_attribute("#q1ja", "aria-checked") == "true"   # опыт: да
        assert page.get_attribute("#q2ja", "aria-checked") == "true"   # выходные: да
        assert page.get_attribute("#q3nej", "aria-checked") == "true"  # 06:00: нет
        assert page.get_attribute("#q3ja", "aria-checked") == "false"
    finally:
        browser.close()
        playwright.stop()


def test_question_without_a_stored_answer_is_left_empty():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <div class="sapMRbG" role="radiogroup" aria-labelledby="qx">
            <span id="qx" class="sapMLabel">Er du medlem af en fagforening?</span>
            <div class="sapMRb" id="xja" role="radio" aria-checked="false">Ja</div>
            <div class="sapMRb" id="xnej" role="radio" aria-checked="false">Nej</div>
          </div>
        """ + SUBMIT_BUTTON)
        report = lidl_apply.fill_answers(page, PROFILE)
        assert page.get_attribute("#xja", "aria-checked") == "false"
        assert page.get_attribute("#xnej", "aria-checked") == "false"
        assert report["unanswered"], "незнакомый вопрос обязан попасть в список"
    finally:
        browser.close()
        playwright.stop()


def test_lidl_date_two_year_goal_and_profile_scope_are_filled():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="start">Hvornår kan du tidligst påbegynde dit ansættelsesforhold hos os?</label>
          <input id="start">
          <label for="goal">Hvor ser du dig selv om to år?</label>
          <input id="goal">
          <label id="scope-label">Min profil må gerne tages i betragtning</label>
          <div class="sapMRbG" role="radiogroup" aria-labelledby="scope-label">
            <div class="sapMRb" id="scope-int" role="radio" aria-checked="false">
              Min profil må gerne tages i betragtning til nuværende og fremtidige
              relevante stillinger i Lidl International.
            </div>
            <div class="sapMRb" id="scope-country" role="radio" aria-checked="false"
                 onclick="this.setAttribute('aria-checked','true')">
              Min profil må gerne tages i betragtning til nuværende og fremtidige
              relevante stillinger i mit bopælsland.
            </div>
            <div class="sapMRb" id="scope-own" role="radio" aria-checked="false">
              Jeg vil kun tages i betragtning til de stillinger, jeg selv har søgt.
            </div>
          </div>
        """ + SUBMIT_BUTTON)
        profile = dict(
            PROFILE,
            two_year_goal="Teamleder med mere ansvar",
            lidl_profile_scope="country",
        )
        report = lidl_apply.fill_answers(page, profile)
        assert page.input_value("#start") == "15.08.2026"
        assert page.input_value("#goal") == "Teamleder med mere ansvar"
        assert page.get_attribute("#scope-country", "aria-checked") == "true"
        assert "Min profil må gerne tages" not in " ".join(report["unanswered"])
    finally:
        browser.close()
        playwright.stop()


def test_translated_lidl_consent_controls_use_the_saved_choices():
    """Chrome translation keeps Lidl's real UI5 table/switch structure intact."""
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="goal">Кем вы видите себя через два года?</label>
          <input id="goal">
          <div id="news-row">
            <span class="sapMText talentPoolText">
              Я хочу узнать больше о соответствующих вакансиях и предстоящих
              карьерных возможностях, а также быть в курсе событий в Lidl.</span>
            <div><div id="news-switch" role="switch" class="sapMSwtCont"
                 style="width:48px;height:28px"
                 aria-checked="false"
                 onclick="this.setAttribute('aria-checked','true')">
              <div class="sapMSwt sapMSwtOff"></div>
            </div></div>
          </div>
          <label>Пожалуйста, ознакомьтесь с моим профилем.</label>
          <table><tbody>
            <tr role="row">
              <td><div class="sapMRb" id="scope-int" role="radio" aria-checked="false"
                   style="width:24px;height:24px"></div></td>
              <td><span class="sapMText visibility-option">
                Мой профиль может быть рассмотрен для вакансий Lidl International.
              </span></td>
            </tr>
            <tr role="row">
              <td><div class="sapMRb" id="scope-country" role="radio" aria-checked="false"
                   style="width:24px;height:24px"
                   onclick="this.setAttribute('aria-checked','true')">
                <input type="radio" name="zprofile_visibility_container_selectGroup">
              </div></td>
              <td><span class="sapMText visibility-option">
                Мой профиль может быть рассмотрен для вакансий в стране моего проживания.
              </span></td>
            </tr>
            <tr role="row">
              <td><div class="sapMRb" id="scope-own" role="radio" aria-checked="false"
                   style="width:24px;height:24px"></div></td>
              <td><span class="sapMText visibility-option">
                Моя кандидатура рассматривается только на должности, на которые я подал заявку.
              </span></td>
            </tr>
          </tbody></table>
        """ + SUBMIT_BUTTON)
        profile = dict(
            PROFILE,
            two_year_goal="Businessuddannelse med praktisk erfaring fra Lidl",
            lidl_newsletter="yes",
            lidl_profile_scope="country",
        )
        report = lidl_apply.fill_answers(page, profile)
        assert page.input_value("#goal") == profile["two_year_goal"]
        assert page.get_attribute("#news-switch", "aria-checked") == "true"
        assert page.get_attribute("#scope-country", "aria-checked") == "true"
        assert "цель на два года" in report["filled"]
        assert "новости Lidl" in report["filled"]
        assert "область учёта профиля Lidl" in report["filled"]
    finally:
        browser.close()
        playwright.stop()


def test_submit_refuses_while_a_question_has_no_answer():
    """Ключевая защита: без ответа кнопка Lidl не нажимается вообще."""
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <div class="sapMRbG" role="radiogroup" aria-labelledby="qx">
            <span id="qx" class="sapMLabel">Er du medlem af en fagforening?</span>
            <div class="sapMRb" id="xja" role="radio" aria-checked="false">Ja</div>
            <div class="sapMRb" id="xnej" role="radio" aria-checked="false">Nej</div>
          </div>
        """ + SUBMIT_BUTTON)
        result = lidl_apply.submit(page, PROFILE)
        assert result["state"] == "blocked"
        assert "нет сохранённого ответа" in result["message"]
        assert page.locator("p").count() == 0, "кнопка была нажата!"
    finally:
        browser.close()
        playwright.stop()


def test_submit_refuses_when_the_profile_is_incomplete():
    playwright, browser, page = _page()
    try:
        page.set_content(QUESTIONS + SUBMIT_BUTTON)
        lidl_apply.fill_answers(page, PROFILE)
        thin = dict(PROFILE, email="")
        result = lidl_apply.submit(page, thin)
        assert result["state"] == "blocked"
        assert "email" in result["message"]
        assert page.locator("p").count() == 0, "кнопка была нажата!"
    finally:
        browser.close()
        playwright.stop()


def test_submit_refuses_while_a_required_field_is_empty():
    playwright, browser, page = _page()
    try:
        page.set_content(QUESTIONS + """
          <span id="req-lbl">Ønsket timeløn</span>
          <input id="req" aria-required="true" aria-labelledby="req-lbl">
        """ + SUBMIT_BUTTON)
        lidl_apply.fill_answers(page, PROFILE)
        result = lidl_apply.submit(page, PROFILE)
        assert result["state"] == "blocked"
        assert "Ønsket timeløn" in result["message"]
        assert page.locator("p").count() == 0, "кнопка была нажата!"
    finally:
        browser.close()
        playwright.stop()


def test_submit_sends_and_requires_a_real_receipt():
    playwright, browser, page = _page()
    try:
        page.set_content(QUESTIONS + SUBMIT_BUTTON)
        lidl_apply.fill_answers(page, PROFILE)
        assert lidl_apply.blockers(page, PROFILE) == []
        result = lidl_apply.submit(page, PROFILE)
        assert result["state"] == "submitted"
        assert "квитанц" in result["message"]
    finally:
        browser.close()
        playwright.stop()


def test_click_without_receipt_is_never_called_submitted():
    playwright, browser, page = _page()
    try:
        page.set_content(QUESTIONS + '<button id="send">Ansøg</button>')
        lidl_apply.fill_answers(page, PROFILE)
        result = lidl_apply.submit(page, PROFILE, wait_seconds=5)
        assert result["state"] == "no_receipt"
        assert "не показал квитанцию" in result["message"]
    finally:
        browser.close()
        playwright.stop()


def test_prepare_stops_on_a_changed_form_before_typing_anything():
    """Форма Lidl переделана — prepare бросает SiteChanged и ничего не вводит."""
    from connectors import site_contract

    playwright, browser, page = _page()
    try:
        page.route("**/easyapply*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body="""<html><body>
              <label for="first">Fornavn</label><input id="first">
              <label for="last">Efternavn</label><input id="last">
              <label for="mail">E-mail-adresse</label><input id="mail">
              <label for="phone">Mobilnummer</label><input id="phone">
            </body></html>""",
        ))
        raised = False
        try:
            lidl_apply.prepare(page, "https://ea-lidl.cfapps.x.hana.ondemand.com/easyapply?job=1",
                               PROFILE)
        except site_contract.SiteChanged as changed:
            raised = True
            assert "загрузка CV" in changed.report["missing"]
        assert raised, "изменённая форма обязана останавливать подготовку"
        assert page.input_value("#first") == "", "поля не должны заполняться"
    finally:
        browser.close()
        playwright.stop()


def test_unrecognised_question_blocks_the_click():
    """Lidl показал вопрос, который мы не разобрали — не жмём вообще."""
    playwright, browser, page = _page()
    try:
        page.set_content(QUESTIONS + """
          <label for="__group99">Hvor har du hørt om os?</label>
          <select id="__group99"><option></option><option>Google</option></select>
        """ + SUBMIT_BUTTON)
        lidl_apply.fill_answers(page, PROFILE)
        result = lidl_apply.submit(page, PROFILE)
        assert result["state"] == "blocked"
        assert "не понял" in result["message"]
        assert page.locator("p").count() == 0, "кнопка была нажата!"
    finally:
        browser.close()
        playwright.stop()
