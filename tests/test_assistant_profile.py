"""Помощник меняет профиль — но только там, где ошибка стоит одной правки.

Личные и юридические поля он не трогает даже по прямой просьбе: они уезжают в
настоящую анкету под именем человека.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assistant
import assistant_profile


def _profile(store: dict):
    """Подменить профиль словарём в памяти — файл Ивана НЕ трогаем."""
    def mutate_profile(updater):
        replacement = updater(store)
        # Обновлятор обычно правит словарь на месте и возвращает его же —
        # чистить store в этом случае значит стереть только что записанное.
        if replacement is not None and replacement is not store:
            store.clear()
            store.update(replacement)
        return dict(store)

    return mock.patch.object(
        assistant_profile.profile_store, "mutate_profile", mutate_profile
    )


def test_soft_field_changes_immediately_and_reports_before_and_after():
    store = {"city": "Herlev"}
    with _profile(store):
        out = assistant.run("update_profile", {"field": "city", "value": "København"})
    assert out["ok"] is True
    assert store["city"] == "København"
    assert "Herlev" in out["reply"] and "København" in out["reply"]
    assert "верни как было" in out["reply"]


def test_hard_fields_are_never_touched_even_when_asked_directly():
    store = {"phone": "+45 11 11 11 11", "email": "ivan@example.com"}
    for field, value in (("phone", "+45 99 99 99 99"), ("email", "new@example.com"),
                         ("cv_path", "C:/other.pdf"), ("citizenship", "DK")):
        with _profile(store):
            out = assistant.run("update_profile", {"field": field, "value": value})
        assert out["ok"] is True          # вежливый ответ, а не ошибка
        assert out["href"] == "/profile"
        assert "не меняю" in out["reply"]
    assert store == {"phone": "+45 11 11 11 11", "email": "ivan@example.com"}


def test_yes_no_and_date_values_are_normalised():
    store = {}
    with _profile(store):
        assistant.run("update_profile", {"field": "work_night", "value": "да, готов"})
        assistant.run("update_profile", {"field": "work_weekends", "value": "нет"})
        assistant.run("update_profile", {"field": "start_date", "value": "01.09.2026"})
    assert store["work_night"] == "yes"
    assert store["work_weekends"] == "no"
    assert store["start_date"] == "2026-09-01"


def test_a_value_that_makes_no_sense_is_refused_without_writing():
    store = {"start_date": "2026-09-01"}
    with _profile(store):
        out = assistant.run("update_profile", {"field": "start_date", "value": "завтра"})
    assert out["ok"] is False
    assert "2026-09-01" in out["reply"]      # подсказали формат
    assert store["start_date"] == "2026-09-01"


def test_undo_returns_the_previous_value():
    store = {"city": "Herlev"}
    with _profile(store):
        assistant.run("update_profile", {"field": "city", "value": "Odense"})
        assert store["city"] == "Odense"
        out = assistant.run("undo_profile", {})
    assert store["city"] == "Herlev"
    assert "Herlev" in out["reply"]


def test_undo_clears_a_field_that_was_empty_before():
    store = {}
    with _profile(store):
        assistant.run("update_profile", {"field": "current_role", "value": "Кассир"})
        assert store["current_role"] == "Кассир"
        assistant.run("undo_profile", {})
    assert store.get("current_role", "") == ""


def test_undo_without_a_change_says_so_plainly():
    assistant._last_profile_change = {}
    out = assistant.run("undo_profile", {})
    assert out["ok"] is True
    assert "ничего в профиле не менял" in out["reply"]


# ── разбор просьбы без ИИ ──────────────────────────────────────────────────

def test_plain_requests_are_understood_without_ai():
    assert assistant.guess_tool("поставь город Копенгаген") == (
        "update_profile", {"field": "city", "value": "копенгаген"})
    assert assistant.guess_tool("измени языки на английский и русский")[0] == "update_profile"
    name, args = assistant.guess_tool("запиши что я готов на ночные смены")
    assert name == "update_profile" and args["field"] == "work_night"
    assert assistant.guess_tool("верни как было") == ("undo_profile", {})


def test_a_sentence_without_a_command_word_is_not_a_profile_change():
    # Иначе рассказ о себе молча переписывал бы профиль.
    assert assistant.guess_tool("в моём городе ничего нет")[0] != "update_profile"
    assert assistant.guess_tool("что есть рядом")[0] == "nearby_jobs"
    assert assistant.guess_tool("нетто херлев")[0] == "search_jobs"


def test_field_names_are_recognised_by_how_people_say_them():
    assert assistant_profile.guess_field("поставь индекс 2700") == "zip"
    assert assistant_profile.guess_field("у меня есть водительские") == "has_drivers_license"
    assert assistant_profile.guess_field("поменяй телефон") == "phone"
    assert assistant_profile.guess_field("найди работу рядом") == ""


def test_every_hard_field_is_outside_the_soft_list():
    assert not set(assistant_profile.HARD_FIELDS) & set(assistant_profile.SOFT_FIELDS)
    for key in ("email", "phone", "cv_path", "date_of_birth", "work_permit"):
        assert key in assistant_profile.HARD_FIELDS


def test_zip_must_look_like_a_postcode():
    value, problem = assistant_profile.clean_value("zip", "abc")
    assert not value and problem
    value, problem = assistant_profile.clean_value("zip", "2700")
    assert value == "2700" and not problem
