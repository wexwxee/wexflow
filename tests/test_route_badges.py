"""Карточка честно объясняет, почему нет времени в пути.

Пустое место рядом с «≈ 3.6 км по прямой» читалось как «сюда не доехать»,
хотя на деле очередь маршрутов просто до этого адреса не дошла: бесплатный
сервис расписаний считает примерно один адрес в 12 секунд.

Правила, которые тут закреплены:
- посчитанный маршрут показывает минуты и транспорт (как раньше);
- не посчитанный — «считаю маршрут», и такая вакансия уходит в очередь;
- отвеченный «маршрута нет» — отдельная подпись, это ответ, а не ожидание;
- ожидание и «нет маршрута» выглядят приглушённо, чтобы не спорить с цифрами.

Запуск:  python tests/test_route_badges.py   (или pytest)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "index.html"
).read_text(encoding="utf-8")
APP_SOURCE = (
    Path(__file__).resolve().parents[1] / "app.py"
).read_text(encoding="utf-8")


def test_uncounted_route_says_it_is_being_calculated():
    assert "считаю маршрут" in TEMPLATE
    assert "route_state.get(j.id) == 'pending'" in TEMPLATE


def test_missing_route_is_told_apart_from_waiting():
    """«Маршрута нет» — это ответ сервиса, а не «ещё считаем»."""
    assert "маршрут не найден" in TEMPLATE
    assert "route_state.get(j.id) == 'none'" in TEMPLATE


def test_real_trip_badge_still_wins():
    """Порядок веток: посчитанный маршрут показывается раньше заглушек."""
    body = TEMPLATE.split("{% if j.id in trips %}")[1][:2600]
    assert body.index("trips[j.id].minutes") < body.index("считаю маршрут")


def test_waiting_state_is_explained_without_blaming_the_place():
    """Человек должен понять: пусто ≠ «доехать нельзя»."""
    tip = TEMPLATE.split("считаю маршрут")[0][-1400:]
    assert "не значит, что доехать нельзя" in tip
    assert "12 секунд" in tip, "стоит назвать причину медленности честно"


def test_placeholders_are_muted_not_loud():
    css = TEMPLATE.split(".badge.trip-wait, .badge.trip-none")[1][:400]
    assert "var(--muted)" in css, "заглушка не должна спорить с настоящими цифрами"
    assert "var(--ok-txt)" not in css


def test_animation_respects_reduced_motion():
    assert "prefers-reduced-motion" in TEMPLATE.split(".badge.trip-wait svg {")[1][:400]


def test_pending_jobs_are_actually_queued():
    """Подпись «считаю» без постановки в очередь была бы обманом."""
    block = APP_SOURCE.split('route_state[j.id] = "pending"')[1][:400]
    assert "need_route.append(j)" in block
    assert "transit_worker.request" in APP_SOURCE


def test_route_state_reaches_the_template():
    assert '"route_state": route_state' in APP_SOURCE


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"OK   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures
                  else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
