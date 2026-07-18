"""Автозапуск WexFlow вместе с Windows (HKCU\\...\\Run, без прав администратора).

Зачем: синк вакансий и автопилот живут, только пока приложение запущено.
Пользователь закрыл окно / перезагрузил ПК — и «обновлено 3 дн назад».
Автозапуск со свёрнутым окном решает это без фоновых служб.

Работает только в собранном приложении (WexFlow.exe): в dev-режиме
sys.executable — это python, прописывать его в автозагрузку бессмысленно.
"""
from __future__ import annotations

import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WexFlow"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_path() -> str:
    return sys.executable or ""


def command_line(exe: str) -> str:
    """Строка для реестра: exe в кавычках (путь с пробелами) + свёрнутый старт."""
    return f'"{exe}" --minimized'


def supported() -> bool:
    return sys.platform == "win32" and is_frozen()


def _winreg():
    import winreg
    return winreg


def enabled() -> bool:
    if sys.platform != "win32":
        return False
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return bool(str(value).strip())
    except OSError:
        return False


def enable() -> bool:
    """Прописать автозапуск. True — получилось."""
    if not supported():
        return False
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                              command_line(exe_path()))
        return True
    except OSError:
        return False


def disable() -> bool:
    """Убрать из автозапуска. True — записи больше нет (в т.ч. если и не было)."""
    if sys.platform != "win32":
        return False
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def status() -> dict:
    return {"supported": supported(), "enabled": enabled()}
