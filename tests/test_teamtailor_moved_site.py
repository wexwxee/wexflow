"""Переезд карьерного сайта фирмы не должен ждать новой версии приложения.

Каталог Teamtailor вшит в сборку, а фирмы переезжают со slug.teamtailor.com
на собственные домены. Старый адрес при этом перенаправляет на новый —
коннектор обязан сам сходить по редиректу и найти живой jobs.json.
Сеть подменяем: тесты не должны зависеть от чужих сайтов.

Запуск:  python tests/test_teamtailor_moved_site.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from connectors import teamtailor as tt

OLD = "https://demo.teamtailor.com"
NEW = "https://career.demo.com"
FEED = {"title": "Demo ApS", "items": [
    {"id": "1", "title": "Butiksassistent", "url": f"{NEW}/jobs/1",
     "_jobposting": {"jobLocation": [{"address": {
         "addressLocality": "København", "addressCountry": "DK"}}]}},
]}


class _Resp:
    def __init__(self, url, status=200, payload=None):
        self.url = httpx.URL(url)
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", str(self.url))
            raise httpx.HTTPStatusError(
                f"Client error '{self.status_code}' for url '{self.url}'",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self):
        if self._payload is None:
            raise ValueError("не JSON")
        return self._payload


def _network(routes):
    """routes: адрес -> _Resp. Возвращает (подмена httpx.get, журнал запросов)."""
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        resp = routes.get(url)
        if resp is None:
            return _Resp(url, status=404)
        return resp

    return fake_get, seen


def _connector():
    conn = tt.TeamtailorConnector()
    tt.TeamtailorConnector._moved.clear()
    return conn


def test_moved_company_is_found_through_redirect():
    fake_get, seen = _network({
        # старый поддомен: фида нет, но корень ведёт на новый сайт
        OLD + "/": _Resp(NEW + "/"),
        NEW + "/jobs.json": _Resp(NEW + "/jobs.json", payload=FEED),
    })
    conn = _connector()
    with mock.patch.object(tt.httpx, "get", fake_get):
        items = conn.fetch_company({"slug": "demo", "name": "Demo ApS"})
    assert [i.title for i in items] == ["Butiksassistent"]
    assert items[0].id == "tt:demo:1", "id вакансии не должен меняться от переезда"
    assert items[0].city == "København"
    assert tt.TeamtailorConnector._moved["demo"] == NEW + "/jobs.json"


def test_found_address_is_reused_without_probing_again():
    fake_get, seen = _network({
        OLD + "/": _Resp(NEW + "/"),
        NEW + "/jobs.json": _Resp(NEW + "/jobs.json", payload=FEED),
    })
    conn = _connector()
    with mock.patch.object(tt.httpx, "get", fake_get):
        conn.fetch_company({"slug": "demo"})
        seen.clear()
        conn.fetch_company({"slug": "demo"})
    assert seen == [NEW + "/jobs.json"], "второй обход идёт сразу по новому адресу"


def test_stale_remembered_address_falls_back_to_catalog():
    fake_get, seen = _network({OLD + "/jobs.json": _Resp(OLD + "/jobs.json", payload=FEED)})
    conn = _connector()
    tt.TeamtailorConnector._moved["demo"] = "https://gone.example.com/jobs.json"
    with mock.patch.object(tt.httpx, "get", fake_get):
        items = conn.fetch_company({"slug": "demo"})
    assert len(items) == 1
    assert tt.TeamtailorConnector._moved["demo"] == OLD + "/jobs.json"


def test_catalog_domain_falls_back_to_teamtailor_subdomain():
    fake_get, seen = _network({OLD + "/jobs.json": _Resp(OLD + "/jobs.json", payload=FEED)})
    conn = _connector()
    with mock.patch.object(tt.httpx, "get", fake_get):
        items = conn.fetch_company({"slug": "demo", "domain": "career.demo.com"})
    assert len(items) == 1, "фирма могла вернуться с своего домена на Teamtailor"


def test_dead_company_still_raises_for_the_watchdog():
    fake_get, seen = _network({})  # всё отвечает 404
    conn = _connector()
    with mock.patch.object(tt.httpx, "get", fake_get):
        try:
            conn.fetch_company({"slug": "demo"})
        except Exception as exc:
            assert "404" in str(exc)
        else:
            raise AssertionError("мёртвая фирма обязана дойти до сторожа")


def test_non_feed_answer_is_not_accepted_as_vacancies():
    fake_get, seen = _network({
        OLD + "/": _Resp(NEW + "/"),
        # новый адрес отвечает 200, но это не фид (например, страница-заглушка)
        NEW + "/jobs.json": _Resp(NEW + "/jobs.json", payload={"error": "not here"}),
    })
    conn = _connector()
    with mock.patch.object(tt.httpx, "get", fake_get):
        try:
            conn.fetch_company({"slug": "demo"})
        except Exception:
            pass
        else:
            raise AssertionError("чужой ответ нельзя принимать за вакансии")
    assert "demo" not in tt.TeamtailorConnector._moved


def test_http_redirect_target_must_stay_https():
    fake_get, seen = _network({
        OLD + "/": _Resp("http://insecure.demo.com/"),
        "http://insecure.demo.com/jobs.json": _Resp(
            "http://insecure.demo.com/jobs.json", payload=FEED),
    })
    conn = _connector()
    with mock.patch.object(tt.httpx, "get", fake_get):
        try:
            conn.fetch_company({"slug": "demo"})
        except Exception:
            pass
        else:
            raise AssertionError("по http вакансии не берём")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
