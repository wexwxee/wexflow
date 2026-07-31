"""Small crash-safe JSON helpers for WexFlow runtime state."""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.RLock()


def read_json(path: Path, default: Any = None, expected_type=None):
    """Read JSON without letting a missing/corrupt state file crash the app."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default
    if expected_type is not None and not isinstance(value, expected_type):
        return default
    return value


def replace_file(temp: Path, target: Path) -> None:
    """Подменить target файлом temp — атомарно, а где нельзя, то хотя бы надёжно.

    Обычный путь — ``os.replace``: читатель никогда не видит полузаписанный файл.
    Но если папка данных зашифрована EFS (у Windows это наследуется от
    ``AppData``), переименование tmp поверх файла падает с WinError 17
    «не удаётся переместить файл на другой диск» — и приложение молча теряет
    возможность сохранять настройки. В этом случае копируем содержимое поверх,
    предварительно отложив прежнюю версию в ``.bak``: не атомарно, но данные
    сохраняются, а при сбое посреди копирования есть что вернуть.
    """
    temp, target = Path(temp), Path(target)
    try:
        os.replace(temp, target)
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) not in (17, 18):   # not-same-device
            raise
    backup = target.with_name(target.name + ".bak")
    try:
        if target.exists():
            shutil.copyfile(target, backup)
        shutil.copyfile(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, value: Any, *, indent=None) -> None:
    """Write JSON through a unique sibling temp file and atomically replace it."""
    with _WRITE_LOCK:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(
            f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=indent)
                stream.flush()
                os.fsync(stream.fileno())
            replace_file(temp, target)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
