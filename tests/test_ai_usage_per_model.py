"""Дневная квота Google — своя у каждой модели.

12.08.2026 Иван заметил: «Gemini вроде используется, а пишет 0 лимитов». Так и
было: один общий флаг гасил индикатор целиком. Упёрлись в дневной лимит
gemini-2.5-flash-lite — интерфейс писал «0% осталось», пока gemini-2.5-flash
спокойно отвечал дальше.
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


def _day() -> str:
    return ai_usage._window()[0]


FLASH = "gemini-2.5-flash"
LITE = "gemini-2.5-flash-lite"


def _payload() -> dict:
    return {
        "daily_limit": 250,
        "days": {_day(): {
            "requests": 35, "daily_exhausted": True,
            "by_model": {
                FLASH: {"requests": 28, "daily_exhausted": False},
                LITE: {"requests": 7, "daily_exhausted": True, "daily_limit": 20},
            },
        }},
    }


def test_a_working_model_is_not_dimmed_by_another_models_limit():
    with _with_usage(_payload()):
        flash = ai_usage.status(model=FLASH)
        lite = ai_usage.status(model=LITE)
    assert flash["daily_exhausted"] is False
    assert flash["used"] == 28
    assert flash["remaining"] > 0 and flash["percent_remaining"] > 0
    assert lite["daily_exhausted"] is True
    assert lite["remaining"] == 0 and lite["limit"] == 20


def test_without_a_model_the_old_shared_answer_is_kept():
    with _with_usage(_payload()):
        shared = ai_usage.status()
    assert shared["daily_exhausted"] is True
    assert shared["used"] == 35


def test_provider_card_follows_the_model_it_actually_uses():
    payload = _payload()
    payload["providers"] = {"acc": {"gemini": {"fp": {"days": {_day(): {"requests": 2}}}}}}
    with _with_usage(payload):
        flash = ai_usage.provider_status("gemini", "acc", "fp", model=FLASH)
        lite = ai_usage.provider_status("gemini", "acc", "fp", model=LITE)
    assert flash["requests"]["used"] == 28
    assert flash["percent_remaining"] > 0, "работающая модель не должна гаснуть"
    assert lite["percent_remaining"] == 0
    assert lite["requests"]["limit"] == 20


def test_a_successful_answer_clears_the_stale_exhaustion_of_that_model():
    payload = _payload()
    with _with_usage(payload) as _patched:
        ai_usage.record_response(LITE, 200, {"usageMetadata": {"totalTokenCount": 10}})
        lite = ai_usage.status(model=LITE)
    assert lite["daily_exhausted"] is False, "модель ответила — значит квота жива"


def test_a_daily_429_marks_only_its_own_model():
    payload = {"daily_limit": 250, "days": {_day(): {"requests": 1, "by_model": {}}}}
    body = {"error": {"message": "Quota exceeded for quota metric "
                                 "'GenerateRequestsPerDayPerProjectPerModel' limit 20"}}
    with _with_usage(payload):
        ai_usage.record_response(LITE, 429, body)
        lite = ai_usage.status(model=LITE)
        flash = ai_usage.status(model=FLASH)
    assert lite["daily_exhausted"] is True
    assert flash["daily_exhausted"] is False


def test_a_minute_limit_is_not_treated_as_a_daily_one():
    payload = {"daily_limit": 250, "days": {_day(): {"requests": 1, "by_model": {}}}}
    body = {"error": {"message": "Quota exceeded for quota metric "
                                 "'GenerateRequestsPerMinutePerProject' limit 15"}}
    with _with_usage(payload):
        ai_usage.record_response(FLASH, 429, body)
        flash = ai_usage.status(model=FLASH)
    assert flash["daily_exhausted"] is False, "минутный лимит — не конец суток"
    assert flash["remaining"] > 0


def _google_429(*violations: tuple[str, str]) -> dict:
    return {"error": {
        "code": 429,
        "message": "You exceeded your current quota.",
        "details": [{
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [
                {"quotaId": quota_id, "quotaMetric":
                    "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                 "quotaValue": value}
                for quota_id, value in violations
            ],
        }],
    }}


def test_a_minute_violation_next_to_a_daily_one_does_not_steal_the_limit():
    """Реальный ответ Google содержит оба ограничения сразу.

    Так и появилось «0 из 20 запросов»: число бралось регуляркой по всему JSON
    и попадало на минутную строку.
    """
    payload = {"days": {_day(): {"requests": 1, "by_model": {}}}}
    body = _google_429(
        ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "20"),
        ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "250"),
    )
    with _with_usage(payload):
        ai_usage.record_response(FLASH, 429, body)
        flash = ai_usage.status(model=FLASH)
    assert flash["daily_exhausted"] is True
    assert flash["limit"] == 250, "дневной лимит, а не минутный"


def test_a_pure_minute_violation_leaves_the_day_alone():
    payload = {"days": {_day(): {"requests": 1, "by_model": {}}}}
    body = _google_429(("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "20"))
    with _with_usage(payload):
        ai_usage.record_response(FLASH, 429, body)
        flash = ai_usage.status(model=FLASH)
    assert flash["daily_exhausted"] is False
    assert flash["limit"] != 20, "минутное число не должно стать дневным лимитом"
