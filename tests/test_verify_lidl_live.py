"""The safe Lidl verifier must use the selected installation and never stale data."""
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

import config
import paths
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
        (config, "LICENSE_PATH"),
        (config, "BROWSER_PROFILE_DIR"),
        (config, "SECRETS_PATH"),
        (config, "LEGACY_SHARED_PROFILE_PATH"),
        (config, "SHARED_PROFILE_PATH"),
        (settings_store, "PATH"),
        (profile_store, "UPLOAD_DIR"),
        (paths, "DATA_DIR"),
        (paths, "SHARED_DIR"),
    ):
        monkeypatch.setattr(module, name, getattr(module, name))

    verify_lidl_live._use_installed_primary_profile()

    assert config.DATA_DIR == data
    assert config.DB_PATH == data / "jobs.db"
    assert config.PROFILE_PATH == data / "profile.json"
    assert config.SHARED_PROFILE_PATH == root / "profile.json"
    assert settings_store.PATH == data / "settings.json"
    assert profile_store.UPLOAD_DIR == data / "uploads"
    assert paths.DATA_DIR == data
    assert paths.SHARED_DIR == root


def test_real_submit_requires_separate_arm_flag():
    try:
        verify_lidl_live.run(job_id="lidl:one", submit=True)
    except RuntimeError as exc:
        assert "--arm-submit" in str(exc)
    else:
        raise AssertionError("submit without a separate arm flag was accepted")


@pytest.mark.parametrize(("source", "brand"), [
    ("salling", "Lidl Danmark"),
    ("lidl", "Netto"),
    ("salling", "Netto"),
])
def test_exact_foreign_job_is_rejected_before_profile_documents_or_browser(
    source, brand,
):
    job = Job(
        id="foreign:one",
        source=source,
        brand=brand,
        title="Foreign vacancy",
        application_link="https://example.test/apply",
    )
    with mock.patch.object(verify_lidl_live, "_job", return_value=job), \
            mock.patch.object(profile_store, "load_profile") as load_profile, \
            mock.patch.object(verify_lidl_live.document_rules, "resolve_profile") as resolve, \
            mock.patch.object(verify_lidl_live, "sync_playwright") as playwright, \
            mock.patch.object(verify_lidl_live.lidl_apply, "prepare") as prepare:
        with pytest.raises(RuntimeError, match="не принадлежит Lidl"):
            verify_lidl_live.run(job_id=job.id)

    load_profile.assert_not_called()
    resolve.assert_not_called()
    playwright.assert_not_called()
    prepare.assert_not_called()


def test_receipt_is_recorded_in_legacy_installed_schema(tmp_path, monkeypatch):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE job (
                id TEXT PRIMARY KEY,
                source TEXT,
                status TEXT,
                applied_at TEXT,
                applied_confidence TEXT,
                application_status_updated_at TEXT,
                application_status_source TEXT
            );
            CREATE TABLE application (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                job_id TEXT,
                state TEXT,
                origin TEXT,
                confidence TEXT,
                offered_at TEXT,
                submitted_at TEXT,
                updated_at TEXT
            );
            INSERT INTO job (id, source, status) VALUES ('lidl:one', 'lidl', 'new');
            INSERT INTO application (source, job_id, state, origin)
                VALUES ('lidl', 'lidl:one', 'submitting', 'assisted');
        """)
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(config, "DB_PATH", database)

    assert verify_lidl_live._record_receipt("lidl:one") is True

    connection = sqlite3.connect(database)
    try:
        job = connection.execute(
            """SELECT status, applied_confidence, application_status_source,
               applied_at FROM job WHERE id = 'lidl:one'"""
        ).fetchone()
        application = connection.execute(
            """SELECT state, origin, confidence, submitted_at
               FROM application WHERE job_id = 'lidl:one'"""
        ).fetchone()
    finally:
        connection.close()
    assert job[:3] == ("applied", "receipt", "submission")
    assert job[3]
    assert application[:3] == ("submitted", "assisted", "receipt")
    assert application[3]
