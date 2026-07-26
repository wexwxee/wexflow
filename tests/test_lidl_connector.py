"""Lidl connector must import a complete, richly structured snapshot."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connector_sync
import connectors
from connectors import lidl


def _row(req_id="717301", title="Butiksassistent - 30 timer - Skagen"):
    return {
        "title": title,
        "company": "Region Vest",
        "descResponsibilities": "<p>En konkurrencedygtig l&oslash;n.</p>",
        "location": {
            "address": "Chr. Xs Vej 53",
            "zipCode": "9990",
            "city": "Skagen",
            "country": "DK",
            "latitude": 57.72258,
            "longitude": 10.58211,
        },
        "onlineFrom": "2026-07-10T11:43:33+00:00",
        "modifiedTime": "2026-07-11T11:43:33+00:00",
        "requisitionId": req_id,
        "salaryValue": "Ufaglært fra 142,17 kr. / time",
        "contractTypeId": "Teilzeit",
        "contractType": "30 timer",
        "employmentAreaId": "Store",
        "recruitingUrlEasyApply": (
            "https://ea-lidl.cfapps.eu20.hana.ondemand.com/"
            f"easyapply/index.html?ReqId={req_id}&sap-language=da_DK"
        ),
    }


def test_lidl_payload_maps_all_useful_fields():
    item = lidl.parse_search_payload({
        "jobs": [_row()],
        "meta": {"totalCount": 1},
    })[0]
    assert item.id == "lidl:717301"
    assert item.company == "Lidl Danmark"
    assert item.requisition_id == "717301"
    assert item.city == "Skagen" and item.zip == "9990"
    assert (item.lat, item.lon) == (57.72258, 10.58211)
    assert item.categories == "salesGeneral"
    assert item.hours == "30 timer" and item.employment_type == "partTime"
    assert item.job_level == "employee"
    assert "løn" in item.description
    assert "easyapply" in item.url

    job = connector_sync.job_from_item(item)
    assert job.source == "lidl"
    assert job.region == "Region Vest"
    assert job.pay_rate == "Ufaglært fra 142,17 kr. / time"
    assert job.requisition_id == "717301"
    assert (job.lat, job.lon) == (57.72258, 10.58211)


def test_lidl_under18_and_full_time_classification():
    row = _row("717302", "Ungarbejder - 7 timer - Skagen")
    assert lidl.job_from_payload(row).job_level == "employeeUnder18"
    row["title"] = "Lagermedarbejder - Fuldtid - Køge"
    row["contractTypeId"] = "Vollzeit"
    row["contractType"] = "Fuldtid"
    row["employmentAreaId"] = "Warehouse"
    item = lidl.job_from_payload(row)
    assert item.job_level == "employee"
    assert item.employment_type == "fullTime"
    assert item.categories == "distributionAndWarehouse"


def test_lidl_rejects_empty_partial_and_duplicate_snapshots():
    bad_payloads = [
        {"jobs": [], "meta": {"totalCount": 0}},
        {"jobs": [_row()], "meta": {"totalCount": 2}},
        {"jobs": [_row(), _row()], "meta": {"totalCount": 2}},
    ]
    for payload in bad_payloads:
        try:
            lidl.parse_search_payload(payload)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe Lidl snapshot was accepted")


def test_lidl_connector_paginates_and_validates_total():
    connector = lidl.LidlConnector()
    rows = [_row("1"), _row("2"), _row("3")]
    pages = {
        1: {"jobs": rows[:2], "meta": {"totalCount": 3}},
        2: {"jobs": rows[2:], "meta": {"totalCount": 3}},
    }
    with mock.patch.object(lidl, "RESULTS_PER_PAGE", 2), \
            mock.patch.object(connector, "_page", side_effect=lambda page: pages[page]):
        items = connector.search()
    assert [item.id for item in items] == ["lidl:1", "lidl:2", "lidl:3"]


def test_lidl_is_registered_for_normal_sync_and_apply_detection():
    assert connectors.get("lidl") is not None
    assert "lidl" in connector_sync.DEFAULT_SOURCES
    from connectors.apply_dispatch import detect, platform_name
    url = _row()["recruitingUrlEasyApply"]
    key = detect(url)
    assert key == "lidl_easy_apply"
    assert platform_name(key) == "Lidl EasyApply"


def test_lidl_source_badge_uses_lidl_brand_colors():
    from pathlib import Path
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert "source-{{ j.source }}" in template
    assert ".badge.source-lidl" in template
    assert "#ffec00" in template
    assert "#0050aa" in template
    assert "#e30613" in template


def test_external_sources_use_the_same_apply_button_as_salling():
    """Lidl and other connectors must not invent their own call to action."""
    from pathlib import Path
    templates = Path(__file__).resolve().parents[1] / "templates"
    index = (templates / "index.html").read_text(encoding="utf-8")
    detail = (templates / "detail.html").read_text(encoding="utf-8")
    assert index.count("Подать →") == 2
    assert "Заполнить форму" not in index and "Продолжить заполнение" not in index
    assert detail.count("Подать заявку →") == 3
    assert "Заполнить форму" not in detail
    assert "Проверить без отправки" in detail
    assert 'name="mode" value="submit"' in detail


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nALL {len(tests)} TESTS PASSED")
