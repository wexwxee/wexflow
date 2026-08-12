"""Журнал аудита подач: группировка по дням (чистые функции, без БД).

Проверяем app._audit_day_label и app._audit_groups — они формируют, что и как
показывается в /audit. Данные не читаются и не пишутся.

Запуск без зависимостей:  python tests/test_audit.py
Или через pytest:         pytest tests/test_audit.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _e(ts, title="x"):
    return {"applied_at": ts, "title": title}


def test_day_labels():
    today = datetime(2026, 6, 30, 12, 0).date()
    assert app._audit_day_label(datetime(2026, 6, 30, 9, 0).date(), today) == "Сегодня"
    assert app._audit_day_label(datetime(2026, 6, 29, 9, 0).date(), today) == "Вчера"
    assert app._audit_day_label(datetime(2026, 6, 15, 9, 0).date(), today) == "15 июня"
    assert app._audit_day_label(datetime(2025, 6, 15, 9, 0).date(), today) == "15 июня 2025"


def test_grouping_by_day():
    now = datetime(2026, 6, 30, 12, 0)
    entries = [
        _e(datetime(2026, 6, 30, 10, 0)),
        _e(datetime(2026, 6, 30, 8, 0)),
        _e(datetime(2026, 6, 29, 20, 0)),
    ]
    groups = app._audit_groups(entries, now)
    assert [g[0] for g in groups] == ["Сегодня", "Вчера"]
    assert len(groups[0][1]) == 2
    assert len(groups[1][1]) == 1


def test_empty():
    assert app._audit_groups([], datetime(2026, 6, 30, 12, 0)) == []


def test_missing_ts_labelled():
    now = datetime(2026, 6, 30, 12, 0)
    groups = app._audit_groups([_e(None)], now)
    assert groups[0][0] == "Без даты"


def test_application_center_counts_and_filters_do_not_mix_silence_with_rejection():
    rows = [
        {"id": "wait", "activity": "submitted", "status": "applied", "tracker": {"action_required": False}},
        {"id": "follow", "activity": "submitted", "status": "applied", "tracker": {"action_required": True, "urgency": 2}},
        {"id": "review", "activity": "submitted", "status": "reviewing", "tracker": {"action_required": False}},
        {"id": "offer", "activity": "submitted", "status": "offer", "tracker": {"action_required": True}},
        {"id": "hired", "activity": "submitted", "status": "hired", "tracker": {"action_required": False}},
        {"id": "reject", "activity": "submitted", "status": "rejected", "tracker": {"action_required": False}},
        {"id": "withdrawn", "activity": "submitted", "status": "withdrawn", "tracker": {"action_required": False}},
        {"id": "silent", "activity": "submitted", "status": "no_response", "tracker": {"action_required": False}},
        {"id": "failed", "activity": "incomplete", "status": "new", "tracker": {"action_required": True, "urgency": 4}},
    ]
    counts = app._application_center_counts(rows)

    assert counts["rejected"] == 1
    assert counts["no_response"] == 1
    assert counts["reviewing"] == 1
    assert counts["offer"] == 1
    assert counts["hired"] == 1
    assert counts["withdrawn"] == 1
    assert counts["action"] == 3
    assert {row["id"] for row in app._application_center_filter(rows, "active")} == {
        "wait", "follow", "review", "offer", "failed",
    }
    assert [row["id"] for row in app._application_center_filter(rows, "waiting")] == ["wait", "follow"]
    assert [row["id"] for row in app._application_center_filter(rows, "hired")] == ["hired"]


if __name__ == "__main__":
    tests = [test_day_labels, test_grouping_by_day, test_empty, test_missing_ts_labelled]
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
