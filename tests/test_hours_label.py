"""«0 ч/нед» — не факт о работе, а молчание источника.

12.08.2026 Иван увидел карточку «0 ч/нед · Частичная занятость». Salling и
правда прислал ``hours = "0"``: так помечают ставки без фиксированных часов.
Показывать это числом нельзя — выглядит как вакансия, где не надо работать.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labels


def test_zero_hours_are_shown_as_nothing():
    for value in ("0", "0.0", "0,0", " 0 ", 0):
        assert labels.hours_label(value) == "", value


def test_real_numbers_keep_the_unit():
    assert labels.hours_label("15") == "15 ч/нед"
    assert labels.hours_label("37,5") == "37,5 ч/нед"
    assert labels.hours_label("15-20") == "15-20 ч/нед"


def test_source_wording_is_left_alone():
    # Иначе выйдет «30 timer ч/нед».
    assert labels.hours_label("30 timer") == "30 timer"
    assert labels.hours_label("Fuldtid") == "Fuldtid"


def test_empty_stays_empty():
    for value in ("", None, "   "):
        assert labels.hours_label(value) == ""


def test_every_place_that_prints_hours_uses_the_same_rule():
    """Подпись часов не должна расходиться между лентой, карточкой и Telegram."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("app.py", "assistant.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "ч/нед" not in source.replace("hours_label", ""), (
            f"{name}: часы печатаются мимо labels.hours_label"
        )


def test_assistant_card_hides_zero_hours():
    import assistant

    class _Job:
        hours = "0"

    assert assistant._hours_label(_Job()) == ""
