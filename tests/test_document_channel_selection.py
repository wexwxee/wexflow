"""Document routing is identical for PC and Telegram application paths."""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import apply
import document_rules
from connectors import apply_dispatch
from db import Job


@contextmanager
def _session_for(job):
    yield mock.Mock(get=mock.Mock(return_value=job))


@contextmanager
def _playwright():
    yield mock.Mock()


def _save_brand_rules(settings_path: Path) -> None:
    with mock.patch.object(document_rules.settings_store, "PATH", settings_path):
        document_rules.save_rule(
            scope="brand",
            brand="lidl",
            brand_label="Lidl",
            cv_path="pc_lidl_cv.pdf",
            cover_letter_path="pc_lidl_cover.pdf",
        )
        document_rules.save_rule(
            scope="brand",
            brand="netto",
            brand_label="Netto",
            cv_path="pc_netto_cv.pdf",
            cover_letter_path="pc_netto_cover.pdf",
        )


def test_pc_lidl_connector_loads_lidl_cv_and_cover():
    job = Job(
        id="lidl-1",
        source="lidl",
        brand="Lidl Danmark",
        application_link="https://example.test/lidl",
    )
    with tempfile.TemporaryDirectory() as td:
        settings_path = Path(td) / "settings.json"
        _save_brand_rules(settings_path)
        with mock.patch.object(document_rules.settings_store, "PATH", settings_path), \
                mock.patch("connectors.fill_common.load_profile", return_value={
                    "cv_path": "global_cv.pdf",
                    "cover_letter_path": "global_cover.pdf",
                }), \
                mock.patch("db.get_session", side_effect=lambda: _session_for(job)):
            profile = apply_dispatch.load_profile_for_job(job.id)

    assert profile["cv_path"] == "pc_lidl_cv.pdf"
    assert profile["cover_letter_path"] == "pc_lidl_cover.pdf"
    assert profile["_document_selection"]["brand"] == "lidl"


def test_pc_salling_worker_and_telegram_batch_worker_load_netto_documents():
    job = Job(id="netto-1", source="salling", brand="Netto", title="Butiksassistent")
    page = mock.Mock()
    page.is_closed.return_value = False
    browser = mock.Mock(pages=[page])
    base_profile = {
        "cv_path": "global_cv.pdf",
        "cover_letter_path": "global_cover.pdf",
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        settings_path = root / "settings.json"
        _save_brand_rules(settings_path)
        common = [
            mock.patch.object(document_rules.settings_store, "PATH", settings_path),
            mock.patch.object(apply, "load_profile", return_value=base_profile),
            mock.patch.object(apply, "get_session", side_effect=lambda: _session_for(job)),
            mock.patch.object(apply, "sync_playwright", side_effect=_playwright),
            mock.patch.object(apply, "_launch_browser", return_value=browser),
            mock.patch.object(apply.config, "BROWSER_PROFILE_DIR", root / "browser"),
            mock.patch.object(apply, "_write_progress"),
        ]
        with common[0], common[1], common[2], common[3], common[4], common[5], common[6], \
                mock.patch.object(apply, "process_job", return_value=False) as process:
            apply.run(job.id, web_mode=False, keep_open=False)
            pc_profile = process.call_args.args[2]

        browser.reset_mock()
        browser.pages = [page]
        with mock.patch.object(document_rules.settings_store, "PATH", settings_path), \
                mock.patch.object(apply, "load_profile", return_value=base_profile), \
                mock.patch.object(apply, "get_session", side_effect=lambda: _session_for(job)), \
                mock.patch.object(apply, "sync_playwright", side_effect=_playwright), \
                mock.patch.object(apply, "_launch_browser", return_value=browser), \
                mock.patch.object(apply.config, "BROWSER_PROFILE_DIR", root / "browser"), \
                mock.patch.object(apply, "_write_progress"), \
                mock.patch.object(apply, "process_job", return_value=False) as process:
            apply.run_batch([job.id], submit=False, web_mode=False, keep_open=False)
            telegram_profile = process.call_args.args[2]

    for profile in (pc_profile, telegram_profile):
        assert profile["cv_path"] == "pc_netto_cv.pdf"
        assert profile["cover_letter_path"] == "pc_netto_cover.pdf"
        assert profile["_document_selection"]["brand"] == "netto"


def test_telegram_lidl_decision_opens_assisted_pc_form_with_job_id():
    job = Job(
        id="lidl-tg-1",
        source="lidl",
        brand="Lidl Danmark",
        application_link="https://example.test/lidl-form",
    )
    with mock.patch.object(app, "get_session", side_effect=lambda: _session_for(job)), \
            mock.patch.object(app.applications, "offered_ids", return_value={job.id}), \
            mock.patch.object(app.applications, "listed_ids", return_value=set()), \
            mock.patch.object(app.applications, "state_of", return_value="offered"), \
            mock.patch.object(app.applications, "mark_submitting") as mark_submitting, \
            mock.patch.object(app, "_claim_connector_launch", return_value=True), \
            mock.patch.object(app, "_launch_connector_filler") as launch, \
            mock.patch.object(app, "_report_apply_result_safe"):
        app._handle_tg_decisions([{"jobId": job.id, "action": "submit"}])

    launch.assert_called_once_with(job.application_link, job.id)
    mark_submitting.assert_called_once_with(
        [job.id], origin="telegram", source="lidl"
    )


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
