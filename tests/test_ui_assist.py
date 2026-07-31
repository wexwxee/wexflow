"""Кастомные списки и ИИ-редактор текста: безопасный UI/API-контракт."""
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import profile_store
from ai_providers.base import AIResult
from connectors import generic_apply, lidl_apply

ROOT = Path(__file__).resolve().parent.parent


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def test_text_assist_uses_shared_gateway_and_returns_reviewable_result():
    result = AIResult(
        ok=True,
        data={
            "text": "Jeg vil gerne udvikle mig i detailhandlen.",
            "changed": True,
            "explanation": "Исправлена грамматика и слегка упрощена фраза.",
            "detected_source": "ru",
        },
        provider="groq",
        model="qwen",
    )
    with (
        mock.patch.object(app.ai_gateway, "available", return_value=True),
        mock.patch.object(app.ai_gateway, "generate_json", return_value=result) as generate,
        mock.patch.object(app.ai_gateway, "usage_payload", return_value={"connected": True}),
    ):
        response = asyncio.run(app.api_ai_text_assist(_Request({
            "text": "Я хочу развиваться в розничной торговле",
            "source": "ru",
            "target": "da",
            "mode": "polish",
        })))
    payload = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "Jeg vil gerne udvikle mig" in payload
    assert '"changed":true' in payload
    prompt = generate.call_args.args[0]
    assert "never invent" in prompt.lower()
    assert "Candidate text as JSON string" in prompt


def test_text_assist_rejects_bad_pair_before_ai_call():
    with mock.patch.object(app.ai_gateway, "generate_json") as generate:
        response = asyncio.run(app.api_ai_text_assist(_Request({
            "text": "Привет",
            "source": "ru",
            "target": "de",
            "mode": "translate",
        })))
    assert response.status_code == 400
    generate.assert_not_called()


def test_shared_ui_replaces_native_selects_and_requires_preview():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "ui_assist.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "ui_assist.css").read_text(encoding="utf-8")
    assert "ui_assist.js" in base and "ui_assist.css" in base
    assert 'overlayRoot().appendChild(portal)' in js
    assert 'overlayRoot().appendChild(modal)' in js
    assert "/api/ai/text-assist" in js
    assert "data-ai-accept" in js and "Оставить исходный" in js
    assert ".wf-native-select" in css and ".wf-select-portal" in css


def test_account_has_legal_help_searchable_citizenship_and_language_hint():
    html = (ROOT / "templates" / "account.html").read_text(encoding="utf-8")
    assert 'name="citizenship" data-searchable="true"' in html
    assert "Danish Police: criminal record" in html
    assert "Украина и Special Act" in html
    assert "data-ai-writing" in html
    assert 'data-recommended-language="da"' in html
    assert "Не хочу указывать" in html


def test_two_year_goal_expands_to_show_the_full_answer():
    html = (ROOT / "templates" / "account.html").read_text(encoding="utf-8")
    assert html.count('<textarea name="two_year_goal"') == 2
    assert html.count("data-auto-grow") >= 2
    assert "textarea.scrollHeight" in html
    assert "textarea.addEventListener('wf:refresh', resize)" in html
    assert '<input type="text" name="two_year_goal"' not in html


def test_gender_decline_and_citizenship_values_are_validated_for_fillers():
    assert profile_store.clean_answer("gender", "prefer_not_say") == "prefer_not_say"
    assert profile_store.clean_answer("gender", "made_up") == ""
    assert ("Ukraine", "Украина") in profile_store.CITIZENSHIP_OPTIONS
    assert "Ønsker ikke at oplyse" in lidl_apply._GENDER_LABELS["prefer_not_say"]
    assert "prefer not to say" in generic_apply._choice_aliases("prefer_not_say")
