"""The motivation sub-toggle must never outlive or bypass the main AI toggle."""
import asyncio
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class _Request:
    def __init__(self, data):
        self._data = data

    async def form(self):
        return self._data


def _body(response):
    return json.loads(response.body.decode("utf-8"))


def test_disabling_main_ai_also_disables_motivation():
    with (
        mock.patch.object(app.settings_store, "set_ai_fill") as set_main,
        mock.patch.object(app.settings_store, "get_ai_fill", return_value=False),
        mock.patch.object(app.settings_store, "get_ai_fill_motivation", return_value=False),
    ):
        response = asyncio.run(app.settings_ai_fill(_Request({"enabled": "0"})))

    set_main.assert_called_once_with(False)
    assert _body(response) == {
        "ok": True,
        "enabled": False,
        "motivation_enabled": False,
    }


def test_settings_store_clears_motivation_in_the_same_mutation():
    data = {"ai_fill": True, "ai_fill_motivation": True}

    def apply_mutation(mutator):
        mutator(data)
        return data

    with mock.patch.object(app.settings_store, "mutate", side_effect=apply_mutation):
        app.settings_store.set_ai_fill(False)

    assert data == {"ai_fill": False, "ai_fill_motivation": False}


def test_motivation_cannot_be_enabled_without_main_ai():
    with (
        mock.patch.object(app.settings_store, "get_ai_fill", return_value=False),
        mock.patch.object(app.settings_store, "set_ai_fill_motivation") as set_motivation,
    ):
        response = asyncio.run(
            app.settings_ai_fill_motivation(_Request({"enabled": "1"}))
        )

    set_motivation.assert_called_once_with(False)
    assert _body(response) == {
        "ok": False,
        "enabled": False,
        "error": "Сначала включи основное ИИ-заполнение.",
    }


def test_ai_controls_live_in_forms_settings_not_account():
    account_source = app.templates.env.loader.get_source(
        app.templates.env, "account.html",
    )[0]
    settings_source = app.templates.env.loader.get_source(
        app.templates.env, "settings.html",
    )[0]

    assert 'id="aiFillToggle"' not in account_source
    assert 'href="/settings/forms"' in account_source
    assert "{% if settings_section == 'forms' %}" in settings_source
    assert 'id="ai-budget"' in settings_source
    assert 'id="aiUsagePercent"' in settings_source
    assert 'id="aiLimitForm"' in settings_source
    assert 'id="aiFillToggle"' in settings_source
    assert "ic.i('edit', 16)" in settings_source
    assert "✍️" not in settings_source


def test_daily_ai_limit_can_be_saved_from_settings():
    usage = {"limit": 500, "remaining": 500, "percent_remaining": 100}
    with mock.patch.object(app.ai_usage, "set_daily_limit", return_value=500) as save, \
            mock.patch.object(app, "_ai_usage_payload", return_value=usage):
        response = asyncio.run(
            app.settings_ai_usage_limit(_Request({"daily_limit": "500"}))
        )

    save.assert_called_once_with(500)
    assert _body(response) == {"ok": True, "usage": usage}
