"""Global safety boundary for the test suite.

Tests must never write to the development ``jobs.db`` or to the candidate's
profile/settings.  Individual tests still replace sessions where they need a
specific fixture database, but the suite-wide defaults point at a disposable
directory.  This also protects us when a module keeps an imported alias such
as ``from db import get_session`` and a local mock misses that alias.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEST_DATA_HANDLE = tempfile.TemporaryDirectory(prefix="wexflow-pytest-")
_TEST_DATA = Path(_TEST_DATA_HANDLE.name)
os.environ["WEXFLOW_TEST_DATA_DIR"] = str(_TEST_DATA)

# ``config`` derives all mutable paths from ``paths`` at import time.  Rebind
# paths first, then import config so every later project-module import inherits
# the disposable defaults.  RESOURCE_DIR deliberately remains the repository:
# templates and static assets are read-only test fixtures.
import paths  # noqa: E402

paths.DATA_DIR = _TEST_DATA
paths.SHARED_DIR = _TEST_DATA

import config  # noqa: E402

config.DATA_DIR = _TEST_DATA
config.SHARED_DIR = _TEST_DATA
config.DB_PATH = _TEST_DATA / "jobs.db"
config.PROFILE_PATH = _TEST_DATA / "profile.json"
config.SHARED_PROFILE_PATH = _TEST_DATA / "profile.json"
config.LEGACY_SHARED_PROFILE_PATH = _TEST_DATA / "profile.json"
config.LICENSE_PATH = _TEST_DATA / "license.json"
config.BROWSER_PROFILE_DIR = _TEST_DATA / "browser_profile"
config.SECRETS_PATH = _TEST_DATA / "secrets.json"


def _fingerprint(path: Path) -> tuple[int, str] | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


@pytest.fixture(scope="session", autouse=True)
def _real_database_must_not_change():
    """Fail the run if any test escapes the disposable database."""
    real_files = [
        _PROJECT_ROOT / "jobs.db",
        _PROJECT_ROOT / "jobs.db-wal",
        _PROJECT_ROOT / "jobs.db-shm",
    ]
    before = {path: _fingerprint(path) for path in real_files}
    yield
    after = {path: _fingerprint(path) for path in real_files}
    try:
        assert after == before, "tests modified the development jobs.db"
    finally:
        # SQLAlchemy keeps the SQLite file open on Windows until the pool is
        # disposed; close it before TemporaryDirectory removes the sandbox.
        import db

        db.engine.dispose()
        _TEST_DATA_HANDLE.cleanup()
