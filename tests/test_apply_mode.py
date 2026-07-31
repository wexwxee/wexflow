"""Два режима подачи и связка «вопрос без ответа → не отправляем».

Приложение существует, чтобы подавать само (режим auto). Но человек должен
иметь возможность оставить последнее слово за собой (режим fill), а ответ,
которого он не давал, не должен подставляться никогда.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as app_module
import form_questions
import settings_store


@pytest.fixture()
def client(tmp_path):
    with mock.patch.object(form_questions, "path", lambda: tmp_path / "questions.json"):
        # loopback-хост: локальный страж режет кросс-сайтовые POST
        yield TestClient(app_module.app, base_url="http://127.0.0.1")


def test_apply_mode_defaults_to_auto_and_switches(client):
    before = settings_store.get_apply_mode()
    try:
        assert settings_store.set_apply_mode("fill") == "fill"
        assert settings_store.get_apply_mode() == "fill"
        response = client.post("/settings/apply-mode", data={"mode": "auto"})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "mode": "auto"}
        assert settings_store.get_apply_mode() == "auto"
        # мусор в поле не должен создавать третий режим
        assert settings_store.set_apply_mode("что-то") == "auto"
    finally:
        settings_store.set_apply_mode(before)


def test_questions_page_shows_pending_and_saves_answer(client):
    form_questions.record(
        [{"text": "Er du villig til at arbejde hver 2. weekend?", "options": ["Ja", "Nej"]}],
        source="lidl", job_title="Butiksassistent - Herlev",
    )
    page = client.get("/questions")
    assert page.status_code == 200
    assert "Er du villig til at arbejde hver 2. weekend?" in page.text
    assert "Ждут ответа" in page.text

    key = form_questions.all_items()[0]["key"]
    saved = client.post("/questions/answer", data={"key": key, "value": "yes"})
    assert saved.status_code == 200
    assert saved.json()["ok"] is True
    assert saved.json()["pending"] == 0
    assert form_questions.answer_for("Er du villig til at arbejde hver 2. weekend?") == "yes"


def test_only_yes_no_answers_are_accepted(client):
    form_questions.record([{"text": "Har du kørekort?"}])
    key = form_questions.all_items()[0]["key"]
    bad = client.post("/questions/answer", data={"key": key, "value": "может быть"})
    assert bad.status_code == 400
    assert form_questions.answer_for("Har du kørekort?") == ""


def test_saved_answer_is_used_by_the_lidl_filler(client):
    """Ответ из приложения важнее ключевых слов: спросили — ответили — подставили."""
    from connectors import lidl_apply

    question = "Er du medlem af en fagforening?"       # ключевыми словами не узнаётся
    form_questions.record([{"text": question, "options": ["Ja", "Nej"]}])
    assert lidl_apply.question_answer(question, {}) == ("", "")

    key = form_questions.all_items()[0]["key"]
    form_questions.set_answer(key, "no")
    assert lidl_apply.question_answer(question, {}) == ("saved", "no")
