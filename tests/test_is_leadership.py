"""Инвариант безопасности: is_leadership ловит руководящие должности
(датские и английские) и НЕ задевает рядовые позиции.

Зачем: автопилот и пакетная подача не должны авто-отправлять заявки на
руководящие роли (поле job_level от Salling недостоверно — отсекаем по
названию). Это первый из тестов на инварианты подачи (F23 / F40).

Запуск без зависимостей:  python tests/test_is_leadership.py
Или через pytest:         pytest tests/test_is_leadership.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import labels

# Должны определяться как РУКОВОДЯЩИЕ (is_leadership == True)
LEADERSHIP = [
    # датские
    "Souschef", "Serviceleder", "Salgsleder", "Driftsleder", "Teamkoordinator",
    "Varehuschef", "Afdelingschef", "Lagerchef", "Butikschef",
    "Projektansvarlig", "HR-ansvarlig", "Direktør",
    # английские
    "Store Manager", "District Manager", "Assistant Manager", "E-commerce Manager",
    "Supervisor", "Shift Supervisor", "Team Lead", "Team Leader", "Shift Lead",
    "Head of Sales", "Department Head", "Director", "Chief Operating Officer",
    "Foreman",
]

# Должны считаться РЯДОВЫМИ (is_leadership == False)
REGULAR = [
    # датские рядовые
    "Salgsassistent", "Kasseassistent", "1. assistent", "Morgenopfylder",
    "Gourmetslagter", "Kontorassistent", "Servicemedarbejder",
    "Servicemedarbejder under 18 år - Brønshøj", "Studentermedhjælper",
    "Lagermedarbejder", "Bager", "Slagter",
    # английские рядовые
    "Sales assistant", "Cashier", "Warehouse worker", "Customer service",
]


def test_leadership_titles_detected():
    missed = [t for t in LEADERSHIP if not labels.is_leadership(t)]
    assert not missed, f"не распознаны как руководящие: {missed}"


def test_regular_titles_not_flagged():
    wrong = [t for t in REGULAR if labels.is_leadership(t)]
    assert not wrong, f"рядовые ошибочно помечены руководящими: {wrong}"


def test_empty_and_none():
    assert labels.is_leadership("") is False
    assert labels.is_leadership(None) is False


if __name__ == "__main__":
    failures = 0
    for fn in (test_leadership_titles_detected,
               test_regular_titles_not_flagged,
               test_empty_and_none):
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
