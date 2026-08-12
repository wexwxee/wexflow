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

import candidate_profiles

APP_NAME = "WexFlow"
_MODULE = "salling"

_PROJECT_DIR = Path(__file__).resolve().parent
_TEST_DATA_DIR = os.environ.get("WEXFLOW_TEST_DATA_DIR", "").strip()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# Папка с ресурсами (только чтение): шаблоны, статика, посев-данные.
if is_frozen():
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    RESOURCE_DIR = _PROJECT_DIR


def data_root() -> Path:
    """Куда писать пользовательские данные (БД, профиль, логин браузера)."""
    # Pytest (including child processes started by tests) must not be able to
    # touch the development or installed candidate data.  The variable is set
    # only by tests/conftest.py; normal dev and frozen paths stay unchanged.
    d = (Path(_TEST_DATA_DIR) if _TEST_DATA_DIR else
         candidate_profiles.data_dir(candidate_profiles.active_profile_id()))
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = data_root()


def shared_root() -> Path:
    """Общие для всех модулей WexFlow данные (единый профиль, подписка).

    В сборке — ``%AppData%\\WexFlow`` (на уровень выше модульной папки salling),
    чтобы Salling и 7-Eleven читали один и тот же профиль. В dev — папка проекта
    (как и остальные данные), чтобы не засорять корень диска.
    """
    if _TEST_DATA_DIR:
        d = Path(_TEST_DATA_DIR)
    elif is_frozen():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        d = Path(base) / APP_NAME
    else:
        d = _PROJECT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


SHARED_DIR = shared_root()


def resource_path(rel: str) -> Path:
    """Путь к ресурсу внутри сборки (или проекта в dev)."""
    return RESOURCE_DIR / rel
