"""Характеризационные тесты хрупких парсеров (F43).

Эти функции разбирают «грязный» текст эвристиками и легко ломаются при
правках регулярок. Тесты ФИКСИРУЮТ текущее поведение (в т.ч. известные
шероховатости), чтобы будущие изменения регэкспов не сломали разбор
молча. Если поведение меняют осознанно — обновляют и эти ожидания.

  - scraper._extract_pay — вытащить ставку из описания вакансии.
  - app._home_city       — город из домашнего адреса (датский формат).

Запуск без зависимостей:  python tests/test_parsers.py
Или через pytest:         pytest tests/test_parsers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper
import app


# ── scraper._extract_pay ──────────────────────────────────────────────────
# (вход описания, ожидаемая ставка). None = ставка не найдена.
PAY_CASES = [
    (None, None),
    ("", None),
    ("Ingen løn nævnt", None),
    ("Arbejde 37 timer om ugen", None),            # «timer» без kr/DKK — не ставка
    ("Vi tilbyder 150 kr./time", "150 kr./time"),
    ("Løn: 120 DKK pr. time", "120 DKK pr. time"),
    ("Cirka 20.000 kr. pr. måned", "20.000 kr. pr. måned"),
    ("Du får 145 kr/t", "145 kr/t"),
    # известная шероховатость: разбор обрывается на «kr.», хвост «i timen» теряется
    ("Startløn 130 kr. i timen", "130 kr."),
]


def test_extract_pay():
    bad = []
    for text, expected in PAY_CASES:
        got = scraper._extract_pay(text)
        if got != expected:
            bad.append(f"{text!r}: ждали {expected!r}, получили {got!r}")
    assert not bad, "разбор ставки изменился:\n  " + "\n  ".join(bad)


# ── app._home_city ────────────────────────────────────────────────────────
# (home-словарь, ожидаемый город). Датский адрес: «… <4 цифры индекс> <город>»,
# хвостовая буква района (V, S, C, NV…) срезается, чтобы совпадало шире.
CITY_CASES = [
    (None, ""),
    ({}, ""),
    ({"address": ""}, ""),
    ({"address": "Gaden uden postnummer"}, ""),     # нет индекса — города нет
    ({"address": "Sonnerupvej 104, 4060 Kirke Saaby"}, "Kirke Saaby"),
    ({"address": "Vesterbrogade 1, 1620 København V"}, "København"),   # срез района «V»
    ({"address": "2300 København S"}, "København"),                   # срез района «S»
    ({"address": "Bygade 2, 5000 Odense"}, "Odense"),
    ({"lookup_address": "Hovedgaden 5, 8000 Aarhus C"}, "Aarhus"),    # берётся lookup_address
]


def test_home_city():
    bad = []
    for home, expected in CITY_CASES:
        got = app._home_city(home)
        if got != expected:
            bad.append(f"{home!r}: ждали {expected!r}, получили {got!r}")
    assert not bad, "разбор города изменился:\n  " + "\n  ".join(bad)


if __name__ == "__main__":
    failures = 0
    for fn in (test_extract_pay, test_home_city):
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
