"""Тесты группировки журнала автопилота (grouped_events).

Ночной скан даёт десятки строк «TG: спросил разрешение — <вакансия>» подряд —
монитор становится нечитаем. grouped_events сворачивает серию из 3+ однотипных
событий в одну строку с количеством, не трогая исходный event_log.

Запуск:  python tests/test_autopilot_event_grouping.py   (или pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autopilot


def _ev(kind, text, ts="2026-07-15T02:00:00"):
    return {"ts": ts, "kind": kind, "text": text}


def test_long_run_collapsed():
    log = [_ev("info", f"TG: спросил разрешение — Вакансия {i}") for i in range(5)]
    out = autopilot.grouped_events(log)
    assert len(out) == 1
    assert out[0]["text"] == "TG: спросил разрешение — по 5 вакансиям"
    assert out[0]["kind"] == "info"
    # ts — от самого свежего события серии (лог хранится новые-первыми)
    assert out[0]["ts"] == log[0]["ts"]


def test_short_run_kept_as_is():
    log = [
        _ev("info", "TG: спросил разрешение — Вакансия 1"),
        _ev("info", "TG: спросил разрешение — Вакансия 2"),
    ]
    assert autopilot.grouped_events(log) == log


def test_different_kinds_not_merged():
    log = [
        _ev("info", "TG: спросил разрешение — А"),
        _ev("submit", "TG: спросил разрешение — Б"),
        _ev("info", "TG: спросил разрешение — В"),
    ]
    assert autopilot.grouped_events(log) == log


def test_series_interrupted_by_other_event():
    log = (
        [_ev("info", f"TG: спросил разрешение — В{i}") for i in range(3)]
        + [_ev("scan", "Проверил базу: подходящих 112, из них новых 8")]
        + [_ev("info", f"TG: спросил разрешение — Д{i}") for i in range(4)]
    )
    out = autopilot.grouped_events(log)
    assert [e["text"] for e in out] == [
        "TG: спросил разрешение — по 3 вакансиям",
        "Проверил базу: подходящих 112, из них новых 8",
        "TG: спросил разрешение — по 4 вакансиям",
    ]


def test_identical_texts_without_dash_collapsed_with_multiplier():
    log = [_ev("scan", "Проверил базу: подходящих 122") for _ in range(4)]
    out = autopilot.grouped_events(log)
    assert len(out) == 1
    assert out[0]["text"] == "Проверил базу: подходящих 122 · ×4"


def test_two_identical_texts_kept_as_is():
    log = [_ev("scan", "Проверил базу: подходящих 122") for _ in range(2)]
    assert autopilot.grouped_events(log) == log


def test_dash_with_non_vacancy_prefix_not_collapsed_by_count():
    # «не отправилось — <ошибка>» нельзя превращать в «по N вакансиям»;
    # но дословные повторы честно сворачиваются в «· ×N»
    log = [_ev("info", "TG: не отправилось — таймаут сети") for _ in range(3)]
    out = autopilot.grouped_events(log)
    assert len(out) == 1
    assert out[0]["text"] == "TG: не отправилось — таймаут сети · ×3"
    # разные ошибки с одним префиксом — остаются отдельными строками
    log2 = [_ev("info", f"TG: не отправилось — ошибка {i}") for i in range(3)]
    assert autopilot.grouped_events(log2) == log2


def test_source_log_not_mutated():
    log = [_ev("info", f"TG: спросил разрешение — В{i}") for i in range(4)]
    before = [dict(e) for e in log]
    autopilot.grouped_events(log)
    assert log == before


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
