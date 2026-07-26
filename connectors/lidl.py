"""Lidl Danmark connector backed by the public career-search JSON API."""
from __future__ import annotations

import html
import json
import math
import re

import httpx

from .base import Connector, JobItem, register

SEARCH_URL = "https://karriere.lidl.dk/api/v1/search"
RESULTS_PER_PAGE = 500
_TIMEOUT = 30.0
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "WexFlow/1.0 (+job-apply-hub)",
}

_CATEGORY_BY_AREA = {
    "store": "salesGeneral",
    "warehouse": "distributionAndWarehouse",
    "customer service": "customerService",
    "purchasing": "procurementAndPurchasingGrocery",
    "facility management": "administration",
    "sales": "salesOperations",
    "trainee": "salesGeneral",
}


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("title") or value.get("name") or ""
    return str(value or "").strip()


def _category(row: dict) -> str | None:
    area = _text(row.get("employmentAreaId")).casefold()
    if not area:
        area = _text((row.get("categories") or {}).get("employment_area", {}).get("id")).casefold()
    return _CATEGORY_BY_AREA.get(area)


def _job_level(row: dict) -> str:
    title = _text(row.get("title")).casefold()
    bulk = _text((row.get("categories") or {}).get("bulk_template")).casefold()
    if "ungarbejder" in title or "ungarbejder" in bulk:
        return "employeeUnder18"
    if re.search(r"\b(?:elev|trainee|graduate|praktik)\w*", title):
        return "apprentice"
    if re.search(
        r"\b\w*(?:chef|leder|koordinator|ansvarlig)\b"
        r"|\b(?:manager|supervisor|director|chief|head)\b",
        title,
    ):
        return "manager"
    return "employee"


def _employment_type(row: dict) -> str | None:
    code = _text(row.get("contractTypeId")).casefold()
    hours = _text(row.get("contractType")).casefold()
    if code == "vollzeit" or hours == "fuldtid":
        return "fullTime"
    if code or hours:
        return "partTime"
    return None


def job_from_payload(row: dict) -> JobItem:
    """Convert one Lidl API row to the shared connector contract."""
    requisition_id = _text(row.get("requisitionId"))
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    application_url = (
        _text(row.get("recruitingUrlEasyApply"))
        or _text(row.get("recruitingUrl"))
        or _text(row.get("recruitingUrlSF"))
        or _text(row.get("jobDetailUrl"))
    )
    description = html.unescape(
        _text(row.get("descResponsibilities")) or _text(row.get("descHeader"))
    )
    try:
        lat = float(location["latitude"]) if location.get("latitude") is not None else None
        lon = float(location["longitude"]) if location.get("longitude") is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    return JobItem(
        source="lidl",
        id=f"lidl:{requisition_id}",
        title=_text(row.get("title")),
        company="Lidl Danmark",
        url=application_url,
        city=_text(location.get("city")) or None,
        street=_text(location.get("address")) or None,
        zip=_text(location.get("zipCode")) or None,
        country=_text(location.get("country")) or "DK",
        lat=lat,
        lon=lon,
        categories=_category(row),
        region=_text(row.get("company")) or None,
        hours=_text(row.get("contractType")) or None,
        employment_type=_employment_type(row),
        job_level=_job_level(row),
        pay_rate=_text(row.get("salaryValue")) or None,
        published=_text(row.get("onlineFrom")) or None,
        modified=_text(row.get("modifiedTime")) or None,
        requisition_id=requisition_id,
        description=description or None,
    )


def parse_search_payload(payload: dict) -> list[JobItem]:
    """Validate a complete Lidl snapshot before allowing it into the database."""
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise RuntimeError("Lidl returned an unknown job-search response")
    rows = payload["jobs"]
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    try:
        total = int(meta.get("totalCount"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Lidl returned an invalid vacancy count") from exc
    if total < 1:
        raise RuntimeError("Lidl returned a suspiciously empty vacancy list")
    ids = [_text(row.get("requisitionId")) for row in rows if isinstance(row, dict)]
    valid_rows = [
        row for row in rows
        if isinstance(row, dict)
        and _text(row.get("requisitionId"))
        and _text(row.get("title"))
    ]
    if len(rows) != total or len(valid_rows) != total or len(set(ids)) != total:
        raise RuntimeError(
            "Lidl returned an incomplete vacancy snapshot: "
            f"expected {total}, received {len(rows)}, unique valid IDs {len(set(ids))}"
        )
    return [job_from_payload(row) for row in valid_rows]


class LidlConnector(Connector):
    key = "lidl"
    name = "Lidl Danmark"
    icon = "🛒"
    color = "#0050aa"

    def _page(self, page: int) -> dict:
        general = {
            "page": page,
            "resultsPerPage": RESULTS_PER_PAGE,
            "sortField": "",
            "sortOrder": "desc",
        }
        response = httpx.get(
            SEARCH_URL,
            params={"general": json.dumps(general, separators=(",", ":"))},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise RuntimeError(f"Lidl returned an invalid vacancy page: {page}")
        return payload

    def search(self) -> list[JobItem]:
        first = self._page(1)
        meta = first.get("meta") if isinstance(first.get("meta"), dict) else {}
        try:
            total = int(meta.get("totalCount"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Lidl returned an invalid vacancy count") from exc
        rows = list(first["jobs"])
        page_count = max(1, math.ceil(total / RESULTS_PER_PAGE))
        for page in range(2, page_count + 1):
            payload = self._page(page)
            page_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            try:
                page_total = int(page_meta.get("totalCount"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Lidl returned an invalid vacancy count on page {page}") from exc
            if page_total != total:
                raise RuntimeError(
                    f"Lidl vacancy count changed during sync: {total} -> {page_total}"
                )
            rows.extend(payload["jobs"])
        return parse_search_payload({"jobs": rows, "meta": {"totalCount": total}})


register(LidlConnector())


if __name__ == "__main__":
    jobs = LidlConnector().search()
    print(f"Lidl Danmark: {len(jobs)} vacancies")
