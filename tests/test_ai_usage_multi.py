"""Мультипровайдерные лимиты: точные заголовки vs оценка, раздельные проценты."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_usage


def _tmp():
    return mock.patch.object(ai_usage, "PATH", Path(tempfile.mkdtemp()) / "ai_usage.json")


def test_groq_requests_headers_are_precise_daily():
    with _tmp():
        ai_usage.record_provider(
            "groq", "qwen/qwen3.6-27b", 200,
            account_id="acctB", fingerprint="fpB",
            usage={"prompt_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            rate_limits={"requests_limit": 1000, "requests_remaining": 900,
                         "tokens_limit": 6000, "tokens_remaining": 4000},
        )
        st = ai_usage.provider_status("groq", "acctB", "fpB")

    assert st["requests"]["precise"] is True
    assert st["requests"]["limit"] == 1000
    assert st["requests"]["remaining"] == 900
    assert st["requests"]["percent_remaining"] == 90
    # минутные токены отдельно и НЕ выдаются за дневной лимит
    assert st["tokens_minute"]["window"] == "minute"
    assert st["tokens_minute"]["remaining"] == 4000
    assert st["tokens_day_local"]["total"] == 150
    assert st["percent_remaining"] == 90


def test_groq_estimate_without_headers_is_not_precise():
    with _tmp():
        ai_usage.record_provider("groq", "qwen/qwen3.6-27b", 200,
                                 account_id="a", fingerprint="f", usage={})
        st = ai_usage.provider_status("groq", "a", "f")
    assert st["requests"]["precise"] is False
    assert st["estimate"] is True
    assert st["tokens_minute"] is None                          # нет заголовков — нет ложной точности


def test_providers_and_accounts_are_not_summed_or_mixed():
    with _tmp():
        ai_usage.record_provider("gemini", "gemini-2.5-flash", 200,
                                 account_id="owner", fingerprint="g1", usage={})
        ai_usage.record_provider("groq", "qwen/qwen3.6-27b", 200,
                                 account_id="owner", fingerprint="q1", usage={})
        ai_usage.record_provider("groq", "qwen/qwen3.6-27b", 200,
                                 account_id="userB", fingerprint="q2", usage={})

        gem = ai_usage.provider_status("gemini", "owner", "g1")
        groq_owner = ai_usage.provider_status("groq", "owner", "q1")
        groq_b = ai_usage.provider_status("groq", "userB", "q2")

    # каждый учитывается отдельно, без смешивания
    assert gem["requests"]["used"] == 1
    assert groq_owner["requests"]["used"] == 1
    assert groq_b["requests"]["used"] == 1
    # чужой fingerprint не виден
    assert ai_usage.provider_status("groq", "userB", "q1")["requests"]["used"] == 0


def test_different_fingerprints_do_not_share_stats():
    with _tmp():
        ai_usage.record_provider("groq", "m", 200, account_id="a", fingerprint="old", usage={})
        ai_usage.record_provider("groq", "m", 200, account_id="a", fingerprint="old", usage={})
        ai_usage.record_provider("groq", "m", 200, account_id="a", fingerprint="new", usage={})
        assert ai_usage.provider_status("groq", "a", "old")["requests"]["used"] == 2
        assert ai_usage.provider_status("groq", "a", "new")["requests"]["used"] == 1


def test_tpd_429_zeroes_provider_percent():
    with _tmp():
        ai_usage.record_provider("groq", "m", 429, account_id="a", fingerprint="f",
                                 error_code="rate_limit_tpd")
        st = ai_usage.provider_status("groq", "a", "f")
    assert st["tpd_exhausted"] is True
    assert st["percent_remaining"] == 0
    assert st["limiting"] == "tokens_day"
    assert st["color"] == "exhausted"


def test_percent_color_thresholds():
    assert ai_usage.percent_color(100) == "green"
    assert ai_usage.percent_color(60) == "green"
    assert ai_usage.percent_color(50) == "normal"
    assert ai_usage.percent_color(25) == "yellow"
    assert ai_usage.percent_color(10) == "red"
    assert ai_usage.percent_color(0) == "exhausted"
    assert ai_usage.percent_color(80, error=True) == "error"


def test_rpd_precise_percent_levels():
    for remaining, expected in ((1000, 100), (500, 50), (250, 25), (100, 10)):
        with _tmp():
            ai_usage.record_provider("groq", "m", 200, account_id="a", fingerprint="f",
                                     rate_limits={"requests_limit": 1000,
                                                  "requests_remaining": remaining})
            st = ai_usage.provider_status("groq", "a", "f")
        assert st["requests"]["percent_remaining"] == expected
