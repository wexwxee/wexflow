"""Вопросы анкет объявляются один раз — в profile_store.ANSWER_FIELDS.

Раньше список был переписан ещё в шести местах (два цикла в шаблоне, список
имён в JS-автосохранении, параметры Form() и словарь ответов в app.py, поля
паспорта кандидата, белый список ИИ-дозаполнения). Забытое место ломалось
молча: поле рисовалось в настройках, но не сохранялось — так уже случилось с
warehouse_experience и english_work.

Тест проверяет не текст полей, а именно связность: что бы ни добавили в
ANSWER_FIELDS, оно доезжает до сохранения, до настроек и до выгрузок.

Запуск:  python -m pytest tests/test_answer_fields_single_source.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as app_module
import candidate_passport
import profile_store
from connectors import ai_fill

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "account.html",
)

BASE_PROFILE = {
    "first_name": "Ivan", "last_name": "Test", "email": "ivan@example.com",
    "phone": "+45 12 34 56 78", "address": "Main street 1", "zip": "2300",
    "city": "København", "country": "Danmark",
}


def _sample(field: profile_store.AnswerField) -> str:
    """Правдоподобный ответ нужного вида — как его пришлёт форма настроек."""
    if field.kind == "yesno":
        return "yes"
    if field.kind == "date":
        return "2026-09-01"
    if field.kind.startswith("choice:"):
        return field.kind.removeprefix("choice:").split(",")[0].strip()
    return "ответ из настроек"


@pytest.fixture()
def saved_profile():
    """Подменяем чтение и запись профиля: настоящий файл трогать нельзя."""
    stored: dict = {}

    def _save(data):
        stored.clear()
        stored.update(profile_store.clean_profile(data))

    def _mutate(updater):
        current = dict(BASE_PROFILE)
        changed = updater(current)
        result = changed if changed is not None else current
        _save(result)
        return dict(stored)

    with mock.patch.object(profile_store, "load_profile", lambda: dict(BASE_PROFILE)), \
            mock.patch.object(profile_store, "mutate_profile", _mutate):
        yield stored


@pytest.fixture()
def client():
    # loopback-хост: локальный страж режет кросс-сайтовые POST
    return TestClient(app_module.app, base_url="http://127.0.0.1",
                      follow_redirects=False)


def _form_payload() -> dict:
    payload = dict(BASE_PROFILE)
    payload["zipcode"] = payload.pop("zip")
    payload.update({field.key: _sample(field) for field in profile_store.ANSWER_FIELDS})
    return payload


@pytest.mark.parametrize("url", ["/account/save", "/settings/profile/autosave"])
def test_every_declared_answer_reaches_the_saved_profile(client, saved_profile, url):
    response = client.post(url, data=_form_payload())
    assert response.status_code in (200, 303), response.text
    for field in profile_store.ANSWER_FIELDS:
        expected = profile_store.clean_answer(field.key, _sample(field))
        assert saved_profile.get(field.key) == expected, (
            f"ответ {field.key} не сохранился — поле забыли в обработчике {url}"
        )


def test_settings_draw_every_auto_ui_question_twice(client):
    """Один и тот же вопрос виден и в общем профиле, и в настройках компании."""
    html = client.get("/profile").text
    for key, human in profile_store.AUTO_UI_ANSWERS:
        assert html.count(f'name="{key}"') >= 2, f"вопрос {key} пропал из настроек"
        assert human in html, f"подпись вопроса {key} пропала из настроек"
    # Смысловая разница блоков сохраняется: у компании пустой вариант означает
    # «взять общий ответ», а не «не отвечать».
    assert "Использовать общий ответ" in html and "Не отвечать" in html


def test_template_does_not_keep_its_own_copy_of_the_list():
    template = open(TEMPLATE, encoding="utf-8").read()
    for key, _human in profile_store.AUTO_UI_ANSWERS:
        assert f"'{key}'" not in template, (
            f"{key} снова вписан в шаблон руками — источник правды один"
        )
    # автосохранение отправляет то, что реально есть в форме
    assert "profileForm.querySelectorAll('input[name]" in template


def test_passport_sections_follow_the_declaration():
    assert candidate_passport.QUESTIONNAIRE_FIELDS == \
        profile_store.export_fields("questionnaire")
    assert candidate_passport.SENSITIVE_FIELDS[-4:] == \
        profile_store.export_fields("sensitive")
    assert set(profile_store.YESNO_ANSWER_KEYS) <= candidate_passport._YES_NO_FIELDS
    # ответы, не помеченные на выгрузку, наружу не уходят
    exported = {key for key, _ in candidate_passport.QUESTIONNAIRE_FIELDS} | \
        {key for key, _ in candidate_passport.SENSITIVE_FIELDS}
    assert "relevant_health_condition" not in exported
    assert not any(key.startswith("lidl_") for key in exported)


def test_ai_fill_sees_every_answer_except_legacy():
    whitelist = set(ai_fill._PROFILE_WHITELIST)
    for field in profile_store.ANSWER_FIELDS:
        if field.scope == profile_store.LEGACY:
            assert field.key not in whitelist, "устаревшее поле модели не нужно"
        else:
            assert field.key in whitelist, (
                f"{field.key} не виден ИИ-дозаполнению — список снова разъехался"
            )


def test_scopes_split_the_answers_without_gaps_or_overlap():
    """Деление на общие/компанейские/согласия — смысловое, его легко потерять."""
    shared = set(profile_store.REUSABLE_ANSWER_KEYS)
    local = set(profile_store.COMPANY_LOCAL_KEYS)
    consent = set(profile_store.COMPANY_CONSENT_KEYS)
    assert consent <= local
    assert not shared & local, "ответ не может быть одновременно общим и локальным"
    legacy = {f.key for f in profile_store.ANSWER_FIELDS if f.scope == profile_store.LEGACY}
    assert shared | local | legacy == set(profile_store.ANSWER_KEYS)
    # согласия работодателю не наследуются между компаниями
    resolved = profile_store.resolve_company_answers(
        {**BASE_PROFILE, "answer_reuse_consent": "yes",
         "lidl_newsletter": "yes", "work_weekends": "yes"},
        "netto",
    )
    assert resolved["lidl_newsletter"] == ""
    assert resolved["work_weekends"] == "yes"
