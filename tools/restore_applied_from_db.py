"""Restore verified application marks from another WexFlow SQLite database.

The target database is never replaced: only jobs that exist in both databases,
have ``applied_at`` in the source and do not have it in the target are updated.
An SQLite-consistent backup is created before every non-empty apply operation.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Candidate:
    job_id: str
    source: str
    applied_at: str
    confidence: str
    current_status: str


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _require_columns(connection: sqlite3.Connection, table: str, names: set[str]) -> set[str]:
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    missing = names - columns
    if missing:
        raise RuntimeError(f"{table}: missing required columns: {', '.join(sorted(missing))}")
    return columns


def _check_integrity(connection: sqlite3.Connection, label: str) -> None:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"{label} database failed SQLite quick_check: {result}")


def find_candidates(source_path: Path, target_path: Path, source_name: str) -> list[Candidate]:
    source = _connect_read_only(source_path)
    target = _connect_read_only(target_path)
    try:
        _check_integrity(source, "source")
        _check_integrity(target, "target")
        source_columns = _require_columns(source, "job", {"id", "applied_at"})
        _require_columns(target, "job", {"id", "source", "status", "applied_at"})

        confidence_sql = "applied_confidence" if "applied_confidence" in source_columns else "NULL"
        marked = source.execute(
            f"SELECT id, applied_at, {confidence_sql} AS confidence "
            "FROM job WHERE applied_at IS NOT NULL ORDER BY id"
        ).fetchall()

        candidates: list[Candidate] = []
        for row in marked:
            current = target.execute(
                "SELECT status, applied_at FROM job WHERE id = ? AND source = ?",
                (row["id"], source_name),
            ).fetchone()
            if current is None or current["applied_at"] is not None:
                continue
            candidates.append(Candidate(
                job_id=str(row["id"]),
                source=source_name,
                applied_at=str(row["applied_at"]),
                confidence=str(row["confidence"] or "recovered"),
                current_status=str(current["status"] or "new"),
            ))
        return candidates
    finally:
        source.close()
        target.close()


def _backup_database(connection: sqlite3.Connection, target_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = target_path.parent / "_backups" / f"application_history_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / target_path.name
    backup = sqlite3.connect(backup_path)
    try:
        connection.backup(backup)
    finally:
        backup.close()
    return backup_path


def apply_candidates(target_path: Path, candidates: list[Candidate]) -> Path | None:
    if not candidates:
        return None

    connection = sqlite3.connect(target_path)
    try:
        _check_integrity(connection, "target")
        _require_columns(
            connection,
            "application",
            {"source", "job_id", "state", "origin", "confidence", "submitted_at", "updated_at"},
        )
        backup_path = _backup_database(connection, target_path)
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")

        connection.execute("BEGIN IMMEDIATE")
        for candidate in candidates:
            cursor = connection.execute(
                "UPDATE job SET applied_at = ?, applied_confidence = COALESCE(applied_confidence, ?), "
                "status = CASE WHEN status IN ('new', 'seen', 'closed') THEN 'applied' ELSE status END "
                "WHERE id = ? AND source = ? AND applied_at IS NULL",
                (candidate.applied_at, candidate.confidence, candidate.job_id, candidate.source),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"job changed while restoring: {candidate.source}/{candidate.job_id}")

            existing = connection.execute(
                "SELECT id, submitted_at FROM application WHERE source = ? AND job_id = ? ORDER BY id LIMIT 1",
                (candidate.source, candidate.job_id),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE application SET state = 'submitted', origin = 'recovered', "
                    "confidence = COALESCE(confidence, ?), submitted_at = COALESCE(submitted_at, ?), "
                    "updated_at = ? WHERE id = ?",
                    (candidate.confidence, candidate.applied_at, now, existing[0]),
                )
            else:
                connection.execute(
                    "INSERT INTO application "
                    "(source, job_id, state, origin, confidence, submitted_at, updated_at) "
                    "VALUES (?, ?, 'submitted', 'recovered', ?, ?, ?)",
                    (candidate.source, candidate.job_id, candidate.confidence, candidate.applied_at, now),
                )
        connection.commit()
        _check_integrity(connection, "restored target")
        return backup_path
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--target-db", required=True, type=Path)
    parser.add_argument("--source-name", default="salling")
    parser.add_argument("--apply", action="store_true", help="write changes after creating a backup")
    args = parser.parse_args()

    candidates = find_candidates(args.source_db, args.target_db, args.source_name)
    print(f"Verified marks eligible for restore: {len(candidates)}")
    by_status: dict[str, int] = {}
    for candidate in candidates:
        by_status[candidate.current_status] = by_status.get(candidate.current_status, 0) + 1
    if by_status:
        print("Current statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(by_status.items())))

    if not args.apply:
        print("Dry run only; pass --apply to restore these marks.")
        return 0

    backup_path = apply_candidates(args.target_db, candidates)
    if backup_path is None:
        print("No changes were necessary.")
    else:
        print(f"Restored: {len(candidates)}")
        print(f"Backup: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
