"""Страницы после чистки ленты: закрытых в списке нет, страны настраиваются.

TestClient без контекстного менеджера не запускает lifespan: планировщик и
опрос Telegram не стартуют, наружу ничего не уходит.

Запуск:  python tests/test_feed_page_country.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app
import feed

client = TestClient(app.app, follow_redirects=False)
INDEX_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "index.html"
).read_text(encoding="utf-8")


def test_status_choices_no_longer_offer_closed():
    """«Закрытые» — не фильтр ленты: подать на такую вакансию нельзя."""
    statuses = re.search(r"\{% set statuses = \{(.*?)\} %\}", INDEX_TEMPLATE, re.S)
    assert statuses, "набор статусов в шаблоне не найден"
    assert "'closed'" not in statuses.group(1)


def test_closed_filter_falls_back_to_active():
    # запрос заведомо ни с чем не совпадает — рендер остаётся дешёвым
    page = client.get("/?status=closed&q=zzzzzzzzzz")
    assert page.status_code == 200
    assert 'value="closed"' not in page.text


def test_profile_page_offers_country_setting():
    page = client.get("/profile")
    assert page.status_code == 200
    assert 'action="/settings/countries"' in page.text
    assert 'name="country" value="DK"' in page.text
    assert 'name="any_country"' in page.text


def test_country_options_carry_counts_and_selection():
    options = app._feed_country_options()
    assert options, "выбор стран не должен быть пустым"
    by_code = {item["code"]: item for item in options}
    assert "DK" in by_code, "Дания должна быть в списке всегда"
    assert by_code["DK"]["name"] == "Дания"
    assert by_code["DK"]["selected"] == (
        feed.any_country() or "DK" in feed.countries()
    )
    assert all(isinstance(item["count"], int) for item in options)


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print("ok")
