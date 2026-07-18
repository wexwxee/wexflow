"""Application-history recovery is selective, transactional, and backed up."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.restore_applied_from_db import apply_candidates, find_candidates


def _source(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE job (id TEXT PRIMARY KEY, applied_at TEXT, applied_confidence TEXT)")
    connection.executemany(
        "INSERT INTO job VALUES (?, ?, ?)",
        [("restore", "2026-01-02 03:04:05", None), ("unmarked", None, None),
         ("absent", "2026-01-03 03:04:05", "manual")],
    )
    connection.commit()
    connection.close()


def _target(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE job (id TEXT PRIMARY KEY, source TEXT, status TEXT, applied_at TEXT, applied_confidence TEXT)"
    )
    connection.execute(
        "CREATE TABLE application (id INTEGER PRIMARY KEY, source TEXT, job_id TEXT, state TEXT, "
        "origin TEXT, confidence TEXT, submitted_at TEXT, updated_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO job VALUES (?, ?, ?, ?, ?)",
        [("restore", "salling", "closed", None, None),
         ("unmarked", "salling", "new", None, None)],
    )
    connection.execute(
        "INSERT INTO application VALUES (NULL, 'salling', 'restore', 'listed', 'feed', NULL, NULL, '2026-01-01')"
    )
    connection.commit()
    connection.close()


def test_selective_restore_and_backup():
    root = Path(tempfile.mkdtemp())
    source = root / "source.db"
    target = root / "target.db"
    _source(source)
    _target(target)

    candidates = find_candidates(source, target, "salling")
    assert [item.job_id for item in candidates] == ["restore"]
    backup = apply_candidates(target, candidates)
    assert backup and backup.exists()

    connection = sqlite3.connect(target)
    restored = connection.execute(
        "SELECT status, applied_at, applied_confidence FROM job WHERE id = 'restore'"
    ).fetchone()
    journal = connection.execute(
        "SELECT state, origin, confidence, submitted_at FROM application WHERE job_id = 'restore'"
    ).fetchone()
    connection.close()
    assert restored == ("applied", "2026-01-02 03:04:05", "recovered")
    assert journal == ("submitted", "recovered", "recovered", "2026-01-02 03:04:05")

    backup_connection = sqlite3.connect(backup)
    before = backup_connection.execute("SELECT status, applied_at FROM job WHERE id = 'restore'").fetchone()
    backup_connection.close()
    assert before == ("closed", None)
    assert find_candidates(source, target, "salling") == []


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nALL {len(tests)} TESTS PASSED")
