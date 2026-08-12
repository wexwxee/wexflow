"""Мультивыбор для Lidl: пачка идёт своим воркером, барьеры остаются на месте.

Правила, которые тут закреплены:
- карточка Lidl показывает флажок выбора, чужие коннекторы — нет;
- пачка Lidl уходит в свой воркер (одно окно, вакансии по очереди), а не в
  воркер Salling, который знает только формы Salling;
- смешанный выбор запускает оба воркера, но строго по очереди;
- в пачку Lidl попадают только настоящие анкеты EasyApply;
- «уже подано» и руководящие отсеиваются для Lidl так же, как для Salling.

Запуск:  python tests/test_lidl_batch.py   (или pytest)
"""
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from connectors import apply_dispatch

LIDL_URL = "https://ea-lidl.cfapps.eu10.hana.ondemand.com/easyapply/?id=42"


def _job(source="lidl", status="new", title="Salgsassistent"):
    return SimpleNamespace(source=source, status=status, title=title, applied_at=None)


def _request():
    return SimpleNamespace(headers={})


def _call(job_ids, mode="submit"):
    """Вызов маршрута без загрузки документов — их проверяет отдельный тест."""
    return app.apply_batch(_request(), job_ids=job_ids, mode=mode,
                           cv_file=None, cover_letter_file=None)


def _batch_stack(stack, snapshot):
    stack.enter_context(mock.patch.object(app, "_load_jobs_snapshot", return_value=snapshot))
    stack.enter_context(mock.patch.object(app, "_claim_apply_slot", return_value=True))
    stack.enter_context(mock.patch.object(app.applications, "mark_submitting"))
    # Площадки здесь считаем доказанными: этот файл про маршрутизацию пачки по
    # воркерам, а правило «первая подача идёт одна» проверяет test_trust.py.
    # Без подмены тесты зависели бы от реальной истории подач на машине.
    stack.enter_context(mock.patch.object(
        app.trust, "stats", side_effect=lambda source: {"proven": True}))


# --- интерфейс ---------------------------------------------------------------

def test_lidl_card_offers_the_selection_checkbox():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert "j.source in ('salling', 'lidl')" in template, (
        "флажок пакетного выбора должен показываться и на карточках Lidl"
    )
    assert "j.source == 'salling'" not in template.split("selwrap")[0][-400:], (
        "старое условие «только Salling» не должно остаться перед флажком"
    )


# --- маршрут пакетной подачи -------------------------------------------------

def test_lidl_only_batch_goes_to_the_connector_worker():
    with ExitStack() as stack:
        _batch_stack(stack, [("lidl:1", _job()), ("lidl:2", _job())])
        salling = stack.enter_context(mock.patch.object(app, "_run_apply_worker"))
        connector = stack.enter_context(mock.patch.object(app, "_launch_connector_batch"))

        response = _call(["lidl:1", "lidl:2"], "submit")

    assert response.status_code == 303
    assert response.headers["location"] == "/?batch=2&mode=submit"
    salling.assert_not_called()
    connector.assert_called_once_with(["lidl:1", "lidl:2"], submit=True)


def test_lidl_batch_is_marked_in_progress_before_the_window_opens():
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            app, "_load_jobs_snapshot", return_value=[("lidl:1", _job())]
        ))
        stack.enter_context(mock.patch.object(app, "_claim_apply_slot", return_value=True))
        marked = stack.enter_context(mock.patch.object(app.applications, "mark_submitting"))
        stack.enter_context(mock.patch.object(app, "_launch_connector_batch"))

        _call(["lidl:1"], "submit")

    marked.assert_called_once_with(["lidl:1"], origin="batch", source="lidl")


def test_mixed_batch_runs_salling_first_then_lidl():
    order = []
    snapshot = [("sal:1", _job(source="salling")), ("lidl:1", _job())]
    with ExitStack() as stack:
        _batch_stack(stack, snapshot)
        stack.enter_context(mock.patch.object(
            app, "_run_apply_worker",
            side_effect=lambda ids, **kw: order.append(("salling", list(ids))),
        ))
        stack.enter_context(mock.patch.object(
            app, "_launch_connector_batch",
            side_effect=lambda ids, **kw: order.append(("lidl", list(ids))),
        ))
        stack.enter_context(mock.patch.object(app, "_wait_for_salling_batch_to_finish"))
        # поток запускаем синхронно, чтобы тест не зависел от планировщика
        stack.enter_context(mock.patch.object(
            app.threading, "Thread",
            side_effect=lambda target, **kw: SimpleNamespace(start=target),
        ))

        _call(["sal:1", "lidl:1"], "submit")

    assert order == [("salling", ["sal:1"]), ("lidl", ["lidl:1"])]


