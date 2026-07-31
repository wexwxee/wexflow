"""Сохранение настроек не должно ломаться в зашифрованной папке (EFS).

Windows наследует шифрование от %AppData%; в такой папке переименование
временного файла поверх настоящего падает с WinError 17 «не удаётся
переместить файл на другой диск». Раньше это молча ломало сохранение —
настройки, профиль и ответы анкет просто не записывались (страница отвечала
500). Теперь запись переживает такую папку.
"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json_store


def _not_same_device(*_args, **_kwargs):
    err = OSError("Системе не удается переместить файл на другой диск")
    err.winerror = 17
    raise err


def test_write_survives_a_folder_where_rename_is_forbidden(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text('{"старое": true}', encoding="utf-8")
    with mock.patch.object(os, "replace", _not_same_device):
        json_store.atomic_write_json(target, {"новое": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"новое": 1}
    # прежнее состояние отложено рядом — если запись оборвётся, есть что вернуть
    assert json.loads((tmp_path / "settings.json.bak").read_text(encoding="utf-8")) \
        == {"старое": True}
    # временные файлы за собой не оставляем
    assert not list(tmp_path.glob("*.tmp"))


def test_first_write_into_such_folder_also_works(tmp_path):
    target = tmp_path / "new.json"
    with mock.patch.object(os, "replace", _not_same_device):
        json_store.atomic_write_json(target, [1, 2, 3])
    assert json.loads(target.read_text(encoding="utf-8")) == [1, 2, 3]


def test_other_errors_are_not_swallowed(tmp_path):
    """Чужую ошибку прятать нельзя — она может значить настоящую поломку."""
    def _denied(*_args, **_kwargs):
        err = OSError("отказано в доступе")
        err.winerror = 5
        raise err

    target = tmp_path / "x.json"
    with mock.patch.object(os, "replace", _denied):
        try:
            json_store.atomic_write_json(target, {"a": 1})
        except OSError as exc:
            assert getattr(exc, "winerror", None) == 5
        else:
            raise AssertionError("ошибка доступа должна была подняться наверх")


def test_settings_and_profile_go_through_the_same_writer():
    """Обе точки сохранения обязаны пользоваться общей подменой файла."""
    import settings_store
    import profile_store
    import candidate_profiles

    for module in (settings_store, profile_store, candidate_profiles):
        source = open(module.__file__, encoding="utf-8").read()
        assert "json_store.replace_file" in source, module.__name__
