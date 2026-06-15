"""Коннектор Teamtailor — только просмотр вакансий (Этап 1 плана).

Открытие: у каждого карьерного сайта Teamtailor есть ПУБЛИЧНЫЙ адрес
``…/jobs.json`` (формат JSON Feed) — отдаёт вакансии готовым структурированным
списком, без ключа, без логина и без скрейпинга. В каждой записи есть
``_jobposting`` (schema.org JobPosting) с полным адресом (улица/город/индекс/
страна) — это ложится прямо в геологику WexFlow (расчёт дороги от дома).

Список самих компаний Teamtailor нигде централизованно не отдаёт, поэтому мы
ведём СВОЙ каталог карьерных сайтов (teamtailor_companies.json) и пополняем его
по одному, проверяя каждую запись (см. verify()).
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .base import Connector, JobItem, register, search_companies

# Каталог-посев лежит рядом с кодом (read-only ресурс, попадёт в сборку).
CATALOG_PATH = Path(__file__).with_name("teamtailor_companies.json")

_HEADERS = {"User-Agent": "WexFlow/1.0 (+job-apply-hub)"}
_TIMEOUT = 20.0


def _feed_url(company: dict) -> str:
    """Адрес публичного JSON-фида компании.

    Поддерживаем оба варианта: поддомен ``slug.teamtailor.com`` и собственный
    карьерный домен (``domain``), который многие фирмы вешают на Teamtailor.
    """
    domain = (company.get("domain") or "").strip()
    if domain:
        base = domain if domain.startswith("http") else f"https://{domain}"
    else:
        base = f"https://{company['slug']}.teamtailor.com"
    return base.rstrip("/") + "/jobs.json"


def _first_address(jobposting: dict) -> dict:
    locs = jobposting.get("jobLocation") or []
    if locs and isinstance(locs, list):
        return (locs[0] or {}).get("address") or {}
    return {}


class TeamtailorConnector(Connector):
    key = "teamtailor"
    name = "Teamtailor"
    icon = "🧵"            # временно; SVG-иконку подключим на этапе UI
    color = "#3f3aff"      # фирменный фиолетовый Teamtailor

    def companies(self) -> list[dict]:
        if not CATALOG_PATH.exists():
            return []
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return data.get("companies", data) if isinstance(data, dict) else data

    def fetch_company(self, company: dict) -> list[JobItem]:
        """Вакансии одной компании. Бросает при сетевой/JSON-ошибке —
        наверху (search) ловится, чтобы одна фирма не валила весь список."""
        url = _feed_url(company)
        r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        company_name = company.get("name") or data.get("title") or company.get("slug", "")
        items: list[JobItem] = []
        for it in data.get("items", []):
            jp = it.get("_jobposting") or {}
            addr = _first_address(jp)
            items.append(
                JobItem(
                    source=self.key,
                    id=f"tt:{company.get('slug') or company.get('domain')}:{it.get('id')}",
                    title=it.get("title") or "",
                    company=company_name,
                    url=it.get("url") or "",
                    city=addr.get("addressLocality"),
                    street=addr.get("streetAddress"),
                    zip=addr.get("postalCode"),
                    country=addr.get("addressCountry"),
                    published=it.get("date_published"),
                    description=it.get("content_html"),
                )
            )
        return items

    def search(self) -> list[JobItem]:
        return search_companies(self.companies(), self.fetch_company)

    def verify(self) -> list[dict]:
        """Проверить каталог: какие компании живы и сколько у них вакансий.
        Удобно при пополнении списка новыми фирмами."""
        report = []
        for c in self.companies():
            row = {"slug": c.get("slug") or c.get("domain"), "name": c.get("name", "")}
            try:
                n = len(self.fetch_company(c))
                row.update(ok=True, jobs=n)
            except Exception as e:
                row.update(ok=False, jobs=0, error=str(e))
            report.append(row)
        return report


register(TeamtailorConnector())


if __name__ == "__main__":
    # Быстрая проверка из консоли: python -m connectors.teamtailor
    conn = TeamtailorConnector()
    for row in conn.verify():
        mark = "OK " if row["ok"] else "FAIL"
        extra = f"{row['jobs']} вакансий" if row["ok"] else row.get("error", "")
        print(f"  {mark}  {row['slug']:<28} {extra}")
