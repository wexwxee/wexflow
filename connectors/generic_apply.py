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

ANSWER_TEXT_FIELDS = (
    (["start date", "available from", "earliest start", "startdato", "hvornår kan du tidligst"], "start_date"),
    (["where do you see yourself in two years", "hvor ser du dig selv om to år"], "two_year_goal"),
    (["referred by", "referral", "henvist", "anbefalet af"], "lidl_referral_name"),
    (["previously employed", "formerly employed", "tidligere har været ansat"], "lidl_previous_employment"),
    (["how did you hear", "how did you find", "kendskab til denne stilling", "hørt om"], "lidl_discovery"),
    (["citizenship", "nationality", "statsborgerskab"], "citizenship"),
    (["health condition", "medical condition", "sygdomme", "arbejdsdygtighed"], "relevant_health_condition"),
)

ANSWER_CHOICE_FIELDS = (
    (["gender", "køn", "koen"], "gender"),
    (["retail experience", "detail branchen", "experience in retail"], "retail_experience"),
    (["every second weekend", "hver 2. weekend", "weekend work"], "work_weekends"),
    (["work evenings", "arbejde om aftenen", "evening shifts"], "work_evenings"),
    (["early morning", "06.00 om morgenen", "morning shifts"], "work_early"),
    (["night shifts", "work nights", "nattevagt"], "work_night"),
    (["driver's license", "driving licence", "kørekort"], "has_drivers_license"),
    (["already employed", "currently employed", "allerede ansat"], "lidl_current_employee"),
    (["work permit", "residence permit", "arbejdstilladelse", "opholdstilladelse"], "work_permit"),
    (["criminal record", "straffeattest", "background check"], "clean_criminal_record"),
    (["job alerts", "career opportunities", "relevante stillinger"], "lidl_newsletter"),
    (["talent pool", "profile consideration", "profil må gerne tages"], "lidl_profile_scope"),
)


def _choice_aliases(value: str) -> set[str]:
    wanted = str(value or "").strip().casefold()
    aliases = {wanted}
    aliases.update({
        "yes": {"yes", "ja", "true"},
        "no": {"no", "nej", "false"},
        "male": {"male", "man", "mand", "mænd"},
        "female": {"female", "woman", "kvinde", "kvinder"},
        "other": {"other", "andet", "non-binary"},
        "international": {"international", "lidl international", "global talent pool"},
        "country": {"country of residence", "bopælsland", "local talent pool"},
        "applied_only": {"only positions i applied", "stillinger, jeg selv har søgt"},
    }.get(wanted, set()))
    return {alias for alias in aliases if alias}


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
    aliases = _choice_aliases(wanted)
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
                long_fragment = any(
                    len(alias) >= 5 and (alias in label or alias in option_value)
                    for alias in aliases
                )
                if label in aliases or option_value in aliases or long_fragment:
                    el.select_option(index=index)
                    return True
        except Exception:
            continue
    return False


def _fill_radio_by_keywords(page, keywords, value) -> bool:
    if not value:
        return False
    aliases = _choice_aliases(value)
    try:
        radios = page.locator('input[type="radio"]').all()
    except Exception:
        return False
    for radio in radios:
        try:
            if not radio.is_visible() or not radio.is_enabled() or radio.is_checked():
                continue
            context = _control_text(radio) + " " + str(radio.evaluate(
                """e => {
                    const group = e.closest(
                        'fieldset,[role="radiogroup"],.form-group,.field,[data-field]'
                    );
                    if (!group) return '';
                    const title = group.querySelector(
                        'legend,.question-title,.field-label,[data-question],label'
                    );
                    return ((title && title.innerText) || '').toLowerCase();
                }"""
            ) or "")
            if not any(str(keyword).lower() in context for keyword in keywords):
                continue
            option = str(radio.evaluate(
                """e => {
                    const bits = [e.value, e.getAttribute('aria-label')];
                    if (e.labels) for (const label of e.labels) bits.push(label.innerText);
                    return bits.filter(Boolean).join(' ').toLowerCase();
                }"""
            ) or "")
            option_words = set(option.replace("/", " ").replace(",", " ").split())
            if any(
                option.strip() == alias
                or alias in option_words
                or (len(alias) >= 5 and alias in option)
                for alias in aliases
            ):
                radio.check()
                return True
        except Exception:
            continue
    return False


def fill_answer_fields(page, profile: dict) -> list[str]:
    """Fill known questionnaire facts already resolved for this company."""
    filled: list[str] = []
    for keywords, key in ANSWER_TEXT_FIELDS:
        value = str(profile.get(key) or "").strip()
        if _fill_by_keywords(page, keywords, value):
            filled.append(key)
    for keywords, key in ANSWER_CHOICE_FIELDS:
        value = str(profile.get(key) or "").strip()
        if (_fill_select_by_keywords(page, keywords, value)
                or _fill_radio_by_keywords(page, keywords, value)):
            filled.append(key)
    return filled


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
    filled.extend(fill_answer_fields(page, profile))

    print(f"  заполнено полей: {filled or '—'}")
    if upload_cv(page, profile):
        filled.append("CV")
    if attach_cover_letter(page, profile):
        filled.append("cover letter")

    # БЕТА (по умолчанию ВЫКЛ): ИИ-дозаполнение полей, которые скрипт не распознал.
    # Флаг WEXFLOW_AI_FILL / secrets "ai_fill". Без флага или без ключа Gemini —
    # это no-op, и поведение остаётся ровно таким, как было. Отправку не жмём.
    ai_details: list[dict] = []
    try:
        from connectors import ai_fill
        ai_details = ai_fill.fill(page, profile, job=None)
        for d in ai_details:
            tag = "черновик" if d.get("kind") == "draft" else "ИИ"
            filled.append(f"{d.get('label')} ({tag})")
        if ai_details:
            print(f"  ИИ дозаполнил: {[d.get('label') for d in ai_details]}")
    except Exception as ai_err:  # noqa: BLE001 — ИИ-слой не должен ломать подачу
        print("  ИИ-дозаполнение пропущено:", str(ai_err)[:120])

    missing = missing_required(page)
    add_banner(page, 0, filled, platform=platform or "форма", missing=missing, ai_details=ai_details)
    print(f"  заполнено: {filled or '—'} | дозаполнить: {missing or '—'}")
    print("  ГОТОВО — НЕ отправляю. Проверь, заполни остальное и отправь сам.")
