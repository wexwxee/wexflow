"""Тесты опроса исхода отправки (apply._await_submission_outcome).

Фиксируют фикс бага F35: под лагом SAP-формы подтверждение приходит с задержкой.
Прежняя единичная мгновенная проверка возвращала неуспех — applied_at не писался.
Новый опрос ждёт исхода до таймаута и фиксирует успех, как только он появился.

Браузер НЕ запускается: проверки страницы (_submission_confirmed / _ansog_present /
accept_consent) подменяются сценарными функциями, а «страница» — заглушка без пауз.
Счётчик опросов увеличивается на каждом page.wait_for_timeout (один раз за цикл).

Запуск:  python tests/test_submit_outcome.py   (или pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apply


class _FakePage:
    """Заглушка страницы: wait_for_timeout не спит, а считает номер опроса."""
    def __init__(self, box):
        self.box = box

    def wait_for_timeout(self, ms):
        self.box["i"] += 1


def _run(confirmed_fn, ansog_fn, timeout_s=30.0, poll_ms=1000):
    """confirmed_fn(i)->bool, ansog_fn(i)->bool; i — номер опроса (с 0)."""
    box = {"i": 0}
    page = _FakePage(box)
    orig = (apply._submission_confirmed, apply._ansog_present, apply.accept_consent)
    apply._submission_confirmed = lambda p: confirmed_fn(box["i"])
    apply._ansog_present = lambda p: ansog_fn(box["i"])
    apply.accept_consent = lambda p: False
    try:
        return apply._await_submission_outcome(page, timeout_s=timeout_s, poll_ms=poll_ms)
    finally:
        (apply._submission_confirmed, apply._ansog_present, apply.accept_consent) = orig


def test_confirm_immediately():
    # квитанция сразу → успех на первой же проверке
    assert _run(lambda i: True, lambda i: True) is True


def test_confirm_after_lag_is_the_F35_fix():
    # квитанция появляется поздно (лаг), кнопка всё это время видна.
    # Прежний код проверял 1 раз в самом начале и вернул бы False (потеря F35).
    assert _run(lambda i: i >= 10, lambda i: True, timeout_s=30) is True


def test_button_gone_stably_is_success():
    # кнопка исчезает с опроса 2 и остаётся исчезнувшей → успех (форма ушла)
    assert _run(lambda i: False, lambda i: i < 2) is True


def test_button_flicker_is_not_success():
    # кнопка «мигнула» (пропала на 1 опрос и вернулась), квитанции нет →
    # это НЕ успех: к дедлайну кнопка на месте.
    assert _run(lambda i: False, lambda i: i != 3, timeout_s=8) is False


def test_never_submitted_returns_false():
    # ни квитанции, ни ухода формы за весь таймаут → неуспех
    assert _run(lambda i: False, lambda i: True, timeout_s=5) is False


def test_timeout_respected_when_unresolved():
    # маленький таймаут: должен завершиться (не зациклиться) и вернуть False
    assert _run(lambda i: False, lambda i: True, timeout_s=3, poll_ms=1000) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
