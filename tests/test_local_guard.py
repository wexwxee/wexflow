"""Единый страж локальных запросов local_guard (F41): фиксируем поведение.

Этот барьер защищает локальные серверы от кросс-сайтовых POST (CSRF). Раньше он
был скопирован в app.py/hub.py/connectors/webapp.py — теперь один модуль.
Тест закрепляет ровно то поведение, что было в копиях (характеризационный).

Запуск без зависимостей:  python tests/test_local_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_guard as lg


def test_loopback_host():
    for h in ["127.0.0.1", "localhost", "127.0.0.1:8000",
              "LOCALHOST:8080", "[::1]", "[::1]:8000", "  127.0.0.1  "]:
        assert lg.is_loopback_host(h), h
    for h in ["", "example.com", "10.0.0.5", "evil.localhost.com", "0.0.0.0"]:
        assert not lg.is_loopback_host(h), h
    # Особенность, сохранённая как в оригинале: голый IPv6 без скобок ([::1] — да,
    # но "::1" — нет), т.к. split(":") его «съедает». Браузер шлёт Host в скобках.
    assert not lg.is_loopback_host("::1")


def test_loopback_url():
    assert lg.is_loopback_url("http://127.0.0.1:8000/x")
    assert lg.is_loopback_url("http://localhost/settings")
    assert not lg.is_loopback_url("http://evil.com/x")
    assert not lg.is_loopback_url("not a url")
    assert not lg.is_loopback_url("")


def test_safe_methods_always_allowed():
    # GET/HEAD и т.п. не проверяются — всегда True, даже с чужого сайта
    assert lg.allowed_write("GET", "evil.com", "http://evil.com", "", "")
    assert lg.allowed_write("get", "127.0.0.1", "", "", "")
    assert lg.allowed_write("HEAD", "evil.com", "", "", "cross-site")


def test_post_requires_loopback_host():
    assert not lg.allowed_write("POST", "evil.com", "", "", "")
    assert not lg.allowed_write("POST", "10.0.0.5:8000", "", "", "")


def test_post_origin_must_be_loopback():
    assert lg.allowed_write("POST", "127.0.0.1:8000", "http://127.0.0.1:8000", "", "")
    assert not lg.allowed_write("POST", "127.0.0.1:8000", "http://evil.com", "", "")


def test_post_referer_when_no_origin():
    assert lg.allowed_write("POST", "localhost", "", "http://localhost/x", "")
    assert not lg.allowed_write("POST", "localhost", "", "http://evil.com/x", "")


def test_post_sec_fetch_site_fallback():
    # нет Origin/Referer → решает Sec-Fetch-Site
    assert lg.allowed_write("POST", "127.0.0.1", "", "", "same-origin")
    assert lg.allowed_write("POST", "127.0.0.1", "", "", "none")
    assert lg.allowed_write("POST", "127.0.0.1", "", "", "")  # заголовка нет — пропускаем
    assert not lg.allowed_write("POST", "127.0.0.1", "", "", "cross-site")
    assert not lg.allowed_write("POST", "127.0.0.1", "", "", "same-site")


def test_cross_site_allowed_matches_post_path():
    # cross_site_allowed — то же, что allowed_write для «пишущего» метода
    assert lg.cross_site_allowed("127.0.0.1", "http://127.0.0.1/x", "", "") == \
        lg.allowed_write("POST", "127.0.0.1", "http://127.0.0.1/x", "", "")
    assert lg.cross_site_allowed("evil.com", "", "", "") is False


if __name__ == "__main__":
    tests = [
        test_loopback_host,
        test_loopback_url,
        test_safe_methods_always_allowed,
        test_post_requires_loopback_host,
        test_post_origin_must_be_loopback,
        test_post_referer_when_no_origin,
        test_post_sec_fetch_site_fallback,
        test_cross_site_allowed_matches_post_path,
    ]
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
