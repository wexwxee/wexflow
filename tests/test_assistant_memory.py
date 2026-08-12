"""Помощник помнит хвост диалога и получает вторую попытку.

Это то, чего не хватало до 1.4.7: «мне 20 лет» после «что есть рядом» читалось
как первое сообщение и уходило в поиск по тексту вакансий.
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


def _gateway(*json_answers):
    gateway = mock.MagicMock()
    gateway.available.return_value = True
    gateway.generate_json.side_effect = list(json_answers)
    return gateway


def _jobs(*titles, tool="search_jobs"):
    return {
        "ok": True, "kind": "jobs", "tool": tool, "tool_human": "Поиск по ленте",
        "reply": "", "results": [{"title": t} for t in titles],
    }


def test_history_is_cleaned_and_capped():
    raw = [{"role": "me", "text": "x" * 500}] * 20
    turns = assistant.clean_history(raw)
    assert len(turns) == assistant.MAX_HISTORY
    assert len(turns[0]["text"]) == assistant.MAX_HISTORY_CHARS

    mixed = assistant.clean_history([
        "мусор", {"role": "wat", "text": "  привет  "}, {"role": "me", "text": ""},
        {"text": "без роли"},
    ])
    assert mixed == [{"role": "bot", "text": "привет"}, {"role": "bot", "text": "без роли"}]
    assert assistant.clean_history(None) == []


def test_router_and_wording_both_see_the_conversation():
    history = [
        {"role": "me", "text": "что есть рядом"},
        {"role": "bot", "text": "Вот что ближе всего к дому"},
    ]
    gateway = _gateway(
        _Answer({"tool": "nearby_jobs", "args": {"query": "рядом"}}),
        _Answer({"reply": "Убрал детские ставки, вот что рядом."}),
    )
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "run", return_value=_jobs("Kasseassistent")):
        out = assistant.ask("мне 20 лет", history=history)

    assert out["reply"] == "Убрал детские ставки, вот что рядом."
    for call in gateway.generate_json.call_args_list:
        assert "что есть рядом" in call.args[0]


def test_empty_result_gets_exactly_one_second_attempt():
    gateway = _gateway(
        _Answer({"tool": "search_jobs", "args": {"query": "нетто херлев 15 часов"}}),
        _Answer({"tool": "search_jobs", "args": {"query": "нетто"}}),
        _Answer({"reply": "Точного совпадения нет, но вот что нашлось по «нетто»."}),
    )
    results = [_jobs(), _jobs("Kasseassistent")]
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "run", side_effect=results):
        out = assistant.ask("нетто херлев 15 часов")

    assert out["retried"] is True
    assert out["results"] == [{"title": "Kasseassistent"}]
    # Ровно три обращения: выбор, вторая попытка, формулировка. Не цикл.
    assert gateway.generate_json.call_count == 3
    assert "Предыдущий инструмент ничего не нашёл" in \
        gateway.generate_json.call_args_list[1].args[0]


def test_a_second_attempt_that_also_fails_keeps_the_honest_empty_answer():
    gateway = _gateway(
        _Answer({"tool": "search_jobs", "args": {"query": "лондон"}}),
        _Answer({"tool": "search_jobs", "args": {"query": "лондон сити"}}),
        _Answer({"reply": "Ничего похожего в базе нет."}),
    )
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "run", side_effect=[_jobs(), _jobs()]):
        out = assistant.ask("работа в лондоне")

    assert out["results"] == []
    assert "retried" not in out


def test_a_full_answer_is_never_retried():
    gateway = _gateway(
        _Answer({"tool": "search_jobs", "args": {"query": "нетто"}}),
        _Answer({"reply": "Нашёл одну."}),
    )
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "run", return_value=_jobs("Kasseassistent")):
        assistant.ask("нетто")
    assert gateway.generate_json.call_count == 2


def test_without_ai_history_changes_nothing_and_nothing_is_retried():
    gateway = mock.MagicMock()
    gateway.available.return_value = False
    with mock.patch.dict(sys.modules, {"ai_gateway": gateway}), \
            mock.patch.object(assistant, "run", return_value=_jobs()) as run:
        out = assistant.ask("нетто херлев", history=[{"role": "me", "text": "привет"}])
    assert out["used_ai"] is False
    assert run.call_count == 1


def test_the_panel_sends_the_conversation_tail():
    source = (
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "static" / "assistant.js"
    ).read_text(encoding="utf-8")
    assert "history: sent" in source
    assert "history.slice(-6)" in source
