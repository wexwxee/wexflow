"""Единые пути WexFlow (Salling): данные пользователя отдельно от кода.

Идея:
- В собранном приложении (PyInstaller, ``sys.frozen``) пользовательские данные
  пишутся в ``%AppData%\\WexFlow\\salling``, а ресурсы (шаблоны, статика, посев)
  читаются из распакованной сборки (``sys._MEIPASS``).
- В обычном dev-запуске (как ты пользуешься сейчас) всё остаётся в папке
  проекта — текущий рабочий процесс не меняется.

Так один и тот же код работает и у тебя в разработке, и у друга в .exe,
причём у друга стартует с чистыми, пустыми данными.
"""
import os
import sys
from pathlib import Path

APP_NAME = "WexFlow"
_MODULE = "salling"

_PROJECT_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# Папка с ресурсами (только чтение): шаблоны, статика, посев-данные.
if is_frozen():
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    RESOURCE_DIR = _PROJECT_DIR


def data_root() -> Path:
    """Куда писать пользовательские данные (БД, профиль, логин браузера)."""
    if is_frozen():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        d = Path(base) / APP_NAME / _MODULE
    else:
        d = _PROJECT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = data_root()


def shared_root() -> Path:
    """Общие для всех модулей WexFlow данные (единый профиль, подписка).

    В сборке — ``%AppData%\\WexFlow`` (на уровень выше модульной папки salling),
    чтобы Salling и 7-Eleven читали один и тот же профиль. В dev — папка проекта
    (как и остальные данные), чтобы не засорять корень диска.
    """
    if is_frozen():
        d = DATA_DIR.parent
    else:
        d = _PROJECT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


SHARED_DIR = shared_root()


def resource_path(rel: str) -> Path:
    """Путь к ресурсу внутри сборки (или проекта в dev)."""
    return RESOURCE_DIR / rel
