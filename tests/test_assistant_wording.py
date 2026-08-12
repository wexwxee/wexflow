"""ИИ пишет слова, приложение отвечает за факты.

Граница проверяется в обе стороны: с ключом ответ звучит по-человечески, без
ключа помощник работает ровно как раньше, а карточки вакансий модель не может
ни создать, ни изменить ни при каких обстоятельствах.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assistant


class _Answer:
    def __init__(self, data, ok=True):
        self.ok = ok
        self.data = data


def _tool_result(**extra):
    base = {
        "ok": True, "kind": "jobs", "tool": "search_jobs",
        "tool_human": "Поиск по ленте",
        "reply": "",
        "results": [{"kind": "job", "title": "Kasseassistent", "subtitle": "Netto · Herlev"}],
    }
    base.update(extra)
    return base


def _ask(text, answer, *, available=True, route=None):
    gateway = mock.MagicMock()
    gateway.available.return_value = available
    gateway.generate_json.return_value = answer
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "_ai_route", return_value=route), \
            mock.patch.object(assistant, "run", return_value=_tool_result()):
        return assistant.ask(text)


def test_ai_writes_the_words_and_the_cards_stay_from_the_database():
    result = _ask("что есть рядом", _Answer({"reply": "Рядом с домом есть пара касс."}))
    assert result["reply"] == "Рядом с домом есть пара касс."
    assert result["ai_wording"] is True
    # Карточка осталась ровно та, что посчитало приложение.
    assert result["results"] == [
        {"kind": "job", "title": "Kasseassistent", "subtitle": "Netto · Herlev"}
    ]


def test_without_a_key_the_assistant_answers_exactly_as_before():
    result = _ask("что есть рядом", None, available=False)
    assert "ai_wording" not in result
    assert result["results"][0]["title"] == "Kasseassistent"


def test_a_broken_or_empty_model_answer_falls_back_to_the_app_text():
    for answer in (_Answer(None), _Answer({"reply": "   "}), _Answer({}, ok=False),
                   _Answer({"reply": 42})):
        result = _ask("что есть рядом", answer)
        assert "ai_wording" not in result, answer.data


def test_model_failure_never_breaks_the_answer():
    gateway = mock.MagicMock()
    gateway.available.return_value = True
    gateway.generate_json.side_effect = RuntimeError("сеть отвалилась")
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "_ai_route", return_value=None), \
            mock.patch.object(assistant, "run", return_value=_tool_result()):
        result = assistant.ask("что есть рядом")
    assert result["ok"] is True
    assert "ai_wording" not in result


def test_markup_and_links_from_the_model_are_stripped():
    assert assistant._clean_wording(
        "<b>Есть</b> вакансии, смотри https://example.com/fake"
    ) == "Есть вакансии, смотри"
    assert assistant._clean_wording("x" * 900) == "x" * assistant.MAX_REPLY


def test_confirmation_card_keeps_its_exact_promise():
    """Обещание «кнопку жмёшь ты» модель переписывать не вправе."""
    confirm = {
        "ok": True, "kind": "confirm", "tool": "prepare_application",
        "tool_human": "Подготовить заявку",
        "reply": "WexFlow заполнит форму и остановится перед отправкой.",
    }
    gateway = mock.MagicMock()
    gateway.available.return_value = True
    gateway.generate_json.return_value = _Answer({"reply": "Уже отправил заявку!"})
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "_ai_route", return_value=None), \
            mock.patch.object(assistant, "run", return_value=confirm):
        result = assistant.ask("подать")
    assert result["reply"] == "WexFlow заполнит форму и остановится перед отправкой."
    assert gateway.generate_json.called is False


def test_prompt_carries_only_tool_data_and_forbids_invention():
    prompt = assistant._wording_prompt("что есть рядом", _tool_result())
    assert "Kasseassistent" in prompt
    assert "ТОЛЬКО" in prompt
    assert "заявку человек отправляет сам" in prompt
    assert "Netto · Herlev" in prompt


def test_greeting_and_help_do_not_become_a_vacancy_search():
    assert assistant.guess_tool("привет") == ("help", {})
    assert assistant.guess_tool("что ты умеешь") == ("help", {})
    reply = assistant.run("help")["reply"]
    assert "Заявку не отправляю" in reply
    assert "что я умею" not in reply.lower()  # себя в список не включает
    assert assistant.guess_tool("нетто херлев")[0] == "search_jobs"
