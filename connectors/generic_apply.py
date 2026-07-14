"""Универсальный заполнитель «по подписям» — для платформ, где имена полей
заранее неизвестны (Greenhouse, Ashby и др.). Ищет поля по name/id/aria-label/
placeholder, заполняет имя/email/телефон, грузит CV и ОСТАНАВЛИВАЕТСЯ.

Менее точен, чем teamtailor_apply, но работает на большинстве форм. Согласие и
«Отправить» — человек.
"""
from __future__ import annotations

from connectors.fill_common import (
    dismiss_cookies, upload_cv, attach_cover_letter, add_banner, missing_required,
    _control_text,
)

FIRST = ["first_name", "first-name", "firstname", "first", "fornavn", "given", "fornamn"]
LAST = ["last_name", "last-name", "lastname", "last", "efternavn", "surname", "efternamn"]
EMAIL = ["email", "e-mail", "mail"]
PHONE = ["phone", "telefon", "mobil", "tlf", "tel"]
FULL = ["full_name", "fullname", "full name", "name", "navn", "dit navn"]
ADDRESS = ["address", "adresse", "street", "gade", "vej"]
ZIP = ["zip", "postal", "postnr", "postnummer", "post code", "postcode"]
CITY = ["city", "town", "bopæl", "kommune"]
COUNTRY = ["country", "land"]
LINKEDIN = ["linkedin"]


def _fill_by_keywords(page, keywords, value) -> bool:
    if not value:
        return False
    for kw in keywords:
        for attr in ("name", "id", "aria-label", "placeholder", "data-qa"):
            op = "=" if kw in {"name", "first", "last", "mail", "tel"} else "*="
            sel = f'input[{attr}{op}"{kw}" i], textarea[{attr}{op}"{kw}" i]'
            try:
                el = page.locator(sel).first
                if (el.count() and el.is_visible() and el.is_editable()
                        and not (el.input_value() or "").strip()):
                    el.fill(value)
                    return True
            except Exception:
                continue
    # Many ATS forms use opaque ids such as question_123 and put the meaning
    # only in a <label>. Fall back to the accessible label/context.
    try:
        controls = page.locator('input:not([type="file"]), textarea').all()
    except Exception:
        controls = []
    for el in controls:
        try:
            field_type = (el.get_attribute("type") or "text").lower()
            if field_type in {"hidden", "checkbox", "radio", "submit", "button", "password", "search"}:
                continue
            if not el.is_visible() or not el.is_editable() or (el.input_value() or "").strip():
                continue
            text = _control_text(el)
            if keywords == FULL and any(word in text for word in ("company", "employer", "virksomhed")):
                continue
            if any(str(keyword).lower() in text for keyword in keywords):
                el.fill(value)
                return True
        except Exception:
            continue
    return False


def _fill_select_by_keywords(page, keywords, value) -> bool:
    if not value:
        return False
    try:
        controls = page.locator("select").all()
    except Exception:
        return False
    wanted = str(value).strip().casefold()
    aliases = {wanted}
    if wanted in {"danmark", "denmark", "dk"}:
        aliases.update({"danmark", "denmark", "dk"})
    for el in controls:
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            text = _control_text(el)
            if not any(str(keyword).lower() in text for keyword in keywords):
                continue
            options = el.locator("option").all()
            for index, option in enumerate(options):
                label = (option.inner_text() or "").strip().casefold()
                option_value = (option.get_attribute("value") or "").strip().casefold()
                if label in aliases or option_value in aliases:
                    el.select_option(index=index)
                    return True
        except Exception:
            continue
    return False


def _fill_email(page, value) -> bool:
    if not value:
        return False
    try:
        el = page.locator('input[type="email"]').first
        if (el.count() and el.is_visible() and el.is_editable()
                and not (el.input_value() or "").strip()):
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
        if (el.count() and el.is_visible() and el.is_editable()
                and not (el.input_value() or "").strip()):
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
    got_first = _fill_by_keywords(page, FIRST, first)
    got_last = _fill_by_keywords(page, LAST, last)
    if got_first:
        filled.append("first_name")
    if got_last:
        filled.append("last_name")
    if not (got_first or got_last) and (first or last):  # форма с одним полем «Имя»
        if _fill_by_keywords(page, FULL, f"{first} {last}".strip()):
            filled.append("name")
    if _fill_email(page, (profile.get("email") or "").strip()):
        filled.append("email")
    if _fill_phone(page, (profile.get("phone") or "").strip()):
        filled.append("phone")
    # дополнительные поля — заполняем, если форма их просит
    for keys, key in ((ADDRESS, "address"), (ZIP, "zip"), (CITY, "city"),
                      (COUNTRY, "country"), (LINKEDIN, "linkedin")):
        value = (profile.get(key) or "").strip()
        if (_fill_by_keywords(page, keys, value)
                or _fill_select_by_keywords(page, keys, value)):
            filled.append(key)

    print(f"  заполнено полей: {filled or '—'}")
    if upload_cv(page, profile):
        filled.append("CV")
    if attach_cover_letter(page, profile):
        filled.append("cover letter")
    missing = missing_required(page)
    add_banner(page, 0, filled, platform=platform or "форма", missing=missing)
    print(f"  заполнено: {filled or '—'} | дозаполнить: {missing or '—'}")
    print("  ГОТОВО — НЕ отправляю. Проверь, заполни остальное и отправь сам.")
