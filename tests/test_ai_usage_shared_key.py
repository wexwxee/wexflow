"""Один ключ Google — один счётчик.

12.08.2026 хаб показывал «0 из 20 запросов, 0%», а индикатор рядом — «90%
осталось». Оба читали один файл, но разные его половины: ИИ-фильтры ленты
пишут в общий дневной счётчик, провайдер помощника — в свой. В тот день Google
уже отвечал RESOURCE_EXHAUSTED, а индикатор звал пользоваться ИИ дальше.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_usage


def _with_usage(payload: dict):
    tmp = Path(tempfile.mkdtemp()) / "ai_usage.json"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    return mock.patch.object(ai_usage, "PATH", tmp)


def _day_key() -> str:
    return ai_usage._window()[0]


def test_filters_traffic_counts_against_the_assistant_provider():
    key = _day_key()
    payload = {
        "daily_limit": 20,
        "days": {key: {"requests": 35, "success": 28, "rate_limited": 6,
                       "daily_exhausted": True}},
        "providers": {"acc": {"gemini": {"fp": {"days": {key: {"requests": 2}}}}}},
    }
    with _with_usage(payload):
        hub = ai_usage.status()
        card = ai_usage.provider_status("gemini", "acc", "fp")

    # Хаб говорил правду и раньше.
    assert hub["remaining"] == 0 and hub["percent_remaining"] == 0
    # Теперь и карточка провайдера показывает то же самое.
    assert card["requests"]["used"] == 35
    assert card["requests"]["remaining"] == 0
    assert card["percent_remaining"] == 0


def test_a_calm_day_is_not_spoiled_by_the_shared_counter():
    key = _day_key()
    payload = {
        "daily_limit": 250,
        "days": {key: {"requests": 4, "daily_exhausted": False}},
        "providers": {"acc": {"gemini": {"fp": {"days": {key: {"requests": 3}}}}}},
    }
    with _with_usage(payload):
        card = ai_usage.provider_status("gemini", "acc", "fp")
    assert card["requests"]["used"] == 4          # берём больший из двух
    assert card["requests"]["remaining"] == 246
    assert card["percent_remaining"] > 90


def test_precise_headers_still_lose_to_a_real_exhaustion():
    """Заголовок «осталось 100» не отменяет полученный от Google отказ."""
    key = _day_key()
    payload = {
        "daily_limit": 250,
        "days": {key: {"requests": 40, "daily_exhausted": True}},
        "providers": {"acc": {"gemini": {"fp": {
            "days": {key: {"requests": 1}},
            "rate_limits": {"requests_limit": 250, "requests_remaining": 100},
        }}}},
    }
    with _with_usage(payload):
        card = ai_usage.provider_status("gemini", "acc", "fp")
    assert card["requests"]["remaining"] == 0
    assert card["percent_remaining"] == 0


def test_other_providers_keep_their_own_counter():
    """У Groq свой ключ и свои сутки — общий счётчик Google его не касается."""
    payload = {
        "daily_limit": 20,
        "days": {_day_key(): {"requests": 999, "daily_exhausted": True}},
        "providers": {"acc": {"groq": {"fp": {
            "days": {ai_usage._utc_day()[0]: {"requests": 1}},
        }}}},
    }
    with _with_usage(payload):
        card = ai_usage.provider_status("groq", "acc", "fp")
    assert card["requests"]["used"] == 1
    assert card["percent_remaining"] > 90
