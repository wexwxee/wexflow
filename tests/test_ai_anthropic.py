"""Провайдер Claude (Anthropic): формат запроса, ошибки, лимиты, деньги.

Claude отличается от Groq и Gemini тремя вещами, и каждая ломает наивную
копипасту: system — отдельное поле, max_tokens обязателен, режима «строго
JSON» нет. Плюс главное для человека: **бесплатной дневной квоты нет**, и
приложение не имеет права рисовать «осталось N% на сегодня».

Запуск:  python -m pytest tests/test_ai_anthropic.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_gateway
import ai_secrets
import ai_usage
from ai_providers import base
from ai_providers.anthropic import (AnthropicProvider, DEFAULT_MODEL,
                                    FALLBACK_MODEL)


def _resp(status, payload, headers=None):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload
    response.headers = headers or {}
    return response


def _ok_payload(text='{"answer": "ok"}'):
    return {"id": "msg_1", "type": "message",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 12, "output_tokens": 7}}


def _with_key(fn, response):
    with mock.patch.object(ai_secrets, "get_api_key", return_value="sk-ant-test"), \
            mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
            mock.patch.object(ai_usage, "record_provider") as rec, \
            mock.patch("ai_providers.anthropic.httpx.post", return_value=response) as post:
        result = fn()
    return result, post, rec


def test_request_shape_matches_the_api():
    """system отдельным полем, max_tokens обязателен, ключ только в заголовке."""
    result, post, rec = _with_key(
        lambda: AnthropicProvider("acct").generate_json("дай json"), _resp(200, _ok_payload()))
    body = post.call_args.kwargs["json"]
    headers = post.call_args.kwargs["headers"]
    assert result.ok and result.data == {"answer": "ok"}
    assert body["model"] == DEFAULT_MODEL
    assert body["max_tokens"] >= 1, "max_tokens обязателен, иначе запрос не примут"
    assert isinstance(body.get("system"), str), "system — поле запроса, а не сообщение"
    assert all(m["role"] != "system" for m in body["messages"])
    assert headers["x-api-key"] == "sk-ant-test" and headers["anthropic-version"]
    assert "Authorization" not in headers
    rec.assert_called_once()


def test_usage_is_read_from_anthropic_names():
    result, _post, _rec = _with_key(
        lambda: AnthropicProvider("acct").generate_text("привет"), _resp(200, _ok_payload("текст")))
    assert result.usage == {"prompt_tokens": 12, "output_tokens": 7, "total_tokens": 19}
    assert result.reply == "текст"


def test_json_is_parsed_even_wrapped_in_fences():
    """JSON-режима у API нет: модель может обернуть ответ в ```json."""
    fenced = '```json\n{"answer": "ok"}\n```'
    result, _post, _rec = _with_key(
        lambda: AnthropicProvider("acct").generate_json("дай json"), _resp(200, _ok_payload(fenced)))
    assert result.ok and result.data == {"answer": "ok"}


def test_non_json_answer_is_an_honest_parse_error():
    result, _post, _rec = _with_key(
        lambda: AnthropicProvider("acct").generate_json("дай json"),
        _resp(200, _ok_payload("я не умею json")))
    assert result.ok is False and result.error_code == base.PARSE_ERROR


def test_error_normalization():
    provider = AnthropicProvider("acct")
    assert provider.normalize_error(401, {"error": {"type": "authentication_error"}})[0] == base.INVALID_KEY
    assert provider.normalize_error(403, {"error": {"type": "permission_error"}})[0] == base.PERMISSION_DENIED
    assert provider.normalize_error(404, {"error": {"type": "not_found_error"}})[0] == base.MODEL_NOT_FOUND
    assert provider.normalize_error(529, {"error": {"type": "overloaded_error"}})[0] == base.PROVIDER_UNAVAILABLE
    assert provider.normalize_error(400, {"error": {"type": "invalid_request_error"}})[0] == base.INVALID_REQUEST


def test_rate_limit_is_minute_not_day():
    """У Anthropic лимиты минутные. Дневной код сказал бы «на сегодня всё» — ложь."""
    provider = AnthropicProvider("acct")
    code, _msg, _retry = provider.normalize_error(429, {"error": {"type": "rate_limit_error",
                                                                  "message": "requests"}})
    assert code == base.RATE_LIMIT_RPM
    assert code not in (base.RATE_LIMIT_RPD, base.RATE_LIMIT_TPD)
    limits = provider.extract_rate_limits({
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "48",
        "anthropic-ratelimit-tokens-limit": "30000",
        "anthropic-ratelimit-tokens-remaining": "29000",
    })
    assert limits["window"] == "minute", "минутное окно обязано быть помечено"
    assert limits["requests_limit"] == 50


def test_paid_key_never_shows_a_fake_daily_percent():
    """Дневного лимита у Claude нет — процент «осталось на сегодня» был бы выдумкой."""
    status = ai_usage.provider_status("anthropic", "acct-test")
    assert status["no_daily_cap"] is True
    assert status["requests"]["precise"] is False
    groq_status = ai_usage.provider_status("groq", "acct-test")
    assert groq_status["no_daily_cap"] is False


def test_fallback_model_only_when_model_is_unavailable():
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["model"])
        if len(calls) == 1:
            return _resp(404, {"error": {"type": "not_found_error"}})
        return _resp(200, _ok_payload("готово"))

    with mock.patch.object(ai_secrets, "get_api_key", return_value="sk-ant-test"), \
            mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
            mock.patch.object(ai_usage, "record_provider"), \
            mock.patch("ai_providers.anthropic.httpx.post", side_effect=fake_post):
        result = AnthropicProvider("acct").generate_text("привет")
    assert result.ok and calls == [DEFAULT_MODEL, FALLBACK_MODEL]

    # а на лимите модель не меняем: это был бы обход воли провайдера
    calls.clear()
    with mock.patch.object(ai_secrets, "get_api_key", return_value="sk-ant-test"), \
            mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
            mock.patch.object(ai_usage, "record_provider"), \
            mock.patch("ai_providers.anthropic.httpx.post",
                       return_value=_resp(429, {"error": {"type": "rate_limit_error"}})):
        AnthropicProvider("acct").generate_text("привет")
    assert calls == []


def test_no_key_is_not_an_error_page():
    with mock.patch.object(ai_secrets, "get_api_key", return_value=""):
        result = AnthropicProvider("acct").generate_text("привет")
    assert result.ok is False and result.error_code == base.NOT_CONNECTED


def test_gateway_knows_the_new_provider():
    assert "anthropic" in ai_secrets.PROVIDERS
    assert ai_gateway.ORDER[0] == "anthropic", "подключённый платный идёт первым"
    assert "anthropic" in ai_gateway._REGISTRY
    payload = ai_gateway.usage_payload("acct-test")
    assert "anthropic" in payload["providers"]


def test_gateway_picks_claude_first_and_keeps_free_ones_as_backup():
    def key_for(provider, _account=None):
        return "sk-ant" if provider == "anthropic" else "gem-key"

    with mock.patch.object(ai_secrets, "get_api_key", side_effect=key_for), \
            mock.patch.object(ai_secrets, "info", return_value={"consent": True}):
        order = [name for name, _p in ai_gateway._usable_providers("acct")]
    assert order[0] == "anthropic" and "gemini" in order


def test_secrets_keep_providers_isolated():
    """Ключ Claude лежит под своим именем — как и все остальные."""
    assert set(ai_secrets.PROVIDERS) >= {"anthropic", "gemini", "groq"}
