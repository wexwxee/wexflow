"""Универсальный заполнитель «по подписям» — для платформ, где имена полей
заранее неизвестны (Greenhouse, Ashby и др.). Ищет поля по name/id/aria-label/
placeholder, заполняет имя/email/телефон, грузит CV и ОСТАНАВЛИВАЕТСЯ.

Менее точен, чем teamtailor_apply, но работает на большинстве форм. Согласие и
«Отправить» — человек.
"""
from __future__ import annotations

from connectors.fill_common import dismiss_cookies, upload_cv, add_banner

FIRST = ["first_name", "first-name", "firstname", "first", "fornavn", "given", "fornamn"]
LAST = ["last_name", "last-name", "lastname", "last", "efternavn", "surname", "efternamn"]
EMAIL = ["email", "e-mail", "mail"]
PHONE = ["phone", "telefon", "mobil", "tlf", "tel"]
FULL = ["full_name", "fullname", "full name", "name", "navn", "dit navn"]


def _fill_by_keywords(page, keywords, value) -> bool:
    if not value:
        return False
    for kw in keywords:
        for attr in ("name", "id", "aria-label", "placeholder", "data-qa"):
            sel = f'input[{attr}*="{kw}" i], textarea[{attr}*="{kw}" i]'
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible() and el.is_editable():
                    el.fill(value)
                    return True
            except Exception:
                continue
    return False


def _fill_email(page, value) -> bool:
    if not value:
        return False
    try:
        el = page.locator('input[type="email"]').first
        if el.count() and el.is_visible():
            el.fill(value)
            return True
    except Exception:
        pass
    return _fill_by_keywords(page, EMAIL, value)


def _fill_phone(page, value) -> bool:
    if not value:
        return False
    try:
        el = page.locator('input[type="tel"]').first
        if el.count() and el.is_visible():
            el.fill(value)
            return True
    except Exception:
        pass
    return _fill_by_keywords(page, PHONE, value)


def prepare(page, url: str, profile: dict, platform: str = "") -> None:
    print(f"  открываю форму: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2200)  # JS-формам нужно отрисоваться
    dismiss_cookies(page)
    page.wait_for_timeout(600)

    filled = []
    first = (profile.get("first_name") or "").strip()
    last = (profile.get("last_name") or "").strip()
    if _fill_by_keywords(page, FIRST, first):
        filled.append("first_name")
    if _fill_by_keywords(page, LAST, last):
        filled.append("last_name")
    if not filled and (first or last):  # форма с одним полем «Имя»
        full = f"{first} {last}".strip()
        if _fill_by_keywords(page, FULL, full):
            filled.append("name")
    if _fill_email(page, (profile.get("email") or "").strip()):
        filled.append("email")
    if _fill_phone(page, (profile.get("phone") or "").strip()):
        filled.append("phone")

    print(f"  заполнено полей: {filled or '—'}")
    upload_cv(page, profile)
    add_banner(page, 0, filled, platform=platform or "форма")
    print("  ГОТОВО — НЕ отправляю. Проверь, заполни остальное и отправь сам.")
