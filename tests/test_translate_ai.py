"""Перевод описания вакансии подключённым ИИ.

Правила, которые тут закреплены:
- ИИ переводит ТОЛЬКО по явной кнопке: фоновый перевод всех вакансий подряд
  сжёг бы бесплатную квоту за один проход;
- если ИИ не ответил, человек всё равно получает перевод обычным движком;
- подпись «Переведено через …» показывает то, чем перевод сделан на самом деле;
- кнопка «с ИИ» появляется, только когда ИИ подключён.

Запуск:  python tests/test_translate_ai.py   (или pytest)
"""
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import translator

HTML = "<p>Er du over 18 år?</p><p>Vi søger en kollega.</p>"


def _no_other_translators(stack):
    """Оставить единственный запасной путь — публичный Google, чтобы тест не
    зависел от того, установлен ли на машине офлайн-переводчик или ключ DeepL."""
    stack.enter_context(mock.patch.object(config, "DEEPL_API_KEY", ""))
    stack.enter_context(mock.patch.object(translator, "_argos_pair_available", return_value=False))
    stack.enter_context(mock.patch.object(
        translator, "_translate_google_to_ru", return_value="<p>обычный</p>"))


def _ai_reply(reply="Тебе есть 18 лет?\n\nМы ищем коллегу.", ok=True, provider="gemini",
              model="gemini-2.5-flash", error=""):
    return SimpleNamespace(ok=ok, reply=reply, provider=provider, model=model,
                           error_message=error)


def test_ai_is_not_used_without_the_explicit_button():
    """Обычный перевод не должен молча тратить квоту ИИ."""
    with ExitStack() as stack:
        generate = stack.enter_context(mock.patch("ai_gateway.generate_text"))
        stack.enter_context(mock.patch.object(translator, "ai_available", return_value=True))
        _no_other_translators(stack)

        html, engine = translator.translate_to_ru_with_engine(HTML)

    assert html == "<p>обычный</p>"
    assert engine == "Google Translate"
    generate.assert_not_called()


def test_ai_translation_is_used_when_asked():
    with ExitStack() as stack:
        generate = stack.enter_context(mock.patch(
            "ai_gateway.generate_text", return_value=_ai_reply()))
        stack.enter_context(mock.patch.object(translator, "ai_available", return_value=True))

        html, engine = translator.translate_to_ru_with_engine(
            HTML, title="Salgsassistent", prefer_ai=True)

    assert "Мы ищем коллегу" in html
    assert html.startswith("<p>")
    assert engine.startswith("ИИ (gemini")
    prompt = generate.call_args.args[0]
    assert "Salgsassistent" in prompt
    assert "Er du over 18" in prompt, "в промпт должен уйти текст вакансии"


def test_ai_prompt_forbids_inventing_text():
    """Человек принимает решение о работе по этому тексту — вольности запрещены."""
    assert "Ничего не добавляй" in translator._AI_PROMPT
    assert "не пересказывай" in translator._AI_PROMPT.replace("\n", " ")


def test_failed_ai_falls_back_to_the_usual_translator():
    with ExitStack() as stack:
        stack.enter_context(mock.patch(
            "ai_gateway.generate_text",
            return_value=_ai_reply(ok=False, reply="", error="лимит исчерпан")))
        stack.enter_context(mock.patch.object(translator, "ai_available", return_value=True))
        _no_other_translators(stack)

        html, engine = translator.translate_to_ru_with_engine(HTML, prefer_ai=True)

    assert html == "<p>обычный</p>"
    assert engine == "Google Translate"


def test_code_fences_are_stripped_from_the_ai_answer():
    with ExitStack() as stack:
        stack.enter_context(mock.patch(
            "ai_gateway.generate_text",
            return_value=_ai_reply(reply="```\nПривет\n```")))
        stack.enter_context(mock.patch.object(translator, "ai_available", return_value=True))

        html, _ = translator.translate_to_ru_with_engine(HTML, prefer_ai=True)

    assert "```" not in html
    assert "Привет" in html


def test_empty_ai_answer_is_not_saved_as_a_translation():
    with ExitStack() as stack:
        stack.enter_context(mock.patch(
            "ai_gateway.generate_text", return_value=_ai_reply(reply="   ")))
        stack.enter_context(mock.patch.object(translator, "ai_available", return_value=True))
        _no_other_translators(stack)

        html, engine = translator.translate_to_ru_with_engine(HTML, prefer_ai=True)

    assert html == "<p>обычный</p>"
    assert engine == "Google Translate"


def test_old_entry_point_still_returns_plain_html():
    """translate_to_ru используют фоновые пути — его сигнатуру не ломаем."""
    with mock.patch.object(translator, "translate_to_ru_with_engine",
                           return_value=("<p>ок</p>", "DeepL")):
        assert translator.translate_to_ru(HTML) == "<p>ок</p>"


def test_detail_page_shows_the_ai_button_only_when_ai_is_connected():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "detail.html"
    ).read_text(encoding="utf-8")
    assert 'name="engine" value="ai"' in template
    assert "{% if translator_ai_available %}" in template
    assert "расходует квоту" in template, "цена ИИ-перевода должна быть названа честно"


def test_route_asks_for_ai_only_when_the_ai_button_was_pressed():
    """Кнопка «с ИИ» и обычная кнопка ведут в один маршрут — различает их engine."""
    seen = []

    def fake(description, *, title="", prefer_ai=False):
        seen.append(prefer_ai)
        return "<p>ок</p>", "DeepL"

    job = SimpleNamespace(description="<p>tekst</p>", title="Kok",
                          description_ru=None, description_ru_engine=None)

    class _Session:
        def get(self, _model, _jid):
            return job

        def add(self, _obj):
            pass

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    import app
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            app.translator, "translate_to_ru_with_engine", side_effect=fake))
        stack.enter_context(mock.patch.object(app, "get_session", return_value=_Session()))
        app.translate_job("job-1", engine="")
        app.translate_job("job-1", engine="ai")

    assert seen == [False, True]
    assert job.description_ru_engine == "DeepL", "движок перевода должен сохраняться"


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"OK   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures
                  else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
