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

from .base import Connector, JobItem, register, search_companies, catalog_path

# Каталог-посев (read-only ресурс, попадёт в сборку через _MEIPASS).
CATALOG_PATH = catalog_path("teamtailor_companies.json")

_HEADERS = {"User-Agent": "WexFlow/1.0 (+job-apply-hub)"}
_TIMEOUT = 20.0


def _base_url(value: str) -> str:
    """Корень карьерного сайта из домена или полного адреса."""
    value = (value or "").strip()
    if not value:
        return ""
    base = value if value.startswith("http") else f"https://{value}"
    return base.rstrip("/")


def _company_bases(company: dict) -> list[str]:
    """Все известные корни сайта фирмы: свой домен и поддомен Teamtailor.

    Порядок = приоритет. Второй адрес нужен для самолечения: фирма могла
    переехать в любую сторону, а каталог у пользователя обновляется только
    вместе с новой версией приложения.
    """
    bases = []
    for value in ((company.get("domain") or ""), (company.get("slug") or "")):
        base = _base_url(value if "." in value or value.startswith("http")
                         else (f"{value}.teamtailor.com" if value else ""))
        if base and base not in bases:
            bases.append(base)
    return bases


def _feed_url(company: dict) -> str:
    """Адрес публичного JSON-фида компании.

    Поддерживаем оба варианта: поддомен ``slug.teamtailor.com`` и собственный
    карьерный домен (``domain``), который многие фирмы вешают на Teamtailor.
    """
    bases = _company_bases(company)
    if not bases:
        raise RuntimeError("в каталоге нет ни домена, ни slug")
    return bases[0] + "/jobs.json"


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

    # Найденные новые адреса переехавших фирм (на время работы приложения).
    _moved: dict[str, str] = {}

    def _fetch_feed(self, url: str) -> dict:
        """Скачать JSON Feed. Бросает, если это не фид (например, HTML-страница)."""
        r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError(f"по адресу {url} не JSON-фид вакансий")
        return data

    def _discover_feed(self, company: dict, tried: str) -> tuple[str, dict] | None:
        """Найти новый адрес фида, если фирма переехала.

        Карьерные сайты переезжают (чаще всего с ``slug.teamtailor.com`` на
        собственный домен), и тогда старый ``jobs.json`` отдаёт 404. Старый
        адрес при этом обычно ПЕРЕНАПРАВЛЯЕТ на новый — идём по редиректу и
        пробуем фид там. Так переезд заживает у всех сам, не дожидаясь новой
        версии приложения с обновлённым каталогом.
        """
        seen = {tried}
        for base in _company_bases(company):
            roots = [base]
            try:  # куда ведёт корень сайта — там и живёт новый фид
                r = httpx.get(base + "/", headers=_HEADERS, timeout=_TIMEOUT,
                              follow_redirects=True)
                final = _base_url(f"{r.url.scheme}://{r.url.host}")
                if final:
                    roots.append(final)
            except Exception:  # noqa: BLE001 — разведка не обязана удаваться
                pass
            for root in roots:
                url = root + "/jobs.json"
                if url in seen or not url.startswith("https://"):
                    continue
                seen.add(url)
                try:
                    return url, self._fetch_feed(url)
                except Exception:  # noqa: BLE001 — просто не тот адрес
                    continue
        return None

    def fetch_company(self, company: dict) -> list[JobItem]:
        """Вакансии одной компании. Бросает при сетевой/JSON-ошибке —
        наверху (search) ловится, чтобы одна фирма не валила весь список."""
        key = str(company.get("slug") or company.get("domain") or "")
        url = self._moved.get(key) or _feed_url(company)
        try:
            data = self._fetch_feed(url)
        except Exception:
            found = self._discover_feed(company, url)
            if found is None:
                self._moved.pop(key, None)  # запомненный адрес тоже мог устареть
                raise
            url, data = found
            self._moved[key] = url
            print(f"  teamtailor: {key or url} — рабочий адрес фида {url}")
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
        errors: list[str] = []
        items = search_companies(self.companies(), self.fetch_company, errors=errors)
        self.last_errors = errors
        return items

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
