"""Кнопки «Отправить / Отмена» под скрином подготовленной анкеты.

Иван: «чтоб под пруфом была кнопка отправить или нет: если нет — отменится и
закроется, если да — продолжит и отправит до конца».

Ключевое: отправку запускает ЧЕЛОВЕК своей кнопкой. Проверяем, что:
  * сигнал из чата доходит до воркера и срабатывает ровно один раз;
  * «Отмена» ничего не отправляет и закрывает окно;
  * «Отправить» доводит форму до конца и отмечает заявку поданной;
  * без подтверждения (обычный прогон с ПК) поведение прежнее.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import apply
import config
from json_store import atomic_write_json


class _Job:
    id = "salling-confirm-1"
    title = "1. assistent - Herlev"
    brand = "netto"
    city = "Herlev"


def _signal(action):
    atomic_write_json(config.prepare_signal_path(_Job.id), {"action": action})


def _clear():
    try:
        config.prepare_signal_path(_Job.id).unlink()
    except OSError:
        pass


def test_signal_is_read_once():
    _clear()
    _signal("submit")
    assert apply.read_phone_decision(_Job.id) == "submit"
    assert apply.read_phone_decision(_Job.id) == "", "сигнал должен срабатывать один раз"


def test_unknown_signal_is_ignored():
    _clear()
    atomic_write_json(config.prepare_signal_path(_Job.id), {"action": "чтотоне то"})
    assert apply.read_phone_decision(_Job.id) == ""
    _clear()


def test_cancel_closes_without_sending():
    _clear()
    _signal("cancel")
    apply._close_requested = False
    with (
        mock.patch.object(apply, "submit_application") as submit,
        mock.patch.object(apply, "_mark_applied") as mark,
        mock.patch.object(apply, "_cloud_report") as report,
        mock.patch.object(apply, "_save_proof"),
    ):
        sent = apply._finish_by_phone(mock.Mock(), _Job())
    assert sent is False
    submit.assert_not_called(), "отмена не должна ничего отправлять"
    mark.assert_not_called()
    assert apply._close_requested is True, "после отмены окно надо закрыть"
    assert report.call_args.args[1] == "prepare_cancelled"
    apply._close_requested = False


def test_confirm_submits_and_marks_applied():
    _clear()
    _signal("submit")
    apply._close_requested = False
    with (
        mock.patch.object(apply, "submit_application", return_value="receipt") as submit,
        mock.patch.object(apply, "_mark_applied") as mark,
        mock.patch.object(apply, "_cloud_report") as report,
        mock.patch.object(apply, "_save_proof", return_value="proof.png"),
        mock.patch.object(apply, "_cloud_proof") as proof,
    ):
        sent = apply._finish_by_phone(mock.Mock(), _Job())
    assert sent is True
    submit.assert_called_once()
    assert mark.call_args.kwargs["confidence"] == "receipt"
    assert report.call_args.args[1] == "submitted"
    proof.assert_called_once()
    apply._close_requested = False


def test_failed_submit_keeps_window_open():
    """Не отправилось — окно НЕ закрываем: человек доделает руками."""
    _clear()
    _signal("submit")
    apply._close_requested = False
    with (
        mock.patch.object(apply, "submit_application", return_value="none"),
        mock.patch.object(apply, "_mark_applied") as mark,
        mock.patch.object(apply, "_cloud_report") as report,
        mock.patch.object(apply, "_save_proof"),
    ):
        sent = apply._finish_by_phone(mock.Mock(), _Job())
    assert sent is False and apply._close_requested is False
    mark.assert_not_called()
    assert report.call_args.args[1] == "failed"


def test_decision_from_chat_writes_signal():
    """Кнопка в чате → приложение кладёт сигнал воркеру."""
    _clear()
    with mock.patch.object(app, "get_session", side_effect=AssertionError("БД не нужна")):
        app._write_prepare_signal(_Job.id, "submit")
    assert apply.read_phone_decision(_Job.id) == "submit"
    _clear()


def test_prepare_actions_pass_the_decision_filter():
    """Новые действия не должны отсеиваться разбором очереди решений."""
    import inspect
    src = inspect.getsource(app._handle_tg_decisions)
    assert '"prepare_submit", "prepare_cancel"' in src
    assert "_write_prepare_signal" in src
