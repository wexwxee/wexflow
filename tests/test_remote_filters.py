"""Настройка фильтров с телефона (панель Mini App → команда set_filters).

Облако — недоверенный вход: команда могла быть подделана или искажена.
_sanitize_remote_filters обязан пропускать только известные ключи с
осмысленными значениями и НИКОГДА — ключи автоотправки (лимиты, режим,
auto_submit): включать необратимое с телефона нельзя.

Чистая функция, без базы и сети. Запуск: python tests/test_remote_filters.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def test_valid_fields_pass():
    out = app._sanitize_remote_filters({
        "max_km": "10", "min_hours": "10", "max_hours": "20",
        "age": "under18", "category": "cashier,baker", "brand": "netto",
    })
    assert out == {
        "max_km": "10", "min_hours": "10", "max_hours": "20",
        "age": "under18", "category": "cashier,baker", "brand": "netto",
    }


def test_only_sent_keys_returned():
    # частичная команда меняет только присланное — остальное дольёт профиль
    out = app._sanitize_remote_filters({"max_km": "25"})
    assert out == {"max_km": "25"}


def test_empty_value_means_clear():
    out = app._sanitize_remote_filters({"max_km": "", "age": ""})
    assert out == {"max_km": "", "age": ""}


def test_unknown_keys_dropped():
    # автоотправку и произвольные ключи с телефона включить нельзя
    out = app._sanitize_remote_filters({
        "auto_submit": True, "daily_limit": "50", "enabled": True,
        "submit_scope": "all", "keywords": "x", "cities": "y",
        "max_km": "5",
    })
    assert out == {"max_km": "5", "keywords": "x", "cities": "y"}


def test_garbage_numbers_dropped():
    out = app._sanitize_remote_filters({
        "max_km": "abc", "min_hours": "-5", "max_hours": "999999",
    })
    assert out == {}


def test_unknown_codes_filtered():
    out = app._sanitize_remote_filters({
        "category": "cashier,__hack__", "brand": "netto,evilbrand",
        "age": "child",
    })
    assert out == {"category": "cashier", "brand": "netto"}


def test_non_dict_input():
    assert app._sanitize_remote_filters(None) == {}
    assert app._sanitize_remote_filters("x") == {}
    assert app._sanitize_remote_filters([1, 2]) == {}


def test_decimal_comma_accepted():
    out = app._sanitize_remote_filters({"max_hours": "37,5"})
    assert out == {"max_hours": "37.5"}


def test_text_fields_sanitized():
    out = app._sanitize_remote_filters({
        "cities": " København , Aarhus,København,  <script>x , " + "ы" * 100,
        "keywords": "weekend, aften",
        "exclude_keywords": "nat",
    })
    assert out["cities"] == "København, Aarhus, scriptx, " + "ы" * 40
    assert out["keywords"] == "weekend, aften"
    assert out["exclude_keywords"] == "nat"


def test_text_fields_item_cap():
    out = app._sanitize_remote_filters({"cities": ",".join(f"c{i}" for i in range(30))})
    assert len(out["cities"].split(", ")) == 10   # не больше 10 значений


def test_schedule_hours():
    out = app._sanitize_remote_filters({"active_from": "8", "active_to": "22"})
    assert out == {"active_from": 8, "active_to": 22}
    out = app._sanitize_remote_filters({"active_from": "-3", "active_to": "99"})
    assert out == {}                                # вне 0..24 — отбрасываем
    out = app._sanitize_remote_filters({"active_from": "abc"})
    assert out == {}
    out = app._sanitize_remote_filters({"active_from": "inf", "active_to": "nan"})
    assert out == {}                                # недоверенный ввод не должен ронять обработчик


if __name__ == "__main__":
    tests = [
        test_valid_fields_pass,
        test_only_sent_keys_returned,
        test_empty_value_means_clear,
        test_unknown_keys_dropped,
        test_garbage_numbers_dropped,
        test_unknown_codes_filtered,
        test_non_dict_input,
        test_decimal_comma_accepted,
        test_text_fields_sanitized,
        test_text_fields_item_cap,
        test_schedule_hours,
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
