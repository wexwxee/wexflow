"""«Подготовить без отправки» с телефона — прогон, который ничего не отправляет.

Кнопка в Telegram-панели шлёт решение prepare. ПК обязан открыть анкету в том
же режиме, что и кнопка «Подготовить» в приложении: заполнить и остановиться
перед отправкой, не помечая вакансию поданной.
"""
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
