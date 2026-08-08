"""Тест списка вакансий, который ПК отдаёт в телефон (_cloud_job_list + payload).

С 26.07.2026 в Mini App уходит не только «подходящее под фильтры», а тот же
список, что видно на главном экране приложения: сперва подходящие, затем
остальные активные (ближние сверху, если задан домашний адрес), с потолком.
Проверяем порядок, потолок, пометку isMatch и богатые поля карточки —
без реальной БД и сети.

Запуск:  python tests/test_cloud_jobs_list.py   (или pytest)
"""
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import autopilot
import applications
import cloud_auth
import settings_store
import account as account_mod


def _job(jid, *, lat=None, lon=None, fs=1.0, brand="netto", status="new"):
    import datetime as _dt
    return SimpleNamespace(
        id=jid, title="Salgsassistent " + jid, brand=brand, categories="sales",
        region="hovedstaden", city="København", street="Vej 1", zip="2700", country="Danmark",
        hours=15, employment_type="partTime", job_level="employee", pay_rate="142 kr/t",
        start_date="01.08.2026", published="2026-07-20", description="Dansk tekst",
        description_ru="", application_link="https://x/" + jid, requisition_id="R" + jid,
        status=status, first_seen=_dt.datetime(2026, 7, int(fs)),
        lat=lat, lon=lon, source="salling",
    )


@pytest.fixture(autouse=True)
def _restore():
    targets = [
        (account_mod, "is_signed_in"), (autopilot, "find_matches"),
        (applications, "submitted_ids"), (applications, "skipped_ids"),
        (applications, "submitting_ids"), (applications, "mark_listed"),
        (settings_store, "get_home"), (cloud_auth, "report_jobs"),
        (app, "_all_active_jobs"),
    ]
    originals = [(o, n, getattr(o, n)) for o, n in targets]
    try:
        yield
    finally:
        for o, n, v in originals:
            setattr(o, n, v)


def _patch(matches, others, home=None):
    sent = []
    account_mod.is_signed_in = lambda: True
    autopilot.find_matches = lambda: list(matches)
    applications.submitted_ids = lambda: set()
    applications.skipped_ids = lambda: set()
    applications.submitting_ids = lambda: set()
    applications.mark_listed = lambda ids: None
    settings_store.get_home = lambda: home
    app._all_active_jobs = lambda: list(matches) + list(others)
    cloud_auth.report_jobs = lambda items, timeout=12: (sent.append(list(items)) or True)
    app._jobs_sync_last = 0.0
    app._cloud_sync_attempt_last["jobs"] = 0.0
    app._cloud_sync_sent_hash.pop("jobs", None)
    return sent


def test_matches_first_then_rest():
    m = [_job("m1"), _job("m2")]
    o = [_job("o1"), _job("o2")]
    _patch(m, o)
    pairs = app._cloud_job_list()
    assert [j.id for j, _ in pairs][:2] == ["m1", "m2"], "подходящие идут первыми"
    assert {j.id for j, _ in pairs} == {"m1", "m2", "o1", "o2"}
    assert [is_m for _, is_m in pairs] == [True, True, False, False]


def test_rest_sorted_by_distance_when_home_known():
    home = {"lat": 55.7, "lon": 12.5}
    far = _job("far", lat=56.5, lon=12.5)
    near = _job("near", lat=55.71, lon=12.5)
    _patch([], [far, near], home=home)
    assert [j.id for j, _ in app._cloud_job_list()] == ["near", "far"]


def test_limit_caps_total():
    m = [_job("m%d" % i) for i in range(3)]
    o = [_job("o%d" % i) for i in range(10)]
    _patch(m, o)
    pairs = app._cloud_job_list(limit=5)
    assert len(pairs) == 5
    assert [j.id for j, _ in pairs][:3] == ["m0", "m1", "m2"], "подходящие не вытесняются"


def test_skipped_and_submitted_are_excluded():
    m = [_job("m1")]
    o = [_job("o1"), _job("o2")]
    _patch(m, o)
    applications.skipped_ids = lambda: {"o1"}
    applications.submitted_ids = lambda: {"m1"}
    ids = [j.id for j, _ in app._cloud_job_list()]
    assert ids == ["o2"], "поданное и пропущенное в телефон не возвращаем"


def test_payload_has_app_card_fields():
    job = _job("p1")
    p = app._tg_job_payload(
        job, is_match=False, home=None,
        trust_row={"proven": False, "label": "Salling Group", "proofs": 0},
    )
    assert p["titleBase"] == "Salgsassistent p1"
    assert p["brandColor"] and p["brandFg"], "цвет бренда — как в карточке приложения"
    assert p["employment"] == "Частичная занятость"
    assert p["ageGroup"] == "adult"
    assert p["payRate"] == "142 kr/t"
    assert p["startDate"] == "01.08.2026"
    assert p["publishedShort"]
    assert p["roleRu"], "русская расшифровка должности"
    assert p["address"].startswith("Vej 1")
    assert p["isMatch"] is False
    assert p["status"] == "new"
    assert p["firstSubmission"] is True
    assert p["platformProven"] is False


def test_payload_and_card_show_proven_platform_to_phone():
    job = _job("trusted")
    row = {"proven": True, "label": "Salling Group", "proofs": 2}
    payload = app._tg_job_payload(job, home=None, trust_row=row)
    card = app._tg_card(job, trust_row=row)
    assert payload["platformProven"] is True
    assert payload["firstSubmission"] is False
    assert payload["platformProofs"] == 2
    assert "Площадка проверена" in card


def test_payload_age_group_uses_same_rule_as_pc_autopilot():
    by_level = _job("u1")
    by_level.job_level = "employeeUnder18"
    assert app._tg_job_payload(by_level, home=None)["ageGroup"] == "under18"

    by_text = _job("u2")
    by_text.title = "Butiksassistent under 18 år"
    by_text.job_level = "employee"
    assert app._tg_job_payload(by_text, home=None)["ageGroup"] == "under18"

    adult = _job("a1")
    adult.title = "Butiksassistent"
    adult.description = "Almindelig stilling"
    assert app._tg_job_payload(adult, home=None)["ageGroup"] == "adult"


def test_sync_sends_both_kinds_and_marks_listed():
    listed = []
    sent = _patch([_job("m1")], [_job("o1")])
    applications.mark_listed = lambda ids: listed.extend(ids)
    assert app._sync_jobs_to_cloud(force=True) is True
    payload = sent[0]
    assert [p["id"] for p in payload] == ["m1", "o1"]
    assert [p["isMatch"] for p in payload] == [True, False]
    # F27: подать из панели можно только показанное — значит показанное помечаем
    assert sorted(listed) == ["m1", "o1"]


def test_application_change_refreshes_both_cloud_feeds():
    # Без pytest-фикстур: файл запускается и обычным python (проверка качества).
    calls = []
    with mock.patch.object(
            app, "_sync_applied_to_cloud",
            lambda force=False: (calls.append(("applied", force)) or True)), \
        mock.patch.object(
            app, "_sync_jobs_to_cloud",
            lambda force=False: (calls.append(("jobs", force)) or True)):
        assert app._sync_application_views_to_cloud(force=True) is True
    assert calls == [("applied", True), ("jobs", True)]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
