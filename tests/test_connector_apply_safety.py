"""Connector-assisted apply must never enter the Salling submit worker."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import Job


def test_salling_worker_barrier_rejects_connector_job():
    connector = Job(id="tt:demo:1", source="teamtailor", title="Demo")
    with mock.patch.object(
        app, "_load_jobs_snapshot", lambda ids: [(ids[0], connector)]
    ), mock.patch.object(
        app.subprocess, "Popen", side_effect=AssertionError("worker launched")
    ):
        assert app._run_apply_worker([connector.id]) is None


def test_connector_filler_rejects_non_http_url():
    with mock.patch.object(
        app.subprocess, "Popen", side_effect=AssertionError("process launched")
    ):
        try:
            app._launch_connector_filler("file:///C:/Windows/System32/calc.exe")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe URL accepted")


def test_connector_crash_does_not_turn_successful_salling_sync_into_failure():
    old_last = app._connector_sync_last
    old_attempt = app._connector_sync_attempt_last
    old_state = dict(app._sync_state)
    try:
        with mock.patch.object(app.scraper, "sync", return_value={"hits": 321}), \
                mock.patch.object(app.connector_sync, "sync", side_effect=RuntimeError("boom")), \
                mock.patch.object(app.autopilot, "scan_and_notify"), \
                mock.patch.object(app.autopilot, "auto_submit_tick"), \
                mock.patch.object(app, "_tg_offer_tick"):
            app._sync_jobs(force_connectors=True)
        assert app._sync_state["sync_failed"] is False
        assert app._sync_state["last_error"] == ""
        assert app._sync_state["last_hits"] == 321
        assert "boom" in app._sync_state["connector_errors"][0]
    finally:
        app._connector_sync_last = old_last
        app._connector_sync_attempt_last = old_attempt
        app._sync_state.clear()
        app._sync_state.update(old_state)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
