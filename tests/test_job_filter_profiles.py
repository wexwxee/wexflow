"""Профили поиска и временные вкладки страницы вакансий."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def test_profile_query_keeps_only_known_filter_fields():
    clean = app._clean_filter_query(
        "city=Herlev&period=today&status=active&group=on"
        "&page=9&profile=secret&redirect=https%3A%2F%2Fevil.invalid"
    )
    assert clean == "city=Herlev&status=active&group=1&period=today"


def test_profile_query_rejects_unknown_enum_values():
    clean = app._clean_filter_query(
        "status=admin&sort=drop-table&period=forever&q=кассир"
    )
    assert clean == "q=%D0%BA%D0%B0%D1%81%D1%81%D0%B8%D1%80"


def test_filter_query_can_remove_one_active_chip():
    filters = {
        "q": "кассир",
        "city": "Herlev",
        "status": "active",
        "period": "3d",
    }
    assert app._filter_query(filters, drop="city") == (
        "q=%D0%BA%D0%B0%D1%81%D1%81%D0%B8%D1%80"
        "&status=active&period=3d"
    )


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
