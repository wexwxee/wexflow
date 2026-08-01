"""Самолечение базы: испорченный журнал не должен ронять всё приложение.

Разбор поломки 01.08.2026: файл jobs.db был полностью цел (5787 вакансий,
1231 заявка), а «database disk image is malformed» давал журнал jobs.db-wal,
разошедшийся с базой. Приложение при этом не запускалось вовсе, и человек
оставался без WexFlow до ручного восстановления.

Запуск:  python tests/test_db_selfheal.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod


def _make_db(path: Path, rows: int = 3) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (n INT)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def test_healthy_db_is_left_alone():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "jobs.db"
        _make_db(path)
        assert db_mod.ensure_healthy_db(path) == ""
        assert path.exists()


def test_broken_journal_is_moved_aside_and_data_survives():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "jobs.db"
        _make_db(path, rows=5)
        wal = path.with_name("jobs.db-wal")
        wal.write_bytes(b"\x37\x7f\x06\x82" + b"\x00" * 60)   # журнал не от этой базы
        path.with_name("jobs.db-shm").write_bytes(b"\x00" * 32)

        # сам файл читается, а вместе с журналом — нет (как было 01.08)
        with mock.patch.object(db_mod, "_sqlite_ok",
                               side_effect=lambda p, ignore_journal=False: ignore_journal):
            note = db_mod.ensure_healthy_db(path)

        assert "журнал" in note, note
        assert not wal.exists(), "испорченный журнал остался на месте"
        assert list(path.parent.glob("_badjournal_*")), "журнал не сохранён для разбора"
        con = sqlite3.connect(path)
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 5
        con.close()


def test_broken_database_falls_back_to_backup():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "jobs.db"
        _make_db(path, rows=7)
        backup = db_mod.backup_db(db_path=path)
        assert backup is not None and backup.exists()
        path.write_bytes(b"not a database at all")

        note = db_mod.ensure_healthy_db(path)
        assert "восстановлена" in note, note
        con = sqlite3.connect(path)
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 7
        con.close()
        assert list(path.parent.glob("_corrupt_*")), "битая база должна остаться для разбора"


def test_backups_are_rotated_and_readable():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "jobs.db"
        _make_db(path)
        made = []
        for i in range(4):
            with mock.patch.object(db_mod, "datetime") as clock:
                clock.now.return_value.strftime.return_value = f"2026080{i}_0100"
                made.append(db_mod.backup_db(keep=2, db_path=path))
        assert all(made)
        left = sorted((path.parent / "_backups").glob("jobs_*.db"))
        assert len(left) == 2, f"должно остаться 2 копии, осталось {len(left)}"
        assert db_mod._sqlite_ok(left[-1], ignore_journal=True)
        assert db_mod.newest_backup(path) is not None


def test_locked_journal_is_not_touched():
    """Чинит тот процесс, который стартовал первым; остальные не мешают."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "jobs.db"
        _make_db(path)
        wal = path.with_name("jobs.db-wal")
        wal.write_bytes(b"\x37\x7f\x06\x82" + b"\x00" * 60)
        with mock.patch.object(db_mod, "_sqlite_ok",
                               side_effect=lambda p, ignore_journal=False: ignore_journal), \
                mock.patch.object(Path, "rename", side_effect=OSError("занят")):
            assert db_mod.ensure_healthy_db(path) == ""
        assert wal.exists(), "чужой занятый журнал трогать нельзя"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
