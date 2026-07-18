"""Crash-safe JSON state files never expose partial writes."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from json_store import atomic_write_json, read_json


def test_round_trip_and_no_temp_files():
    root = Path(tempfile.mkdtemp())
    path = root / "state.json"
    atomic_write_json(path, {"text": "Кøbenhavn", "count": 2}, indent=2)
    assert read_json(path, {}, dict) == {"text": "Кøbenhavn", "count": 2}
    assert not list(root.glob("*.tmp"))


def test_corrupt_or_wrong_type_returns_default():
    root = Path(tempfile.mkdtemp())
    path = root / "state.json"
    path.write_text("{broken", encoding="utf-8")
    assert read_json(path, {"safe": True}, dict) == {"safe": True}
    path.write_text("[]", encoding="utf-8")
    assert read_json(path, {"safe": True}, dict) == {"safe": True}


def test_parallel_writers_leave_valid_complete_json():
    root = Path(tempfile.mkdtemp())
    path = root / "state.json"
    errors = []

    def writer(index):
        try:
            atomic_write_json(path, {"writer": index, "payload": "x" * 1000})
        except Exception as exc:  # pragma: no cover - reported through assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["writer"] in range(20) and saved["payload"] == "x" * 1000


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
