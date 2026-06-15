"""Коннектор Greenhouse — просмотр вакансий (только Дания).

Публичный Job Board API без ключа: GET boards-api.greenhouse.io/v1/boards/{c}/jobs
Greenhouse — международная площадка, поэтому оставляем только датские вакансии
(по полю location). Подача по таким вакансиям работает через универсальный
заполнитель (generic_apply) — форма Greenhouse заполняется по подписям.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .base import Connector, JobItem, register, is_denmark, search_companies

CATALOG_PATH = Path(__file__).with_name("greenhouse_companies.json")
_H = {"User-Agent": "WexFlow/1.0 (+job-apply-hub)"}
_TIMEOUT = 25.0


class GreenhouseConnector(Connector):
    key = "greenhouse"
    name = "Greenhouse"
    icon = "🌱"
    color = "#1f9d55"

    def companies(self) -> list[dict]:
        if not CATALOG_PATH.exists():
            return []
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return data.get("companies", data) if isinstance(data, dict) else data

    def fetch_company(self, company: dict) -> list[JobItem]:
        token = company["token"]
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        r = httpx.get(url, headers=_H, timeout=_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        name = company.get("name") or token
        out: list[JobItem] = []
        for j in r.json().get("jobs", []):
            loc = (j.get("location") or {}).get("name") or ""
            if not is_denmark(loc):
                continue
            out.append(JobItem(
                source=self.key,
                id=f"gh:{token}:{j.get('id')}",
                title=j.get("title") or "",
                company=name,
                url=j.get("absolute_url") or "",
                city=loc or None,
                published=j.get("updated_at"),
            ))
        return out

    def search(self) -> list[JobItem]:
        return search_companies(self.companies(), self.fetch_company)


register(GreenhouseConnector())
