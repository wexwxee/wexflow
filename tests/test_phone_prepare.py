"""«Подготовить без отправки» с телефона — прогон, который ничего не отправляет.

Кнопка в Telegram-панели шлёт решение prepare. ПК обязан открыть анкету в том
же режиме, что и кнопка «Подготовить» в приложении: заполнить и остановиться
перед отправкой, не помечая вакансию поданной.
"""
import io
import os
import sys
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import Job


@contextmanager
def _session_for(job):
    yield mock.Mock(get=mock.Mock(return_value=job))


def test_prepare_from_phone_opens_salling_form_without_submitting():
    job = Job(id="salling-prep-1", source="salling", brand="Netto")
    with (
        mock.patch.object(app, "get_session", side_effect=lambda: _session_for(job)),
        mock.patch.object(app.applications, "offered_ids", return_value={job.id}),
        mock.patch.object(app.applications, "listed_ids", return_value=set()),
        mock.patch.object(app, "_launch_salling_apply") as launch,
        mock.patch.object(app.autopilot, "tg_submit_batch") as submit_batch,
        mock.patch.object(app.cloud_auth, "send_digest") as digest,
    ):
        app._handle_tg_decisions([{"jobId": job.id, "action": "prepare"}])

    launch.assert_called_once_with([job.id], submit=False)
    submit_batch.assert_not_called()          # никакой настоящей подачи
    assert "без отправки" in digest.call_args.args[0]


def test_prepare_from_phone_opens_connector_form_without_submitting():
    job = Job(
        id="lidl-prep-1",
        source="lidl",
        brand="Lidl Danmark",
        application_link="https://example.test/lidl-form",
    )
    with (
        mock.patch.object(app, "get_session", side_effect=lambda: _session_for(job)),
        mock.patch.object(app.applications, "offered_ids", return_value={job.id}),
        mock.patch.object(app.applications, "listed_ids", return_value=set()),
        mock.patch.object(app.applications, "mark_submitting") as mark_submitting,
        mock.patch.object(app, "_launch_connector_filler") as launch,
        mock.patch.object(app.cloud_auth, "send_digest"),
    ):
        app._handle_tg_decisions([{"jobId": job.id, "action": "prepare"}])

    # даже у Lidl, где настоящая подача идёт с submit=True, прогон — без отправки
    launch.assert_called_once_with(job.application_link, job.id, submit=False)
    mark_submitting.assert_not_called()       # «подано» нигде не появляется


def test_prepare_respects_offered_gate():
    """F27: открываем без отправки только то, что WexFlow сам показывал."""
    job = Job(id="stranger-1", source="salling")
    with (
        mock.patch.object(app, "get_session", side_effect=lambda: _session_for(job)),
        mock.patch.object(app.applications, "offered_ids", return_value=set()),
        mock.patch.object(app.applications, "listed_ids", return_value=set()),
        mock.patch.object(app, "_launch_salling_apply") as launch,
        mock.patch.object(app.cloud_auth, "send_digest") as digest,
    ):
        app._handle_tg_decisions([{"jobId": job.id, "action": "prepare"}])

    launch.assert_not_called()
    assert "не начался" in digest.call_args.args[0]


# --- отчёт в телефон: пульт должен показывать, что делает ПК (28.07.2026) ---

def test_prepare_reports_progress_to_phone():
    """Нажал «Подготовить» — панель обязана увидеть preparing, а не тишину."""
    job = Job(id="salling-prep-2", source="salling", brand="Netto")
    with (
        mock.patch.object(app, "get_session", side_effect=lambda: _session_for(job)),
        mock.patch.object(app.applications, "offered_ids", return_value={job.id}),
        mock.patch.object(app.applications, "listed_ids", return_value=set()),
        mock.patch.object(app, "_launch_salling_apply"),
        mock.patch.object(app.cloud_auth, "send_digest"),
        mock.patch.object(app, "_report_apply_result_safe") as report,
    ):
        app._handle_tg_decisions([{"jobId": job.id, "action": "prepare"}])

    states = [call.args[1] for call in report.call_args_list]
    assert states == ["preparing"], states


