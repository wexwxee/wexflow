"""The safe Lidl verifier must use the selected installation and never stale data."""
import sqlite3
from pathlib import Path

import config
import profile_store
import settings_store
from db import Job
from tools import verify_lidl_live


def _database(path: Path, jobs: list[Job]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("""
            CREATE TABLE job (
                id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                brand TEXT,
                city TEXT,
                street TEXT,
                zip TEXT,
                application_link TEXT,
                status TEXT,
                published TEXT
            )
        """)
        connection.executemany(
            """INSERT INTO job
               (id, source, title, brand, city, street, zip,
                application_link, status, published)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(
                job.id, job.source, job.title, job.brand, job.city,
                job.street, job.zip, job.application_link, job.status,
                job.published,
            ) for job in jobs],
        )
        connection.commit()
    finally:
        connection.close()


def test_job_uses_current_configured_database_and_newest_active_lidl(
    tmp_path, monkeypatch,
):
    database = tmp_path / "installed.db"
    _database(database, [
        Job(
            id="lidl:old",
            source="lidl",
            title="Old active",
            status="new",
            published="2026-08-01T00:00:00+00:00",
            application_link="https://example.test/old",
        ),
        Job(
            id="lidl:new",
            source="lidl",
            title="New active",
            status="new",
            published="2026-08-08T00:00:00+00:00",
            application_link="https://example.test/new",
        ),
        Job(
            id="lidl:closed",
            source="lidl",
            title="Newest but closed",
            status="closed",
            published="2026-08-09T00:00:00+00:00",
            application_link="https://example.test/closed",
        ),
    ])
    monkeypatch.setattr(config, "DB_PATH", database)

    assert verify_lidl_live._job().id == "lidl:new"


def test_installed_mode_rebinds_database_profile_settings_and_answer_bank(
    tmp_path, monkeypatch,
):
    root = tmp_path / "WexFlow"
    data = root / "salling"
    data.mkdir(parents=True)
    (data / "settings.json").write_text("{}", encoding="utf-8")
    (data / "jobs.db").write_bytes(b"")
    (root / "profile.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("APPDATA", str(tmp_path))
    for module, name in (
        (config, "DATA_DIR"),
        (config, "DB_PATH"),
        (config, "PROFILE_PATH"),
        (config, "SHARED_DIR"),
        (config, "LEGACY_SHARED_PROFILE_PATH"),
        (config, "SHARED_PROFILE_PATH"),
        (settings_store, "PATH"),
        (profile_store, "UPLOAD_DIR"),
    ):
        monkeypatch.setattr(module, name, getattr(module, name))

    verify_lidl_live._use_installed_primary_profile()

    assert config.DATA_DIR == data
    assert config.DB_PATH == data / "jobs.db"
    assert config.PROFILE_PATH == data / "profile.json"
    assert config.SHARED_PROFILE_PATH == root / "profile.json"
    assert settings_store.PATH == data / "settings.json"
    assert profile_store.UPLOAD_DIR == data / "uploads"
