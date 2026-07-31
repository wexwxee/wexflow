"""Prepared connector windows stay usable and close only after real inactivity."""
import os
import sys
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

