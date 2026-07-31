"""Банк вопросов анкет: спросили — записали, человек ответил — подставляем.

Смысл: приложение должно подаваться само, но ответ за человека не выдумывает.
Значит вопрос обязан доехать до интерфейса, ответ — сохраниться, а подача —
взять его в следующий раз.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import form_questions


@pytest.fixture()
def bank(tmp_path):
    with mock.patch.object(form_questions, "path", lambda: tmp_path / "form_questions.json"):
        yield


def test_new_questions_are_recorded_once(bank):
    asked = [
        {"text": "Er du villig til at arbejde hver 2. weekend?", "options": ["Ja", "Nej"]},
        {"text": "Er du medlem af en fagforening?", "options": ["Ja", "Nej"]},
    ]
    assert form_questions.record(asked, source="lidl", job_title="Butiksassistent") == 2
    # тот же вопрос из другой вакансии — не дубль, а счётчик встреч
    assert form_questions.record(asked, source="lidl") == 0
    rows = form_questions.all_items()
    assert len(rows) == 2
    assert all(row["seen_count"] == 2 for row in rows)
    assert form_questions.pending_count() == 2


def test_same_question_written_differently_is_one_question(bank):
    form_questions.record([{"text": "Kan du møde kl 06.00 om morgenen?"}])
    form_questions.record([{"text": "  KAN DU MØDE KL 06.00 OM MORGENEN?  "}])
    assert len(form_questions.all_items()) == 1


def test_answer_is_saved_and_read_back(bank):
    form_questions.record([{"text": "Har du erfaring med detail?", "options": ["Ja", "Nej"]}])
    key = form_questions.all_items()[0]["key"]
    assert form_questions.set_answer(key, "yes")
    assert form_questions.answer_for("Har du erfaring med detail?") == "yes"
    assert form_questions.pending_count() == 0
    # ответ можно снять — вопрос снова ждёт человека
    form_questions.set_answer(key, "")
    assert form_questions.answer_for("Har du erfaring med detail?") == ""
    assert form_questions.pending_count() == 1


def test_unknown_key_is_not_invented(bank):
    assert form_questions.set_answer("нет-такого", "yes") is False
    assert form_questions.all_items() == []


def test_broken_file_does_not_crash_the_bank(bank):
    form_questions.path().write_text("{это не json", encoding="utf-8")
    assert form_questions.all_items() == []
    assert form_questions.record([{"text": "Kan du arbejde om aftenen?"}]) == 1


def test_questions_are_grouped_by_store_and_role(bank):
    """Магазинов много, а у руководящих ролей анкета своя — группы отдельные."""
    form_questions.record(
        [{"text": "Har du erfaring med (detail) branchen?", "options": ["Ja", "Nej"]}],
        source="lidl", store_label="Lidl", role_kind="regular", job_title="Butiksassistent",
    )
    form_questions.record(
        [{"text": "Har du erfaring med ledelse?", "options": ["Ja", "Nej"]}],
        source="lidl", store_label="Lidl", role_kind="lead", job_title="Souschef",
    )
    form_questions.record(
        [{"text": "Er du villig til at arbejde hver 2. weekend?"}],
        source="salling", store_label="Netto", role_kind="regular",
    )
    stores = {group["label"]: group for group in form_questions.by_store()}
    assert set(stores) == {"Lidl", "Netto"}
    assert len(stores["Lidl"]["items"]) == 1
    assert len(stores["Lidl"]["lead_items"]) == 1
    assert stores["Lidl"]["pending"] == 2
    assert stores["Netto"]["total"] == 1


def test_danish_questions_get_a_russian_reading(bank):
    assert form_questions.translate_ru("Kan du møde kl 06.00 om morgenen?") == \
        "Можешь выходить к 06:00 утра?"
    assert form_questions.translate_ru("Kan du begå dig ubesværet på dansk?") == \
        "Свободно ли ты общаешься на датском?"
    # незнакомую фразу с известным словом объясняем хотя бы в общем
    assert "выходны" in form_questions.translate_ru("Vil du tage en ekstra weekendvagt?").lower()
    # совсем незнакомое не выдумываем
    assert form_questions.translate_ru("Hvilken farve er din bil?") == ""


def test_cloud_payload_carries_translation_and_store(bank):
    form_questions.record([{"text": "Kan du møde kl 06.00 om morgenen?"}],
                          source="lidl", store_label="Lidl", role_kind="lead")
    row = form_questions.cloud_payload()[0]
    assert row["store"] == "Lidl"
    assert row["lead"] is True
    assert row["textRu"] == "Можешь выходить к 06:00 утра?"
    assert row["answer"] == ""
