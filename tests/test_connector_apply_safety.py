"""Connector-assisted apply must never enter the Salling submit worker."""
import os
import sys
import json
import tempfile
from pathlib import Path
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


class _LiveProcess:
    def poll(self):
        return None


class _ClosedProcess:
    def poll(self):
        return 0


def test_connector_filler_waits_for_real_browser_confirmation():
    with tempfile.TemporaryDirectory() as folder:
        status = Path(folder) / "status.json"
        commands = []

        def spawn(cmd, **_kwargs):
            commands.append(cmd)
            status.write_text(json.dumps({
                "job_id": "tt:demo:1", "state": "browser_opened",
            }), encoding="utf-8")
            return _LiveProcess()

        with mock.patch.object(app, "_connector_status_path", return_value=status), \
                mock.patch.object(app.subprocess, "Popen", side_effect=spawn):
            state = app._launch_connector_filler(
                "https://demo.teamtailor.com/jobs/1", "tt:demo:1")
        assert state == "browser_opened"
        assert "tt:demo:1" in commands[0]


def test_lidl_real_submit_mode_is_forwarded_to_worker_explicitly():
    with tempfile.TemporaryDirectory() as folder:
        status = Path(folder) / "status.json"
        commands = []

        def spawn(cmd, **_kwargs):
            commands.append(cmd)
            status.write_text(json.dumps({
                "job_id": "lidl:1", "state": "browser_opened",
            }), encoding="utf-8")
            return _LiveProcess()

        with mock.patch.object(app, "_connector_status_path", return_value=status), \
                mock.patch.object(app.subprocess, "Popen", side_effect=spawn):
            app._launch_connector_filler(
                "https://ea-lidl.cfapps.eu20.hana.ondemand.com/easyapply/"
                "index.html?ReqId=1",
                "lidl:1",
                submit=True,
            )

    assert "--submit" in commands[0]


def test_connector_filler_surfaces_worker_error_instead_of_green_success():
    with tempfile.TemporaryDirectory() as folder:
        status = Path(folder) / "status.json"

        def spawn(_cmd, **_kwargs):
            status.write_text(json.dumps({
                "job_id": "gh:demo:2", "state": "error",
                "message": "Не найден профиль кандидата",
            }, ensure_ascii=False), encoding="utf-8")
            return _LiveProcess()

        with mock.patch.object(app, "_connector_status_path", return_value=status), \
                mock.patch.object(app.subprocess, "Popen", side_effect=spawn):
            try:
                app._launch_connector_filler(
                    "https://boards.greenhouse.io/demo/jobs/2", "gh:demo:2")
            except RuntimeError as exc:
                assert "профиль кандидата" in str(exc)
            else:
                raise AssertionError("worker error was reported as success")


def test_phone_gets_confirmed_only_from_connector_receipt():
    with tempfile.TemporaryDirectory() as folder:
        status = Path(folder) / "status.json"
        status.write_text(json.dumps({
            "job_id": "lidl:confirmed", "state": "submitted",
            "message": "Lidl подтвердил получение заявки.",
        }, ensure_ascii=False), encoding="utf-8")
        app._connector_launches["lidl:confirmed"] = 1.0
        app._connector_processes["lidl:confirmed"] = _LiveProcess()
        with mock.patch.object(app, "_connector_status_path", return_value=status), \
                mock.patch.object(app, "_report_apply_result_safe", return_value=True) as report, \
                mock.patch.object(app, "_sync_applied_to_cloud") as sync:
            app._watch_connector_result_for_phone("lidl:confirmed", "lidl")
        report.assert_called_once_with(
            "lidl:confirmed", "submitted", "Lidl подтвердил получение заявки.",
        )
        sync.assert_called_once_with(force=True)
        assert "lidl:confirmed" not in app._connector_launches
        assert "lidl:confirmed" not in app._connector_processes


def test_phone_gets_unconfirmed_when_connector_closes_without_receipt():
    with tempfile.TemporaryDirectory() as folder:
        status = Path(folder) / "status.json"
        status.write_text(json.dumps({
            "job_id": "lidl:closed", "state": "submit_ready",
        }), encoding="utf-8")
        app._connector_launches["lidl:closed"] = 1.0
        app._connector_processes["lidl:closed"] = _ClosedProcess()
        with mock.patch.object(app, "_connector_status_path", return_value=status), \
                mock.patch.object(app.applications, "mark_failed") as failed, \
                mock.patch.object(app, "_report_apply_result_safe", return_value=True) as report:
            app._watch_connector_result_for_phone("lidl:closed", "lidl")
        failed.assert_called_once_with(["lidl:closed"], source="lidl")
        report.assert_called_once_with(
            "lidl:closed", "failed",
            "Окно закрыто, но сайт не подтвердил получение заявки.",
        )


def test_live_connector_process_cannot_be_reclaimed_after_five_minutes():
    job_id = "lidl:still-open"
    app._connector_launches[job_id] = app.time.monotonic() - 360
    app._connector_processes[job_id] = _LiveProcess()
    try:
        assert app._claim_connector_launch(job_id) is False
    finally:
        app._release_connector_launch(job_id)


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