def test_mixed_batch_waits_for_salling_before_opening_lidl():
    order = []
    snapshot = [("sal:1", _job(source="salling")), ("lidl:1", _job())]
    with ExitStack() as stack:
        _batch_stack(stack, snapshot)
        stack.enter_context(mock.patch.object(
            app, "_run_apply_worker", side_effect=lambda ids, **kw: order.append("salling")))
        stack.enter_context(mock.patch.object(
            app, "_wait_for_salling_batch_to_finish", side_effect=lambda *a, **k: order.append("wait")))
        stack.enter_context(mock.patch.object(
            app, "_launch_connector_batch", side_effect=lambda ids, **kw: order.append("lidl")))
        stack.enter_context(mock.patch.object(
            app.threading, "Thread",
            side_effect=lambda target, **kw: SimpleNamespace(start=target),
        ))

        _call(["sal:1", "lidl:1"], "submit")

    assert order == ["salling", "wait", "lidl"], (
        "два заполняющих браузера не должны работать одновременно"
    )


def test_other_connectors_still_cannot_be_submitted_in_a_batch():
    with ExitStack() as stack:
        _batch_stack(stack, [("tt:1", _job(source="teamtailor"))])
        salling = stack.enter_context(mock.patch.object(app, "_run_apply_worker"))
        connector = stack.enter_context(mock.patch.object(app, "_launch_connector_batch"))

        response = _call(["tt:1"], "submit")

    assert "error=" in response.headers["location"]
    salling.assert_not_called()
    connector.assert_not_called()


def test_applied_and_leadership_lidl_jobs_are_filtered_out():
    snapshot = [
        ("lidl:done", _job(status="applied")),
        ("lidl:boss", _job(title="Store Manager")),
    ]
    with ExitStack() as stack:
        _batch_stack(stack, snapshot)
        connector = stack.enter_context(mock.patch.object(app, "_launch_connector_batch"))

        response = _call(["lidl:done", "lidl:boss"], "submit")

    assert "error=" in response.headers["location"]
    connector.assert_not_called()


def test_failed_lidl_launch_is_reported_and_does_not_leave_jobs_in_progress():
    """Окно не открылось — человек видит причину, а заявки не висят «в работе»."""
    with ExitStack() as stack:
        _batch_stack(stack, [("lidl:1", _job())])
        stack.enter_context(mock.patch.object(
            app, "_launch_connector_batch",
            side_effect=RuntimeError("профиль кандидата не найден"),
        ))
        failed = stack.enter_context(mock.patch.object(app.applications, "mark_failed"))

        response = _call(["lidl:1"], "submit")

    assert "error=" in response.headers["location"]
    failed.assert_called_once_with(["lidl:1"], source="lidl")


def test_batch_reserves_every_job_so_a_single_card_cannot_open_a_second_window():
    process = SimpleNamespace(poll=lambda: None, wait=lambda: None)
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(app.subprocess, "Popen", return_value=process))
        stack.enter_context(mock.patch.object(app, "_await_connector_window", return_value="browser_opened"))
        stack.enter_context(mock.patch.object(
            app.threading, "Thread", side_effect=lambda **kw: SimpleNamespace(start=lambda: None)
        ))
        app._launch_connector_batch(["lidl:1", "lidl:2"], submit=True)
        try:
            assert app._claim_connector_launch("lidl:2") is False, (
                "пока идёт пачка, отдельная карточка не должна открыть своё окно"
            )
        finally:
            app._release_connector_launch("lidl:1")
            app._release_connector_launch("lidl:2")


def test_salling_worker_still_refuses_lidl_ids():
    """Барьер источника в воркере Salling остаётся последним рубежом."""
    with mock.patch.object(app, "_load_jobs_snapshot", return_value=[("lidl:1", _job())]), \
         mock.patch.object(app.subprocess, "Popen") as popen:
        result = app._run_apply_worker(["lidl:1"], submit=True)

    assert result is None
    popen.assert_not_called()


# --- воркер пачки ------------------------------------------------------------

def test_connector_batch_command_carries_ids_and_submit_flag():
    command = app._connector_batch_cmd(["a", "b"], submit=True)
    assert "--batch" in command or "--worker-connector-batch" in command
    assert command[-1] == "--submit"
    assert "a" in command and "b" in command


def test_connector_batch_command_without_submit_never_asks_to_send():
    assert "--submit" not in app._connector_batch_cmd(["a"], submit=False)


