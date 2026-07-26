"""Conservative filler for Lidl Denmark's SAP UI5 EasyApply form.

Only profile facts and documents are inserted. Screening answers, declarations,
consents, profile visibility and the final submit button always stay manual.
"""
from __future__ import annotations

import re
from pathlib import Path

from connectors.fill_common import add_banner, dismiss_cookies


def normalize_phone(value: str) -> str:
    """Use the international 00-prefix required by the Danish Lidl form."""
    raw = re.sub(r"[\s().-]+", "", str(value or "").strip())
    if raw.startswith("+"):
        return "00" + raw[1:]
    if raw.isdigit() and len(raw) == 8:
        return "0045" + raw
    return raw


def _fill_labeled(page, label: str, value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    try:
        control = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if (control.count() and control.is_visible() and control.is_editable()
                and not (control.input_value() or "").strip()):
            control.fill(value)
            return True
    except Exception:
        pass
    return False


def _select_ui5(page, label: str, value: str) -> bool:
    """Select a value from a sap.m.Select associated with a visible label."""
    value = str(value or "").strip()
    if not value:
        return False
    aliases = {value.casefold()}
    if value.casefold() in {"danmark", "denmark", "dk"}:
        aliases.update({"danmark", "denmark"})
    try:
        labels = page.locator("label").all()
        wanted_label = label.casefold()
        matching_label = next(
            (
                candidate for candidate in labels
                if (candidate.inner_text() or "").strip().casefold() == wanted_label
            ),
            None,
        )
        if matching_label is None:
            matching_label = next(
                (
                    candidate for candidate in labels
                    if (candidate.inner_text() or "").strip().casefold().startswith(
                        wanted_label
                    )
                ),
                None,
            )
        if matching_label is None:
            return False
        label_id = matching_label.get_attribute("id") or ""
        target_id = matching_label.get_attribute("for") or ""
        controls = page.locator(
            f'[role="combobox"][aria-labelledby="{label_id}"]'
            if label_id else '[role="combobox"]'
        )
        control = controls.first
        if not control.count() and target_id.endswith("-hiddenInput"):
            control = page.locator(f"#{target_id.removesuffix('-hiddenInput')}-hiddenSelect")
        if not control.count() or not control.is_visible():
            return False
        click_target = control
        if (control.get_attribute("id") or "").endswith("-hiddenSelect"):
            click_target = control.locator("xpath=..")
        click_target.click()
        page.wait_for_timeout(150)
        options = page.locator('[role="option"], .sapMSelectListItem').all()
        for option in options:
            text = (option.inner_text() or "").strip().casefold()
            if option.is_visible() and text in aliases:
                option.click()
                return True
        page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    return False


def _upload(page, selector: str, path: str) -> bool:
    path = str(path or "").strip()
    if not path or not Path(path).is_file():
        return False
    try:
        control = page.locator(selector).first
        if control.count():
            control.set_input_files(path)
            return True
    except Exception:
        pass
    return False


def _screening_question_count(page) -> int:
    try:
        return page.locator('label[for^="__group"]').count()
    except Exception:
        return 0


def prepare(page, url: str, profile: dict) -> None:
    print(f"  открываю Lidl EasyApply: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.get_by_label(re.compile(r"Fornavn", re.I)).first.wait_for(
        state="visible", timeout=60_000
    )
    dismiss_cookies(page)

    filled: list[str] = []
    fields = (
        ("Fornavn", profile.get("first_name"), "first_name"),
        ("Efternavn", profile.get("last_name"), "last_name"),
        ("E-mail-adresse", profile.get("email"), "email"),
        ("Mobilnummer", normalize_phone(profile.get("phone") or ""), "phone"),
    )
    for label, value, key in fields:
        if _fill_labeled(page, label, value or ""):
            filled.append(key)

    if _select_ui5(page, "Land", profile.get("country") or ""):
        filled.append("country")
    if _upload(page, 'input[type="file"][name="EACVUploader"]',
               profile.get("cv_path") or ""):
        filled.append("CV")
    if _upload(page, 'input[type="file"][name="EACoverLetterUploader"]',
               profile.get("cover_letter_path") or ""):
        filled.append("cover letter")

    questions = _screening_question_count(page)
    missing = [
        "ответы Lidl и дата выхода",
        "видимость профиля",
    ]
    if questions:
        missing.append(f"{questions} вопросов вакансии")
    add_banner(
        page,
        questions,
        filled,
        platform="Lidl EasyApply",
        missing=missing,
    )
    print(f"  заполнено: {filled or '—'}")
    print("  ГОТОВО — ответы, согласия и финальная кнопка оставлены тебе.")
