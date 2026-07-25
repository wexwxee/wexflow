"""Провайдеры Groq/Gemini: JSON-режим, разбор ошибок и заголовков, fallback модели."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_secrets
import ai_usage
from ai_providers import base
from ai_providers.groq import GroqProvider, DEFAULT_MODEL, FALLBACK_MODEL
from ai_providers.gemini import GeminiProvider


def _resp(status, payload, headers=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload
    r.headers = headers or {}
    return r


def _groq(monkey_key="gsk_test"):
    p = GroqProvider("acctX")
    return p


def test_groq_json_mode_and_usage_and_headers():
    headers = {
        "X-RateLimit-Limit-Requests": "1000",
        "X-RateLimit-Remaining-Requests": "742",
        "x-ratelimit-reset-requests": "2m30s",
        "X-RateLimit-Limit-Tokens": "6000",
        "X-RateLimit-Remaining-Tokens": "5000",
    }
    payload = {
        "id": "chatcmpl-1",
        "choices": [{"message": {"content": '{"answer": "ok"}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    with mock.patch.object(ai_secrets, "get_api_key", return_value="gsk_test"), \
         mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
         mock.patch.object(ai_usage, "record_provider") as rec, \
         mock.patch("ai_providers.groq.httpx.post", return_value=_resp(200, payload, headers)) as post:
        res = _groq().generate_json("give me json")

    assert res.ok and res.data == {"answer": "ok"}
    assert res.provider == "groq" and res.model == DEFAULT_MODEL
    assert res.usage == {"prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    # requests-заголовки — дневные; tokens-заголовки — минутное окно, не смешиваем
    assert res.rate_limits["requests_limit"] == 1000
    assert res.rate_limits["requests_remaining"] == 742
    assert res.rate_limits["tokens_limit"] == 6000
    assert res.rate_limits["tokens_remaining"] == 5000
    # json_mode: тело содержит response_format
    assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}
    assert post.call_args.kwargs["json"]["model"] == DEFAULT_MODEL
    rec.assert_called_once()


def test_groq_error_normalization():
    cases = {
        401: base.INVALID_KEY,
        403: base.PERMISSION_DENIED,
        404: base.MODEL_NOT_FOUND,
        400: base.INVALID_REQUEST,
        500: base.PROVIDER_UNAVAILABLE,
    }
    p = _groq()
    for code, expected in cases.items():
        ecode, _msg, _retry = p.normalize_error(code, {"error": {"message": "x"}})
        assert ecode == expected, code


def test_groq_429_distinguishes_rpm_rpd_tpm_tpd():
    p = _groq()
    assert p.normalize_error(429, {"error": {"message": "Rate limit reached for requests per minute"}})[0] == base.RATE_LIMIT_RPM
    assert p.normalize_error(429, {"error": {"message": "limit reached for requests per day"}})[0] == base.RATE_LIMIT_RPD
    assert p.normalize_error(429, {"error": {"message": "tokens per minute (TPM) exceeded"}})[0] == base.RATE_LIMIT_TPM
    assert p.normalize_error(429, {"error": {"message": "tokens per day limit reached"}})[0] == base.RATE_LIMIT_TPD


def test_groq_falls_back_qwen_to_gpt_oss_only_on_model_gone():
    ok_payload = {"choices": [{"message": {"content": '{"a":1}'}}], "usage": {}}
    with mock.patch.object(ai_secrets, "get_api_key", return_value="gsk_test"), \
         mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
         mock.patch.object(ai_usage, "record_provider"), \
         mock.patch("ai_providers.groq.httpx.post",
                    side_effect=[_resp(404, {"error": {"message": "model_not_found"}}),
                                 _resp(200, ok_payload)]) as post:
        res = _groq().generate_json("x")
    assert res.ok and res.model == FALLBACK_MODEL
    assert post.call_count == 2
    assert post.call_args_list[0].kwargs["json"]["model"] == DEFAULT_MODEL
    assert post.call_args_list[1].kwargs["json"]["model"] == FALLBACK_MODEL


def test_groq_does_not_switch_model_on_rate_limit():
    with mock.patch.object(ai_secrets, "get_api_key", return_value="gsk_test"), \
         mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
         mock.patch.object(ai_usage, "record_provider"), \
         mock.patch("ai_providers.groq.httpx.post",
                    return_value=_resp(429, {"error": {"message": "requests per minute"}})) as post:
        res = _groq().generate_json("x")
    assert not res.ok and res.error_code == base.RATE_LIMIT_RPM
    assert post.call_count == 1                                  # лимит != смена модели


def test_groq_timeout_and_offline():
    import httpx
    with mock.patch.object(ai_secrets, "get_api_key", return_value="gsk_test"), \
         mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
         mock.patch.object(ai_usage, "record_provider"), \
         mock.patch("ai_providers.groq.httpx.post", side_effect=httpx.TimeoutException("t")):
        res = _groq().generate_json("x")
    assert res.error_code == base.PROVIDER_TIMEOUT
    with mock.patch.object(ai_secrets, "get_api_key", return_value="gsk_test"), \
         mock.patch.object(ai_secrets, "fingerprint", return_value="fp"), \
         mock.patch.object(ai_usage, "record_provider"), \
         mock.patch("ai_providers.groq.httpx.post", side_effect=OSError("down")):
        res = _groq().generate_json("x")
    assert res.error_code == base.OFFLINE


def test_groq_validate_key_uses_models_list_no_generation():
    payload = {"data": [{"id": DEFAULT_MODEL}, {"id": FALLBACK_MODEL}]}
    with mock.patch.object(ai_secrets, "get_api_key", return_value="gsk_test"), \
         mock.patch("ai_providers.groq.httpx.get", return_value=_resp(200, payload)) as get, \
         mock.patch("ai_providers.groq.httpx.post") as post:
        res = _groq().validate_key(use_generation=False)
    assert res.ok and res.data["has_primary"] is True
    assert get.call_count == 1 and post.call_count == 0


def test_gemini_provider_records_legacy_and_multi():
    payload = {
        "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
    }
    with mock.patch.object(ai_secrets, "get_api_key", return_value="AIza"), \
         mock.patch.object(ai_secrets, "fingerprint", return_value="gfp"), \
         mock.patch.object(ai_usage, "record_response") as legacy, \
         mock.patch.object(ai_usage, "record_provider") as multi, \
         mock.patch("ai_providers.gemini.httpx.post", return_value=_resp(200, payload)):
        res = GeminiProvider("acctA").generate_json("hi")
    assert res.ok and res.data == {"ok": True}
    legacy.assert_called_once()                                 # хаб-индикатор 1.3.21 жив
    multi.assert_called_once()                                  # + мультипровайдерный учёт
