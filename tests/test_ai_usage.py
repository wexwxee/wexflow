"""Local Gemini usage meter: daily window, percentages and quota exhaustion."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_filters
import ai_usage


def test_usage_meter_counts_requests_and_tokens_in_pacific_day():
    with tempfile.TemporaryDirectory() as td, \
            mock.patch.object(ai_usage, "PATH", Path(td) / "ai_usage.json"):
        ai_usage.set_daily_limit(100)
        ai_usage.record_response("gemini-2.5-flash", 200, {
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 8,
                "totalTokenCount": 20,
            },
        })
        current = ai_usage.status()

    assert current["limit"] == 100
    assert current["used"] == 1
    assert current["remaining"] == 99
    assert current["percent_remaining"] == 99
    assert current["success"] == 1
    assert current["total_tokens"] == 20
    reset = dt.datetime.fromtimestamp(current["reset_at"], tz=dt.timezone.utc)
    assert reset > dt.datetime.now(dt.timezone.utc)


def test_daily_429_sets_zero_and_detects_google_limit():
    payload = {
        "error": {
            "message": "Quota exceeded for requests per day, limit: 250",
            "details": [{
                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            }],
        },
    }
    with tempfile.TemporaryDirectory() as td, \
            mock.patch.object(ai_usage, "PATH", Path(td) / "ai_usage.json"):
        ai_usage.record_response("gemini-2.5-flash", 429, payload)
        current = ai_usage.status()

    assert current["daily_exhausted"] is True
    assert current["remaining"] == 0
    assert current["percent_remaining"] == 0
    assert current["limit"] == 250
    assert current["limit_detected_from_google"] is True


def test_minute_429_does_not_fake_daily_exhaustion():
    payload = {"error": {"message": "Requests per minute limit reached. Retry in 20s."}}
    with tempfile.TemporaryDirectory() as td, \
            mock.patch.object(ai_usage, "PATH", Path(td) / "ai_usage.json"):
        ai_usage.set_daily_limit(100)
        ai_usage.record_response("gemini-2.5-flash", 429, payload)
        current = ai_usage.status()

    assert current["daily_exhausted"] is False
    assert current["remaining"] == 99
    assert current["rate_limited"] == 1


def test_shared_gateway_records_each_real_http_response():
    response = mock.Mock(status_code=200)
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": '{"answer":"ok"}'}]}}],
        "usageMetadata": {"totalTokenCount": 11},
    }
    with mock.patch.object(ai_filters, "api_key", return_value="key"), \
            mock.patch.object(ai_filters, "_models_to_try", return_value=["gemini-test"]), \
            mock.patch.object(ai_filters.httpx, "post", return_value=response), \
            mock.patch.object(ai_filters.ai_usage, "record_response") as record:
        result = ai_filters.generate_json("test")

    assert result["ok"] is True
    record.assert_called_once_with("gemini-test", 200, response.json.return_value)
