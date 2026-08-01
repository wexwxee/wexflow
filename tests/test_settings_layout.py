"""Раскладка настроек: один список разделов и половины «Аккаунт» / «Профиль».

Настройки уже разъезжались: список разделов существовал дважды (плитки и
вкладки), и раздел «ИИ и лимиты» остался без плитки — попасть в него можно
было только изнутри другой страницы. Тест держит структуру на месте.

TestClient без контекстного менеджера не запускает lifespan: планировщик и
опрос Telegram не стартуют, наружу ничего не уходит.

Запуск:  python tests/test_settings_layout.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app

client = TestClient(app.app, follow_redirects=False)


def _get(path):
    response = client.get(path)
    assert response.status_code == 200, f"{path} → {response.status_code}"
    return response.text


def _tabs(html):
    nav = re.search(r'<nav class="sec-nav">(.*?)</nav>', html, re.S)
    assert nav, "полоска вкладок не отрисовалась"
    return re.findall(r'href="([^"]+)"', nav.group(1))


def _cards(html):
    return re.findall(r'<a class="settings-card"[^>]*href="([^"]+)"', html)


def test_tiles_and_tabs_come_from_the_same_list():
    urls = [s["url"] for s in app.SETTINGS_SECTIONS]
    assert _cards(_get("/settings")) == urls, "плитки разошлись со списком разделов"
    assert _tabs(_get("/settings/salling")) == urls, "вкладки разошлись со списком"


def test_every_section_opens():
    for section in app.SETTINGS_SECTIONS:
        path = section["url"].split("#")[0]
        assert client.get(path).status_code == 200, f"раздел {section['key']} не открылся"


def test_section_keys_and_urls_are_unique():
    keys = [s["key"] for s in app.SETTINGS_SECTIONS]
    urls = [s["url"] for s in app.SETTINGS_SECTIONS]
    assert len(set(keys)) == len(keys) and len(set(urls)) == len(urls)


def test_ai_settings_are_one_section():
    html = _get("/settings/forms")
    assert 'id="ai-providers"' in html, "подключение ключа ИИ пропало"
    assert 'id="ai-budget"' in html, "лимиты ИИ пропали"
    # старый адрес остаётся рабочим — на него ведут закладки
    moved = client.get("/settings/ai")
    assert moved.status_code in (302, 303, 307, 308)
    assert moved.headers["location"].startswith("/settings/forms")


def test_account_and_profile_are_separate_halves():
    account = _get("/account")
    profile = _get("/profile")
    assert 'id="telegram-setup"' in account and 'id="telegram-setup"' not in profile
    for block in ('id="profileForm"', 'id="home"', 'id="company-answers"'):
        assert block in profile, f"{block} потерялся на странице профиля"
        assert block not in account, f"{block} остался в «Аккаунте»"


def test_home_address_is_shown_with_its_value():
    """Перенос дома в «Профиль» когда-то забыл про переменную home в контексте."""
    profile = _get("/profile")
    assert 'id="homeAddressInput"' in profile
    home = app.settings_store.get_home()
    if home:
        assert home["address"] in profile, "адрес дома не подставился в поле"


def test_salling_page_points_to_moved_blocks():
    html = _get("/settings/salling")
    assert 'href="/profile#home"' in html, "ссылка на домашний адрес потерялась"
    assert 'href="/profile"' in html, "ссылка на профиль потерялась"


def test_lidl_page_links_to_its_form_answers():
    assert 'href="/profile?company=lidl#company-answers"' in _get("/settings/lidl")
    assert client.get("/profile?company=lidl").status_code == 200


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
