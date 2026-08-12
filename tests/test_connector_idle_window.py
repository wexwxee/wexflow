"""Prepared connector windows stay usable and close only after real inactivity."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors import apply_dispatch


class _Page:
    def __init__(self, activity_ms):
        self.activity_ms = activity_ms
        self.tracker_calls = 0

    def evaluate(self, script, *_args):
        if "__wexflowLastActivity || Date.now()" in script:
            return self.activity_ms
        if "__wexflowIdleTrackerInstalled" in script:
            self.tracker_calls += 1
        return None


class _PersistentContext:
    """launch_persistent_context has browser=None while its pages are alive."""
    browser = None

    def __init__(self, page):
        self.pages = [page]
        self.init_scripts = []

    def add_init_script(self, script):
        self.init_scripts.append(script)


def test_persistent_context_does_not_close_immediately():
    page = _Page(activity_ms=1_000_000)
    ctx = _PersistentContext(page)

    with mock.patch.object(apply_dispatch.time, "time", return_value=1001.0), \
            mock.patch.object(apply_dispatch.time, "sleep", side_effect=RuntimeError("kept open")), \
            mock.patch.object(apply_dispatch, "_write_status"):
        try:
            apply_dispatch._wait_until_closed(
                ctx, page=page, idle_seconds=300,
            )
        except RuntimeError as exc:
            assert str(exc) == "kept open"
        else:
            raise AssertionError("persistent browser was treated as already closed")

    assert ctx.init_scripts
    assert page.tracker_calls >= 1


def test_prepared_window_closes_after_five_minutes_without_activity():
    page = _Page(activity_ms=1_000_000)
    ctx = _PersistentContext(page)

    with mock.patch.object(apply_dispatch.time, "time", return_value=1300.001), \
            mock.patch.object(apply_dispatch.time, "sleep") as sleep, \
            mock.patch.object(apply_dispatch, "_write_status") as status:
        apply_dispatch._wait_until_closed(
            ctx, page=page, job_id="lidl:idle", idle_seconds=300,
        )

    sleep.assert_not_called()
    status.assert_called_once_with(
        "lidl:idle",
        "idle_closed",
        "Окно закрыто после 5 мин бездействия.",
    )


def test_telegram_cancel_closes_prepared_lidl_without_submit():
    import apply

    page = _Page(activity_ms=1_000_000)
    ctx = _PersistentContext(page)
    with mock.patch.object(apply, "read_phone_decision", return_value="cancel"), \
            mock.patch.object(apply_dispatch.time, "time", return_value=1001.0), \
            mock.patch.object(apply_dispatch, "_report_phone_status", return_value=True) as report, \
            mock.patch.object(apply_dispatch, "_write_status") as status:
        apply_dispatch._wait_until_closed(
            ctx,
            page=page,
            platform="lidl_easy_apply",
            job_id="lidl:telegram-cancel",
            profile={},
        )

    status.assert_called_once_with(
        "lidl:telegram-cancel",
        "prepare_cancelled",
        "Отменено из Telegram — заявка не отправлена.",
        phone_reported=True,
    )
    report.assert_called_once_with(
        "lidl:telegram-cancel",
        "prepare_cancelled",
        "Отменено из Telegram — заявка не отправлена.",
    )


def test_telegram_submit_finishes_the_open_lidl_form():
    import apply
    from connectors import lidl_apply

    page = _Page(activity_ms=1_000_000)
    ctx = _PersistentContext(page)
    profile = {"first_name": "Ivan"}
    result = {"state": "submitted", "message": "Lidl принял заявку."}
    with mock.patch.object(apply, "read_phone_decision", return_value="submit"), \
            mock.patch.object(lidl_apply, "submit", return_value=result) as submit, \
            mock.patch.object(
                apply_dispatch, "_record_confirmed_submission", return_value=True,
            ) as record, \
            mock.patch.object(apply_dispatch, "_send_proof_to_chat", return_value=True) as proof, \
            mock.patch.object(apply_dispatch, "_report_phone_status") as report, \
            mock.patch.object(apply_dispatch, "_write_status") as status, \
            mock.patch.object(apply_dispatch.time, "time", return_value=1001.0):
        apply_dispatch._wait_until_closed(
            ctx,
            page=page,
            platform="lidl_easy_apply",
            job_id="lidl:telegram-submit",
            profile=profile,
        )

    submit.assert_called_once_with(page, profile)
    record.assert_called_once_with("lidl:telegram-submit")
    status.assert_called_once_with(
        "lidl:telegram-submit", "submitted", "Lidl принял заявку.",
        phone_reported=True,
    )
    report.assert_not_called()
    proof.assert_called_once_with(page, "lidl:telegram-submit")


def test_review_card_submit_signal_uses_worker_and_records_receipt():
    from connectors import lidl_apply

    page = _Page(activity_ms=1_000_000)
    ctx = _PersistentContext(page)
    profile = {"first_name": "Ivan"}
    result = {"state": "submitted", "message": "Lidl принял заявку."}
    with mock.patch.object(lidl_apply, "take_explicit_submit_request", return_value=True), \
            mock.patch.object(lidl_apply, "submit", return_value=result) as submit, \
            mock.patch.object(lidl_apply, "show_explicit_submit_result") as show, \
            mock.patch.object(
                apply_dispatch, "_record_confirmed_submission", return_value=True,
            ) as record, \
            mock.patch.object(apply_dispatch, "_send_proof_to_chat", return_value=True) as proof, \
            mock.patch.object(apply_dispatch, "_report_phone_status") as report, \
            mock.patch.object(apply_dispatch, "_write_status") as status, \
            mock.patch.object(apply_dispatch.time, "time", return_value=1001.0):
        apply_dispatch._wait_until_closed(
            ctx,
            page=page,
            platform="lidl_easy_apply",
            job_id="lidl:review-submit",
            profile=profile,
        )

    submit.assert_called_once_with(page, profile)
    show.assert_called_once_with(page, "submitted", "Lidl принял заявку.")
    record.assert_called_once_with("lidl:review-submit")
    status.assert_called_once_with(
        "lidl:review-submit", "submitted", "Lidl принял заявку.",
        phone_reported=True,
    )
    report.assert_not_called()
    proof.assert_called_once_with(page, "lidl:review-submit")


def test_detected_lidl_receipt_is_cleaned_before_telegram_proof():
    from connectors import lidl_apply

    page = _Page(activity_ms=1_000_000)
    ctx = _PersistentContext(page)
    events = []
    with mock.patch.object(
            lidl_apply, "submission_receipt_visible", return_value=True), \
            mock.patch.object(
                lidl_apply,
                "prepare_submission_proof",
                side_effect=lambda _page: events.append("accepted") or True,
            ), \
            mock.patch.object(
                apply_dispatch,
                "_record_confirmed_submission",
                side_effect=lambda _job_id: events.append("recorded") or True,
            ), \
            mock.patch.object(
                apply_dispatch,
                "_send_proof_to_chat",
                side_effect=lambda _page, _job_id: events.append("proof"),
            ), \
            mock.patch.object(apply_dispatch, "_write_status"), \
            mock.patch.object(apply_dispatch.time, "time", return_value=1001.0), \
            mock.patch.object(
                apply_dispatch.time,
                "sleep",
                side_effect=RuntimeError("proof sent"),
            ):
        try:
            apply_dispatch._wait_until_closed(
                ctx,
                page=page,
                platform="lidl_easy_apply",
                job_id="lidl:receipt-proof",
                profile={},
            )
        except RuntimeError as exc:
            assert str(exc) == "proof sent"
        else:
            raise AssertionError("receipt loop did not finish the proof check")

    assert events == ["accepted", "recorded", "proof"]


def test_telegram_receipt_is_not_green_when_local_persistence_fails():
    import apply
    from connectors import lidl_apply

    page = _Page(activity_ms=1_000_000)
    ctx = _PersistentContext(page)
    result = {"state": "submitted", "message": "Lidl принял заявку."}
    with mock.patch.object(apply, "read_phone_decision", return_value="submit"), \
            mock.patch.object(lidl_apply, "submit", return_value=result), \
            mock.patch.object(
                apply_dispatch, "_record_confirmed_submission", return_value=False,
            ) as record, \
            mock.patch.object(
                apply_dispatch, "_notify_terminal", return_value=True,
            ) as notify, \
            mock.patch.object(apply_dispatch, "_send_proof_to_chat") as proof, \
            mock.patch.object(apply_dispatch, "_report_phone_status") as green, \
            mock.patch.object(apply_dispatch, "_write_status") as status, \
            mock.patch.object(apply_dispatch.time, "time", return_value=1001.0):
        apply_dispatch._wait_until_closed(
            ctx,
            page=page,
            platform="lidl_easy_apply",
            job_id="lidl:persist-failed",
            profile={},
        )

    record.assert_called_once_with("lidl:persist-failed")
    notify.assert_called_once_with(
        page,
        "lidl:persist-failed",
        prepared=True,
        note=apply_dispatch._RECEIPT_PERSIST_FAILURE,
        state="unconfirmed",
        message=apply_dispatch._RECEIPT_PERSIST_FAILURE,
    )
    status.assert_called_once_with(
        "lidl:persist-failed",
        "no_receipt",
        apply_dispatch._RECEIPT_PERSIST_FAILURE,
        phone_reported=True,
    )
    proof.assert_not_called()
    green.assert_not_called()


def test_prepared_connector_proof_requests_telegram_buttons():
    page = mock.Mock()
    with mock.patch.object(apply_dispatch, "_proof_to_chat") as proof:
        apply_dispatch._send_prepared_proof_to_chat(page, "lidl:buttons")
    proof.assert_called_once_with(page, "lidl:buttons", prepared=True, ask_send=True)


def test_proof_filename_is_a_real_png_not_a_windows_alt_stream():
    import apply
    import cloud_auth
    import config

    saved = []
    page = mock.Mock()

    def screenshot(*, path, full_page):
        saved.append(Path(path))
        Path(path).write_bytes(b"png")

    page.screenshot.side_effect = screenshot
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(config, "DATA_DIR", Path(tmp)), \
            mock.patch.object(apply, "_proof_photo_b64", return_value="cG5n"), \
            mock.patch.object(cloud_auth, "report_apply_proof", return_value=True):
        assert apply_dispatch._proof_to_chat(
            page, "lidl:727909", prepared=True, note="Проверка"
        ) is True
    assert len(saved) == 1
    assert saved[0].suffix == ".png"
    assert ":" not in saved[0].name
    assert "lidl_727909" in saved[0].name
