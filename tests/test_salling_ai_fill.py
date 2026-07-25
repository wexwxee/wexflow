"""The batch AI switch reaches the Salling form before optional submission."""
import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apply


def test_process_job_runs_ai_after_documents_in_dry_run():
    events = []
    page = SimpleNamespace(
        goto=lambda *args, **kwargs: None,
        wait_for_timeout=lambda *_: None,
    )
    job = SimpleNamespace(
        id="job-1",
        title="Kasseassistent",
        city="Herlev",
        brand="netto",
        description="",
        application_link="https://example.test/apply",
    )
    patches = [
        mock.patch.object(apply, "wait_for_login_if_needed"),
        mock.patch.object(apply, "add_job_banner"),
        mock.patch.object(apply, "wait_for_application_form"),
        mock.patch.object(apply, "accept_consent"),
        mock.patch.object(apply, "upload_documents", side_effect=lambda *_: events.append("documents")),
        mock.patch.object(apply, "_run_ai_fill", side_effect=lambda *_args, **_kwargs: events.append("ai") or []),
    ]
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        sent = apply.process_job(page, job, {}, submit=False, ai_fill=True)

    assert sent is False
    assert events == ["documents", "ai"]


if __name__ == "__main__":
    test_process_job_runs_ai_after_documents_in_dry_run()
    print("OK   test_process_job_runs_ai_after_documents_in_dry_run")
