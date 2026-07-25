"""Local, privacy-safe Gemini request meter for the WexFlow UI.

Gemini does not expose a simple "remaining quota" endpoint. WexFlow therefore
counts only requests sent by this installation and compares them with the
user-configured daily limit. The window follows Gemini RPD rules: reset at
midnight Pacific time.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from json_store import atomic_write_json, read_json

DEFAULT_DAILY_LIMIT = 250
MAX_DAILY_LIMIT = 1_000_000
AI_STUDIO_RATE_LIMIT_URL = "https://aistudio.google.com/rate-limit?timeRange=last-28-days"
PATH = config.DATA_DIR / "ai_usage.json"
_THREAD_LOCK = threading.RLock()

try:
    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - tzdata is bundled, fixed offset is only a rescue path.
    _PACIFIC = dt.timezone(dt.timedelta(hours=-8))


@contextmanager
def _file_lock():
    """Cross-process lock: the desktop server and apply worker both use Gemini."""
    lock_path = Path(PATH).with_name(Path(PATH).name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - release target is Windows.
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _window(now: dt.datetime | None = None) -> tuple[str, float]:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    pacific = current.astimezone(_PACIFIC)
    tomorrow = pacific.date() + dt.timedelta(days=1)
    reset = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=_PACIFIC)
    return pacific.date().isoformat(), reset.timestamp()


def _empty_day() -> dict:
    return {
        "requests": 0,
        "success": 0,
        "errors": 0,
        "rate_limited": 0,
        "daily_exhausted": False,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "last_request_at": 0.0,
        "by_model": {},
    }


def _load() -> dict:
    return read_json(PATH, default={}, expected_type=dict) or {}


def _daily_quota_failure(payload: dict | None) -> tuple[bool, int | None]:
    try:
        raw = json.dumps(payload or {}, ensure_ascii=False).casefold()
    except Exception:
        raw = ""
    daily = any(marker in raw for marker in (
        "perday",
        "per_day",
        "per day",
        "requestsperday",
        "requests per day",
        "rpd",
    ))
    observed_limit = None
    if daily:
        match = re.search(r"\blimit\b[^0-9]{0,8}([0-9][0-9_,.]*)", raw)
        if match:
            try:
                observed_limit = int(re.sub(r"\D", "", match.group(1)))
            except ValueError:
                observed_limit = None
    return daily, observed_limit


def record_response(model: str, status_code: int, payload: dict | None = None) -> None:
    """Count one HTTP response from Gemini, including failed quota-consuming calls."""
    key, _ = _window()
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    code = int(status_code or 0)
    daily_exhausted, observed_limit = _daily_quota_failure(payload) if code == 429 else (False, None)
    usage = (payload or {}).get("usageMetadata") or {}

    with _file_lock():
        data = _load()
        days = data.get("days")
        if not isinstance(days, dict):
            days = {}
        day = days.get(key)
        if not isinstance(day, dict):
            day = _empty_day()
        day["requests"] = int(day.get("requests") or 0) + 1
        day["last_request_at"] = now
        if code == 200:
            day["success"] = int(day.get("success") or 0) + 1
        else:
            day["errors"] = int(day.get("errors") or 0) + 1
        if code == 429:
            day["rate_limited"] = int(day.get("rate_limited") or 0) + 1
        if daily_exhausted:
            day["daily_exhausted"] = True

        for target, source in (
            ("prompt_tokens", "promptTokenCount"),
            ("output_tokens", "candidatesTokenCount"),
            ("total_tokens", "totalTokenCount"),
        ):
            try:
                day[target] = int(day.get(target) or 0) + max(0, int(usage.get(source) or 0))
            except (TypeError, ValueError):
                pass

        model_key = str(model or "unknown")[:80]
        by_model = day.get("by_model")
        if not isinstance(by_model, dict):
            by_model = {}
        model_row = by_model.get(model_key)
        if not isinstance(model_row, dict):
            model_row = {"requests": 0, "success": 0, "rate_limited": 0}
        model_row["requests"] = int(model_row.get("requests") or 0) + 1
        if code == 200:
            model_row["success"] = int(model_row.get("success") or 0) + 1
        if code == 429:
            model_row["rate_limited"] = int(model_row.get("rate_limited") or 0) + 1
        by_model[model_key] = model_row
        day["by_model"] = by_model
        days[key] = day
        data["days"] = dict(sorted(days.items())[-8:])
        if observed_limit and 1 <= observed_limit <= MAX_DAILY_LIMIT:
            data["daily_limit"] = observed_limit
            data["limit_detected_from_google"] = True
        atomic_write_json(PATH, data, indent=2)


def set_daily_limit(value: int) -> int:
    limit = max(1, min(int(value), MAX_DAILY_LIMIT))
    with _file_lock():
        data = _load()
        data["daily_limit"] = limit
        data["limit_detected_from_google"] = False
        atomic_write_json(PATH, data, indent=2)
    return limit


def status(now: dt.datetime | None = None) -> dict:
    key, reset_at = _window(now)
    with _file_lock():
        data = _load()
    try:
        limit = int(data.get("daily_limit") or DEFAULT_DAILY_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_DAILY_LIMIT
    limit = max(1, min(limit, MAX_DAILY_LIMIT))
    day = data.get("days", {}).get(key)
    if not isinstance(day, dict):
        day = _empty_day()
    used = max(0, int(day.get("requests") or 0))
    exhausted = bool(day.get("daily_exhausted"))
    remaining = 0 if exhausted else max(0, limit - used)
    percent = 0 if remaining <= 0 else max(1, min(100, int(remaining * 100 / limit)))
    return {
        "day": key,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "percent_remaining": percent,
        "percent_used": 100 - percent,
        "reset_at": reset_at,
        "success": max(0, int(day.get("success") or 0)),
        "errors": max(0, int(day.get("errors") or 0)),
        "rate_limited": max(0, int(day.get("rate_limited") or 0)),
        "daily_exhausted": exhausted,
        "last_request_at": float(day.get("last_request_at") or 0),
        "prompt_tokens": max(0, int(day.get("prompt_tokens") or 0)),
        "output_tokens": max(0, int(day.get("output_tokens") or 0)),
        "total_tokens": max(0, int(day.get("total_tokens") or 0)),
        "by_model": day.get("by_model") if isinstance(day.get("by_model"), dict) else {},
        "limit_detected_from_google": bool(data.get("limit_detected_from_google")),
        "local_estimate": True,
        "rate_limits_url": AI_STUDIO_RATE_LIMIT_URL,
    }
