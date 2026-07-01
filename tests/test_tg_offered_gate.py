"""Гейт F27: подаём только вакансии, которые приложение само предлагало.

Реальную (необратимую) подачу запускает решение ✅ из облака. Чтобы сбой или
подмена в облаке не заставили подать «непредложенную» вакансию, autopilot.
tg_submit_batch пропускает к подаче только id из журнала предложенного
(tg_offered_ids). Здесь проверяем чистую функцию partition_offered — без БД,
без сети, без единой реальной подачи.

Запуск без зависимостей:  python tests/test_tg_offered_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autopilot


def test_known_and_unknown_split():
    known, unknown = autopilot.partition_offered(["a", "b", "c"], {"a", "c"})
    assert known == ["a", "c"]
    assert unknown == ["b"]


def test_nothing_offered_refuses_all():
    known, unknown = autopilot.partition_offered(["a", "b"], [])
    assert known == []
    assert unknown == ["a", "b"]


def test_all_offered_pass():
    known, unknown = autopilot.partition_offered(["x", "y"], {"x", "y"})
    assert known == ["x", "y"]
    assert unknown == []


def test_dedupe_keeps_first_order():
    known, unknown = autopilot.partition_offered(["a", "a", "b", "b"], {"a"})
    assert known == ["a"]          # дубль убран
    assert unknown == ["b"]        # дубль убран


def test_blank_and_none_ids_skipped():
    known, unknown = autopilot.partition_offered(["", "  ", None, "a"], {"a"})
    assert known == ["a"]
    assert unknown == []


def test_empty_input():
    assert autopilot.partition_offered([], {"a"}) == ([], [])
    assert autopilot.partition_offered(None, None) == ([], [])


def test_order_preserved():
    known, unknown = autopilot.partition_offered(["c", "a", "b"], {"a", "b", "c"})
    assert known == ["c", "a", "b"]


if __name__ == "__main__":
    tests = [
        test_known_and_unknown_split,
        test_nothing_offered_refuses_all,
        test_all_offered_pass,
        test_dedupe_keeps_first_order,
        test_blank_and_none_ids_skipped,
        test_empty_input,
        test_order_preserved,
    ]
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
