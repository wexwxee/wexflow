"""Адаптивный Telegram poller: отзывчивость без шторма запросов."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def test_normal_poll_intervals():
    assert app._tg_poll_delay(0, signed_in=True) == 6
    assert app._tg_poll_delay(0, signed_in=False) == 15
    assert app._tg_poll_delay(0, signed_in=True, had_work=True) == 2


def test_poll_backoff_is_bounded():
    assert [app._tg_poll_delay(n, True) for n in range(1, 6)] == [6, 12, 24, 48, 60]
    assert app._tg_poll_delay(100, True) == 60


def test_cloud_connection_state_is_honest():
    assert app._telegram_cloud_state(False, 0, 0) == "paused"
    assert app._telegram_cloud_state(True, 0, 0) == "checking"
    assert app._telegram_cloud_state(True, 0, 100) == "online"
    assert app._telegram_cloud_state(True, 3, 100) == "offline"


def test_failed_sync_retries_soon_without_request_storm():
    original_time = app.time.time
    original_attempts = dict(app._cloud_sync_attempt_last)
    clock = [1000.0]
    try:
        app.time.time = lambda: clock[0]
        app._cloud_sync_attempt_last["jobs"] = 0.0
        assert app._begin_cloud_sync("jobs", 0.0, 300) == 1000.0

        # The caller did not move last_success because the request failed.
        clock[0] = 1010.0
        assert app._begin_cloud_sync("jobs", 0.0, 300) is None
        clock[0] = 1015.0
        assert app._begin_cloud_sync("jobs", 0.0, 300) == 1015.0

        # Once successful, the normal five-minute cooldown applies.
        clock[0] = 1100.0
        assert app._begin_cloud_sync("jobs", 1015.0, 300) is None
        assert app._begin_cloud_sync("jobs", 1015.0, 300, force=True) == 1100.0
    finally:
        app.time.time = original_time
        app._cloud_sync_attempt_last.clear()
        app._cloud_sync_attempt_last.update(original_attempts)


def test_explicit_logout_blocks_background_relogin():
    original_paused = app.account_mod.cloud_sync_paused
    original_fetch = app.cloud_auth.fetch_session
    original_last = app._tg_session_sync_last
    calls = []
    try:
        app.account_mod.cloud_sync_paused = lambda: True
        app.cloud_auth.fetch_session = lambda **_kw: calls.append(True)
        app._tg_session_sync_last = 0
        app._sync_account_from_cloud()
        assert calls == []
    finally:
        app.account_mod.cloud_sync_paused = original_paused
        app.cloud_auth.fetch_session = original_fetch
        app._tg_session_sync_last = original_last


if __name__ == "__main__":
    tests = [test_normal_poll_intervals, test_poll_backoff_is_bounded,
             test_cloud_connection_state_is_honest,
             test_failed_sync_retries_soon_without_request_storm,
             test_explicit_logout_blocks_background_relogin]
    for fn in tests:
        fn()
        print(f"OK   {fn.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
