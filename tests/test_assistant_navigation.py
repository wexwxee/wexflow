"""«Открой вакансии» — это просьба перейти, а не искать.

12.08.2026 такая фраза уходила в поиск по тексту объявлений и возвращала
«ничего не нашлось» дважды подряд: один раз словами ИИ, второй — заготовкой
приложения.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as salling_app
import assistant


def test_open_requests_go_to_navigation_not_search():
    assert assistant.guess_tool("открой вакансии хочу сам посмотреть")[0] == "open_page"
    assert assistant.guess_tool("открой журнал")[0] == "open_page"
    assert assistant.guess_tool("перейди в настройки")[0] == "open_page"
    # Обычный поиск остаётся поиском.
    assert assistant.guess_tool("нетто херлев")[0] == "search_jobs"
    assert assistant.guess_tool("что есть рядом")[0] == "nearby_jobs"


def test_each_page_opens_by_the_words_people_use():
    cases = {
        "открой вакансии": "/",
        "открой ленту": "/",
        "покажи страницу журнал": "/audit",
        "открой мои заявки": "/audit",
        "открой профиль": "/profile",
        "открой автопилот": "/autopilot",
        "открой подачу по ссылке": "/apply-by-link",
        "открой настройки": "/settings",
        "открой кабинет lidl": "/settings/lidl",
    }
    for phrase, href in cases.items():
        out = assistant.run("open_page", {"page": phrase})
        assert out["ok"] is True, phrase
        assert out["href"] == href, phrase
        assert out["button"], phrase


def test_an_unknown_section_lists_what_exists():
    out = assistant.run("open_page", {"page": "открой холодильник"})
    assert out["ok"] is False
    assert "вакансии" in out["reply"] and "журнал заявок" in out["reply"]


def test_every_page_really_exists_in_the_app():
    """Белый список не должен разъехаться с настоящими маршрутами."""
    routes = {getattr(route, "path", "") for route in salling_app.app.routes}
    for href, human, aliases in assistant.PAGES:
        assert href in routes, f"{human}: маршрута {href} нет"
        assert aliases, human


def test_editing_the_profile_wins_over_opening_it():
    """«Открой профиль и поставь город» — это всё-таки правка, а не переход."""
    name, args = assistant.guess_tool("открой профиль и поставь город Оденсе")
    assert name == "update_profile"
    assert args["field"] == "city"


def test_panel_shows_the_empty_hint_only_when_the_app_wrote_the_answer():
    source = (Path(__file__).resolve().parent.parent
              / "static" / "assistant.js").read_text(encoding="utf-8")
    assert "data.empty_hint && !data.ai_wording" in source


def test_job_answers_say_that_page_filters_do_not_apply():
    """Помощник смотрит всю ленту — иначе его находки выглядят как ошибка списка."""
    from unittest import mock

    import feed

    with mock.patch.object(assistant, "_visible_jobs", return_value=[]), \
            mock.patch.object(feed, "hide_barrier", return_value=False):
        out = assistant.run("search_jobs", {"query": "нетто"})
    assert out["scope_note"] == assistant.SCOPE_NOTE
    assert "фильтры страницы" in assistant.SCOPE_NOTE

    source = (Path(__file__).resolve().parent.parent
              / "static" / "assistant.js").read_text(encoding="utf-8")
    # Строку показываем только когда есть что показывать.
    assert "data.scope_note && list.length" in source


def test_wording_prompt_asks_for_informal_russian():
    prompt = assistant._wording_prompt("что есть рядом", {
        "ok": True, "kind": "jobs", "tool_human": "Поиск по ленте", "results": [],
    })
    assert "на «ты»" in prompt
