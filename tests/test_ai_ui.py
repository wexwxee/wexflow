"""Интерфейс ИИ: мастер Groq, карточки провайдеров, sidebar-индикатор, отсутствие ключей в HTML."""
import os
import re
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.requests import Request

import app
import ai_gateway
import ai_secrets

ROOT = Path(__file__).resolve().parent.parent
SECRET_GROQ = "gsk_LIVE_SECRET_VALUE_ABCD"
SECRET_GEMINI = "AIza_LIVE_SECRET_VALUE_WXYZ"


def _request(path="/settings/ai") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path,
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000),
    })


def _usage_payload(groq_connected=True, gemini_connected=False):
    providers = {
        "groq": {
            "provider": "groq", "connected": groq_connected, "source": "stored",
            "mask": "••••ABCD", "fingerprint": "fp1", "consent": True,
            "model": "qwen/qwen3.6-27b", "role": "primary",
            "added_at": 0, "last_checked_at": 0, "last_check_ok": True,
            "usage": {
                "percent_remaining": 74, "color": "green", "limiting": "requests",
                "estimate": False, "reset_at": 0, "last_error_code": "",
                "requests": {"used": 260, "limit": 1000, "remaining": 740, "precise": True},
                "tokens_minute": {"limit": 6000, "remaining": 5200, "percent_remaining": 86,
                                  "precise": True, "window": "minute"},
                "tokens_day_local": {"prompt": 10, "output": 5, "total": 15, "estimate": True},
            } if groq_connected else None,
        },
        "gemini": {
            "provider": "gemini", "connected": gemini_connected, "source": "",
            "mask": "", "fingerprint": "", "consent": False, "model": "",
            "role": "", "added_at": 0, "last_checked_at": 0, "last_check_ok": None,
            "usage": None,
        },
    }
    return {
        "connected": groq_connected or gemini_connected,
        "primary": "groq" if groq_connected else ("gemini" if gemini_connected else ""),
        "active": {"provider": "groq", "model": "qwen/qwen3.6-27b"} if groq_connected else None,
        "compact": ({"provider": "groq", "model": "qwen/qwen3.6-27b", "percent_remaining": 74,
                     "color": "green", "limiting": "requests", "estimate": False,
                     "role": "primary"} if groq_connected else None),
        "providers": providers,
        "legacy": None,
    }


def _render(usage=None):
    usage = usage or _usage_payload()
    with mock.patch.object(app.ai_gateway, "usage_payload", return_value=usage), \
         mock.patch.object(app.ai_gateway, "available", return_value=usage["connected"]), \
         mock.patch.object(app.ai_secrets, "legacy_gemini_key", return_value=""), \
         mock.patch.object(app.ai_secrets, "info", return_value={"connected": False}):
        response = app.settings_ai(_request())
    return response.body.decode("utf-8")


def test_ai_settings_page_renders_wizard_with_exact_groq_steps():
    html = _render(_usage_payload(groq_connected=False))

    assert "ИИ и лимиты" in html
    # шаги мастера совпадают с реальным окном Groq (включая Expiration и Submit)
    for step in ("API Keys", "Create API Key", "Display Name", "WexFlow",
                 "Expiration", "No expiration", "Submit", "только один раз"):
        assert step in html, step
    assert "https://console.groq.com/keys" in html
    assert "Я создал ключ — продолжить" in html
    assert "Проверить и подключить" in html
    # согласие и политика данных
    assert "только необходимые поля профиля" in html
    assert "https://console.groq.com/docs/your-data" in html
    # личный аккаунт, а не общая организация
    assert "свой личный аккаунт Groq" in html and "организацию" in html
    # поле ключа — password, значение не отражается в HTML
    assert 'type="password" id="aiKeyInput"' in html


def test_wizard_explains_provider_versus_model():
    """Qwen — модель внутри Groq, а не отдельный провайдер: это должно быть явно сказано."""
    html = _render(_usage_payload(groq_connected=False))

    assert "Провайдер и модель — разные вещи" in html
    assert "Qwen 3.6" in html
    # у неподключённого провайдера модель тоже показана
    assert "Модель:" in html
    assert "подключаешь" in html.casefold() or "подключать модель не нужно" in html