def test_batch_jobs_keeps_only_real_lidl_easyapply_forms():
    jobs = {
        "lidl:1": SimpleNamespace(id="lidl:1", title="Kassemedarbejder", city="Brønshøj",
                                  application_link=LIDL_URL),
        "tt:1": SimpleNamespace(id="tt:1", title="Barista", city="Aarhus",
                                application_link="https://acme.teamtailor.com/jobs/1"),
        "empty": SimpleNamespace(id="empty", title="Без ссылки", city="", application_link=""),
    }

    class _Session:
        def get(self, _model, jid):
            return jobs.get(jid)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    with mock.patch("db.get_session", return_value=_Session()):
        picked = apply_dispatch.batch_jobs(["lidl:1", "tt:1", "empty", "lidl:1", "нет"])

    assert [item["id"] for item in picked] == ["lidl:1"]
    assert picked[0]["url"] == LIDL_URL


def test_worker_handshake_uses_the_first_requested_id():
    """Приложение ждёт статус по первому переданному id — воркер обязан писать
    именно по нему, даже если эта вакансия не прошла отбор."""
    written = []
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(apply_dispatch, "batch_jobs", return_value=[]))
        stack.enter_context(mock.patch.object(
            apply_dispatch, "_write_status",
            side_effect=lambda jid, state, message="": written.append((jid, state)),
        ))
        apply_dispatch.run_batch(["битая:1", "lidl:2"], submit=True)

    assert written and written[0][0] == "битая:1"
    assert written[0][1] == "error"


def test_skipped_jobs_do_not_stay_marked_as_in_progress():
    """Вакансию, не прошедшую отбор, нельзя оставить висеть «в работе»."""
    released = []
    boom = RuntimeError("до браузера в тесте не доходим")
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            apply_dispatch, "batch_jobs",
            return_value=[{"id": "lidl:2", "title": "", "city": "", "url": LIDL_URL}],
        ))
        stack.enter_context(mock.patch.object(apply_dispatch, "_write_status"))
        stack.enter_context(mock.patch.object(
            apply_dispatch, "_mark_batch_failed", side_effect=released.append))
        # Настоящий браузер в тестах не запускаем НИКОГДА: обрываем ровно там,
        # где воркер собрался бы его открыть.
        stack.enter_context(mock.patch(
            "playwright.sync_api.sync_playwright", side_effect=boom))
        # И не трогаем рабочий файл прогресса приложения.
        stack.enter_context(mock.patch("apply._write_progress"))
        stack.enter_context(mock.patch("apply._cloud_progress"))
        try:
            apply_dispatch.run_batch(["битая:1", "lidl:2"], submit=False)
        except RuntimeError as exc:
            assert exc is boom

    assert released == ["битая:1"]


def test_batch_worker_never_submits_without_the_explicit_flag():
    """Прогон «Подготовить» не должен дойти до кнопки Ansøg."""
    import inspect
    source = inspect.getsource(apply_dispatch._prepare_one)
    body = source.split("if not submit:")[1]
    assert "lidl_apply.submit" not in source.split("if not submit:")[0], (
        "отправка не может стоять до проверки режима"
    )
    assert "lidl_apply.submit" in body


def test_batch_receipt_persistence_failure_is_unconfirmed_not_submitted():
    item = {"id": "lidl:persist-failed", "url": LIDL_URL}
    page = mock.Mock()
    result = {"state": "submitted", "message": "Lidl принял заявку."}
    from connectors import lidl_apply

    with mock.patch.object(lidl_apply, "prepare"), \
            mock.patch.object(lidl_apply, "submit", return_value=result), \
            mock.patch.object(
                apply_dispatch, "_record_confirmed_submission", return_value=False,
            ), \
            mock.patch.object(
                apply_dispatch, "_report_receipt_persist_failure",
                return_value=apply_dispatch._RECEIPT_PERSIST_FAILURE,
            ) as report, \
            mock.patch.object(apply_dispatch, "_send_proof_to_chat") as proof, \
            mock.patch.object(apply_dispatch, "_report_phone_status") as green, \
            mock.patch.object(apply_dispatch, "_write_status") as status:
        state, message = apply_dispatch._prepare_one(
            page, item, {}, submit=True,
        )

    assert state == "no_receipt"
    assert message == apply_dispatch._RECEIPT_PERSIST_FAILURE
    report.assert_called_once_with(page, "lidl:persist-failed")
    proof.assert_not_called()
    green.assert_not_called()
    status.assert_not_called()


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"OK   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures
                  else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
