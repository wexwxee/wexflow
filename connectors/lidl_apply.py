"""Conservative filler for Lidl Denmark's SAP UI5 EasyApply form.

Only profile facts and documents are inserted. Screening answers, declarations,
consents and profile visibility stay manual. The final submit can be armed as a
separate, explicit action and is never triggered by preparation alone.
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


def split_address(value: str) -> tuple[str, str]:
    """Split a Danish one-line address into street name and house number.

    Lidl asks for «Gade» and «Husnummer» separately, while the WexFlow profile
    keeps one line. Floor/door details after a comma stay out of both fields —
    the house-number input only accepts six characters.
    """
    line = str(value or "").split(",")[0].strip()
    match = re.match(r"^(.*?)[\s.]+(\d+\s*[A-Za-zÆØÅæøå]?)$", line)
    if not match:
        return line, ""
    street = match.group(1).strip(" .,")
    number = re.sub(r"\s+", "", match.group(2))
    if not street:
        return line, ""
    return street, number[:6]


def _caption_input_id(page, caption: str) -> str | None:
    """Find the input that a bare «Gade:»-style caption belongs to.

    These four address captions are rendered as plain sap.m.Label spans without
    a `for` attribute, so get_by_label cannot see them. The input that follows
    the caption in document order is the right one — but only when it carries no
    label of its own, otherwise we would grab the next real question instead.
    """
    try:
        return page.evaluate(
            """(caption) => {
                const wanted = caption.trim().toLowerCase();
                const labels = [...document.querySelectorAll('.sapMLabel')];
                const target = labels.find(el =>
                    (el.innerText || '').trim().replace(/[:*\\s]+$/, '').toLowerCase() === wanted);
                if (!target) return null;
                const all = [...document.querySelectorAll('*')];
                const start = all.indexOf(target);
                for (let i = start + 1; i < all.length && i < start + 40; i++) {
                    const node = all[i];
                    if (node.tagName !== 'INPUT') continue;
                    if (!node.classList.contains('sapMInputBaseInner')) continue;
                    if (node.getAttribute('aria-labelledby')) return null;
                    return node.id || null;
                }
                return null;
            }""",
            caption,
        )
    except Exception:
        return None


def _fill_caption(page, caption: str, value: str) -> bool:
    """Fill an address field addressed only by its visible caption."""
    value = str(value or "").strip()
    if not value:
        return False
    control_id = _caption_input_id(page, caption)
    if not control_id:
        return False
    try:
        control = page.locator(f"#{control_id}")
        if (control.count() and control.is_visible() and control.is_editable()
                and not (control.input_value() or "").strip()):
            control.fill(value)
            return True
    except Exception:
        pass
    return False


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


def _wait_for_picker_to_close(page) -> None:
    """Let the country popover finish closing before the summary banner shows."""
    try:
        page.wait_for_selector('[role="option"]:visible', state="hidden", timeout=3000)
    except Exception:
        pass


def _screening_question_count(page) -> int:
    try:
        return page.locator('label[for^="__group"]').count()
    except Exception:
        return 0


_SUBMIT_TEXT_RE = re.compile(r"^\s*(Ansøg|Send ansøgning)\s*$", re.I)
_RECEIPT_RE = re.compile(
    r"(tak\s+for\s+din\s+ansøgning|ansøgning(?:en)?\s+er\s+modtaget|"
    r"vi\s+har\s+modtaget\s+din\s+ansøgning|tak\s+for\s+din\s+interesse)",
    re.I,
)


def _submit_button(page):
    """Return Lidl's real final button, never WexFlow's overlay control."""
    try:
        candidates = page.get_by_role("button", name=_SUBMIT_TEXT_RE)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if candidate.is_visible():
                return candidate
    except Exception:
        pass
    return None


def submission_checkpoint(page) -> dict:
    """Describe the non-destructive checkpoint at Lidl's final button."""
    button = _submit_button(page)
    reached = button is not None
    enabled = False
    if button is not None:
        try:
            enabled = bool(button.is_enabled())
        except Exception:
            enabled = False
    return {
        "reached_submit": reached,
        "submit_enabled": enabled,
        "submit_requested": bool(
            page.evaluate("() => Boolean(window.__wexflowSubmitRequested)")
        ) if reached else False,
    }