def test_gemini_can_be_connected_by_any_user():
    """Обычный пользователь тоже может подключить Gemini — не только ссылка «про Gemini»."""
    html = _render(_usage_payload(groq_connected=False))

    assert "Подключить Gemini" in html or "gemini" in html
    assert "https://aistudio.google.com/apikey" in html
    assert "Google AI Studio" in html
    # мастер параметризован обоими провайдерами
    assert '"gemini"' in html and '"groq"' in html


def test_legacy_gemini_binding_is_offered_on_the_card():
    with mock.patch.object(app.ai_gateway, "usage_payload",
                           return_value=_usage_payload(groq_connected=False)), \
         mock.patch.object(app.ai_gateway, "available", return_value=False), \
         mock.patch.object(app.ai_secrets, "legacy_gemini_key", return_value="AIza_legacy"), \
         mock.patch.object(app.ai_secrets, "info", return_value={"connected": False}):
        html = app.settings_ai(_request()).body.decode("utf-8")

    assert "aiBindLegacy" in html
    assert "Привязать старый ключ" in html
    assert "AIza_legacy" not in html          # сам ключ наружу не отдаётся


def test_connected_card_shows_mask_model_and_separate_limits():
    html = _render()

    # маска приходит в JSON-полезной нагрузке (tojson экранирует •  как •)
    assert ("••••ABCD" in html) or ("\\u2022\\u2022\\u2022\\u2022ABCD" in html)
    assert "qwen" in html
    assert "Проверить" in html and "Заменить ключ" in html and "Отключить" in html
    # раздельные лимиты: дневные запросы и минутные токены явно разведены
    assert "Запросы за день" in html
    assert "Токены в минуту" in html
    assert "не дневной" in html                       # TPM не выдаётся за суточный
    assert "локальная оценка WexFlow" in html
    # без заголовков лимит не выдумывается
    assert "станет известен после первого ответа" in html


def test_no_api_key_ever_appears_in_html():
    usage = _usage_payload()
    html = _render(usage)
    assert SECRET_GROQ not in html
    assert SECRET_GEMINI not in html
    assert "gsk_" not in html.replace("gsk_…", "")   # плейсхолдер placeholder допустим
    assert "key_enc" not in html


def test_sidebar_indicator_is_in_shared_shell_not_per_page():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "ai_indicator.js" in base                  # один общий include

    # ни один шаблон страницы не дублирует компонент
    for path in (ROOT / "templates").glob("*.html"):
        if path.name == "base.html":
            continue
        text = path.read_text(encoding="utf-8")
        assert "ai_indicator.js" not in text, path.name


def test_indicator_script_contract():
    js = (ROOT / "static" / "ai_indicator.js").read_text(encoding="utf-8")

    # опрос локального endpoint, без внешних провайдеров
    assert "/api/ai/usage" in js
    assert "api.groq.com" not in js and "generativelanguage" not in js
    # один таймер и пауза при скрытой вкладке
    assert "state.timer" in js and "document.hidden" in js
    assert "__wexflowAiIndicator" in js               # защита от дублей
    # popover: Escape, клик снаружи, доступность
    assert 'e.key === "Escape"' in js
    assert "aria-label" in js and 'role", "dialog"' in js
    # состояние без ключа
    assert "Подключить ИИ" in js
    # пороги цветов
    for color in ("green", "yellow", "red", "exhausted", "error"):
        assert color in js


def test_usage_endpoint_payload_has_no_secret_fields():
    usage = _usage_payload()
    with mock.patch.object(app.ai_gateway, "usage_payload", return_value=usage), \
         mock.patch.object(app.ai_filters, "gemini_available", return_value=False), \
         mock.patch.object(app.ai_usage, "status", return_value={"percent_remaining": 100}):
        payload = app._ai_usage_payload()

    text = repr(payload)
    assert "key_enc" not in text and "gsk_" not in text and "AIza" not in text
    assert payload["ai"]["providers"]["groq"]["mask"] == "••••ABCD"


def test_ai_suggest_without_ai_returns_wizard_not_500():
    with mock.patch.object(app.ai_filters, "available", return_value=False):
        import asyncio

        class _Req:
            async def json(self):
                return {"text": "кассир"}

        result = asyncio.run(app.api_autopilot_ai_suggest(_Req()))
    assert result["ok"] is False
    assert result["error_code"] == "not_connected"
    assert result["setupUrl"] == "/settings/ai"
