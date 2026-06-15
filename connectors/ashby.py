"""Коннектор Ashby — просмотр вакансий (только Дания).

Публичный API без ключа: GET api.ashbyhq.com/posting-api/job-board/{org}
Ashby тоже международная, поэтому фильтруем по location на датские. Подача —
через универсальный заполнитель (generic_apply).
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .base import Connector, JobItem, register, is_denmark, search_companies

CATALOG_PATH = Path(__file__).with_name("ashby_companies.json")
_H = {"User-Agent": "WexFlow/1.0 (+job-apply-hub)"}
_TIMEOUT = 25.0


class AshbyConnector(Connector):
    key = "ashby"
    name = "Ashby"
    icon = "⬢"
    color = "#4f46e5"

    def companies(self) -> list[dict]:
        if not CATALOG_PATH.exists():
            return []
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return data.get("companies", data) if isinstance(data, dict) else data

    def fetch_company(self, company: dict) -> list[JobItem]:
        org = company["org"]
        url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
        r = httpx.get(url, headers=_H, timeout=_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        name = company.get("name") or org
        out: list[JobItem] = []
        for j in r.json().get("jobs", []):
            loc = j.get("location") or ""
            if not is_denmark(loc):
                continue
            out.append(JobItem(
                source=self.key,
                id=f"ashby:{org}:{j.get('id')}",
                title=j.get("title") or "",
                company=name,
                url=j.get("applyUrl") or j.get("jobUrl") or "",
                city=loc or None,
                published=j.get("publishedAt"),
                description=j.get("descriptionHtml"),
            ))
        return out

    def search(self) -> list[JobItem]:
        return search_companies(self.companies(), self.fetch_company)


register(AshbyConnector())
