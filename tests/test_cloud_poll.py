"""Адаптивный Telegram poller: отзывчивость без шторма запросов."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def test_normal_poll_intervals():
    # Холостой пульс намеренно редкий (TG_IDLE_POLL_SEC) — он жёг лимит команд
    # облачного Redis. Сверяемся с константой, чтобы тест не устаревал при её смене.
    assert app._tg_poll_delay(0, signed_in=True) == app.TG_IDLE_POLL_SEC
    assert app._tg_poll_delay(0, signed_in=False) == 20
    # сразу после работы отвечаем быстро — отзывчивость там, где она нужна
    assert app._tg_poll_delay(0, signed_in=True, had_work=True) == 2


def test_poll_backoff_is_bounded():
    assert [app._tg_poll_delay(n, True) for n in range(1, 6)] == [15, 30, 60, 120, 240]
    assert app._tg_poll_delay(100, True) == 900


def test_cloud_connection_state_is_honest():
    assert app._telegram_cloud_state(False, 0, 0) == "paused"
    assert app._telegram_cloud_state(True, 0, 0) == "checking"
    assert app._telegram_cloud_state(True, 0, 100) == "online"
    assert app._telegram_cloud_state(True, 3, 100) == "offline"


def test_failed_sync_retries_soon_without_request_storm():
    original_time = app.time.time
    original_attempts = dict(app._cloud_sync_attempt_last)
    original_failures = dict(app._cloud_sync_fail_streak)
    clock = [1000.0]
    try:
        app.time.time = lambda: clock[0]
        app._cloud_sync_attempt_last["jobs"] = 0.0
        app._cloud_sync_fail_streak["jobs"] = 0
        assert app._begin_cloud_sync("jobs", 0.0, 300) == 1000.0

        # После неуспеха следующий повтор отодвигается экспоненциально.
        app._finish_cloud_sync("jobs", False)
        clock[0] = 1010.0
        assert app._begin_cloud_sync("jobs", 0.0, 300) is None
        clock[0] = 1060.0
        assert app._begin_cloud_sync("jobs", 0.0, 300) == 1060.0

        # Once successful, the normal five-minute cooldown applies.
        app._finish_cloud_sync("jobs", True)
        clock[0] = 1100.0
        assert app._begin_cloud_sync("jobs", 1060.0, 300) is None
        assert app._begin_cloud_sync("jobs", 1015.0, 300, force=True) == 1100.0
    finally:
        app.time.time = original_time
        app._cloud_sync_attempt_last.clear()
        app._cloud_sync_attempt_last.update(original_attempts)
        app._cloud_sync_fail_streak.clear()
        app._cloud_sync_fail_streak.update(original_failures)


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


def test_logout_while_cloud_request_is_in_flight_wins():
    original_paused = app.account_mod.cloud_sync_paused
    original_fetch = app.cloud_auth.fetch_session
    original_apply = app.account_mod.apply_cloud_session
    original_last = app._tg_session_sync_last
    checks = iter([False, True])
    applied = []
    try:
        app.account_mod.cloud_sync_paused = lambda: next(checks)
        app.cloud_auth.fetch_session = lambda **_kw: {"tgId": "42", "plan": "pro"}
        app.account_mod.apply_cloud_session = lambda user: applied.append(user)
        app._tg_session_sync_last = 0
        app._sync_account_from_cloud()
        assert applied == []
    finally:
        app.account_mod.cloud_sync_paused = original_paused
        app.cloud_auth.fetch_session = original_fetch
        app.account_mod.apply_cloud_session = original_apply
        app._tg_session_sync_last = original_last


if __name__ == "__main__":
    tests = [test_normal_poll_intervals, test_poll_backoff_is_bounded,
             test_cloud_connection_state_is_honest,
             test_failed_sync_retries_soon_without_request_storm,
             test_explicit_logout_blocks_background_relogin,
             test_logout_while_cloud_request_is_in_flight_wins]
    for fn in tests:
        fn()
        print(f"OK   {fn.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")


def test_open_panel_makes_pc_listen_fast():
    """Телефон — пульт: пока панель открыта, ПК опрашивает облако за секунды.
    Без этого команда ждала холостого пульса (до двух минут) и кнопка казалась
    неработающей."""
    assert app._tg_poll_delay(0, signed_in=True, panel_active=True) == app.TG_LIVE_POLL_SEC
    assert app.TG_LIVE_POLL_SEC <= 5
    # закрытая панель возвращает экономный режим
    assert app._tg_poll_delay(0, signed_in=True, panel_active=False) == app.TG_IDLE_POLL_SEC
    # сбой связи важнее живого режима — иначе долбили бы облако каждые 4 c
    assert app._tg_poll_delay(3, signed_in=True, panel_active=True) == 60
