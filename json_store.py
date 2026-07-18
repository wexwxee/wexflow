"""Small crash-safe JSON helpers for WexFlow runtime state."""
from __future__ import annotations

import json
import os
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
            os.replace(temp, target)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
