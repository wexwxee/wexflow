"""Тесты сторожей деградации (шаг 7, Блок 2).

Приложение стоит на чужих недокументированных опорах (лента вакансий Salling,
их форма подачи). Сторожа замечают падение опоры и показывают баннер:
  - «источник вакансий не отвечает» — синк упал или принёс 0 вакансий;
  - «подача N раз подряд не подтвердилась» — вероятно, Salling изменил сайт.

Проверяем чистые функции над снимком состояния — без базы и сети.

Запуск:  python tests/test_health_watchdogs.py   (или pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import applications


def _ids(warns):
    return [w["id"] for w in warns]


def test_healthy_state_no_warnings():
    assert app._health_warnings(last_hits=480, sync_failed=False, fail_streak=0) == []


def test_before_first_sync_no_warnings():
    # приложение только запустилось (last_hits ещё None) — не пугаем зря
    assert app._health_warnings(last_hits=None, sync_failed=False, fail_streak=0) == []


def test_zero_hits_raises_source_warning():
    warns = app._health_warnings(last_hits=0, sync_failed=False, fail_streak=0)
    assert _ids(warns) == ["source-down"]


def test_sync_failure_raises_source_warning():
    warns = app._health_warnings(last_hits=480, sync_failed=True, fail_streak=0)
    assert _ids(warns) == ["source-down"]


def test_three_failed_submits_raise_apply_warning():
    warns = app._health_warnings(last_hits=480, sync_failed=False, fail_streak=3)
    assert _ids(warns) == ["apply-unconfirmed"]
    assert "3" in warns[0]["text"]


def test_two_failed_submits_are_not_enough():
    assert app._health_warnings(last_hits=480, sync_failed=False, fail_streak=2) == []


def test_both_warnings_together():
    warns = app._health_warnings(last_hits=0, sync_failed=False, fail_streak=5)
    assert _ids(warns) == ["source-down", "apply-unconfirmed"]


def test_cloud_failures_raise_telegram_warning():
    warns = app._health_warnings(
        last_hits=480, sync_failed=False, fail_streak=0, cloud_fail_streak=3)
    assert _ids(warns) == ["telegram-cloud-down"]
    assert "локальный поиск" in warns[0]["text"]


def test_two_cloud_failures_do_not_alarm():
    assert app._health_warnings(
        last_hits=480, sync_failed=False, fail_streak=0, cloud_fail_streak=2) == []


def test_leading_failed_counts_streak():
    # свежие первыми: 3 неудачи подряд, потом успех — серия равна 3
    assert applications._leading_failed(["failed", "failed", "failed", "submitted"]) == 3


def test_leading_failed_stops_at_success():
    # последняя попытка успешна — серии нет, даже если раньше были неудачи
    assert applications._leading_failed(["submitted", "failed", "failed"]) == 0


def test_leading_failed_empty():
    assert applications._leading_failed([]) == 0


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
