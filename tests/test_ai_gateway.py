"""Маршрутизация gateway: режимы провайдеров, безопасный fallback, изоляция аккаунтов."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_gateway
import ai_secrets
from ai_providers import base
from ai_providers.base import AIResult


class _Fake(base.BaseProvider):
    def __init__(self, name, avail, results):
        self.provider_name = name
        self._avail = avail
        self._results = list(results)
        self.calls = 0

    def available(self):
        return self._avail

    @property
    def model_name(self):
        return self.provider_name + "-model"

    def generate_json(self, *a, **k):
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return r


def _ok(provider):
    return AIResult(ok=True, provider=provider, model=provider + "-model",
                    data={"answer": provider}, error_code=base.OK)


def _err(provider, code):
    return AIResult(ok=False, provider=provider, error_code=code, error_message="e")


def _wire(gem, groq):
    return (
        mock.patch.object(ai_gateway, "GeminiProvider", lambda acc, **k: gem),
        mock.patch.object(ai_gateway, "GroqProvider", lambda acc, **k: groq),
        mock.patch.object(ai_gateway, "_consent_ok", return_value=True),
    )


def _run(gem, groq, fn):
    with _wire(gem, groq)[0], _wire(gem, groq)[1], _wire(gem, groq)[2]:
        return fn()


def test_gemini_only_routes_to_gemini():
    gem = _Fake("gemini", True, [_ok("gemini")])
    groq = _Fake("groq", False, [_ok("groq")])
    res = _run(gem, groq, lambda: ai_gateway.generate_json("x", account_id="a"))
    assert res.ok and res.provider == "gemini"
    assert groq.calls == 0
    assert _run(gem, groq, lambda: ai_gateway.active_provider("a")) == "gemini"


def test_groq_only_routes_to_groq():
    gem = _Fake("gemini", False, [_ok("gemini")])
    groq = _Fake("groq", True, [_ok("groq")])
    res = _run(gem, groq, lambda: ai_gateway.generate_json("x", account_id="a"))
    assert res.ok and res.provider == "groq"
    assert gem.calls == 0


def test_no_keys_returns_not_connected_not_500():
    gem = _Fake("gemini", False, [])
    groq = _Fake("groq", False, [])
    res = _run(gem, groq, lambda: ai_gateway.generate_json("x", account_id="a"))
    assert res.ok is False and res.error_code == base.NOT_CONNECTED
    assert _run(gem, groq, lambda: ai_gateway.available("a")) is False


def test_owner_falls_back_to_groq_on_gemini_daily_limit():
    gem = _Fake("gemini", True, [_err("gemini", base.RATE_LIMIT_RPD)])
    groq = _Fake("groq", True, [_ok("groq")])
    res = _run(gem, groq, lambda: ai_gateway.generate_json("x", account_id="a"))
    assert res.ok and res.provider == "groq"
    assert res.usage.get("fell_back_from") == "gemini"
    assert gem.calls == 1 and groq.calls == 1


def test_owner_does_not_fall_back_on_minute_limit():
    gem = _Fake("gemini", True, [_err("gemini", base.RATE_LIMIT_RPM)])
    groq = _Fake("groq", True, [_ok("groq")])
    res = _run(gem, groq, lambda: ai_gateway.generate_json("x", account_id="a"))
    assert res.ok is False and res.provider == "gemini"
    assert groq.calls == 0                                      # минутный лимит != резерв


def test_owner_does_not_fall_back_on_invalid_request():
    gem = _Fake("gemini", True, [_err("gemini", base.INVALID_REQUEST)])
    groq = _Fake("groq", True, [_ok("groq")])
    res = _run(gem, groq, lambda: ai_gateway.generate_json("x", account_id="a"))
    assert res.ok is False and groq.calls == 0


def test_transient_timeout_is_retried_bounded():
    gem = _Fake("gemini", True, [_err("gemini", base.PROVIDER_TIMEOUT), _ok("gemini")])
    groq = _Fake("groq", False, [])
    with mock.patch.object(ai_gateway.time, "sleep"):
        res = _run(gem, groq, lambda: ai_gateway.generate_json("x", account_id="a", retries=1))
    assert res.ok and gem.calls == 2                            # один повтор, затем успех


def test_groq_without_consent_is_not_usable():
    gem = _Fake("gemini", False, [])
    groq = _Fake("groq", True, [_ok("groq")])
    with mock.patch.object(ai_gateway, "GeminiProvider", lambda acc, **k: gem), \
         mock.patch.object(ai_gateway, "GroqProvider", lambda acc, **k: groq), \
         mock.patch.object(ai_secrets, "info", return_value={"consent": False}):
        res = ai_gateway.generate_json("x", account_id="a")
        assert res.error_code == base.NOT_CONNECTED
        assert ai_gateway.active_provider("a") == ""


def test_two_account_isolation_and_legacy_regression():
    store = {("gemini", "owner"): "AIza", ("groq", "owner"): "gskO",
             ("groq", "userB"): "gskB"}

    def keyfn(provider, account_id=None):
        return store.get((provider, account_id), "")

    with mock.patch.object(ai_secrets, "get_api_key", side_effect=keyfn), \
         mock.patch.object(ai_secrets, "info", return_value={"consent": True}):
        # owner: и Gemini, и Groq пригодны, основной — Gemini
        assert ai_gateway.active_provider("owner") == "gemini"
        owner_names = [n for n, _ in ai_gateway._usable_providers("owner")]
        assert owner_names == ["gemini", "groq"]
        # userB: только Groq; Gemini владельца (даже legacy) недоступен
        assert ai_gateway.active_provider("userB") == "groq"
        b_names = [n for n, _ in ai_gateway._usable_providers("userB")]
        assert "gemini" not in b_names