def submission_receipt_visible(page) -> bool:
    """Require positive Lidl confirmation text; a disappearing button is not enough."""
    try:
        text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        return False
    return bool(_RECEIPT_RE.search(text or ""))


def arm_explicit_submit(page) -> bool:
    """Add a separate WexFlow final action that clicks Lidl only after confirmation."""
    if _submit_button(page) is None:
        return False
    try:
        return bool(page.evaluate(
            """() => {
                const host = document.getElementById('wexflow-banner');
                const root = host && host.shadowRoot;
                if (!root) return false;
                if (root.getElementById('wexflow-real-submit')) return true;
                const action = document.createElement('button');
                action.id = 'wexflow-real-submit';
                action.type = 'button';
                action.textContent = 'Отправить заполненную анкету';
                action.style.cssText =
                    'width:100%;margin-top:10px;padding:10px 12px;border:0;border-radius:9px;'
                    + 'background:#16d86b;color:#07170d;font-weight:800;cursor:pointer;';
                const note = document.createElement('div');
                note.textContent =
                    'Это реальная отправка. Сначала заполни оставшиеся вопросы Lidl.';
                note.style.cssText = 'margin-top:8px;color:#ffcf70;font-size:12px;';
                action.addEventListener('click', () => {
                    const buttons = [...document.querySelectorAll('button')];
                    const nativeButton = buttons.find(button =>
                        /^(Ansøg|Send ansøgning)$/i.test((button.innerText || '').trim()));
                    if (!nativeButton || nativeButton.disabled
                            || nativeButton.getAttribute('aria-disabled') === 'true') {
                        alert('Форма Lidl ещё не готова: заполни обязательные поля и вопросы.');
                        return;
                    }
                    if (!confirm(
                        'Отправить эту заявку в Lidl сейчас? После подтверждения отменить нельзя.'
                    )) return;
                    window.__wexflowSubmitRequested = Date.now();
                    nativeButton.click();
                    action.disabled = true;
                    action.textContent = 'Отправляю…';
                });
                root.querySelector('.card')?.append(note, action);
                return true;
            }"""
        ))
    except Exception:
        return False


def prepare(page, url: str, profile: dict, allow_submit: bool = False) -> dict:
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

    street, house_number = split_address(profile.get("address") or "")
    address_fields = (
        ("Gade", street, "street"),
        ("Husnummer", house_number, "house number"),
        ("Postnummer", profile.get("zip"), "zip"),
        ("By", profile.get("city"), "city"),
    )
    for caption, value, key in address_fields:
        if _fill_caption(page, caption, value or ""):
            filled.append(key)

    if _select_ui5(page, "Land", profile.get("country") or ""):
        filled.append("country")
    _wait_for_picker_to_close(page)
    if _upload(page, 'input[type="file"][name="EACVUploader"]',
               profile.get("cv_path") or ""):
        filled.append("CV")
    if _upload(page, 'input[type="file"][name="EACoverLetterUploader"]',
               profile.get("cover_letter_path") or ""):
        filled.append("cover letter")

    questions = _screening_question_count(page)
    missing = [
        "пол (Køn)",
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
    checkpoint = submission_checkpoint(page)
    if allow_submit and checkpoint["reached_submit"]:
        checkpoint["submit_armed"] = arm_explicit_submit(page)
    else:
        checkpoint["submit_armed"] = False
    print(f"  заполнено: {filled or '—'}")
    if checkpoint["reached_submit"]:
        print("  ДОШЁЛ ДО КНОПКИ ANSØG — подготовка её не нажимала.")
    else:
        print("  warning: финальная кнопка Ansøg не найдена.")
    if checkpoint["submit_armed"]:
        print("  РЕАЛЬНАЯ ОТПРАВКА ВКЛЮЧЕНА — только через отдельное подтверждение.")
    else:
        print("  ПРОВЕРКА БЕЗ ОТПРАВКИ — ответы, согласия и Ansøg оставлены тебе.")
    return checkpoint
