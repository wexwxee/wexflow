"""Документы в панели пакетной подачи сохраняются до запуска воркера."""
import io
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.datastructures import UploadFile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def _job():
    return SimpleNamespace(source="salling", status="new", title="Кассир")


def test_batch_upload_is_saved_before_worker_starts():
    cv = UploadFile(filename="new_cv.pdf", file=io.BytesIO(b"pdf"))
    events = []
    saved_profile = {"cv_path": "saved/new_cv.pdf"}

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            app, "_load_jobs_snapshot", return_value=[("job-1", _job())]
        ))
        stack.enter_context(mock.patch.object(
            app, "_partition_submit_ids", return_value=(["job-1"], [], [])
        ))
        stack.enter_context(mock.patch.object(
            app.profile_store, "load_profile", return_value={"cv_path": "old.pdf"}
        ))
        files_result = stack.enter_context(mock.patch.object(
            app,
            "_profile_files_result",
            side_effect=lambda *args: (events.append("documents") or saved_profile, ""),
        ))
        save_profile = stack.enter_context(mock.patch.object(
            app.profile_store,
            "save_profile",
            side_effect=lambda profile: events.append("saved"),
        ))
        stack.enter_context(mock.patch.object(app, "_claim_apply_slot", return_value=True))
        worker = stack.enter_context(mock.patch.object(
            app,
            "_run_apply_worker",
            side_effect=lambda ids, submit: events.append("worker"),
        ))

        response = app.apply_batch(
            SimpleNamespace(headers={}),
            job_ids=["job-1"],
            mode="dry",
            cv_file=cv,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?batch=1&mode=dry"
    assert events == ["documents", "saved", "worker"]
    assert files_result.call_args.args[3] is cv
    save_profile.assert_called_once_with(saved_profile)
    worker.assert_called_once_with(["job-1"], submit=False)


def test_invalid_batch_upload_does_not_start_worker():
    cv = UploadFile(filename="bad.exe", file=io.BytesIO(b"bad"))

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            app, "_load_jobs_snapshot", return_value=[("job-1", _job())]
        ))
        stack.enter_context(mock.patch.object(
            app, "_partition_submit_ids", return_value=(["job-1"], [], [])
        ))
        stack.enter_context(mock.patch.object(
            app.profile_store, "load_profile", return_value={}
        ))
        stack.enter_context(mock.patch.object(
            app, "_profile_files_result", return_value=({}, "Можно загрузить только PDF.")
        ))
        save_profile = stack.enter_context(mock.patch.object(app.profile_store, "save_profile"))
        claim = stack.enter_context(mock.patch.object(app, "_claim_apply_slot"))
        worker = stack.enter_context(mock.patch.object(app, "_run_apply_worker"))

        response = app.apply_batch(
            SimpleNamespace(headers={"referer": "/"}),
            job_ids=["job-1"],
            mode="submit",
            cv_file=cv,
        )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    save_profile.assert_not_called()
    claim.assert_not_called()
    worker.assert_not_called()


def test_batch_panel_contains_inline_document_controls():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'enctype="multipart/form-data"' in template
    assert 'name="cv_file"' in template
    assert 'name="cover_letter_file"' in template
    assert "data-doc-open" in template
    assert "/settings/salling#documents" in template


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