def test_prepare_reports_failure_when_gate_blocks():
    """Отказ по гейту F27 — тоже событие: телефон не должен ждать вечно."""
    job = Job(id="stranger-2", source="salling")
    with (
        mock.patch.object(app, "get_session", side_effect=lambda: _session_for(job)),
        mock.patch.object(app.applications, "offered_ids", return_value=set()),
        mock.patch.object(app.applications, "listed_ids", return_value=set()),
        mock.patch.object(app, "_launch_salling_apply") as launch,
        mock.patch.object(app.cloud_auth, "send_digest"),
        mock.patch.object(app, "_report_apply_result_safe") as report,
    ):
        app._handle_tg_decisions([{"jobId": job.id, "action": "prepare"}])

    launch.assert_not_called()
    assert [call.args[1] for call in report.call_args_list] == ["prepare_failed"]


def test_connector_prepare_watcher_reports_ready_as_prepared(tmp_path):
    """Статус воркера ready → в телефон уходит prepared, а НЕ «подано»."""
    status = tmp_path / "connector_apply_status_x.json"
    status.write_text(
        '{"job_id": "lidl:1", "state": "ready", "message": "Форма подготовлена."}',
        encoding="utf-8",
    )
    with (
        mock.patch.object(app, "_connector_status_path", return_value=status),
        mock.patch.object(app, "_release_connector_launch"),
        mock.patch.object(app, "_report_apply_result_safe") as report,
    ):
        app._watch_connector_prepare_for_phone("lidl:1")

    assert report.call_count == 1
    job_id, state, msg = report.call_args.args
    assert (job_id, state) == ("lidl:1", "prepared")
    assert "Форма подготовлена." in msg


def test_connector_prepare_watcher_reports_error(tmp_path):
    status = tmp_path / "connector_apply_status_y.json"
    status.write_text(
        '{"job_id": "lidl:2", "state": "error", "message": "Профиль не заполнен"}',
        encoding="utf-8",
    )
    with (
        mock.patch.object(app, "_connector_status_path", return_value=status),
        mock.patch.object(app, "_release_connector_launch"),
        mock.patch.object(app, "_report_apply_result_safe") as report,
    ):
        app._watch_connector_prepare_for_phone("lidl:2")

    assert report.call_args.args[1] == "prepare_failed"


def test_dry_run_reports_prepared_states_to_cloud():
    """apply.py в режиме прогона обязан отчитываться в облако (раньше молчал)."""
    import re
    source = io.open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apply.py"),
        encoding="utf-8",
    ).read()
    body = source.split("def run_batch(")[1]
    assert '"preparing"' in body, "прогон не сообщает о старте заполнения"
    assert "_prepare_report(job_error)" in body, "прогон не сообщает итог"
    # сводка прогресса больше не только для реальной подачи
    assert not re.search(r"if submit:\n\s+_cloud_progress\(prog\)", body)


def test_stale_prepare_does_not_open_browser_next_morning():
    """Прогон — действие «здесь и сейчас». Если ПК проснулся через сутки, он не
    должен сам открыть браузер: человек давно не у экрана."""
    import time as _time
    now = _time.time() * 1000
    assert app._tg_prepare_expired({"ts": now - 31 * 60 * 1000}, now) is True
    assert app._tg_prepare_expired({"ts": now - 60 * 1000}, now) is False
    assert app._tg_prepare_expired({}, now) is False        # старое облако без ts

    job = Job(id="salling-prep-old", source="salling", brand="Netto")
    stale = now - 45 * 60 * 1000
    with (
        mock.patch.object(app, "get_session", side_effect=lambda: _session_for(job)),
        mock.patch.object(app.applications, "offered_ids", return_value={job.id}),
        mock.patch.object(app.applications, "listed_ids", return_value=set()),
        mock.patch.object(app, "_launch_salling_apply") as launch,
        mock.patch.object(app.cloud_auth, "send_digest"),
        mock.patch.object(app, "_report_apply_result_safe") as report,
    ):
        app._handle_tg_decisions([{"jobId": job.id, "action": "prepare", "ts": stale}])

    launch.assert_not_called()
    assert report.call_args.args[1] == "prepare_failed"
    assert "устарел" in report.call_args.args[2]
