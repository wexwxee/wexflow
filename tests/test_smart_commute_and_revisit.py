"""Умный фильтр дороги и безопасный возврат старых откликов."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def test_route_filter_is_strict_about_unknown_routes():
    assert not app._route_matches(None, max_minutes=45)
    assert not app._route_matches({}, max_minutes=45)


def test_route_filter_checks_minutes_and_transfers_together():
    trip = {"minutes": 38, "transfers": 1}
    assert app._route_matches(trip, max_minutes=45, max_transfers=1)
    assert not app._route_matches(trip, max_minutes=30, max_transfers=1)
    assert not app._route_matches(trip, max_minutes=45, max_transfers=0)


def test_old_application_age_is_human_and_never_negative():
    now = dt.datetime(2026, 8, 3, 12, 0, 0)
    assert app._applied_age_days(now - dt.timedelta(days=75, hours=2), now) == 75
    assert app._applied_age_days(now + dt.timedelta(days=1), now) == 0
    assert app._applied_age_days(None, now) is None


def test_revisit_policy_excludes_sensitive_application_stages():
    assert {"applied", "rejected", "no_response"} == app.REVISITABLE_APPLICATION_STATUSES
    assert "interview" not in app.REVISITABLE_APPLICATION_STATUSES
    assert "offer" not in app.REVISITABLE_APPLICATION_STATUSES
    assert app.DEFAULT_REVISIT_DAYS == 60


def test_job_card_explains_transfer_count():
    template = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "index.html",
    )
    html = open(template, encoding="utf-8").read()
    assert "без пересадок" in html
    assert "trips[j.id].transfers" in html


def test_revisited_jobs_get_preview_before_pagination_without_breaking_sort():
    source = open(app.__file__, encoding="utf-8").read()
    preview = "revisited_preview = [j for j in jobs if j.id in revisited_applied][:3]"
    assert preview in source
    assert source.index(preview) < source.index("# --- группировка по магазину")
    assert "jobs.sort(key=lambda j: j.id not in revisited_applied)" not in source
