"""Read-only evidence for the WexFlow product-plan acceptance criteria.

The script deliberately opens SQLite in ``mode=ro`` and prints aggregate
counts only: no vacancy titles, candidate data, or document paths.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def database_path(installed: bool) -> Path:
    if not installed:
        return ROOT / "jobs.db"
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return appdata / "WexFlow" / "salling" / "jobs.db"


def _rows(connection: sqlite3.Connection, sql: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(sql).fetchall()]


def _matches_hash(path: Path, expected: str) -> bool:
    expected = str(expected or "").strip().lower()
    if len(expected) != 64 or not path.is_file():
        return False
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == expected
    except OSError:
        return False


def audit(path: Path) -> int:
    if not path.is_file():
        print(f"ERROR database is absent: {path}")
        return 2

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    try:
        verdict = connection.execute("PRAGMA quick_check(1)").fetchone()
        print(f"DATABASE={path}")
        print(f"QUICK_CHECK={verdict[0] if verdict else 'no result'}")

        print("JOBS source | total | open | applied")
        for source, total, opened, applied in _rows(
            connection,
            """
            SELECT source,
                   COUNT(*),
                   SUM(CASE WHEN status != 'closed' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN applied_at IS NOT NULL OR status = 'applied'
                            THEN 1 ELSE 0 END)
              FROM job
          GROUP BY source
          ORDER BY COUNT(*) DESC, source
            """,
        ):
            print(f"  {source or '(empty)'} | {total} | {opened or 0} | {applied or 0}")

        print("APPLIED_CONFIDENCE source | confidence | count")
        for source, confidence, count in _rows(
            connection,
            """
            SELECT source, COALESCE(applied_confidence, '(empty)'), COUNT(*)
              FROM job
             WHERE applied_at IS NOT NULL OR status = 'applied'
          GROUP BY source, COALESCE(applied_confidence, '(empty)')
          ORDER BY 1, 2
            """,
        ):
            print(f"  {source or '(empty)'} | {confidence} | {count}")

        evidence_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='applicationevidence'"
        ).fetchone()
        evidence_counts = dict(
            _rows(
                connection,
                "SELECT source, COUNT(*) FROM applicationevidence GROUP BY source",
            )
            if evidence_table
            else []
        )
        print("APPLICATIONS source | state | count")
        for source, state, count in _rows(
            connection,
            """
            SELECT source,
                   state,
                   COUNT(*)
              FROM application
          GROUP BY source, state
          ORDER BY source, state
            """,
        ):
            print(f"  {source or '(empty)'} | {state or '(empty)'} | {count}")
        orphan_count = connection.execute(
            """
            SELECT COUNT(*)
              FROM application AS a
         LEFT JOIN job AS j ON j.id = a.job_id AND j.source = a.source
             WHERE j.id IS NULL
            """
        ).fetchone()[0]
        print(f"ORPHAN_APPLICATIONS={int(orphan_count or 0)}")
        print("ORPHAN_APPLICATIONS_BY_STATE source | state | count")
        for source, state, count in _rows(
            connection,
            """
            SELECT a.source, a.state, COUNT(*)
              FROM application AS a
         LEFT JOIN job AS j ON j.id = a.job_id AND j.source = a.source
             WHERE j.id IS NULL
          GROUP BY a.source, a.state
          ORDER BY a.source, a.state
            """,
        ):
            print(f"  {source or '(empty)'} | {state or '(empty)'} | {count}")
        test_sentinel = connection.execute(
            "SELECT COUNT(*) FROM application WHERE job_id = 'lidl:verify'"
        ).fetchone()[0]
        print(f"KNOWN_TEST_SENTINEL_LIDL_VERIFY={int(test_sentinel or 0)}")
        print("SUBMITTED_REGISTRY_CONFIDENCE source | confidence | count")
        for source, confidence, count in _rows(
            connection,
            """
            SELECT source, COALESCE(confidence, '(empty)'), COUNT(*)
              FROM application
             WHERE state = 'submitted'
          GROUP BY source, COALESCE(confidence, '(empty)')
          ORDER BY 1, 2
            """,
        ):
            print(f"  {source or '(empty)'} | {confidence} | {count}")
        print("EVIDENCE source | count")
        for source, count in sorted(evidence_counts.items()):
            print(f"  {source or '(empty)'} | {count}")

        receipt_ids: set[tuple[str, str]] = set()
        verified_email_ids: set[tuple[str, str]] = set()
        if evidence_table:
            for source, job_id, kind, rel_path, fingerprint, authentication in _rows(
                connection,
                """
                SELECT source, job_id, kind, path, fingerprint,
                       COALESCE(authentication, '')
                  FROM applicationevidence
                """,
            ):
                root = (path.parent / "logs" /
                        ("applied" if kind == "receipt_screen" else "email"))
                artifact = root / Path(str(rel_path or "")).name
                if not _matches_hash(artifact, fingerprint):
                    continue
                key = (str(source or ""), str(job_id or ""))
                if kind == "receipt_screen":
                    receipt_ids.add(key)
                elif (kind == "email"
                      and authentication in {"dkim_verified", "provider_verified"}):
                    verified_email_ids.add(key)

        trust: dict[str, dict[str, int]] = {}
        for source, job_id, confidence in _rows(
            connection,
            """
            SELECT source, id, COALESCE(applied_confidence, '')
              FROM job
             WHERE applied_at IS NOT NULL OR status = 'applied'
            """,
        ):
            row = trust.setdefault(
                source or "(empty)",
                {"submitted": 0, "receipts": 0, "portal": 0, "emails": 0,
                 "without_proof": 0},
            )
            row["submitted"] += 1
            if confidence == "portal":
                row["portal"] += 1
            elif confidence == "receipt":
                if (str(source or ""), str(job_id or "")) in receipt_ids:
                    row["receipts"] += 1
                else:
                    row["without_proof"] += 1
        for source, _job_id in verified_email_ids:
            row = trust.setdefault(
                source or "(empty)",
                {"submitted": 0, "receipts": 0, "portal": 0, "emails": 0,
                 "without_proof": 0},
            )
            row["emails"] += 1

        print("TRUST source | submitted | receipt+screen | portal | email | receipt_without_screen | proven")
        for source, row in sorted(trust.items()):
            proven = bool(row["receipts"] or row["portal"] or row["emails"])
            print(
                f"  {source} | {row['submitted']} | {row['receipts']} | {row['portal']} | "
                f"{row['emails']} | {row['without_proof']} | {str(proven).lower()}"
            )
    finally:
        connection.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--installed",
        action="store_true",
        help="inspect %%APPDATA%%/WexFlow/salling/jobs.db instead of the development database",
    )
    args = parser.parse_args()
    return audit(database_path(args.installed))


if __name__ == "__main__":
    raise SystemExit(main())
