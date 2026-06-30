"""Тест единого отсева подачи app._partition_submit_ids (шаг 1.2, F39).

Это «воротина», через которую теперь проходит фильтрация авто- и пакетной
подачи. Проверяем нерушимые правила на чистой функции (без БД):
поданные и руководящие отсеиваются, дубли убираются, None пропускается,
поданность важнее руководящести.

Запуск:  python tests/test_apply_serializer.py   (или pytest)
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from db import Job


def _job(title="Salgsassistent", status="new", applied_at=None):
    return Job(id="x", title=title, status=status, applied_at=applied_at)


def test_regular_jobs_are_safe():
    picked = [("a", _job("Salgsassistent")), ("b", _job("Kasseassistent"))]
    safe, applied, leadership = app._partition_submit_ids(picked)
    assert safe == ["a", "b"]
    assert applied == [] and leadership == []


def test_applied_excluded():
    picked = [
        ("a", _job(status="applied")),
        ("b", _job(applied_at=datetime.datetime(2026, 6, 11))),
    ]
    safe, applied, leadership = app._partition_submit_ids(picked)
    assert safe == []
    assert len(applied) == 2 and leadership == []


def test_leadership_excluded():
    picked = [("a", _job("Store Manager")), ("b", _job("Souschef")), ("c", _job("Supervisor"))]
    safe, applied, leadership = app._partition_submit_ids(picked)
    assert safe == []
    assert len(leadership) == 3 and applied == []


def test_dedup_and_none_skipped():
    picked = [("a", _job()), ("a", _job()), ("b", None), ("", _job())]
    safe, applied, leadership = app._partition_submit_ids(picked)
    assert safe == ["a"]  # дубль 'a' убран, None и пустой id пропущены


def test_applied_takes_precedence_over_leadership():
    picked = [("a", _job("Store Manager", status="applied"))]
    safe, applied, leadership = app._partition_submit_ids(picked)
    assert safe == []
    assert len(applied) == 1 and leadership == []


def test_mixed_batch_partitions_correctly():
    picked = [
        ("ok1", _job("Salgsassistent")),
        ("lead", _job("District Manager")),
        ("done", _job(status="applied")),
        ("ok2", _job("Bager")),
    ]
    safe, applied, leadership = app._partition_submit_ids(picked)
    assert safe == ["ok1", "ok2"]
    assert [jid for jid, _ in applied] == ["done"]
    assert [jid for jid, _ in leadership] == ["lead"]


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
