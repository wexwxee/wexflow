"""Квитанция сайта не должна теряться из-за формы датского слова.

13.08.2026 Иван прислал снимок: Salling показал диалог «Udført · Ansøgningen er
sendt», а WexFlow написал «Отправлено без квитанции». Правило требовало
притяжательное «DIN ansøgning er sendt», сайт же пишет определённую форму
«ansøgningEN». Из-за одного суффикса ни одна подача в Salling не получала
квитанцию: доверие площадке не росло, а человек получал предупреждение там,
где всё прошло идеально.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apply as salling_apply
from connectors import lidl_apply


class _Frame:
    """Кадр страницы, который умеет ровно то, что спрашивает проверка."""

    def __init__(self, text: str):
        self.text = text

    def get_by_text(self, rx):
        found = bool(rx.search(self.text))

        class _Locator:
            def count(self_inner):
                return 1 if found else 0

        return _Locator()


def _salling_sees(text: str) -> bool:
    frames = [_Frame(text)]
    original = salling_apply._all_frames
    salling_apply._all_frames = lambda page: frames
    try:
        return salling_apply._submission_confirmed(object())
    finally:
        salling_apply._all_frames = original


REAL_DIALOG = "Udført\nAnsøgningen er sendt\nOK"


def test_the_exact_dialog_from_the_screenshot_counts_as_a_receipt():
    assert _salling_sees(REAL_DIALOG) is True


def test_every_common_danish_wording_counts():
    for text in (
        "Ansøgningen er sendt",
        "Ansøgningen er afsendt",
        "Ansøgningen er blevet modtaget",
        "Din ansøgning er sendt",
        "Tak for din ansøgning",
        "Vi har modtaget din ansøgning",
        "Your application has been sent",
        "Application received",
    ):
        assert _salling_sees(text) is True, text


def test_a_bare_done_dialog_is_not_a_receipt():
    """«Udført» — общий заголовок SAP, он бывает и в середине анкеты."""
    assert _salling_sees("Udført") is False
    assert _salling_sees("OK") is False
    assert _salling_sees("Gem ansøgning som kladde") is False


def test_an_unsent_form_is_still_not_a_receipt():
    assert _salling_sees("Ansøg") is False
    assert _salling_sees("Send ansøgning") is False, "кнопка отправки — не квитанция"


def test_lidl_reads_the_same_wordings():
    for text in ("Ansøgningen er sendt", "Ansøgningen er afsendt",
                 "Ansøgningen er modtaget", "Tak for din ansøgning",
                 "Your application has been submitted"):
        assert lidl_apply._RECEIPT_RE.search(text), text
    for text in ("Udført", "Ansøg", "Send ansøgning"):
        assert not lidl_apply._RECEIPT_RE.search(text), text
