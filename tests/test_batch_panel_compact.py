"""Панель пакетного выбора умеет сворачиваться.

Развёрнутая панель на увеличенном масштабе (125–150%) закрывала половину
списка — а отмечать вакансии нужно именно в списке. Проверяем, что:
- есть кнопка сворачивания и решение человека запоминается;
- на невысоком экране панель сворачивается сама, до выбора человека;
- в свёрнутом виде остаются счётчик и обе кнопки действий, а объяснения
  (документы, ИИ, предупреждение о подтверждении) прячутся.

Запуск:  python tests/test_batch_panel_compact.py   (или pytest)
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "index.html"
).read_text(encoding="utf-8")


def test_collapse_button_exists_next_to_clear():
    assert 'id="selcollapse"' in TEMPLATE
    assert 'class="batch-clear batch-collapse"' in TEMPLATE
    assert 'id="selclear"' in TEMPLATE, "кнопка очистки выбора должна остаться"


def test_compact_state_is_remembered_between_visits():
    assert "wex_batch_compact" in TEMPLATE
    assert re.search(r"localStorage\.setItem\(COMPACT_KEY", TEMPLATE), (
        "выбор человека должен сохраняться"
    )
    assert re.search(r"localStorage\.getItem\(COMPACT_KEY", TEMPLATE)


def test_panel_collapses_itself_only_until_the_person_decides():
    """Своё решение важнее автоподбора: если человек уже выбирал — не спорим."""
    block = TEMPLATE.split("function fitPanel()")[1][:700]
    assert "stored !== null" in block, "сохранённый выбор человека должен иметь приоритет"
    assert "innerHeight" in block, "автосворачивание должно считаться от высоты экрана"


def test_measurement_happens_in_the_expanded_state():
    """Мерить высоту в уже свёрнутом виде бессмысленно — она всегда мала."""
    tail = TEMPLATE.split("function fitPanel()")[1][:700]
    assert tail.index("classList.remove('compact')") < tail.index("scrollHeight")


def test_compact_view_keeps_actions_and_hides_explanations():
    css = TEMPLATE.split(".batchbar.compact")[1:]
    joined = "".join(css)
    for hidden in (".batch-subtitle", ".batch-doc-note", ".batch-ai", ".batch-safety"):
        assert hidden in joined, f"{hidden} должен скрываться в компактном виде"
    assert ".bactions" not in joined.split("display:none")[0], (
        "кнопки действий скрывать нельзя — ради них панель и открыта"
    )


def test_zoom_change_is_handled_live():
    assert "addEventListener('resize'" in TEMPLATE, (
        "масштаб меняют во время работы — панель должна пересчитываться"
    )


def test_panel_recalculates_when_it_appears():
    block = TEMPLATE.split("function refreshSel()")[1][:700]
    assert "fitPanel()" in block


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
