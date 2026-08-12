from __future__ import annotations

import pytest

import scheduler


class _EmptySession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def exec(self, _statement):
        return _Rows()


class _Rows(list):
    def all(self):
        return self


def test_lookback_covers_configured_interval():
    assert scheduler._lookback_minutes(None) == 35
    assert scheduler._lookback_minutes(30) == 35
    assert scheduler._lookback_minutes(60) == 65
    assert scheduler._lookback_minutes(120) == 125


def test_standalone_tick_reports_source_recovery(monkeypatch):
    reports = []
    monkeypatch.setattr(scheduler.scraper, "sync", lambda: {"hits": 321})
    monkeypatch.setattr(scheduler.source_health, "report", lambda *a, **k: reports.append((a, k)))
    monkeypatch.setattr(scheduler, "get_session", lambda: _EmptySession())

    scheduler.job_tick(60)

    assert reports == [(('salling',), {'hits': 321})]


def test_standalone_tick_records_failure_and_reraises(monkeypatch):
    reports = []
    monkeypatch.setattr(
        scheduler.scraper,
        "sync",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(scheduler.source_health, "report", lambda *a, **k: reports.append((a, k)))

    with pytest.raises(RuntimeError, match="offline"):
        scheduler.job_tick(60)

    assert reports == [
        (('salling',), {'hits': None, 'error': 'RuntimeError: offline'})
    ]
